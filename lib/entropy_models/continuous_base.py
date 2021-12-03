from typing import List, Tuple, Dict, Union, Callable
import math

import torch
import torch.nn as nn
import torch.distributions
from torch.distributions import Distribution
from compressai._CXX import pmf_to_quantized_cdf as _pmf_to_quantized_cdf
from compressai import ans

from .utils import quantization_offset

from lib.torch_utils import minkowski_tensor_wrapped_fn


def batched_pmf_to_quantized_cdf(pmf: torch.Tensor,
                                 pmf_length: torch.Tensor,
                                 max_length: int,
                                 entropy_coder_precision: int = 16):
    """
    Args:
        pmf: (channels, max_length) float
        pmf_length: (channels, ) int32
        max_length: max length of pmf, int
        entropy_coder_precision:
    Returns:
        quantized cdf (channels, max_length + 2)
    """
    cdf = torch.zeros((len(pmf_length), max_length + 2),
                      dtype=torch.int32, device=pmf.device)
    for i in range(len(pmf)):
        p = pmf[i][: pmf_length[i]]
        overflow = (1 - torch.sum(p)).clip(0)
        p = p.tolist()
        p.append(overflow.item())
        c = _pmf_to_quantized_cdf(p, entropy_coder_precision)
        cdf[i, : len(c)] = torch.tensor(c, dtype=torch.int32)
    return cdf


class DistributionQuantizedCDFTable(nn.Module):
    """
    Provide function that can generate flat quantized CDF table
    used by range coder.

    If "cdf" is available, use "cdf" to generate.
    Otherwise, base distribution should have derivable
    "icdf" or cdf-related functions for tails estimation.
    Then CDF table is generated by tails and "prob" function.

    The names of parameters for tails estimation end with "aux_param",
    which are supposed to be specially treated in training.
    """
    def __init__(self,
                 base: Distribution,
                 init_scale: int = 10,
                 tail_mass: float = 2 ** -8,
                 cdf_precision: int = 16,
                 ):
        super(DistributionQuantizedCDFTable, self).__init__()
        self.base = base
        self.init_scale = init_scale
        self.tail_mass = tail_mass
        self.cdf_precision = cdf_precision

        self.register_buffer('cached_cdf_table', torch.tensor([], dtype=torch.int32))
        self.register_buffer('cached_cdf_length', torch.empty(self.batch_numel, dtype=torch.int32))
        self.register_buffer('cached_cdf_offset', torch.empty(self.batch_numel, dtype=torch.int32))
        self.register_buffer('requires_updating_cdf_table', torch.tensor([True]))
        self.cached_cdf_table_list = None
        self.cached_cdf_length_list = None
        self.cached_cdf_offset_list = None

        if len(base.event_shape) != 0:
            raise NotImplementedError

        try:
            _ = self.base.cdf(0.5)
        except NotImplementedError:
            self.base_cdf_available = False
        else:
            self.base_cdf_available = True

        if not self.base_cdf_available:
            self.estimate_tail()

    def update_base(self, new_base: Distribution):
        assert type(new_base) is type(self.base)
        assert new_base.batch_shape == self.base.batch_shape
        assert new_base.event_shape == self.base.event_shape
        self.base = new_base

    def estimate_tail(self) -> None:
        self.e_input_values = []  # type: List[torch.Tensor]
        self.e_functions = []  # type: List[Callable[[torch.Tensor], torch.Tensor]]
        self.e_target_values = []  # type: List[Union[int, float, torch.Tensor]]

        try:
            _ = self.base.icdf(0.5)
        except NotImplementedError:
            icdf_available = False
        else:
            icdf_available = True

        # Lower tail estimation.
        if icdf_available:
            self.lower_tail_fn = lambda: self.base.icdf(self.tail_mass / 2)
            self.lower_tail_aux_param = None

        elif hasattr(self.base, 'lower_tail'):
            self.lower_tail_fn = lambda: self.base.lower_tail(self.tail_mass)
            self.lower_tail_aux_param = None

        else:
            self.lower_tail_fn = None
            self.lower_tail_aux_param = nn.Parameter(
                torch.full(self.batch_shape,
                           fill_value=-self.init_scale,
                           dtype=torch.float))
            self.e_input_values.append(self.lower_tail_aux_param)
            if hasattr(self.base, 'logits_cdf_for_estimation'):
                self.e_functions.append(
                    lambda *args, **kwargs: self.base.logits_cdf_for_estimation(*args, **kwargs)
                )
                self.e_target_values.append(
                    math.log(self.tail_mass / 2 / (1 - self.tail_mass / 2))
                )
            elif hasattr(self.base, 'log_cdf_for_estimation'):
                self.e_functions.append(
                    lambda *args, **kwargs: self.base.log_cdf_for_estimation(*args, **kwargs)
                )
                self.e_target_values.append(math.log(self.tail_mass / 2))
            else: raise NotImplementedError

        # Upper tail estimation.
        if icdf_available:
            self.upper_tail_fn = lambda: self.base.icdf(1 - self.tail_mass / 2)
            self.upper_tail_aux_param = None

        elif hasattr(self.base, 'upper_tail'):
            self.upper_tail_fn = lambda: self.base.upper_tail(self.tail_mass)
            self.upper_tail_aux_param = None

        else:
            self.upper_tail_fn = None
            self.upper_tail_aux_param = nn.Parameter(
                torch.full(self.batch_shape,
                           fill_value=self.init_scale,
                           dtype=torch.float))
            self.e_input_values.append(self.upper_tail_aux_param)
            if hasattr(self.base, 'logits_cdf_for_estimation'):
                self.e_functions.append(
                    lambda *args, **kwargs: self.base.logits_cdf_for_estimation(*args, **kwargs)
                )
                self.e_target_values.append(
                    -math.log(self.tail_mass / 2 / (1 - self.tail_mass / 2))
                )
            elif hasattr(self.base, 'log_survival_function_for_estimation'):
                self.e_functions.append(
                    lambda *args, **kwargs: self.base.log_survival_function_for_estimation(*args, **kwargs)
                )
                self.e_target_values.append(math.log(self.tail_mass / 2))
            else: raise NotImplementedError

    def lower_tail(self):
        if self.lower_tail_aux_param is not None:
            return self.lower_tail_aux_param
        else:
            return self.lower_tail_fn()

    def upper_tail(self):
        if self.upper_tail_aux_param is not None:
            return self.upper_tail_aux_param
        else:
            return self.upper_tail_fn()

    def mean(self):
        return self.base.mean()

    @property
    def batch_shape(self):
        return self.base.batch_shape

    @property
    def event_shape(self):
        return torch.Size([])

    @property
    def batch_numel(self):
        return self.base.batch_shape.numel()

    @property
    def batch_ndim(self):
        return len(self.base.batch_shape)

    def log_prob(self, value):
        return self.base.log_prob(value)

    def aux_loss(self):
        """
        aux_loss is supposed to be minimized during training to
        estimate distribution tails, which is necessary for CDF table
        generation using "prob" function.
        Distribution with learnable params is supposed to have
        "stop_gradient" arg in their "e_functions".
        """
        if self.e_input_values == self.e_functions == self.e_target_values == []:
            return 0
        else:
            loss = []
            for i, f, t in zip(self.e_input_values, self.e_functions, self.e_target_values):
                try:
                    # Stop gradient of learnable params in distribution
                    # by trying to send a flag.
                    # This try-except block will cause unexpected behavior
                    # if a distribution with learnable params but without
                    # stop_gradient arg is used.
                    p = f(i, stop_gradient=True)
                except TypeError:
                    p = f(i)
                loss.append(torch.abs(p - t).mean())
            return sum(loss)

    @torch.no_grad()
    def build_quantized_cdf_table(self):
        if self.base_cdf_available:
            raise NotImplementedError

        else:
            # TODO(jonycgn, relational): Consider not using offset when soft quantization is used.
            offset = quantization_offset(self.base)

            lower_tail = self.lower_tail()
            upper_tail = self.upper_tail()

            # minima < lower_tail - offset
            # maxima > upper_tail - offset
            minima = torch.floor(lower_tail - offset).to(torch.int32)
            maxima = torch.ceil(upper_tail - offset).to(torch.int32)
            # For stability.
            maxima.clip_(minima)

            # PMF starting positions and lengths.
            pmf_start = minima + offset
            pmf_length = maxima - minima + 1

            # Sample the densities in the computed ranges, possibly computing more
            # samples than necessary at the upper end.
            max_length = pmf_length.max().item()
            if max_length > 2048:
                print(f"Very wide PMF with {max_length} elements may lead to out of memory issues. "
                      "Consider priors with smaller dispersion or increasing `tail_mass` parameter.")
            samples = torch.arange(max_length, device=pmf_start.device)
            samples = samples.reshape(max_length,
                                      *[1] * len(self.base.batch_shape))
            samples = samples + pmf_start[None, ...]  # broadcast

            if hasattr(self.base, 'prob'):
                pmf = self.base.prob(samples)
            else:
                pmf = torch.exp(self.base.log_prob(samples))

            # Collapse batch dimensions of distribution.
            pmf = pmf.reshape(max_length, -1).T
            pmf_length = pmf_length.reshape(-1)

            cdf = batched_pmf_to_quantized_cdf(pmf, pmf_length, max_length, self.cdf_precision)
            cdf_length = pmf_length + 2
            cdf_offset = minima.reshape(-1)

            self.cached_cdf_table = cdf
            self.cached_cdf_length[...] = cdf_length
            self.cached_cdf_offset[...] = cdf_offset
            self.update_quantized_cdf_list()
            self.requires_updating_cdf_table[:] = False

    def update_quantized_cdf_list(self):
        """
        Lists used by range coder.
        Should be updated once self.cached_cdf_table changes.
        """
        self.cached_cdf_table_list = self.cached_cdf_table.tolist()
        self.cached_cdf_length_list = self.cached_cdf_length.tolist()
        self.cached_cdf_offset_list = self.cached_cdf_offset.tolist()

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        """
        Call model.eval() after training before saving state dict
        to use precomputed cdf table latter.
        """
        flag_key = prefix + 'requires_updating_cdf_table'
        if flag_key not in state_dict or state_dict[flag_key]:
            # Delete invalid values in state dict.
            # Those values are supposed to be rebuilt via "model.eval()".
            # Warning of "IncompatibleKeys(missing_keys=[
            # 'entropy_bottleneck.prior.cached_cdf_table',
            # 'entropy_bottleneck.prior.cached_cdf_length',
            # 'entropy_bottleneck.prior.cached_cdf_offset'])"
            # is expected.
            try:
                del state_dict[prefix + 'cached_cdf_table']
            except KeyError: pass
            try:
                del state_dict[prefix + 'cached_cdf_length']
            except KeyError: pass
            try:
                del state_dict[prefix + 'cached_cdf_offset']
            except KeyError: pass
            print('Warning: cached cdf table in state dict requires updating.\n'
                  'You need to call model.eval() to build it after loading state dict '
                  'before any inference.')
            super(DistributionQuantizedCDFTable, self)._load_from_state_dict(
                state_dict, prefix, local_metadata, strict,
                missing_keys, unexpected_keys, error_msgs)
        else:
            # Placeholder
            self.cached_cdf_table = torch.empty_like(
                state_dict[prefix + 'cached_cdf_table'],
                device=self.cached_cdf_table.device)
            super(DistributionQuantizedCDFTable, self)._load_from_state_dict(
                state_dict, prefix, local_metadata, strict,
                missing_keys, unexpected_keys, error_msgs)
            self.update_quantized_cdf_list()

    def train(self, mode: bool = True):
        """
        Use model.train() to invalidate cached cdf table.
        Use model.eval() to call build_quantized_cdf_table().
        """
        if mode is True:
            self.requires_updating_cdf_table[:] = True
        else:
            if self.requires_updating_cdf_table:
                self.build_quantized_cdf_table()
        return super(DistributionQuantizedCDFTable, self).train(mode=mode)

    def _apply(self, fn):
        """
        Code from nn.Module._apply function.
        """
        def compute_should_use_set_data(tensor, tensor_applied):
            # noinspection PyUnresolvedReferences,PyProtectedMember
            if torch._has_compatible_shallow_copy_type(tensor, tensor_applied):
                return not torch.__future__.get_overwrite_module_params_on_conversion()
            else:
                return False

        def distribution_param_apply(obj):
            for var_name, var in obj.__dict__.items():
                if isinstance(var, nn.Parameter):
                    with torch.no_grad():
                        param_applied = fn(var)
                    should_use_set_data = \
                        compute_should_use_set_data(var, param_applied)
                    if should_use_set_data:
                        var.data = param_applied
                    else:
                        assert var.is_leaf
                        obj.__dict__[var_name] = \
                            nn.Parameter(param_applied, var.requires_grad)

                    if var.grad is not None:
                        with torch.no_grad():
                            grad_applied = fn(var.grad)
                        should_use_set_data = \
                            compute_should_use_set_data(var.grad, grad_applied)
                        if should_use_set_data:
                            var.grad.data = grad_applied
                        else:
                            assert var.grad.is_leaf
                            obj.__dict__[var_name].grad = \
                                grad_applied.requires_grad_(var.grad.requires_grad)
                    obj.__dict__[var_name].data = param_applied

                elif isinstance(var, torch.Tensor):
                    obj.__dict__[var_name] = fn(var)

                elif isinstance(var, List):
                    for i, v in enumerate(var):
                        if isinstance(v, torch.Tensor):
                            var[i] = fn(v)

                elif isinstance(var, Distribution):
                    distribution_param_apply(var)

        distribution_param_apply(self.base)
        super(DistributionQuantizedCDFTable, self)._apply(fn)


class ContinuousEntropyModelBase(nn.Module):
    def __init__(self,
                 prior: Distribution,
                 coding_ndim: int,
                 init_scale: int = 10,
                 tail_mass: float = 2 ** -8,
                 range_coder_precision: int = 16):
        super(ContinuousEntropyModelBase, self).__init__()
        # "self.prior" is supposed to be able to generate
        # flat quantized CDF table used by range coder.
        self.prior: DistributionQuantizedCDFTable = DistributionQuantizedCDFTable(
            base=prior,
            init_scale=init_scale,
            tail_mass=tail_mass,
            cdf_precision=range_coder_precision
        )
        self.coding_ndim = coding_ndim
        self.range_coder_precision = range_coder_precision
        self.range_encoder = ans.RansEncoder()
        self.range_decoder = ans.RansDecoder()
        if self.range_coder_precision != 16:
            raise NotImplementedError

    def perturb(self, x: torch.Tensor) -> torch.Tensor:
        if not hasattr(self, "_noise"):
            setattr(self, "_noise", torch.empty(x.shape, dtype=torch.float, device=x.device))
        self._noise.resize_(x.shape)
        self._noise.uniform_(-0.5, 0.5)
        x = x + self._noise
        return x

    @torch.no_grad()
    @minkowski_tensor_wrapped_fn({1: [0, 1]})
    def quantize(self, x: torch.Tensor, offset=None, return_dequantized: bool = False) \
            -> Tuple[torch.Tensor, torch.Tensor]:
        if offset is None: offset = quantization_offset(self.prior.base)
        x -= offset
        torch.round_(x)
        quantized_x = x.to(torch.int32)
        if return_dequantized is True:
            x += offset
        return quantized_x, x

    @torch.no_grad()
    def dequantize(self, x: torch.Tensor, offset=None) -> torch.Tensor:
        if offset is None: offset = quantization_offset(self.prior.base)
        if isinstance(offset, torch.Tensor) and x.device != offset.device:
            x = x.to(offset.device)
        x += offset
        return x.to(torch.float)

    def forward(self, *args, **kwargs) \
            -> Union[Tuple[torch.Tensor, Dict[str, torch.Tensor]],
                     Tuple[torch.Tensor, Dict[str, torch.Tensor], List]]:
        raise NotImplementedError

    def compress(self, *args, **kwargs):
        raise NotImplementedError

    def decompress(self, *args, **kwargs):
        raise NotImplementedError

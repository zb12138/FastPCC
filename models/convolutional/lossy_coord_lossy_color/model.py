import io
from typing import List, Union, Tuple, Generator, Optional
import math

import torch
import torch.nn as nn
import MinkowskiEngine as ME
from MinkowskiEngine.MinkowskiSparseTensor import SparseTensorQuantizationMode
try:
    from pytorch3d.ops.knn import knn_points, knn_gather
except ImportError: pass

from lib.utils import Timer
from lib.torch_utils import TorchCudaMaxMemoryAllocated, concat_loss_dicts
from lib.data_utils import PCData
from lib.evaluators import PCGCEvaluator

from .geo_lossl_em import GeoLosslessEntropyModel
from .layers import Encoder, Decoder, \
    HyperDecoderGenUpsample, HyperDecoderUpsample, EncoderGeoLossl, \
    ResidualGeoLossl, DecoderGeoLossl
from .model_config import ModelConfig


class PCC(nn.Module):

    @staticmethod
    def params_divider(s: str) -> int:
        if '.em_lossless_based' in s:
            if '.blocks_out_first' in s:
                return 0
            elif '.blocks' in s:
                return 1
            else:
                return 2
        else:
            return 0

    def __init__(self, cfg: ModelConfig):
        super(PCC, self).__init__()
        self.cfg = cfg
        ME.set_sparse_tensor_operation_mode(ME.SparseTensorOperationMode.SHARE_COORDINATE_MANAGER)
        self.minkowski_algorithm = getattr(ME.MinkowskiAlgorithm, cfg.minkowski_algorithm)
        self.evaluator = PCGCEvaluator(
            cfg.mpeg_pcc_error_command, 16
        )
        assert len(cfg.encoder_channels) == 2
        assert len(cfg.compressed_channels) == len(cfg.geo_lossl_part_channels)

        self.encoder = Encoder(
            4,
            cfg.geo_lossl_part_channels[0],
            cfg.encoder_channels,
            cfg.adaptive_pruning,
            cfg.adaptive_pruning_num_scaler,
            cfg.conv_region_type,
            cfg.activation
        )
        self.decoder = Decoder(
            cfg.geo_lossl_part_channels[0],
            3,
            cfg.decoder_channels,
            cfg.conv_region_type,
            cfg.activation
        )
        enc_lossl = EncoderGeoLossl(
            cfg.geo_lossl_part_channels[:-1],
            cfg.geo_lossl_part_channels,
            cfg.conv_region_type,
            cfg.activation
        )
        hyper_dec_coord = HyperDecoderGenUpsample(
            cfg.geo_lossl_part_channels[1:],
            1,
            cfg.conv_region_type,
            cfg.activation
        )
        hyper_dec_fea = HyperDecoderUpsample(
            cfg.geo_lossl_part_channels[1:],
            cfg.geo_lossl_part_channels[:-1],
            cfg.conv_region_type,
            cfg.activation
        )
        self.em_lossless_based = self.init_em_lossless_based(
            enc_lossl,
            ResidualGeoLossl(
                tuple(_ * 2 for _ in cfg.geo_lossl_part_channels[:-1]),
                cfg.compressed_channels[:-1],
                cfg.conv_region_type, cfg.activation,
            ),
            DecoderGeoLossl(
                cfg.compressed_channels[:-1],
                cfg.geo_lossl_part_channels[:-1],
                cfg.geo_lossl_part_channels[:-1],
                cfg.conv_region_type,
                cfg.activation
            ),
            hyper_dec_coord, hyper_dec_fea
        )
        self.linear_warmup_fea_step = (self.cfg.warmup_fea_loss_factor -
                                       self.cfg.bits_loss_factor) / self.cfg.warmup_steps
        self.linear_warmup_color_step = (self.cfg.warmup_color_loss_factor -
                                         self.cfg.color_recon_loss_factor) / self.cfg.warmup_steps

    def init_em_lossless_based(
            self, encoder_geo_lossless, residual_block, decoder_block,
            hyper_decoder_coord_geo_lossless, hyper_decoder_fea_geo_lossless,
    ):
        em_lossless_based = GeoLosslessEntropyModel(
            self.cfg.geo_lossl_part_channels[-1],
            self.cfg.bottleneck_process,
            self.cfg.bottleneck_scaler,
            encoder=encoder_geo_lossless,
            residual_block=residual_block,
            decoder_block=decoder_block,
            hyper_decoder_coord=hyper_decoder_coord_geo_lossless,
            hyper_decoder_fea=hyper_decoder_fea_geo_lossless
        )
        return em_lossless_based

    def forward(self, pc_data: PCData):
        if self.training:
            sparse_pc = self.get_sparse_pc(pc_data.xyz, pc_data.color)
            return self.train_forward(sparse_pc, pc_data.training_step, pc_data.batch_size)
        else:
            assert pc_data.batch_size == 1, 'Only supports batch size == 1 during testing.'
            if isinstance(pc_data.xyz, torch.Tensor):
                sparse_pc = self.get_sparse_pc(pc_data.xyz, pc_data.color)
                return self.test_forward(sparse_pc, pc_data)
            else:
                sparse_pc_partitions = self.get_sparse_pc_partitions(pc_data.xyz, pc_data.color)
                return self.test_partitions_forward(sparse_pc_partitions, pc_data)

    def get_sparse_pc(self, xyz: torch.Tensor, color: Optional[torch.Tensor] = None,
                      tensor_stride: int = 1,
                      only_return_coords: bool = False)\
            -> Union[ME.SparseTensor, Tuple[ME.CoordinateMapKey, ME.CoordinateManager]]:
        ME.clear_global_coordinate_manager()
        global_coord_mg = ME.CoordinateManager(
            D=3,
            coordinate_map_type=ME.CoordinateMapType.CUDA if
            xyz.is_cuda
            else ME.CoordinateMapType.CPU,
            minkowski_algorithm=self.minkowski_algorithm
        )
        ME.set_global_coordinate_manager(global_coord_mg)
        if only_return_coords:
            pc_coord_key = global_coord_mg.insert_and_map(xyz, [tensor_stride] * 3)[0]
            return pc_coord_key, global_coord_mg
        else:
            sparse_pc_feature = torch.cat((
                torch.div(color, 255),
                torch.full(
                    (color.shape[0], 1), fill_value=2,
                    dtype=torch.float,
                    device=color.device
                )), 1)
            sparse_pc = ME.SparseTensor(
                features=sparse_pc_feature,
                coordinates=xyz,
                tensor_stride=[tensor_stride] * 3,
                coordinate_manager=global_coord_mg,
                quantization_mode=SparseTensorQuantizationMode.UNWEIGHTED_AVERAGE,
            )
            return sparse_pc

    def get_sparse_pc_partitions(self, xyz: List[torch.Tensor], color: List[torch.Tensor]) -> Generator:
        # The first one is supposed to be the original coordinates.
        for idx in range(1, len(xyz)):
            yield self.get_sparse_pc(xyz[idx], color[idx])

    def train_forward(self, sparse_pc: ME.SparseTensor,
                      training_step: int, batch_size: int):
        warmup_forward = training_step < self.cfg.warmup_steps

        strided_fea_list, points_num_list = self.encoder(sparse_pc)
        feature = strided_fea_list[-1]

        bottleneck_feature, loss_dict = self.em_lossless_based(feature, batch_size)

        decoder_loss_dict = self.decoder(
            bottleneck_feature, points_num_list,
            sparse_pc.coordinate_map_key, sparse_pc.F[:, :-1].mul(255).round_()
        )
        concat_loss_dicts(loss_dict, decoder_loss_dict)

        if warmup_forward:
            if self.cfg.linear_warmup:
                fea_loss_factor = self.cfg.warmup_fea_loss_factor - \
                    self.linear_warmup_fea_step * training_step
                color_loss_factor = self.cfg.warmup_color_loss_factor - \
                    self.linear_warmup_color_step * training_step
            else:
                fea_loss_factor = self.cfg.warmup_fea_loss_factor
                color_loss_factor = self.cfg.warmup_color_loss_factor
        else:
            fea_loss_factor = self.cfg.bits_loss_factor
            color_loss_factor = self.cfg.color_recon_loss_factor

        for key in loss_dict:
            if key.endswith('bits_loss'):
                if 'fea' in key:
                    loss_dict[key] *= fea_loss_factor
                else:
                    loss_dict[key] *= self.cfg.bits_loss_factor
        loss_dict['color_recon_loss'] *= color_loss_factor
        loss_dict['coord_recon_loss'] *= self.cfg.coord_recon_loss_factor

        loss_dict['loss'] = sum(loss_dict.values())
        for key in loss_dict:
            if key != 'loss':
                loss_dict[key] = loss_dict[key].item()
        return loss_dict

    def test_forward(self, sparse_pc: ME.SparseTensor, pc_data: PCData):
        with Timer() as encoder_t, TorchCudaMaxMemoryAllocated() as encoder_m:
            compressed_bytes, sparse_tensor_coords = self.compress(sparse_pc)
        del sparse_pc
        ME.clear_global_coordinate_manager()
        torch.cuda.empty_cache()
        with Timer() as decoder_t, TorchCudaMaxMemoryAllocated() as decoder_m:
            coord_recon, color_recon = self.decompress(compressed_bytes, sparse_tensor_coords)
        ret = self.evaluator.log_batch(
            preds=[coord_recon],
            targets=[pc_data.xyz[:, 1:]],
            compressed_bytes_list=[compressed_bytes],
            pc_data=pc_data,
            preds_color=[color_recon],
            targets_color=[pc_data.color],
            extra_info_dicts=[
                {'encoder_elapsed_time': encoder_t.elapsed_time,
                 'encoder_max_cuda_memory_allocated': encoder_m.max_memory_allocated,
                 'decoder_elapsed_time': decoder_t.elapsed_time,
                 'decoder_max_cuda_memory_allocated': decoder_m.max_memory_allocated}
            ]
        )
        return ret

    def test_partitions_forward(self, sparse_pc_partitions: Generator, pc_data: PCData):
        with Timer() as encoder_t, TorchCudaMaxMemoryAllocated() as encoder_m:
            compressed_bytes, sparse_tensor_coords_list = self.compress_partitions(sparse_pc_partitions)
        del sparse_pc_partitions
        ME.clear_global_coordinate_manager()
        torch.cuda.empty_cache()
        with Timer() as decoder_t, TorchCudaMaxMemoryAllocated() as decoder_m:
            coord_recon, color_recon = self.decompress_partitions(compressed_bytes, sparse_tensor_coords_list)
        ret = self.evaluator.log_batch(
            preds=[coord_recon],
            targets=[pc_data.xyz[0]],
            compressed_bytes_list=[compressed_bytes],
            pc_data=pc_data,
            preds_color=[color_recon],
            targets_color=[pc_data.color[0]],
            extra_info_dicts=[
                {'encoder_elapsed_time': encoder_t.elapsed_time,
                 'encoder_max_cuda_memory_allocated': encoder_m.max_memory_allocated,
                 'decoder_elapsed_time': decoder_t.elapsed_time,
                 'decoder_max_cuda_memory_allocated': decoder_m.max_memory_allocated}
            ]
        )
        return ret

    def compress(self, sparse_pc: ME.SparseTensor) -> Tuple[bytes, torch.Tensor]:
        strided_fea_list, points_num_list = self.encoder(sparse_pc)
        feature = strided_fea_list[-1]

        em_bytes, bottom_fea_recon, fea_recon = self.em_lossless_based.compress(feature, 1)
        sparse_tensor_coords_stride = bottom_fea_recon.tensor_stride[0]
        sparse_tensor_coords = bottom_fea_recon.C

        with io.BytesIO() as bs:
            if self.cfg.adaptive_pruning:
                bs.write(b''.join(
                    (_[0].to_bytes(3, 'little', signed=False) for _ in
                     points_num_list)
                ))
            bs.write(int(math.log2(sparse_tensor_coords_stride)).to_bytes(
                     1, 'little', signed=False))
            bs.write((sparse_tensor_coords[:, 1:].numel() // 2).to_bytes(2, 'little', signed=False))
            for i, el in enumerate((sparse_tensor_coords[:, 1:].reshape(-1) // sparse_tensor_coords_stride).tolist()):
                if i % 2 == 1:
                    bs.write(((last_el << 4) & el).to_bytes(1, 'little', signed=False))
                else:
                    last_el = el
            bs.write(em_bytes)
            compressed_bytes = bs.getvalue()
        return compressed_bytes, sparse_tensor_coords

    def compress_partitions(self, sparse_pc_partitions: Generator) \
            -> Tuple[bytes, List[torch.Tensor]]:
        compressed_bytes_list = []
        sparse_tensor_coords_list = []
        for sparse_pc in sparse_pc_partitions:
            compressed_bytes, sparse_tensor_coords = self.compress(sparse_pc)
            ME.clear_global_coordinate_manager()
            compressed_bytes_list.append(compressed_bytes)
            sparse_tensor_coords_list.append(sparse_tensor_coords)

        concat_bytes = b''.join((len(s).to_bytes(3, 'little', signed=False) + s
                                 for s in compressed_bytes_list))
        return concat_bytes, sparse_tensor_coords_list

    def decompress(self, compressed_bytes: bytes, sparse_tensor_coords: torch.Tensor
                   ) -> Tuple[torch.Tensor, torch.Tensor]:
        with io.BytesIO(compressed_bytes) as bs:
            if self.cfg.adaptive_pruning:
                points_num_list = []
                for idx in range(1):
                    points_num_list.append([int.from_bytes(bs.read(3), 'little', signed=False)])
            else:
                points_num_list = None
            tensor_stride = 2 ** int.from_bytes(bs.read(1), 'little', signed=False)
            sparse_tensor_coords_bytes_len = int.from_bytes(bs.read(2), 'little', signed=False)
            sparse_tensor_coords_bytes = bs.read(sparse_tensor_coords_bytes_len)
            em_bytes = bs.read()

        fea_recon = self.em_lossless_based.decompress(
            em_bytes,
            self.get_sparse_pc(
                sparse_tensor_coords,
                tensor_stride=tensor_stride,
                only_return_coords=True
            ))

        decoder_fea = self.decoder(fea_recon, points_num_list)
        coord_recon = decoder_fea.C[:, 1:]
        color_recon_raw = decoder_fea.F
        color_recon = color_recon_raw.round_()

        return coord_recon, color_recon

    def decompress_partitions(self, concat_bytes: bytes,
                              sparse_tensor_coords_list: List[torch.Tensor]
                              ) -> Tuple[torch.Tensor, torch.Tensor]:
        coord_recon_list = []
        color_recon_list = []
        concat_bytes_len = len(concat_bytes)

        with io.BytesIO(concat_bytes) as bs:
            while bs.tell() != concat_bytes_len:
                length = int.from_bytes(bs.read(3), 'little', signed=False)
                coord_recon, color_recon = self.decompress(
                    bs.read(length), sparse_tensor_coords_list.pop(0)
                )
                coord_recon_list.append(coord_recon)
                color_recon_list.append(color_recon)
                ME.clear_global_coordinate_manager()

        coord_recon_concat = torch.cat(coord_recon_list, 0)
        color_recon_concat = torch.cat(color_recon_list, 0)
        return coord_recon_concat, color_recon_concat

    def train(self, mode: bool = True):
        """
        Use model.train() to reset evaluator.
        """
        if mode is True:
            self.evaluator.reset()
        return super(PCC, self).train(mode=mode)
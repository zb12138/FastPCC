from dataclasses import dataclass
from lib.config import SimpleConfig


@dataclass
class ModelConfig(SimpleConfig):
    res_block_type: str = 'InceptionResNet'
    compressed_channels: int = 8
    reconstruct_loss_type: str = 'BCE'  # BCE or Dist
    adaptive_pruning: bool = True
    bottleneck_scaler: int = 2 ** 7
    bpp_loss_factor: float = 0.3
    reconstruct_loss_factor: float = 1.0
    dist_upper_bound = 2.0
    aux_loss_factor: float = 10.0
    bottleneck: str = 'DeepFactorized'
    balance_loss_factor: float = 1.0
    pred_fea_point_coords: bool = False
    coords_loss_factor: float = 1.0

    # only for test phase:
    chamfer_dist_test_phase: bool = False
    mpeg_pcc_error_command: str = 'pc_error_d'
    mpeg_pcc_error_threads: int = 16

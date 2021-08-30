from dataclasses import dataclass
from typing import Tuple

from lib.config import SimpleConfig


@dataclass
class ModelConfig(SimpleConfig):
    input_feature_type: str = 'Occupation'  # Occupation or Coordinate
    basic_block_type: str = 'InceptionResBlock'  # ResBlock or InceptionResBlock
    basic_block_num: int = 3
    use_batch_norm: bool = False
    activation: str = 'relu'
    use_skip_connection: bool = False
    skipped_fea_fusion_method: str = 'Cat'  # Add or Cat
    encoder_channels: Tuple[int, ...] = (16, 32, 64, 32)
    compressed_channels: int = 8
    decoder_channels: Tuple[int, ...] = (64, 32, 16)
    skip_connection_channels: Tuple[int, ...] = (16, 32, 64)

    hyperprior: str = 'None'
    hyper_compressed_channels: int = 8
    hyper_bpp_loss_factor: float = 0.2
    hyper_encoder_channels: Tuple[int, ...] = (16, 16, 16, 16)
    hyper_decoder_channels: Tuple[int, ...] = (16, 16, 16, 16)
    prior_indexes_range: Tuple[int, ...] = (64, )

    reconstruct_loss_type: str = 'BCE'  # BCE or Dist or Focal
    dist_upper_bound = 2.0
    adaptive_pruning: bool = True
    adaptive_pruning_num_scaler: float = 1.0
    bpp_loss_factor: float = 0.2
    reconstruct_loss_factor: float = 1.0
    bpp_target: float = 0.0

    # only for test phase:
    chamfer_dist_test_phase: bool = False
    mpeg_pcc_error_command: str = 'pc_error_d'
    mpeg_pcc_error_threads: int = 8

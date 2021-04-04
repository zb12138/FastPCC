from dataclasses import dataclass
from lib.config import SimpleConfig

@dataclass
class ModelConfig(SimpleConfig):
    input_points_num: int = 1024
    input_points_dim: int = 3
    sample_method: str = 'uniform'
    neighbor_num: int = 8
    transformer_dim: int = 256

    first_mlp_dim: int = 32
    encoder_blocks_num: int = 2
    chnl_upscale_per_block: int = 2
    dowansacle_per_block: int = 4

    encoded_points_dim: int = 128

    decoder_blocks_num: int = 2
    chnl_downscale_per_block: int = 2
    upsacle_per_block: int = 4

    bpp_loss_factor: float = 1e-2
    aux_loss_factor: float = 1e-4

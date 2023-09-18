from lib.simple_config import SimpleConfig
from dataclasses import dataclass
from typing import Tuple, Union


@dataclass
class DatasetConfig(SimpleConfig):
    # Files list can be generated automatically using all.csv.
    root: str = 'datasets/ShapeNet/ShapeNetCore.v2'
    shapenet_all_csv: str = 'all.csv'
    train_filelist_path: str = 'train_list.txt'
    val_filelist_path: str = 'val_list.txt'  # not used for now.
    test_filelist_path: str = 'test_list.txt'
    train_divisions: Union[str, Tuple[str, ...]] = 'train'
    test_divisions: Union[str, Tuple[str, ...]] = 'test'

    # '.obj' or '.solid.binvox' or '.surface.binvox' or ['.solid.binvox', '.surface.binvox']
    data_format: Union[str, Tuple[str, ...]] = '.surface.binvox'

    # For '.obj' files.
    mesh_sample_points_num: int = 0
    mesh_sample_point_method: str = 'uniform'
    mesh_sample_point_resolution: int = 0
    ply_cache_dtype: str = '<u2'

    random_rotation: bool = False
    kd_tree_partition_max_points_num: int = 0

    resolution: int = 128

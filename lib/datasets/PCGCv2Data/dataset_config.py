from lib.simple_config import SimpleConfig
from dataclasses import dataclass
from typing import Tuple


@dataclass
class DatasetConfig(SimpleConfig):
    root: str = 'datasets/PCGCv2Data'
    train_filelist_path: str = 'train_list.txt'
    test_filelist_path: str = 'test_list.txt'

    train_split_ratio: float = 1.0

    resolution: int = 256
    with_file_path: bool = True
    with_resolution: bool = True
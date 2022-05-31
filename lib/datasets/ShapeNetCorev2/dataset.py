import os
from glob import glob
import hashlib

import numpy as np
from scipy.spatial.transform import Rotation as R
import open3d as o3d
import torch
import torch.utils.data

try:
    import MinkowskiEngine as ME
except ImportError: ME = None

from lib.torch_utils import kd_tree_partition
from lib.data_utils import binvox_rw, PCData, pc_data_collate_fn, write_ply_file
from lib.datasets.ShapeNetCorev2.dataset_config import DatasetConfig
from lib.data_utils import o3d_coords_sampled_from_triangle_mesh, normalize_coords


class ShapeNetCorev2(torch.utils.data.Dataset):
    def __init__(self, cfg: DatasetConfig, is_training, logger):
        super(ShapeNetCorev2, self).__init__()
        if cfg.data_format in ['.solid.binvox', '.surface.binvox'] or \
                cfg.data_format == ['.solid.binvox', '.surface.binvox'] or \
                cfg.data_format == ['.surface.binvox', '.solid.binvox']:
            assert cfg.resolution in [128, 0]
        elif cfg.data_format != '.obj':
            raise NotImplementedError
        data_format = [cfg.data_format] if isinstance(cfg.data_format, str) else cfg.data_format

        # define files list path and cache path
        if is_training:
            filelist_abs_path = os.path.join(cfg.root, cfg.train_filelist_path)
        else:
            filelist_abs_path = os.path.join(cfg.root, cfg.test_filelist_path)

        # generate files list
        if not os.path.exists(filelist_abs_path):
            logger.info('no filelist is given. Trying to generate...')
            file_list = []
            with open(os.path.join(cfg.root, cfg.shapenet_all_csv)) as f:
                f.readline()
                for line in f:
                    _, synset_id, _, model_id, split = line.strip().split(',')
                    file_paths = [
                        os.path.join(synset_id, model_id, 'models', 'model_normalized' + d_format)
                        for d_format in data_format
                        if model_id != '7edb40d76dff7455c2ff7551a4114669'
                        # 7edb40d76dff7455c2ff7551a4114669 seems to be problematic
                    ]
                    for file_path in file_paths:
                        if os.path.exists(os.path.join(cfg.root, file_path)):
                            if (is_training and split == 'train') or \
                                    not is_training and split == 'test':
                                file_list.append(file_path)
            with open(filelist_abs_path, 'w') as f:
                f.writelines([_ + '\n' for _ in file_list])

        # load files list
        self.file_list = []
        logger.info(f'using filelist: "{filelist_abs_path}"')
        with open(filelist_abs_path) as f:
            for line in f:
                line = line.strip()
                ext_name = '.' + '.'.join(os.path.split(line)[1].rsplit('.', 2)[1:])
                assert ext_name == cfg.data_format or ext_name in cfg.data_format, \
                    f'"{line}" in "{filelist_abs_path}" is inconsistent with ' \
                    f'data format "{cfg.data_format}" in config'
                self.file_list.append(os.path.join(cfg.root, line))

        try:
            if cfg.data_format == '.surface.binvox':
                if is_training:
                    assert len(self.file_list) == 35765 - 80 - 1  # 80 have no binvox files
                else:
                    assert len(self.file_list) == 10266 - 13  # 13 have no binvox files
            elif cfg.data_format == '.solid.binvox':
                pass
            elif cfg.data_format == '.obj':
                pass
        except AssertionError as e:
            logger.info('wrong number of files.')
            raise e

        self.cache_root = os.path.join(
            cfg.root, 'cache',
            hashlib.new(
                'md5',
                f'{filelist_abs_path} '
                f'{cfg.mesh_sample_points_num} '
                f'{cfg.mesh_sample_point_method} '
                f'{cfg.mesh_sample_point_resolution} '
                f'{cfg.ply_cache_dtype} '
                f'{cfg.kd_tree_partition_max_points_num}'.encode('utf-8')
            ).hexdigest()
        )
        if cfg.data_format == '.obj':
            self.cached_file_list = [
                _.replace(cfg.root, self.cache_root, 1).replace('.obj', '.ply', 1)
                for _ in self.file_list]
            if os.path.isfile(os.path.join(
                self.cache_root,
                'train_all_cached' if is_training else 'test_all_cached'
            )):
                logger.info(f'using cache : {self.cache_root}')
                if cfg.kd_tree_partition_max_points_num != 0:
                    self.file_list = []
                    for cached_file_path in self.cached_file_list:
                        self.file_list.extend(glob(f'{cached_file_path[:-4]}_*.ply'))
                else:
                    self.file_list = self.cached_file_list
                self.cached_file_list = None
                self.use_cache = True
                self.gen_cache = False
            else:
                os.makedirs(self.cache_root, exist_ok=True)
                with open(os.path.join(self.cache_root, 'dataset_config.yaml'), 'w') as f:
                    f.write(cfg.to_yaml())
                self.use_cache = False
                self.gen_cache = True
        else:
            self.cached_file_list = None
            self.use_cache = self.gen_cache = False

        logger.info(f'filelist[0]: {self.file_list[0]}')
        logger.info(f'filelist[1]: {self.file_list[1]}')
        logger.info(f'length of filelist: {len(self.file_list)}')
        self.cfg = cfg

    def __len__(self):
        return len(self.file_list)

    def __getitem__(self, index):
        file_path = self.file_list[index]
        if self.cfg.data_format != '.obj':
            with open(file_path, 'rb') as f:
                xyz = binvox_rw.read_as_coord_array(f).data.T.astype(np.float64)
                xyz = np.ascontiguousarray(xyz)
                if self.cfg.kd_tree_partition_max_points_num != 0:
                    xyz = kd_tree_partition(torch.from_numpy(xyz), self.cfg.kd_tree_partition_max_points_num)
                    xyz = xyz[np.random.randint(len(xyz))].numpy()
        else:
            if self.use_cache:
                xyz = np.asarray(o3d.io.read_point_cloud(file_path).points)
            else:
                xyz = o3d_coords_sampled_from_triangle_mesh(
                    file_path,
                    self.cfg.mesh_sample_points_num,
                    sample_method=self.cfg.mesh_sample_point_method,
                )[0]
                if self.cfg.mesh_sample_point_resolution != 0:
                    xyz = normalize_coords(xyz)
                    xyz *= (self.cfg.mesh_sample_point_resolution - 1)
                    xyz = np.round(xyz)
                    unique_map = ME.utils.sparse_quantize(xyz, return_maps_only=True).numpy()
                    xyz = xyz[unique_map]
                if self.cfg.kd_tree_partition_max_points_num != 0:
                    xyz_pars_list = kd_tree_partition(
                        torch.from_numpy(xyz), self.cfg.kd_tree_partition_max_points_num
                    )
                    cache_file_base_path = self.cached_file_list[index][:-4]
                    os.makedirs(os.path.dirname(cache_file_base_path), exist_ok=True)
                    for par_idx, xyz_par in enumerate(xyz_pars_list):
                        write_ply_file(
                            xyz_par.numpy(), f'{cache_file_base_path}_{par_idx}.ply',
                            xyz_dtype=self.cfg.ply_cache_dtype
                        )
                    return
                else:
                    cache_file_path = self.cached_file_list[index]
                    os.makedirs(os.path.dirname(cache_file_path), exist_ok=True)
                    write_ply_file(xyz, self.cached_file_list[index], xyz_dtype=self.cfg.ply_cache_dtype)
                    return

        if self.cfg.random_rotation:
            xyz = R.random().apply(xyz)

        if self.cfg.normalize_coords:
            xyz = normalize_coords(xyz)

        if self.cfg.resolution != 0:
            if self.cfg.normalize_coords:
                xyz *= (self.cfg.resolution - 1)
            xyz = np.round(xyz)
            unique_map = ME.utils.sparse_quantize(xyz, return_maps_only=True).numpy()
            xyz = xyz[unique_map]

        return PCData(
            xyz=torch.from_numpy(xyz),
            file_path=file_path,
            ori_resolution=None if self.cfg.data_format == '.obj' else 128,
            resolution=self.cfg.resolution
        )

    def collate_fn(self, batch):
        return pc_data_collate_fn(batch, sparse_collate=self.cfg.resolution != 0)


if __name__ == '__main__':
    config = DatasetConfig()
    config.data_format = '.obj'
    config.train_filelist_path = 'train_list_obj.txt'
    config.mesh_sample_points_num = 500000
    config.resolution = 256

    from loguru import logger
    dataset = ShapeNetCorev2(config, True, logger)
    dataloader = torch.utils.data.DataLoader(dataset, 4, shuffle=False, collate_fn=dataset.collate_fn)
    dataloader = iter(dataloader)
    sample: PCData = next(dataloader)

    from lib.vis import plt_draw_xyz, plt_batched_sparse_xyz
    batched_xyz = sample.xyz
    if config.resolution == 0:
        plt_draw_xyz(batched_xyz[0])
        plt_draw_xyz(batched_xyz[1])
    else:
        plt_batched_sparse_xyz(batched_xyz, 0, True)
        plt_batched_sparse_xyz(batched_xyz, 1, True)
    print('Done')

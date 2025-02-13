import os
import os.path as osp
from glob import glob
import hashlib

import numpy as np
from scipy.spatial.transform import Rotation as R
import open3d as o3d
import torch
import torch.utils.data
import MinkowskiEngine as ME

from lib.data_utils import PCData, pc_data_collate_fn, \
    binvox_rw, write_ply_file, kd_tree_partition_randomly
from lib.datasets.ShapeNetCorev2.dataset_config import DatasetConfig
from lib.data_utils import o3d_coords_sampled_from_triangle_mesh, normalize_coords


class ShapeNetCorev2(torch.utils.data.Dataset):
    def __init__(self, cfg: DatasetConfig, is_training, logger):
        super(ShapeNetCorev2, self).__init__()
        assert cfg.resolution > 1
        if cfg.data_format in ['.solid.binvox', '.surface.binvox'] or \
                cfg.data_format == ['.solid.binvox', '.surface.binvox'] or \
                cfg.data_format == ['.surface.binvox', '.solid.binvox']:
            self.use_binvox = True
        elif cfg.data_format == '.obj':
            assert cfg.mesh_sample_point_resolution > 1
            self.use_binvox = False
        else:
            raise RuntimeError
        self.is_training = is_training
        data_format = [cfg.data_format] if isinstance(cfg.data_format, str) else cfg.data_format

        # define files list path and cache path
        if is_training:
            filelist_abs_path = osp.join(cfg.root, cfg.train_filelist_path)
            official_divisions = cfg.train_divisions
        else:
            filelist_abs_path = osp.join(cfg.root, cfg.test_filelist_path)
            official_divisions = cfg.test_divisions
        if isinstance(official_divisions, str):
            official_divisions = (official_divisions,)

        # generate files list
        if not osp.exists(filelist_abs_path):
            logger.info('no filelist is given. Trying to generate...')
            if 'all' not in official_divisions:
                file_list = []
                with open(osp.join(cfg.root, cfg.shapenet_all_csv)) as f:
                    f.readline()
                    for line in f:
                        _, synset_id, _, model_id, split = line.strip().split(',')
                        file_paths = [
                            osp.join(synset_id, model_id, 'models', 'model_normalized' + d_format)
                            for d_format in data_format]
                        for file_path in file_paths:
                            if osp.exists(osp.join(cfg.root, file_path)):
                                if split in official_divisions:
                                    file_list.append(file_path)
            else:
                file_list = (_[len(cfg.root)+1:] for _ in glob(f'{cfg.root}/*/*/*/*.obj'))
            with open(filelist_abs_path, 'w') as f:
                for _ in file_list:
                    # 7edb40d76dff7455c2ff7551a4114669 seems to be problematic
                    if osp.split(osp.split(_)[0])[0].endswith('7edb40d76dff7455c2ff7551a4114669'):
                        continue
                    f.write(_)
                    f.write('\n')

        # load files list
        self.file_list = []
        logger.info(f'using filelist: "{filelist_abs_path}"')
        with open(filelist_abs_path) as f:
            for line in f:
                line = line.strip()
                ext_name = '.' + '.'.join(osp.split(line)[1].rsplit('.', 2)[1:])
                assert ext_name == cfg.data_format or ext_name in cfg.data_format, \
                    f'"{line}" in "{filelist_abs_path}" is inconsistent with ' \
                    f'data format "{cfg.data_format}" in config'
                self.file_list.append(osp.join(cfg.root, line))

        try:
            if cfg.data_format == '.surface.binvox':
                if is_training and len(official_divisions) == 1 \
                        and official_divisions[0] == ('train',):
                    assert len(self.file_list) == 35765 - 80 - 1  # 80 have no binvox files
                elif not is_training and len(official_divisions) == 1 \
                        and official_divisions[0] == ('test',):
                    assert len(self.file_list) == 10266 - 13  # 13 have no binvox files
            elif cfg.data_format == '.solid.binvox':
                pass
            elif cfg.data_format == '.obj':
                pass
        except AssertionError as e:
            logger.info('wrong number of files.')
            raise e

        if cfg.data_format == '.obj' and cfg.generate_cache:
            self.cache_root = osp.join(
                cfg.root, 'cache',
                hashlib.new(
                    'md5',
                    f'{filelist_abs_path} '
                    f'{cfg.mesh_sample_points_num} '
                    f'{cfg.mesh_sample_point_method} '
                    f'{cfg.mesh_sample_point_resolution} '
                    f'{cfg.ply_cache_dtype} '.encode('utf-8')
                ).hexdigest()
            )
            self.cached_file_list = [
                _.replace(cfg.root, self.cache_root, 1).replace('.obj', '.ply', 1)
                for _ in self.file_list]
            if osp.isfile(osp.join(
                self.cache_root,
                'train_all_cached' if is_training else 'test_all_cached'
            )):
                logger.info(f'using cache : {self.cache_root}')
                self.file_list = self.cached_file_list
                self.cached_file_list = None
                self.use_cache = True
                self.gen_cache = False
            else:
                os.makedirs(self.cache_root, exist_ok=True)
                with open(osp.join(self.cache_root, 'dataset_config.yaml'), 'w') as f:
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
        self.logger = logger

    def __len__(self):
        return len(self.file_list)

    def __getitem__(self, index):
        file_path = self.file_list[index]
        if self.use_binvox:
            with open(file_path, 'rb') as f:
                xyz = binvox_rw.read_as_coord_array(f).data.T.astype(np.float64)
                xyz = np.ascontiguousarray(xyz)
            resolution = 128
        else:
            if self.use_cache:
                xyz = np.asarray(o3d.io.read_point_cloud(file_path).points)
            else:
                xyz = o3d_coords_sampled_from_triangle_mesh(
                    file_path,
                    self.cfg.mesh_sample_points_num,
                    sample_method=self.cfg.mesh_sample_point_method,
                )[0]
                normalize_coords(xyz)
                xyz *= self.cfg.mesh_sample_point_resolution
                if self.gen_cache:
                    xyz = xyz.astype(self.cfg.ply_cache_dtype)
                    xyz = xyz[ME.utils.sparse_quantize(xyz, return_maps_only=True)]
                    cache_file_path = self.cached_file_list[index]
                    write_ply_file(xyz, cache_file_path, self.cfg.ply_cache_dtype, make_dirs=True)
                    return
            resolution = self.cfg.mesh_sample_point_resolution

        if self.cfg.random_rotation:
            xyz = R.random().apply(xyz)
            xyz -= xyz.min(0)

        if self.cfg.resolution != resolution:
            xyz *= self.cfg.resolution / resolution
        xyz = ME.utils.sparse_quantize(xyz).numpy()  # floor to int32

        par_num = self.cfg.kd_tree_partition_max_points_num
        if par_num != 0 and xyz.shape[0] > par_num:
            xyz = kd_tree_partition_randomly(xyz, par_num)
            xyz -= xyz.min(0)

        if self.cfg.random_offset != 0:
            xyz += np.random.randint(0, self.cfg.random_offset, 3, dtype=np.int32)

        return PCData(
            xyz=torch.from_numpy(xyz),
            file_path=file_path
        )

    def collate_fn(self, batch):
        return pc_data_collate_fn(batch, sparse_collate=True)


if __name__ == '__main__':
    config = DatasetConfig()

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

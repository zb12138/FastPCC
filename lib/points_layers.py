from itertools import combinations
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from lib.torch_utils import MLPBlock
from lib.pointnet_utils import index_points, index_points_dists


class TransformerBlock(nn.Module):
    def __init__(self, d_in, d_model, nneighbor, return_idx=True, d_out=None) -> None:
        """
        d_in: input feature channels
        d_model: internal channels
        d_out: output channels (default: d_model)
        """
        super().__init__()
        self.nneighbor = nneighbor
        self.return_idx = return_idx
        self.fc_delta = nn.Sequential(
            nn.Linear(3, d_model),
            nn.ReLU(inplace=True),
            nn.Linear(d_model, d_model)
        )

        self.fc1 = nn.Linear(d_in, d_model)
        self.w_qs = nn.Linear(d_model, d_model, bias=False)
        self.w_ks = nn.Linear(d_model, d_model, bias=False)
        self.w_vs = nn.Linear(d_model, d_model, bias=False)

        self.fc_gamma = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(inplace=True),
            nn.Linear(d_model, d_model)
        )

        if d_out is None: d_out = d_model
        self.fc2 = nn.Linear(d_model, d_out)
        self.shortout_fc = nn.Linear(d_in, d_out)

    # xyz: b, n, 3, features: b, n, d_in
    def forward(self, x):
        xyz, feature, relative_knn_xyz, knn_idx = x
        if relative_knn_xyz is None or knn_idx is None:
            knn_idx = torch.cdist(xyz, xyz, compute_mode='donot_use_mm_for_euclid_dist').topk(self.nneighbor, dim=-1,
                                                                                              largest=False,
                                                                                              sorted=True)[1]
            relative_knn_xyz = xyz[:, :, None, :] - index_points(xyz, knn_idx)  # knn_xyz: b, n, k, 3
        else:
            assert feature.shape[1] == relative_knn_xyz.shape[1] == knn_idx.shape[1]
            assert knn_idx.shape[2] == relative_knn_xyz.shape[2] == self.nneighbor

        pos_enc = self.fc_delta(relative_knn_xyz)  # pos_enc: b, n, k, d_model TODO: pos_encoding

        ori_features = feature
        feature = self.fc1(feature)
        knn_feature = index_points(feature, knn_idx)  # knn_feature: b, n, k, d_model
        query, key, value = self.w_qs(feature), self.w_ks(knn_feature), self.w_vs(knn_feature)
        # query: b, n, d_model   key, value: b, n, k, d_model

        attn = self.fc_gamma(query[:, :, None, :] - key + pos_enc)  # attn: b, n, k, d_model
        attn = F.softmax(attn / np.sqrt(key.size(-1)), dim=-2)

        feature = torch.einsum('bmnf,bmnf->bmf', attn,
                               value + pos_enc)  # (attn * (value + pos_enc)).sum(dim=2) feature: b, n, d_model
        feature = self.fc2(feature) + self.shortout_fc(ori_features)  # feature: b, n, d_out

        if self.return_idx:
            return xyz, feature, relative_knn_xyz, knn_idx
        else:
            return xyz, feature, None, None


class TransitionDown(nn.Module):  # TODO: random sample
    def __init__(self, nsample:int=None, sample_rate:float=None, sample_method:str='uniform'):
        super(TransitionDown, self).__init__()
        assert (nsample is None) != (sample_rate is None)
        self.nsample = nsample
        self.sample_rate = sample_rate
        self.sample_method = sample_method

    def forward(self, x):
        xyz, feature, raw_relative_feature, neighbors_idx = x
        assert not xyz.requires_grad
        if raw_relative_feature is not None: assert not raw_relative_feature.requires_grad
        if neighbors_idx is not None: assert not neighbors_idx.requires_grad
        if self.training: assert feature.requires_grad
        points_num = xyz.shape[1]

        if self.nsample is not None: nsample = self.nsample
        else:
            nsample = int(self.sample_rate * points_num)
            assert nsample / points_num == self.sample_rate

        if self.sample_method == 'uniform':
            uniform_choice = np.random.choice(points_num, nsample, replace=False)
            return xyz[:, uniform_choice], feature[:, uniform_choice], None, None

        elif self.sample_method == 'inverse_knn_density':
            freqs = []  # (batch_size, points_num)
            for ni in neighbors_idx:
                # (points_num, )
                freqs.append(ni.reshape(-1).bincount(minlength=points_num))
            # (batch_size, nsample)
            indexes = torch.multinomial(1 / torch.stack(freqs, dim=0), nsample, replacement=False)
            return index_points(xyz, indexes), index_points(feature, indexes), None, None

        else:
            raise NotImplementedError

    def __repr__(self):
        return f'TransitionDown({self.nsample}, {self.sample_rate}, {self.sample_method})'


class NeighborFeatureGenerator:
    def __init__(self, neighbor_num):
        super(NeighborFeatureGenerator, self).__init__()
        assert neighbor_num > 1
        self.neighbor_num = neighbor_num
        self.channels = None

    def __call__(self, xyz):
        raise NotImplementedError


class RandLANeighborFea(NeighborFeatureGenerator):
    def __init__(self, neighbor_num):
        super(RandLANeighborFea, self).__init__(neighbor_num)
        self.channels = 3 + 3 + 3 + 1

    def __call__(self, xyz):
        dists = torch.cdist(xyz, xyz, compute_mode='donot_use_mm_for_euclid_dist')
        relative_dists, neighbors_idx = dists.topk(self.neighbor_num, dim=-1, largest=False, sorted=True)
        neighbors_xyz = index_points(xyz, neighbors_idx)

        expanded_xyz = xyz[:, :, None].expand(-1, -1, neighbors_xyz.shape[2], -1)
        relative_xyz = expanded_xyz - neighbors_xyz
        relative_dists = relative_dists[:, :, :, None]
        relative_feature = torch.cat([relative_dists, relative_xyz, expanded_xyz, neighbors_xyz], dim=-1)

        return relative_feature, neighbors_idx


class RotationInvariantDistFea(NeighborFeatureGenerator):
    def __init__(self, neighbor_num:int, anchor_points:int):
        super(RotationInvariantDistFea, self).__init__(neighbor_num)
        if anchor_points >= 4:
            self.anchor_points = anchor_points
            self.channels = (anchor_points * (anchor_points - 1) // 2) * 2 + anchor_points ** 2
        else:
            raise NotImplementedError

    def __call__(self, xyz):
        assert len(xyz.shape) == 3 and xyz.shape[2] == 3
        xyz_dists = torch.cdist(xyz, xyz, compute_mode='donot_use_mm_for_euclid_dist')
        neighbors_dists, neighbors_idx = xyz_dists.topk(max(self.neighbor_num, self.anchor_points),
                                                        dim=2, largest=False, sorted=True)
        raw_relative_feature = self.gather_dists(xyz_dists, neighbors_dists, neighbors_idx)
        del xyz_dists
        return raw_relative_feature, neighbors_idx[:, :, :self.neighbor_num]

    def gather_dists(self, xyz_dists, neighbors_dists, neighbors_idx):
        # xyz_dists is still necessary here and can not be replaced by neighbors_dists

        # (B, N, 15 if self.anchor_points == 6)
        intra_anchor_dists = self.gen_intra_anchor_dists(xyz_dists, neighbors_dists[:, :, :self.anchor_points],
                                                         neighbors_idx[:, :, :self.anchor_points])
        # (B, N, self.neighbor_num, self.anchor_points)
        inter_anchor_dists = self.gen_inter_anchor_dists(xyz_dists, neighbors_idx)
        # (B, N, self.neighbor_num, 15)
        center_intra_anchor_dists = intra_anchor_dists[:, :, None, :].expand(-1, -1, self.neighbor_num, -1)
        # (B, N, self.neighbor_num, 15)
        nerigbor_intra_anchor_dists = index_points(intra_anchor_dists, neighbors_idx[:, :, :self.neighbor_num])
        # (B, N, self.neighbor_num, 15 + 15 + self.anchor_points)
        relative_feature = torch.cat([center_intra_anchor_dists, nerigbor_intra_anchor_dists, inter_anchor_dists],
                                     dim=3)

        return relative_feature

    def gen_intra_anchor_dists(self, xyz_dists, neighbors_dists, neighbors_idx):
        if self.anchor_points >= 4:
            sub_anchor_dists = []
            for pi, pj in combinations(range(1, self.anchor_points), 2):
                sub_anchor_dists.append(index_points_dists(xyz_dists,
                                                           neighbors_idx[:, :, pi],
                                                           neighbors_idx[:, :, pj])[:, :, None])
            intra_anchor_dists = torch.cat([neighbors_dists[:, :, 1:], *sub_anchor_dists], dim=2)
            return intra_anchor_dists

        else:
            raise NotImplementedError

    def gen_inter_anchor_dists(self, xyz_dists, neighbors_idx):
        batch_size, points_num, _ = xyz_dists.shape
        anchor_points_idx = neighbors_idx[:, :, :self.anchor_points]  # (B, N, anchor_points)
        neighbors_idx = neighbors_idx[:, :, :self.neighbor_num]

        # (B, N, neighbor_num) -> (batch_size, points_num, neighbor_num, anchor_points)
        neighbors_anchor_points_idx = index_points(anchor_points_idx, neighbors_idx)

        anchor_points_idx = anchor_points_idx[:, :, None, :, None].expand(-1, -1, self.neighbor_num, -1, self.anchor_points)
        neighbors_anchor_points_idx = neighbors_anchor_points_idx[:, :, :, None, :].expand(-1, -1, -1, self.anchor_points, -1)

        # (B, N, neighbor_num, anchor_points(center points index), anchor_points(neighbor points index))
        relative_dists = index_points_dists(xyz_dists, anchor_points_idx, neighbors_anchor_points_idx)
        relative_dists = relative_dists.reshape(batch_size, points_num, self.neighbor_num, self.anchor_points ** 2)
        return relative_dists


class LocalFeatureAggregation(nn.Module):
    def __init__(self, in_channels, neighbor_feature_generator:NeighborFeatureGenerator,
                 neighbor_fea_out_chnls, out_channels, return_neighbor_based_fea=True):
        super(LocalFeatureAggregation, self).__init__()

        self.mlp_neighbor_fea = MLPBlock(neighbor_feature_generator.channels,
                                         neighbor_fea_out_chnls, activation='leaky_relu(0.2)', batchnorm='nn.bn1d')
        self.mlp_attn = nn.Linear(in_channels + neighbor_fea_out_chnls, in_channels + neighbor_fea_out_chnls, bias=False)
        self.mlp_out = MLPBlock(in_channels + neighbor_fea_out_chnls, out_channels, activation=None, batchnorm='nn.bn1d')
        if in_channels != 0: self.mlp_shortcut = MLPBlock(in_channels, out_channels, None, 'nn.bn1d')
        else: self.mlp_shortcut = None

        self.neighbor_feature_generator = neighbor_feature_generator
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.neighbor_fea_out_chnls = neighbor_fea_out_chnls
        self.return_neighbor_based_fea = return_neighbor_based_fea

    def forward(self, x):
        # There are three typical situations:
        #   1. this layer is the first layer of the model. All the rest value will be calculated using xyz
        #   and returned if self.return_neighbor_based_fea is True.
        #   2. this layer is after a sampling layer, which is supposed to be the same as situation 1.
        #   To do this, a sampling layer should return raw_relative_feature and neighbors_idx as None,
        #   or, the layer before sampling should has return_neighbor_based_fea == False.
        #   3. this layer is an normal layer in the model. raw_relative_feature and neighbors_idx from last layer
        #   will be directly used.
        # This format of inputs and outputs is aimed to simplify the forward function of top-level module.

        xyz, feature, raw_neighbors_feature, neighbors_idx = x
        if self.in_channels == 0: assert feature is None
        batch_size, points_num, _ = xyz.shape
        ori_feature = feature

        # calculate these value in dataloader using cpu?
        if raw_neighbors_feature is None or neighbors_idx is None:
            raw_neighbors_feature, neighbors_idx = self.neighbor_feature_generator(xyz)

        if self.in_channels != 0:
            feature = index_points(feature, neighbors_idx)

        neighbors_feature = raw_neighbors_feature.reshape(batch_size,
                                                          points_num * self.neighbor_feature_generator.neighbor_num,
                                                          self.neighbor_feature_generator.channels)
        neighbors_feature = self.mlp_neighbor_fea(neighbors_feature)
        neighbors_feature = neighbors_feature.reshape(batch_size, points_num,
                                                      self.neighbor_feature_generator.neighbor_num,
                                                      self.neighbor_fea_out_chnls)

        if self.in_channels != 0:
            feature = torch.cat([feature, neighbors_feature], dim=3)
        else:
            feature = neighbors_feature

        feature = self.attn_pooling(feature)

        if self.in_channels != 0:
            feature = F.leaky_relu(self.mlp_shortcut(ori_feature) + self.mlp_out(feature), negative_slope=0.2)
        else:
            feature = F.leaky_relu(self.mlp_out(feature), negative_slope=0.2)

        if not self.return_neighbor_based_fea:
            raw_neighbors_feature = neighbors_idx = None
        return xyz, feature, raw_neighbors_feature, neighbors_idx

    def attn_pooling(self, feature):
        attn = F.softmax(self.mlp_attn(feature), dim=2)
        feature = attn * feature
        feature = torch.sum(feature, dim=2)
        return feature


def transformer_block_t():
    input_xyz = torch.rand(16, 1024, 3)
    input_feature = torch.rand(16, 1024, 32)
    transfomer_block = TransformerBlock(d_in=32, d_model=512, nneighbor=16)
    output_fea = transfomer_block(input_xyz, input_feature)
    print('Done')


if __name__ == '__main__':
    class Model(nn.Module):
        def __init__(self):
            super(Model, self).__init__()
            neighbor_feature_generator = RotationInvariantDistFea(16, 4)
            self.layers = nn.Sequential(LocalFeatureAggregation(0, neighbor_feature_generator, 16, 32),)

        def forward(self, x):
            out1 = self.layers((x, None, None, None))
            return out1

    model = Model()
    model.eval()
    xyz = torch.rand(16, 100, 3)
    with torch.no_grad():
        out = model(xyz)
    print('Done')
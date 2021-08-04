from typing import List, Tuple, Union, Optional, Any, Callable
from functools import partial

import numpy as np
import torch
import torch.nn as nn
import MinkowskiEngine as ME

from lib.sparse_conv_layers import GenerativeUpsample, GenerativeUpsampleMessage


class BaseConvBlock(nn.Module):
    def __init__(self,
                 conv_class: Callable,
                 in_channels, out_channels, kernel_size, stride,
                 dilation=1, dimension=3,
                 bn: bool = False,
                 act: Union[str, nn.Module, None] = 'relu'):
        super(BaseConvBlock, self).__init__()

        self.conv = conv_class(
            in_channels, out_channels,
            kernel_size=kernel_size,
            stride=stride,
            dilation=dilation,
            bias=not bn,
            dimension=dimension)
        self.bn = ME.MinkowskiBatchNorm(out_channels) if bn else None
        if act is None or isinstance(act, nn.Module):
            self.act = act
        elif act == 'relu':
            self.act = ME.MinkowskiReLU(inplace=True)
        elif act.startswith('leaky_relu'):
            self.act = ME.MinkowskiLeakyReLU(
                negative_slope=float(act.split('(', 1)[1].split(')', 1)[0]),
                inplace=True)
        else: raise NotImplementedError

    def forward(self, x, **kwargs):
        x = self.conv(x, **kwargs)
        if self.bn is not None:
            x = self.bn(x)
        if self.act is not None:
            x = self.act(x)
        return x


class ConvBlock(BaseConvBlock):
    def __init__(self, *args, **kwargs):
        super(ConvBlock, self).__init__(ME.MinkowskiConvolution, *args, **kwargs)


class GenConvTransBlock(BaseConvBlock):
    def __init__(self, *args, **kwargs):
        super(GenConvTransBlock, self).__init__(ME.MinkowskiGenerativeConvolutionTranspose, *args, **kwargs)


class ResBlock(nn.Module):
    def __init__(self, channels, bn: bool, act: Optional[str]):
        super(ResBlock, self).__init__()
        self.conv0 = ConvBlock(channels, channels, 3, 1, bn=bn, act=act)
        self.conv1 = ConvBlock(channels, channels, 3, 1, bn=bn, act=None)

    def forward(self, x):
        out = self.conv1(self.conv0(x))
        out += x
        return out


class InceptionResBlock(nn.Module):
    def __init__(self, channels, bn: bool, act: Optional[str], out_channels=None):
        super(InceptionResBlock, self).__init__()
        if out_channels is None: out_channels = channels
        self.path_0 = nn.Sequential(
            ConvBlock(channels, out_channels // 4, 3, 1, bn=bn, act=act),
            ConvBlock(out_channels // 4, out_channels // 2, 3, 1, bn=bn, act=None))

        self.path_1 = nn.Sequential(
            ConvBlock(channels, out_channels // 4, 1, 1, bn=bn, act=act),
            ConvBlock(out_channels // 4, out_channels // 4, 3, 1, bn=bn, act=act),
            ConvBlock(out_channels // 4, out_channels // 2, 1, 1, bn=bn, act=None))

        if out_channels != channels:
            self.skip = ConvBlock(channels, out_channels, 3, 1, bn=bn, act=None)
        else:
            self.skip = None

    def forward(self, x):
        out0 = self.path_0(x)
        out1 = self.path_1(x)
        out = ME.cat(out0, out1) + (self.skip(x) if self.skip is not None else x)
        return out


class Encoder(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 intra_channels: Tuple[int],
                 basic_block_type: str,
                 basic_blocks_num: int,
                 use_batch_norm: bool,
                 act: Optional[str],
                 use_skip_connection: bool,
                 skip_connection_channels: Tuple[int] = (0, 0, 0)):
        super(Encoder, self).__init__()
        assert len(intra_channels) - 1 == len(skip_connection_channels)

        self.use_skip_connection = use_skip_connection
        if basic_block_type == 'ResNet':
            basic_block = partial(ResBlock, bn=use_batch_norm, act=act)
        elif basic_block_type == 'InceptionResNet':
            basic_block = partial(InceptionResBlock, bn=use_batch_norm, act=act)
        else: raise NotImplementedError

        self.first_block = ConvBlock(in_channels, intra_channels[0], 3, 1, bn=use_batch_norm, act=act)
        self.blocks = nn.ModuleList()

        for idx in range(len(intra_channels) - 1):
            block = [
                ConvBlock(intra_channels[idx],
                          intra_channels[idx], 3, 1, bn=use_batch_norm, act=act),

                ConvBlock(intra_channels[idx],
                          intra_channels[idx + 1], 2, 2, bn=use_batch_norm, act=act),

                *[basic_block(intra_channels[idx + 1]) for _ in range(basic_blocks_num)]
            ]

            if idx == len(intra_channels) - 2:
                block.append(ConvBlock(intra_channels[idx + 1],
                                       out_channels, 3, 1, bn=use_batch_norm, act=None))

            self.blocks.append(nn.Sequential(*block))

        downsample_blocks_num = len(self.blocks)

        if self.use_skip_connection:
            self.skip_blocks = nn.ModuleList()
            for idx, (ch, skip_ch) in enumerate(zip(intra_channels[:-1], skip_connection_channels)):
                skip_block = [
                    ConvBlock(ch, ch, 3, 1, bn=use_batch_norm, act=act),
                    ConvBlock(ch, skip_ch,
                              2 ** (downsample_blocks_num - idx),
                              2 ** (downsample_blocks_num - idx), bn=use_batch_norm, act=act),
                    ConvBlock(skip_ch, skip_ch, 3, 1, bn=use_batch_norm, act=None)
                ]

                self.skip_blocks.append(nn.Sequential(*skip_block))
        else:
            self.skip_blocks = None

    def forward(self, x) -> Union[ME.SparseTensor, List[ME.SparseTensor], List[List[int]]]:
        points_num_list = [[_.shape[0] for _ in x.decomposed_coordinates]]
        cached_feature_list = []

        x = self.first_block(x)

        for idx, block in enumerate(self.blocks):
            if self.use_skip_connection:
                cached_feature_list.append(self.skip_blocks[idx](x))
            else:
                cached_feature_list.append(x)
            x = block(x)
            if idx != len(self.blocks) - 1:
                points_num_list.append([_.shape[0] for _ in x.decomposed_coordinates])

        return x, cached_feature_list, points_num_list


class SequentialKwArgs(nn.Sequential):
    def __init__(self, *args):
        super(SequentialKwArgs, self).__init__(*args)

    def forward(self, x, **kwargs):
        for idx, module in enumerate(self):
            if idx == 0: x = module(x, **kwargs)
            else: x = module(x)
        return x


class DecoderBlock(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 basic_block_type: str,
                 basic_blocks_num: int,
                 use_batch_norm: bool,
                 act: Optional[str],
                 **kwargs):
        super(DecoderBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.basic_blocks_num = basic_blocks_num

        if basic_block_type == 'ResNet':
            basic_block = partial(ResBlock, bn=use_batch_norm, act=act)
        elif basic_block_type == 'InceptionResNet':
            basic_block = partial(InceptionResBlock, bn=use_batch_norm, act=act)
        else: raise NotImplementedError

        upsample_block = SequentialKwArgs(
            GenConvTransBlock(self.in_channels, self.out_channels, 2, 2, bn=use_batch_norm, act=act),
            ConvBlock(self.out_channels, self.out_channels, 3, 1, bn=use_batch_norm, act=act),
            *[basic_block(self.out_channels) for _ in range(self.basic_blocks_num)])

        classify_block = ConvBlock(self.out_channels, 1, 3, 1, bn=use_batch_norm,
                                   act=act if kwargs.get('loss_type', None) == 'Dist' else None)

        self.generative_upsample = GenerativeUpsample(upsample_block, classify_block, **kwargs)

    def forward(self, x: GenerativeUpsampleMessage):
        return self.generative_upsample(x)


class Decoder(nn.Module):
    def __init__(self,
                 in_channels,
                 intra_channels: Tuple[int],
                 basic_block_type: str,
                 basic_blocks_num: int,
                 use_batch_norm: bool,
                 act: Optional[str],
                 use_skip_connection: bool,
                 skipped_fea_fusion_method: str,
                 skip_connection_channels: Tuple[int] = (0, 0),
                 **kwargs):
        super(Decoder, self).__init__()
        assert len(intra_channels) == len(skip_connection_channels)
        self.use_skip_connection = use_skip_connection

        self.blocks = nn.Sequential(*[
            DecoderBlock(in_channels if idx == 0 else intra_channels[idx - 1], ch,
                         basic_block_type, basic_blocks_num, use_batch_norm, act,
                         use_cached_feature=use_skip_connection,
                         cached_feature_fusion_method=skipped_fea_fusion_method,
                         **kwargs) for idx, ch in enumerate(intra_channels)])

        if use_skip_connection:
            self.skip_blocks = nn.ModuleList()
            for idx, (ch, intra_ch) in enumerate(zip(skip_connection_channels, intra_channels[::-1])):
                self.skip_blocks.append(
                    nn.Sequential(
                        GenConvTransBlock(ch, intra_ch,
                                          2 ** (len(skip_connection_channels) - idx),
                                          2 ** (len(skip_connection_channels) - idx),
                                          bn=use_batch_norm, act=act),
                        ConvBlock(intra_ch, intra_ch, 3, 1,
                                  bn=use_batch_norm, act=None)
                    )
                )
        else:
            self.skip_blocks = None

    def forward(self, x: GenerativeUpsampleMessage):
        if self.use_skip_connection:
            for idx, skip_block in enumerate(self.skip_blocks):
                x.cached_fea_list[idx] = skip_block(x.cached_fea_list[idx])
        return self.blocks(x)

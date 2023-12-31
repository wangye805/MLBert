# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Copyright (c) 2018-2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Variant of the resnet module that takes cfg as an argument.
Example usage. Strings may be specified in the config file.
    model = ResNet(
        "StemWithFixedBatchNorm",
        "BottleneckWithFixedBatchNorm",
        "ResNet50StagesTo4",
    )
OR:
    model = ResNet(
        "StemWithGN",
        "BottleneckWithGN",
        "ResNet50StagesTo4",
    )
Custom implementations may be written in user code and hooked in via the
`register_*` functions.
"""
from collections import namedtuple

import torch
import torch.nn.functional as F
from torch import nn
from maskrcnn_benchmark.layers.nhwc import MaxPool2d_NHWC, FrozenBatchNorm2d_NHWC
from maskrcnn_benchmark.layers.nhwc.misc import Conv2d_NHWC
from maskrcnn_benchmark.layers.nhwc import kaiming_uniform_
from maskrcnn_benchmark.layers.nhwc import nchw_to_nhwc_transform, nhwc_to_nchw_transform
from maskrcnn_benchmark.layers import FrozenBatchNorm2d
from maskrcnn_benchmark.layers import Conv2d
from maskrcnn_benchmark.modeling.make_layers import group_norm
from maskrcnn_benchmark.utils.registry import Registry
from maskrcnn_benchmark.utils.comm import get_rank
from apex.contrib.bottleneck.halo_exchangers import HaloExchangerNoComm, HaloExchangerAllGather, HaloExchangerSendRecv, HaloExchangerPeer

class GatherTensorFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, x):
        if H_split:
            if explicit_nhwc:
                N,Hs,W,C = list(x.shape)
                H = Hs * spatial_group_size
                x_ag = x.new(*[N,H,W,C])
                xl_ag = [x_ag[:,i*Hs:(i+1)*Hs,:,:] for i in range(spatial_group_size)]
            else:
                N,C,Hs,W = list(x.shape)
                H = Hs * spatial_group_size
                x_ag = x.new(*[N,C,H,W])
                xl_ag = [x_ag[:,:,i*Hs:(i+1)*Hs,:] for i in range(spatial_group_size)]
            nn = Hs
        else:
            if explicit_nhwc:
                N,H,Ws,C = list(x.shape)
                W = Ws * spatial_group_size
                x_ag = x.new(*[N,H,W,C])
                xl_ag = [x_ag[:,:,i*Ws:(i+1)*Ws,:] for i in range(spatial_group_size)]
            else:
                N,C,H,Ws = list(x.shape)
                W = Ws * spatial_group_size
                x_ag = x.new(*[N,C,H,W])
                xl_ag = [x_ag[:,:,:,i*Ws:(i+1)*Ws] for i in range(spatial_group_size)]
            nn = Ws
        torch.distributed.all_gather(xl_ag,x,group=spatial_communicator) #,no_copy=True if xl_ag[0].is_contiguous(memory_format=torch.contiguous_format) else False)
        if explicit_nhwc and cast_to_nchw:
            x_ag = nhwc_to_nchw_transform(x_ag)
        ctx.args_for_backward = (spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, nn)
        return x_ag

    @staticmethod
    def backward(ctx, y):
        spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, nn = ctx.args_for_backward
        memory_format = torch.channels_last if not explicit_nhwc and y.is_contiguous(memory_format=torch.channels_last) else torch.contiguous_format
        if H_split:
            if explicit_nhwc:
                yy = y[:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:,:].contiguous(memory_format=memory_format)
            else:
                yy = y[:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:].contiguous(memory_format=memory_format)
        else:
            if explicit_nhwc:
                yy = y[:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:].contiguous(memory_format=memory_format)
            else:
                yy = y[:,:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1)].contiguous(memory_format=memory_format)
        if spatial_group_size > 1:
            yy *= spatial_group_size
        return None, None, None, None, None, None, yy

class GatherTensor(nn.Module):

    def __init__(self, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split):
        super(GatherTensor, self).__init__()

        self.explicit_nhwc = explicit_nhwc
        self.cast_to_nchw = cast_to_nchw
        self.H_split = H_split
        # Option to do a (redundant) all-reduce instead of all-gather
        # All-reduce is more expensive than all-gather, but there appears to be a bug in all-gather
        # that prevents it from working properly inside a cuda graphed section.
        self.reconfigure(spatial_group_size, spatial_group_rank, spatial_communicator, None, None, None)
        self.gather = GatherTensorFunction.apply

    # called with spatial_parallel_args, hence the redundant arguments
    def reconfigure(self, spatial_group_size, spatial_group_rank, spatial_communicator, halo_ex, spatial_method, use_delay_kernel):
        self.spatial_group_size = spatial_group_size
        self.spatial_group_rank = spatial_group_rank
        self.spatial_communicator = spatial_communicator

    def forward(self, x):
        if self.spatial_group_size > 1:
            return self.gather(self.spatial_group_size, self.spatial_group_rank, self.spatial_communicator, self.explicit_nhwc, self.cast_to_nchw, self.H_split, x)
        else:
            if self.explicit_nhwc and self.cast_to_nchw:
                x = nhwc_to_nchw_transform(x)
            return x

class GatherTensorsFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, s0, s1, s2, s3, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, x0, x1, x2, x3):
        current_stream = torch.cuda.current_stream()
        sl, xl = [s0, s1, s2, s3], [x0, x1, x2, x3]
        for s in sl:
            s.wait_stream(current_stream)
        rval, nnn = [], []
        for s, x in zip(sl,xl):
            if H_split:
                if explicit_nhwc:
                    N,Hs,W,C = list(x.shape)
                    H = Hs * spatial_group_size
                    x_ag = x.new(*[N,H,W,C])
                    xl_ag = [x_ag[:,i*Hs:(i+1)*Hs,:,:] for i in range(spatial_group_size)]
                else:
                    N,C,Hs,W = list(x.shape)
                    H = Hs * spatial_group_size
                    x_ag = x.new(*[N,C,H,W])
                    xl_ag = [x_ag[:,:,i*Hs:(i+1)*Hs,:] for i in range(spatial_group_size)]
                nnn.append(Hs)
            else:
                if explicit_nhwc:
                    N,H,Ws,C = list(x.shape)
                    W = Ws * spatial_group_size
                    x_ag = x.new(*[N,H,W,C])
                    xl_ag = [x_ag[:,:,i*Ws:(i+1)*Ws,:] for i in range(spatial_group_size)]
                else:
                    N,C,H,Ws = list(x.shape)
                    W = Ws * spatial_group_size
                    x_ag = x.new(*[N,C,H,W])
                    xl_ag = [x_ag[:,:,:,i*Ws:(i+1)*Ws] for i in range(spatial_group_size)]
                nnn.append(Ws)
            with torch.cuda.stream(s):
                torch.distributed.all_gather(xl_ag,x,group=spatial_communicator,no_copy=True if xl_ag[0].is_contiguous(memory_format=torch.contiguous_format) else False)
                if explicit_nhwc and cast_to_nchw:
                    x_ag = nhwc_to_nchw_transform(x_ag)
            rval.append(x_ag)
        for s in sl:
            current_stream.wait_stream(s)
        ctx.args_for_backward = (sl, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, nnn)
        #print("%d :: x shapes = %s" % (get_rank(), str([list(x.shape) for x in rval])))
        return tuple(rval)

    @staticmethod
    def backward(ctx, y0, y1, y2, y3):
        current_stream = torch.cuda.current_stream()
        sl, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split, nnn = ctx.args_for_backward
        yl = [y0, y1, y2, y3]
        for s in sl:
            s.wait_stream(current_stream)
        rval = []
        for s, y, nn in zip(sl,yl,nnn):
            with torch.cuda.stream(s):
                memory_format = torch.channels_last if not explicit_nhwc and y.is_contiguous(memory_format=torch.channels_last) else torch.contiguous_format
                if H_split:
                    if explicit_nhwc:
                        yy = y[:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:,:].contiguous(memory_format=memory_format)
                    else:
                        yy = y[:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:].contiguous(memory_format=memory_format)
                else:
                    if explicit_nhwc:
                        yy = y[:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1),:].contiguous(memory_format=memory_format)
                    else:
                        yy = y[:,:,:,nn*spatial_group_rank:nn*(spatial_group_rank+1)].contiguous(memory_format=memory_format)
                #print("%d :: y.shape = %s, yy.shape = %s" % (get_rank(), str(list(y.shape)), str(list(yy.shape))))
                if spatial_group_size > 1:
                    yy *= spatial_group_size
                rval.append(yy)
        for s in sl:
            current_stream.wait_stream(s)
        #print("%d :: len(rval) = %d" % (get_rank(), len(rval)))
        return (None, None, None, None, None, None, None, None, None, None,) + tuple(rval)

class GatherTensors(nn.Module):

    def __init__(self, spatial_group_size, spatial_group_rank, spatial_communicator, explicit_nhwc, cast_to_nchw, H_split):
        super(GatherTensors, self).__init__()

        self.explicit_nhwc = explicit_nhwc
        self.cast_to_nchw = cast_to_nchw
        self.H_split = H_split
        # Option to do a (redundant) all-reduce instead of all-gather
        # All-reduce is more expensive than all-gather, but there appears to be a bug in all-gather
        # that prevents it from working properly inside a cuda graphed section.
        self.reconfigure(spatial_group_size, spatial_group_rank, spatial_communicator, None, None, None)
        self.gather = GatherTensorsFunction.apply

    # called with spatial_parallel_args, hence the redundant arguments
    def reconfigure(self, spatial_group_size, spatial_group_rank, spatial_communicator, halo_ex, spatial_method, use_delay_kernel):
        self.spatial_group_size = spatial_group_size
        self.spatial_group_rank = spatial_group_rank
        self.spatial_communicator = spatial_communicator

    def forward(self, x0, x1, x2, x3):
        if self.spatial_group_size > 1:
            streams = [torch.cuda.Stream() for _ in range(4)]
            return self.gather(*streams, self.spatial_group_size, self.spatial_group_rank, self.spatial_communicator, self.explicit_nhwc, self.cast_to_nchw, self.H_split, x0, x1, x2, x3)
        else:
            if self.explicit_nhwc and self.cast_to_nchw:
                x0 = nhwc_to_nchw_transform(x0)
                x1 = nhwc_to_nchw_transform(x1)
                x2 = nhwc_to_nchw_transform(x2)
                x3 = nhwc_to_nchw_transform(x3)
            #print("%d :: REF x shapes = %s" % (get_rank(), str([list(x.shape) for x in [x0,x1,x2,x3]])))
            return x0, x1, x2, x3

# ResNet stage specification
StageSpec = namedtuple(
    "StageSpec",
    [
        "index",  # Index of the stage, eg 1, 2, ..,. 5
        "block_count",  # Numer of residual blocks in the stage
        "return_features",  # True => return the last feature map from this stage
    ],
)

# -----------------------------------------------------------------------------
# Standard ResNet models
# -----------------------------------------------------------------------------
# ResNet-50 (including all stages)
ResNet50StagesTo5 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, False), (2, 4, False), (3, 6, False), (4, 3, True))
)
# ResNet-50 up to stage 4 (excludes stage 5)
ResNet50StagesTo4 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, False), (2, 4, False), (3, 6, True))
)
# ResNet-101 (including all stages)
ResNet101StagesTo5 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, False), (2, 4, False), (3, 23, False), (4, 3, True))
)
# ResNet-101 up to stage 4 (excludes stage 5)
ResNet101StagesTo4 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, False), (2, 4, False), (3, 23, True))
)
# ResNet-50-FPN (including all stages)
ResNet50FPNStagesTo5 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, True), (2, 4, True), (3, 6, True), (4, 3, True))
)
# ResNet-101-FPN (including all stages)
ResNet101FPNStagesTo5 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, True), (2, 4, True), (3, 23, True), (4, 3, True))
)
# ResNet-152-FPN (including all stages)
ResNet152FPNStagesTo5 = tuple(
    StageSpec(index=i, block_count=c, return_features=r)
    for (i, c, r) in ((1, 3, True), (2, 8, True), (3, 36, True), (4, 3, True))
)

class ResNet(nn.Module):
    def __init__(self, cfg):
        super(ResNet, self).__init__()

        # If we want to use the cfg in forward(), then we should make a copy
        # of it and store it for later use:
        # self.cfg = cfg.clone()

        # Translate string names to implementations
        stem_module = _STEM_MODULES[cfg.MODEL.RESNETS.STEM_FUNC]
        stage_specs = _STAGE_SPECS[cfg.MODEL.BACKBONE.CONV_BODY]
        first_transformation_module = _TRANSFORMATION_MODULES[cfg.MODEL.RESNETS.FIRST_TRANS_FUNC]
        transformation_module = _TRANSFORMATION_MODULES[cfg.MODEL.RESNETS.TRANS_FUNC]

        # Construct the stem module
        self.stem = stem_module(cfg)

        # Constuct the specified ResNet stages
        num_groups = cfg.MODEL.RESNETS.NUM_GROUPS
        width_per_group = cfg.MODEL.RESNETS.WIDTH_PER_GROUP
        in_channels = cfg.MODEL.RESNETS.STEM_OUT_CHANNELS
        stage2_bottleneck_channels = num_groups * width_per_group
        stage2_out_channels = cfg.MODEL.RESNETS.RES2_OUT_CHANNELS
        self.freeze_at = cfg.MODEL.BACKBONE.FREEZE_CONV_BODY_AT
        self.stages = []
        self.return_features = {}

        # Get the tensor layout (NHWC vs NCHW)
        self.nhwc = cfg.NHWC
        for stage_idx, stage_spec in enumerate(stage_specs):
            name = "layer" + str(stage_spec.index)
            stage2_relative_factor = 2 ** (stage_spec.index - 1)
            bottleneck_channels = stage2_bottleneck_channels * stage2_relative_factor
            out_channels = stage2_out_channels * stage2_relative_factor
            module = _make_stage(
                first_transformation_module if stage_idx==0 else transformation_module,
                in_channels,
                bottleneck_channels,
                out_channels,
                stage_spec.block_count,
                num_groups,
                cfg.MODEL.RESNETS.STRIDE_IN_1X1,
                first_stride=int(stage_spec.index > 1) + 1,
                nhwc=self.nhwc
            )
            in_channels = out_channels
            self.add_module(name, module)
            self.stages.append(name)
            self.return_features[name] = stage_spec.return_features

        self.has_fpn = "FPN" in cfg.MODEL.BACKBONE.CONV_BODY
        # Optionally freeze (requires_grad=False) parts of the backbone
        self._freeze_backbone(self.freeze_at)
        self.gather = GatherTensor(1, 0, None, self.nhwc, False, True)

    def _freeze_backbone(self, freeze_at):
        if freeze_at < 0:
            return
        for stage_index in range(freeze_at):
            if stage_index == 0:
                m = self.stem  # stage 0 is the stem
            else:
                m = getattr(self, "layer" + str(stage_index))
            for p in m.parameters():
                p.requires_grad = False

    def forward(self, x):
        outputs, streams = [], []
        with torch.no_grad():
            x = self.stem(x)
        for stage_index, stage_name in enumerate(self.stages, start=1):
            if stage_index < self.freeze_at:
                with torch.no_grad():
                    x = getattr(self, stage_name)(x)
            else:
                x = getattr(self, stage_name)(x)
            if self.return_features[stage_name]:
                stream = torch.cuda.Stream()
                streams.append(stream)
                stream.wait_stream(torch.cuda.current_stream())
                with torch.cuda.stream(stream):
                    xo = self.gather(x)
                    if get_rank() == 0:
                        print("%d :: Gatherered %s -> %s" % (get_rank(), str(x.shape), str(list(xo.shape))))
                outputs.append( xo )
        for stream in streams:
            torch.cuda.current_stream().wait_stream(stream)
        return outputs


class ResNetHead(nn.Module):
    def __init__(
        self,
        block_module,
        stages,
        num_groups=1,
        width_per_group=64,
        stride_in_1x1=True,
        stride_init=None,
        res2_out_channels=256,
        dilation=1,
        nhwc=False
    ):
        super(ResNetHead, self).__init__()

        stage2_relative_factor = 2 ** (stages[0].index - 1)
        stage2_bottleneck_channels = num_groups * width_per_group
        out_channels = res2_out_channels * stage2_relative_factor
        in_channels = out_channels // 2
        bottleneck_channels = stage2_bottleneck_channels * stage2_relative_factor

        block_module = _TRANSFORMATION_MODULES[block_module]

        self.stages = []
        stride = stride_init
        for stage in stages:
            name = "layer" + str(stage.index)
            if not stride:
                stride = int(stage.index > 1) + 1
            module = _make_stage(
                block_module,
                in_channels,
                bottleneck_channels,
                out_channels,
                stage.block_count,
                num_groups,
                stride_in_1x1,
                first_stride=stride,
                dilation=dilation,
                nhwc=nhwc
            )
            stride = None
            self.add_module(name, module)
            self.stages.append(name)

    def forward(self, x):
        for stage in self.stages:
            x = getattr(self, stage)(x)
        return x


def _make_stage(
    transformation_module,
    in_channels,
    bottleneck_channels,
    out_channels,
    block_count,
    num_groups,
    stride_in_1x1,
    first_stride,
    dilation=1,
    nhwc=False,
):
    blocks = []
    stride = first_stride
    for _ in range(block_count):
        blocks.append(
            transformation_module(
                in_channels,
                bottleneck_channels,
                out_channels,
                num_groups,
                stride_in_1x1,
                stride,
                dilation=dilation,
                nhwc=nhwc
            )
        )
        stride = 1
        in_channels = out_channels
    return nn.Sequential(*blocks)

class Bottleneck(torch.jit.ScriptModule):
    __constants__ = ['downsample']
    def __init__(
            self,
            in_channels,
            bottleneck_channels,
            out_channels,
            num_groups,
            stride_in_1x1,
            stride,
            dilation,
            norm_func,
            nhwc=False
    ):
        super(Bottleneck, self).__init__()
        conv = Conv2d_NHWC if nhwc else Conv2d
        if in_channels != out_channels:
            down_stride = stride if dilation == 1 else 1
            self.downsample = nn.Sequential(
                conv(
                    in_channels, out_channels,
                    kernel_size=1, stride=down_stride, bias=False
                ),
                norm_func(out_channels),
            )
            for modules in [self.downsample,]:
                for l in modules.modules():
                    if isinstance(l, conv):
                        kaiming_uniform_(l.weight, a=1, nhwc=nhwc)
        else:
            self.downsample = None

        if dilation > 1:
            stride = 1 # reset to be 1

        # The original MSRA ResNet models have stride in the first 1x1 conv
        # The subsequent fb.torch.resnet and Caffe2 ResNe[X]t implementations have
        # stride in the 3x3 conv
        stride_1x1, stride_3x3 = (stride, 1) if stride_in_1x1 else (1, stride)

        self.conv1 = conv(
            in_channels,
            bottleneck_channels,
            kernel_size=1,
            stride=stride_1x1,
            bias=False,
        )
        self.bn1 = norm_func(bottleneck_channels)
        # TODO: specify init for the above
        self.conv2 = conv(
            bottleneck_channels,
            bottleneck_channels,
            kernel_size=3,
            stride=stride_3x3,
            padding=dilation,
            bias=False,
            groups=num_groups,
            dilation=dilation
        )
        self.bn2 = norm_func(bottleneck_channels)
        self.conv3 = conv(
            bottleneck_channels, out_channels, kernel_size=1, bias=False
        )
        self.bn3 = norm_func(out_channels)

        for l in [self.conv1, self.conv2, self.conv3,]:
            kaiming_uniform_(l.weight, a=1, nhwc=nhwc)

    @torch.jit.script_method
    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = F.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = F.relu(out)

        out0 = self.conv3(out)
        out = self.bn3(out0)
        if self.downsample is not None:
            identity = self.downsample(x)
        out = out + identity
        out = out.relu()

        return out


class _BaseStem(torch.jit.ScriptModule):
    def __init__(self, cfg, norm_func):
        super(_BaseStem, self).__init__()

        out_channels = cfg.MODEL.RESNETS.STEM_OUT_CHANNELS
        self.nhwc = cfg.NHWC
        conv = Conv2d_NHWC if self.nhwc else Conv2d
        self.conv1 = conv(
              3, out_channels, kernel_size=7, stride=2, padding=3, bias=False
          )

        self.bn1 = norm_func(out_channels)
        for l in [self.conv1,]:
            kaiming_uniform_(l.weight, a=1)

    @torch.jit.script_method
    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = F.relu(x)
        return x

class BaseStem(torch.nn.Module):
    def __init__(self, cfg, norm_func):
        super(BaseStem, self).__init__()

        self._base_stem = _BaseStem(cfg, norm_func)

        out_channels = cfg.MODEL.RESNETS.STEM_OUT_CHANNELS
        self.nhwc = cfg.NHWC
        max_pool = MaxPool2d_NHWC if self.nhwc else nn.MaxPool2d
        self.max_pool = max_pool(kernel_size=3, stride=2, padding=1)
        self.spatial_group_size = 1
        self.spatial_group_rank = 0
        self.H_split = True

    def reconfigure(self, spatial_group_size, spatial_group_rank, H_split):
        self.spatial_group_size = spatial_group_size
        self.spatial_group_rank = spatial_group_rank
        self.H_split = H_split

    def slice(self, x):
        memory_format = torch.channels_last if not self.nhwc and x.is_contiguous(memory_format=torch.channels_last) else torch.contiguous_format
        if self.H_split:
            if self.nhwc:
                if self.spatial_group_rank == 0:
                    x = x[:,:-2,:,:]
                elif self.spatial_group_rank == self.spatial_group_size-1:
                    x = x[:,2:,:,:]
                else:
                    x = x[:,2:-2,:,:]
            else:
                if self.spatial_group_rank == 0:
                    x = x[:,:,:-2,:]
                elif self.spatial_group_rank == self.spatial_group_size-1:
                    x = x[:,:,2:,:]
                else:
                    x = x[:,:,2:-2,:]
        else:
            if self.nhwc:
                if self.spatial_group_rank == 0:
                    x = x[:,:,:-2,:]
                elif self.spatial_group_rank == self.spatial_group_size-1:
                    x = x[:,:,2:,:]
                else:
                    x = x[:,:,2:-2,:]
            else:
                if self.spatial_group_rank == 0:
                    x = x[:,:,:,:-2]
                elif self.spatial_group_rank == self.spatial_group_size-1:
                    x = x[:,:,:,2:]
                else:
                    x = x[:,:,:,2:-2]
        return x.contiguous(memory_format=memory_format)

    # TODO: make jit work with nhwc max_pool function
    # @torch.jit.script_method
    def forward(self, x):
        #print("%d :: BEFORE x.shape = %s" % (get_rank(), str(list(x.shape))))
        x = self._base_stem(x)
        #print("%d :: AFTER x.shape = %s" % (get_rank(), str(list(x.shape))))
        x = self.max_pool(x)
        # slice output
        if self.spatial_group_size > 1:
            x = self.slice(x)
        return x

try:
    import apex
    from apex.contrib.bottleneck.bottleneck import Bottleneck as FastBottleneck
    class FastBottleneckWithFixedBatchNorm(FastBottleneck):
        def __init__(
                self,
                in_channels,
                bottleneck_channels,
                out_channels,
                num_groups=1,
                stride_in_1x1=True,
                stride=1,
                dilation=1,
                nhwc=False
        ):
            if not nhwc:
                print("Error: Apex bottleneck only support nhwc")
            if num_groups > 1:
                print("Error: Apex bottleneck only support group 1")
            if not stride_in_1x1:
                print("Error: Apex bottleneck only support stride_in_1x1")
            super(FastBottleneckWithFixedBatchNorm, self).__init__(
                in_channels=in_channels,
                bottleneck_channels=bottleneck_channels,
                out_channels=out_channels,
                stride=stride,
                dilation=dilation,
                explicit_nhwc=nhwc,
                use_cudnn=True
            )
except ImportError:
    print("Fast bottleneck not installed. importing to native implementaion.")
    FastBottleneckWithFixedBatchNorm = None

try:
    import apex
    from apex.contrib.bottleneck.bottleneck import SpatialBottleneck as SpatialBottleneck
    class SpatialBottleneckWithFixedBatchNorm(SpatialBottleneck):
        def __init__(
                self,
                in_channels,
                bottleneck_channels,
                out_channels,
                num_groups=1,
                stride_in_1x1=True,
                stride=1,
                dilation=1,
                nhwc=False
        ):
            if not nhwc:
                print("Error: Apex bottleneck only support nhwc")
            if num_groups > 1:
                print("Error: Apex bottleneck only support group 1")
            if not stride_in_1x1:
                print("Error: Apex bottleneck only support stride_in_1x1")
            super(SpatialBottleneckWithFixedBatchNorm, self).__init__(
                in_channels=in_channels,
                bottleneck_channels=bottleneck_channels,
                out_channels=out_channels,
                stride=stride,
                dilation=dilation,
                explicit_nhwc=nhwc,
                use_cudnn=True
            )
except ImportError:
    print("Spatial bottleneck not installed. importing to native implementaion.")
    SpatialBottleneckWithFixedBatchNorm = None


class BottleneckWithFixedBatchNorm(Bottleneck):
    def __init__(
        self,
        in_channels,
        bottleneck_channels,
        out_channels,
        num_groups=1,
        stride_in_1x1=True,
        stride=1,
        dilation=1,
        nhwc=False
    ):
        frozen_batch_norm = FrozenBatchNorm2d_NHWC if nhwc else FrozenBatchNorm2d
        super(BottleneckWithFixedBatchNorm, self).__init__(
            in_channels=in_channels,
            bottleneck_channels=bottleneck_channels,
            out_channels=out_channels,
            num_groups=num_groups,
            stride_in_1x1=stride_in_1x1,
            stride=stride,
            dilation=dilation,
            norm_func=frozen_batch_norm,
            nhwc=nhwc
        )


class StemWithFixedBatchNorm(BaseStem):
    def __init__(self, cfg):
        norm_func=FrozenBatchNorm2d_NHWC if cfg.NHWC else FrozenBatchNorm2d
        super(StemWithFixedBatchNorm, self).__init__(
            cfg, norm_func
        )


class BottleneckWithGN(Bottleneck):
    def __init__(
        self,
        in_channels,
        bottleneck_channels,
        out_channels,
        num_groups=1,
        stride_in_1x1=True,
        stride=1,
        dilation=1,
        nhwc=False
    ):
        super(BottleneckWithGN, self).__init__(
            in_channels=in_channels,
            bottleneck_channels=bottleneck_channels,
            out_channels=out_channels,
            num_groups=num_groups,
            stride_in_1x1=stride_in_1x1,
            stride=stride,
            dilation=dilation,
            norm_func=group_norm
        )


class StemWithGN(BaseStem):
    def __init__(self, cfg):
        super(StemWithGN, self).__init__(cfg, norm_func=group_norm)


_TRANSFORMATION_MODULES = Registry({
    "BottleneckWithFixedBatchNorm": BottleneckWithFixedBatchNorm,
    "FastBottleneckWithFixedBatchNorm": FastBottleneckWithFixedBatchNorm if FastBottleneckWithFixedBatchNorm else BottleneckWithFixedBatchNorm,
    "SpatialBottleneckWithFixedBatchNorm": SpatialBottleneckWithFixedBatchNorm if SpatialBottleneckWithFixedBatchNorm else BottleneckWithFixedBatchNorm,
    "BottleneckWithGN": BottleneckWithGN,
})

_STEM_MODULES = Registry({
    "StemWithFixedBatchNorm": StemWithFixedBatchNorm,
    "StemWithGN": StemWithGN,
})

_STAGE_SPECS = Registry({
    "R-50-C4": ResNet50StagesTo4,
    "R-50-C5": ResNet50StagesTo5,
    "R-101-C4": ResNet101StagesTo4,
    "R-101-C5": ResNet101StagesTo5,
    "R-50-FPN": ResNet50FPNStagesTo5,
    "R-50-FPN-RETINANET": ResNet50FPNStagesTo5,
    "R-101-FPN": ResNet101FPNStagesTo5,
    "R-101-FPN-RETINANET": ResNet101FPNStagesTo5,
    "R-152-FPN": ResNet152FPNStagesTo5,
})

_HALO_EXCHANGERS = Registry({
    "HaloExchangerNoComm": HaloExchangerNoComm,
    "HaloExchangerAllGather": HaloExchangerAllGather,
    "HaloExchangerSendRecv": HaloExchangerSendRecv,
    "HaloExchangerPeer": HaloExchangerPeer,
})

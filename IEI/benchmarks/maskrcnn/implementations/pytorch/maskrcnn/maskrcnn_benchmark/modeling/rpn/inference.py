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
import torch
import itertools
import numpy as np
from maskrcnn_benchmark.modeling.box_coder import BoxCoder
from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist
from maskrcnn_benchmark.structures.boxlist_ops import boxlist_nms
from maskrcnn_benchmark.structures.boxlist_ops import remove_small_boxes
from maskrcnn_benchmark.utils.comm import get_rank
from ..utils import cat
from .utils import permute_and_flatten
from torch.nn.utils.rnn import pad_sequence
from maskrcnn_benchmark import _C as C

class RPNPostProcessor(torch.nn.Module):
    """
    Performs post-processing on the outputs of the RPN boxes, before feeding the
    proposals to the heads
    """

    def __init__(
        self,
        pre_nms_top_n,
        post_nms_top_n,
        nms_thresh,
        min_size,
        box_coder=None,
        fpn_post_nms_top_n=None,
        per_image_search=False,
        cache_constants=False,
        use_split_topk=False
    ):
        """
        Arguments:
            pre_nms_top_n (int)
            post_nms_top_n (int)
            nms_thresh (float)
            min_size (int)
            box_coder (BoxCoder)
            fpn_post_nms_top_n (int)
        """
        super(RPNPostProcessor, self).__init__()
        self.pre_nms_top_n = pre_nms_top_n
        self.post_nms_top_n = post_nms_top_n
        self.nms_thresh = nms_thresh
        self.min_size = min_size
        self.per_image_search = per_image_search
        self.use_split_topk = use_split_topk
        self.cached_constants = {} if cache_constants else None
        self.part_idx_offset = {}

        if box_coder is None:
            box_coder = BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))
        self.box_coder = box_coder

        if fpn_post_nms_top_n is None:
            fpn_post_nms_top_n = post_nms_top_n
        self.fpn_post_nms_top_n = fpn_post_nms_top_n

    def add_gt_proposals(self, proposals, targets):
        """
        Arguments:
            proposals: list[BoxList]
            targets: list[BoxList]
        """
        # Get the device we're operating on
        device = proposals[0].bbox.device

        gt_boxes = [target.copy_with_fields([]) for target in targets]

        # later cat of bbox requires all fields to be present for all bbox
        # so we need to add a dummy for objectness that's missing
        for gt_box in gt_boxes:
            gt_box.add_field("objectness", torch.ones(len(gt_box), device=device))

        proposals = [
            cat_boxlist((proposal, gt_box))
            for proposal, gt_box in zip(proposals, gt_boxes)
        ]

        return proposals

    def forward_for_single_feature_map(self, anchors, objectness, box_regression, nms = True):
        """
        Arguments:
            anchors: list[BoxList]
            objectness: tensor of size N, A, H, W
            box_regression: tensor of size N, A * 4, H, W
            nms: True if nms is applied, False otherwise
        """
        device = objectness.device
        N, A, H, W = objectness.shape

        num_anchors = A * H * W

        # If inputs are on GPU, use a faster path
        use_fast_cuda_path = (objectness.is_cuda and box_regression.is_cuda)
        # Encompasses box decode, clip_to_image and remove_small_boxes calls
        if use_fast_cuda_path:
            objectness = objectness.reshape(N, -1) # Now [N, AHW]
            objectness = objectness.sigmoid()

            pre_nms_top_n = min(self.pre_nms_top_n, num_anchors)
            objectness, topk_idx = objectness.topk(pre_nms_top_n, dim=1, sorted=True)

            # Get all image shapes, and cat them together
            image_shapes = [box.size for box in anchors]
            image_shapes_cat = torch.tensor([box.size for box in anchors], device=objectness.device).float()

            # Get a single tensor for all anchors
            concat_anchors = torch.cat([a.bbox for a in anchors], dim=0)

            # Note: Take all anchors, we'll index accordingly inside the kernel
            # only take the anchors corresponding to the topk boxes
            concat_anchors = concat_anchors.reshape(N, -1, 4) # [batch_idx, topk_idx]

            # Return pre-nms boxes, associated scores and keep flag
            # Encompasses:
            # 1. Box decode
            # 2. Box clipping
            # 3. Box filtering
            # At the end we need to keep only the proposals & scores flagged
            # Note: topk_idx, objectness are sorted => proposals, objectness, keep are also
            # sorted -- this is important later
            proposals, objectness, keep = C.GeneratePreNMSUprightBoxes(
                                    N,
                                    A,
                                    H,
                                    W,
                                    topk_idx,
                                    objectness.float(),    # Need to cast these as kernel doesn't support fp16
                                    box_regression.float(),
                                    concat_anchors,
                                    image_shapes_cat,
                                    pre_nms_top_n,
                                    self.min_size,
                                    self.box_coder.bbox_xform_clip,
                                    True)


            # view as [N, pre_nms_top_n, 4]
            proposals = proposals.view(N, -1, 4)
            objectness = objectness.view(N, -1)
        else:
            # reverse the reshape from before ready for permutation
            objectness = objectness.reshape(N, A, H, W)
            objectness = objectness.permute(0, 2, 3, 1).reshape(N, -1)
            objectness = objectness.sigmoid()

            pre_nms_top_n = min(self.pre_nms_top_n, num_anchors)
            objectness, topk_idx = objectness.topk(pre_nms_top_n, dim=1, sorted=True)

            # put in the same format as anchors
            box_regression = box_regression.view(N, -1, 4, H, W).permute(0, 3, 4, 1, 2)
            box_regression = box_regression.reshape(N, -1, 4)


            batch_idx = torch.arange(N, device=device)[:, None]
            box_regression = box_regression[batch_idx, topk_idx]

            image_shapes = [box.size for box in anchors]
            concat_anchors = torch.cat([a.bbox for a in anchors], dim=0)
            concat_anchors = concat_anchors.reshape(N, -1, 4)[batch_idx, topk_idx]

            proposals = self.box_coder.decode(
                box_regression.view(-1, 4), concat_anchors.view(-1, 4)
            )
   
            proposals = proposals.view(N, -1, 4)

        # handle non-fast path without changing the loop
        if not use_fast_cuda_path:
            keep = [None for _ in range(N)]

        result = []
        for proposal, score, im_shape, k in zip(proposals, objectness, image_shapes, keep):
            if use_fast_cuda_path:
                # Note: Want k to be applied per-image instead of all-at-once in batched code earlier
                #       clip_to_image and remove_small_boxes already done in single kernel
                p = proposal.masked_select(k[:, None]).view(-1, 4)
                score = score.masked_select(k)
                boxlist = BoxList(p, im_shape, mode="xyxy")
            else:
                boxlist = BoxList(proposal, im_shape, mode="xyxy")
                boxlist = boxlist.clip_to_image(remove_empty=False)
                boxlist = remove_small_boxes(boxlist, self.min_size)
            boxlist.add_field("objectness", score)
            if nms:
                boxlist = boxlist_nms(
                    boxlist,
                    self.nms_thresh,
                    max_proposals=self.post_nms_top_n,
                    score_field="objectness",
                )
            result.append(boxlist)
        return result
                
    def batched_nms(self, sampled_boxes):
        """
        Applies batched NMS on proposals from each image and feature map in parallel.
        Runs everything on GPU and avoids CPU copies
        Requires that proposals for each BoxList object in the input are sorted 
        with respect to scores.
         
        Arguments:
            sampled_boxes: list[list[BoxList]]              
        """
        boxlist_batched = []
        num_boxes_batched = []
        num_levels = len(sampled_boxes)
        num_images = len(sampled_boxes[0])
        for box_list in sampled_boxes:
             for box in box_list:
                 boxlist_batched.append(box.bbox)
                 num_boxes_batched.append(box.bbox.size(0))
        num_boxes_tensor = torch.tensor(num_boxes_batched, device=box.bbox.device, dtype=torch.int32)
        boxes_cat = torch.cat(boxlist_batched)
        # nms_batched requires input boxes to be sorted, which is the case here
        keep_inds_batched = C.nms_batched(boxes_cat, num_boxes_batched, num_boxes_tensor, self.nms_thresh)
        ptr = 0
        start_idx = 0
        sampled_boxes_post_nms = []
        # seperate concatanated boxes for each image/feature map
        for i in range(num_levels):
          per_level_boxlist = []
          for j in range(0, num_images):
            end_idx = start_idx + num_boxes_batched[ptr]
            keep_per_box = keep_inds_batched[start_idx:end_idx]
            inds_per_box = keep_per_box.nonzero().squeeze(1)
            keep_size = min(inds_per_box.size(0), self.post_nms_top_n)
            inds_per_box = inds_per_box[:keep_size]
            per_level_boxlist.append(sampled_boxes[i][j][inds_per_box])
            ptr = ptr + 1
            start_idx = end_idx
          sampled_boxes_post_nms.append(per_level_boxlist)
        return sampled_boxes_post_nms                

    def compute_constant_tensors(self, N, A, H_max, W_max, num_fmaps, anchors, objectness):
        device = anchors[0].device
        num_anchors_per_level=[]
        for l in range(len(objectness)):
            #num_anchors_per_level.append(objectness[l].view(N, -1).size(1))
            objectness_shape = list(objectness[l].shape)
            num_anchors_per_level.append(np.prod(objectness_shape)//objectness_shape[0])
        num_anchors_per_level = torch.tensor(num_anchors_per_level, device = device)

        # batched height, width data for feature maps
        fmap_size_list = [[obj.shape[2],obj.shape[3]] for obj in objectness]
        fmap_size_cat = torch.tensor([[obj.shape[2], obj.shape[3]] for obj in objectness], device = device)
        
        num_max_proposals = [self.pre_nms_top_n] * (N * num_fmaps)
        num_max_props_tensor = torch.tensor(num_max_proposals, device = device, dtype = torch.int32)

        return N, A, H_max, W_max, num_fmaps, num_anchors_per_level, fmap_size_cat, num_max_proposals, num_max_props_tensor

    def get_constant_tensors(self, anchors, objectness):
        (N, A, H_max, W_max), num_fmaps = objectness[0].shape, len(objectness)
        if self.cached_constants is not None:
            key = (N, A, H_max, W_max, num_fmaps)
            if key in self.cached_constants:
                return self.cached_constants[key]
            else:
                cc = self.compute_constant_tensors(N, A, H_max, W_max, num_fmaps, anchors, objectness)
                #print("key = %s :: cc = %s" % (str(key), str(cc)))
                self.cached_constants[key] = cc
                return cc
        else:
            return self.compute_constant_tensors(N, A, H_max, W_max, num_fmaps, anchors, objectness)
    
    def split_topk(self, input, k, sorted=False, num_chunks=2):
        N0, N1 = list(input.shape)
        NN0, NN1 = N0*num_chunks, N1//num_chunks
        part_input = input.reshape([NN0,NN1])
        part_val, part_idx = torch.topk(part_input, k=k, dim=1)
        key = (N0, N1)
        if not key in self.part_idx_offset:
            pio = torch.tensor([NN1*i for i in range(num_chunks)]*N0, dtype=part_idx.dtype, pin_memory=True).to(device='cuda', non_blocking=True).reshape([NN0,1])
            self.part_idx_offset[key] = pio
        else:
            pio = self.part_idx_offset[key]
        part_idx.add_(pio)
        part_val = part_val.reshape([N0,k*num_chunks])
        part_idx = part_idx.reshape([N0,k*num_chunks])
        top_val, idx2 = torch.topk(part_val, k=k, sorted=sorted, dim=1)
        top_idx = torch.gather(part_idx, 1, idx2)
        return top_val, top_idx

    def forward(self, anchors, objectness, box_regression, image_shapes_cat, targets=None):
        """
        Arguments:
            anchors: list[list[BoxList]]
            objectness: list[tensor]
            box_regression: list[tensor]

        Returns:
            boxlists (list[BoxList]): the post-processed anchors, after
                applying box decoding and NMS
        """

        current_stream = torch.cuda.current_stream()
        stream1 = torch.cuda.Stream()
        stream1.wait_stream(current_stream)
        device = anchors[0].device #this is the batched anchors tensor

        N, A, H_max, W_max, num_fmaps, num_anchors_per_level, fmap_size_cat, num_max_proposals, num_max_props_tensor = self.get_constant_tensors(anchors, objectness)

        # initialize batched objectness, regression tensors and then form them
        batched_objectness_tensor = torch.empty([num_fmaps, N, A * H_max * W_max], dtype = objectness[0].dtype, device=objectness[0].device).fill_(-65500)
        for i in range(num_fmaps):
            H, W = objectness[i].shape[2], objectness[i].shape[3]
            batched_objectness_tensor[i,:,:(A * H * W)] = objectness[i].reshape(N, -1)
        batched_objectness_tensor = batched_objectness_tensor.reshape(num_fmaps * N, -1)
        batched_objectness_tensor = batched_objectness_tensor.sigmoid()
        if self.use_split_topk:
            batched_objectness_topk, topk_idx = self.split_topk(batched_objectness_tensor, self.pre_nms_top_n, sorted=True, num_chunks=4)
        else:
            batched_objectness_topk, topk_idx = batched_objectness_tensor.topk(self.pre_nms_top_n, dim=1, sorted=True)
        batched_objectness_topk = batched_objectness_topk.float() # Need to cast these as kernel doesn't support fp16

        with torch.cuda.stream(stream1):
            batched_regression_tensor = torch.empty([num_fmaps, N, 4 * A * H_max * W_max], dtype = objectness[0].dtype, device=objectness[0].device).fill_(-1)
            for i in range(num_fmaps):
                H, W = objectness[i].shape[2], objectness[i].shape[3]
                batched_regression_tensor[i,:,:(4 * A * H * W)] = box_regression[i].reshape(N, -1)
            batched_regression_tensor = batched_regression_tensor.float() # Need to cast these as kernel doesn't support fp16
            batched_regression_tensor.record_stream(current_stream)
        current_stream.wait_stream(stream1)

        batched_anchor_tensor, image_shapes = anchors[0], anchors[2]

        # generate proposals using a batched kernel
        proposals_gen, objectness_gen, keep_gen = C.GeneratePreNMSUprightBoxesBatched(
                                N,
                                A,
                                H_max*W_max,
                                A*H_max*W_max,
                                fmap_size_cat,
                                num_anchors_per_level,
                                topk_idx,
                                batched_objectness_topk,
                                batched_regression_tensor,
                                batched_anchor_tensor,
                                image_shapes_cat,
                                self.pre_nms_top_n,
                                self.min_size,
                                self.box_coder.bbox_xform_clip,
                                True)
    
        # keep is padded with 0s for image,fmap pairs where num_proposals<self.pre_nms_top_n

        keep_gen = keep_gen.reshape(N * num_fmaps, self.pre_nms_top_n)
        proposals_gen = proposals_gen.reshape(N * num_fmaps * self.pre_nms_top_n, 4)

        # perform batched NMS kernel
        keep_nms_batched = C.nms_batched(proposals_gen, num_max_proposals, num_max_props_tensor, keep_gen, self.nms_thresh).bool()
        keep_nms_batched = keep_nms_batched.reshape(num_fmaps, N, -1)
        keep = keep_nms_batched.reshape(num_fmaps, N, self.pre_nms_top_n)
        # switch leading two dimensions from (f_map, image) to (image, fmap)
        proposals_gen = proposals_gen.reshape(num_fmaps, N, self.pre_nms_top_n, 4)
        objectness_gen = objectness_gen.reshape(num_fmaps, N, self.pre_nms_top_n)
        keep = keep.permute(1, 0, 2)
        objectness_gen = objectness_gen.permute(1, 0, 2)
        proposals_gen = proposals_gen.permute(1, 0, 2, 3)
        if not self.training:
            # split batched results back into boxlists
            keep = keep.split(1)
            objectness_gen = objectness_gen.split(1)
            proposals_gen = proposals_gen.split(1)
            boxlists=[]
            for i in range(N):
                boxlist = BoxList(proposals_gen[i][keep[i]], image_shapes[i], mode="xyxy")
                boxlist.add_field("objectness", objectness_gen[i][keep[i]])
                boxlists.append(boxlist)
            if num_fmaps > 1:
                boxlists = self.select_over_all_levels(boxlists)
            return boxlists

        target_bboxes, target_objectness, _, _ = targets
        if self.per_image_search: # TO-DO: aren't per image and per batch search the same when N == 1
            # Post NMS per image search
            stream1.wait_stream(current_stream)
            objectness_gen.masked_fill_(~keep, -1)
            objectness_gen = objectness_gen.reshape(N,-1)
            objectness = objectness_gen
            _, inds_post_nms_top_n = torch.topk(objectness, self.fpn_post_nms_top_n, dim=1, sorted=False)
            inds_post_nms_top_n, _ = inds_post_nms_top_n.sort()
            with torch.cuda.stream(stream1):
                proposals_gen.masked_fill_((~keep).unsqueeze(3), -1)
                proposals_gen = proposals_gen.reshape(N,-1,4)
                batch_inds = torch.arange(N, device=device)[:,None]
            stream1.wait_stream(current_stream)
            objectness = torch.gather(objectness_gen, dim=1, index=inds_post_nms_top_n)
            objectness = torch.cat([objectness, target_objectness], dim=1)
            with torch.cuda.stream(stream1):
                proposals = proposals_gen[batch_inds, inds_post_nms_top_n]
                proposals = torch.cat([proposals, target_bboxes], dim=1)
                proposals.record_stream(current_stream)
            current_stream.wait_stream(stream1)
        else:
            # Post NMS per batch search
            objectness_gen = objectness_gen * keep
            objectness_gen = objectness_gen.reshape(-1)
            _, inds_post_nms_top_n = torch.topk(objectness_gen, min(self.fpn_post_nms_top_n,objectness_gen.shape[0]), dim=0, sorted=False)

            objectness = torch.empty_like(objectness_gen).fill_(-1)
            objectness[inds_post_nms_top_n] = objectness_gen[inds_post_nms_top_n]
            objectness = objectness.reshape(N,-1)
            proposals = torch.zeros_like(proposals_gen).reshape(-1,4)
            proposals[inds_post_nms_top_n] = proposals_gen.reshape(-1,4)[inds_post_nms_top_n]
            proposals = proposals.reshape(N,-1,4)

            stream1.wait_stream(current_stream)
            proposals = torch.cat([proposals, target_bboxes], dim=1)
            with torch.cuda.stream(stream1):
                objectness = torch.cat([objectness, target_objectness], dim=1)
                objectness.record_stream(current_stream)
            current_stream.wait_stream(stream1)

        return [proposals, objectness, image_shapes]



    def select_over_all_levels(self, boxlists):
        num_images = len(boxlists)
        # different behavior during training and during testing:
        # during training, post_nms_top_n is over *all* the proposals combined, while
        # during testing, it is over the proposals for each image
        # TODO resolve this difference and make it consistent. It should be per image,
        # and not per batch
        if self.training:
            objectness = torch.cat(
                [boxlist.get_field("objectness") for boxlist in boxlists], dim=0
            ) if num_images > 1 else boxlists[0].get_field("objectness")
            box_sizes = [len(boxlist) for boxlist in boxlists]
            post_nms_top_n = min(self.fpn_post_nms_top_n, len(objectness))
            _, inds_sorted = torch.topk(objectness, post_nms_top_n, dim=0, sorted=True)
            inds_mask = torch.zeros_like(objectness, dtype=torch.uint8)
            inds_mask[inds_sorted] = 1
            inds_mask = inds_mask.split(box_sizes)
            for i in range(num_images):
                boxlists[i] = boxlists[i][inds_mask[i]]
        else:
            for i in range(num_images):
                objectness = boxlists[i].get_field("objectness")
                post_nms_top_n = min(self.fpn_post_nms_top_n, len(objectness))
                _, inds_sorted = torch.topk(
                    objectness, post_nms_top_n, dim=0, sorted=True
                )
                boxlists[i] = boxlists[i][inds_sorted]
        return boxlists

def make_rpn_postprocessor(config, rpn_box_coder, is_train):
    per_image_search = config.MODEL.RPN.FPN_POST_NMS_TOP_N_PER_IMAGE
    fpn_post_nms_top_n = config.MODEL.RPN.FPN_POST_NMS_TOP_N_TRAIN
    if not is_train:
        fpn_post_nms_top_n = config.MODEL.RPN.FPN_POST_NMS_TOP_N_TEST

    pre_nms_top_n = config.MODEL.RPN.PRE_NMS_TOP_N_TRAIN
    post_nms_top_n = config.MODEL.RPN.POST_NMS_TOP_N_TRAIN
    if not is_train:
        pre_nms_top_n = config.MODEL.RPN.PRE_NMS_TOP_N_TEST
        post_nms_top_n = config.MODEL.RPN.POST_NMS_TOP_N_TEST
    nms_thresh = config.MODEL.RPN.NMS_THRESH
    min_size = config.MODEL.RPN.MIN_SIZE
    box_selector = RPNPostProcessor(
        pre_nms_top_n=pre_nms_top_n,
        post_nms_top_n=post_nms_top_n,
        nms_thresh=nms_thresh,
        min_size=min_size,
        box_coder=rpn_box_coder,
        fpn_post_nms_top_n=fpn_post_nms_top_n,
        per_image_search=per_image_search,
        cache_constants=config.PRECOMPUTE_RPN_CONSTANT_TENSORS and is_train,
        use_split_topk=config.MODEL.RPN.USE_SPLIT_TOPK,
    )
    return box_selector

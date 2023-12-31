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
import bisect
import copy
import logging

import torch.utils.data
from maskrcnn_benchmark.utils.comm import get_rank, get_world_size
from maskrcnn_benchmark.utils.batch_size import per_gpu_batch_size
from maskrcnn_benchmark.utils.imports import import_file

from . import datasets as D
from . import samplers

from .collate_batch import BatchCollator
from .transforms import build_transforms

from maskrcnn_benchmark.data.datasets.coco import COCODALIDataloader
from maskrcnn_benchmark.data.datasets.coco import HybridDataLoader3

def build_dataset(dataset_list, transforms, dataset_catalog, is_train=True, global_transforms=False, transforms_properties=None, comm=None, master_rank=0):
    """
    Arguments:
        dataset_list (list[str]): Contains the names of the datasets, i.e.,
            coco_2014_trian, coco_2014_val, etc
        transforms (callable): transforms to apply to each (image, target) sample
        dataset_catalog (DatasetCatalog): contains the information on how to
            construct a dataset.
        is_train (bool): whether to setup the dataset for training or testing
    """
    if not isinstance(dataset_list, (list, tuple)):
        raise RuntimeError(
            "dataset_list should be a list of strings, got {}".format(dataset_list)
        )
    datasets = []
    total_datasets_size = 0
    for dataset_name in dataset_list:
        data = dataset_catalog.get(dataset_name)
        factory = getattr(D, data["factory"])
        args = data["args"]
        # for COCODataset, we want to remove images without annotations
        # during training
        if data["factory"] == "COCODataset" or data["factory"] == "COCODatasetPYT":
            args["remove_images_without_annotations"] = is_train
        if data["factory"] == "COCODatasetPYT":
            args["global_transforms"] = global_transforms
            args["transforms_properties"] = transforms_properties
            args["comm"] = comm
            args["master_rank"] = master_rank
        if data["factory"] == "PascalVOCDataset":
            args["use_difficult"] = not is_train
        args["transforms"] = transforms
        # make dataset from factory
        dataset = factory(**args)
        total_datasets_size += len(dataset)
        datasets.append(dataset)

    # for testing, return a list of datasets
    if not is_train:
        return datasets, total_datasets_size

    # for training, concatenate all datasets into a single one
    dataset = datasets[0]
    if len(datasets) > 1:
        dataset = D.ConcatDataset(datasets)

    return [dataset], total_datasets_size


def make_data_sampler(dataset, shuffle, distributed, rank, num_ranks):
    if distributed:
        return samplers.DistributedSampler(dataset, shuffle=shuffle, num_replicas=num_ranks, rank=rank)
    if shuffle:
        sampler = torch.utils.data.sampler.RandomSampler(dataset)
    else:
        sampler = torch.utils.data.sampler.SequentialSampler(dataset)
    return sampler


def _quantize(x, bins):
    bins = copy.copy(bins)
    bins = sorted(bins)
    quantized = list(map(lambda y: bisect.bisect_right(bins, y), x))
    return quantized


def _compute_aspect_ratios(dataset):
    aspect_ratios = []
    for i in range(len(dataset)):
        img_info = dataset.get_img_info(i)
        aspect_ratio = float(img_info["height"]) / float(img_info["width"])
        aspect_ratios.append(aspect_ratio)
    return aspect_ratios


def make_batch_data_sampler(
    dataset, sampler, aspect_grouping, images_per_batch, num_iters=None, start_iter=0, random_number_generator=None,
):
    if aspect_grouping:
        if not isinstance(aspect_grouping, (list, tuple)):
            aspect_grouping = [aspect_grouping]
        aspect_ratios = _compute_aspect_ratios(dataset)
        group_ids = _quantize(aspect_ratios, aspect_grouping)
        batch_sampler = samplers.GroupedBatchSampler(
            sampler, group_ids, images_per_batch, drop_uneven=False
        )
    else:
        batch_sampler = torch.utils.data.sampler.BatchSampler(
            sampler, images_per_batch, drop_last=False
        )
    if num_iters is not None:
        batch_sampler = samplers.IterationBasedBatchSampler(
            batch_sampler, num_iters, start_iter, random_number_generator,
        )
    return batch_sampler


def make_data_loader(
        cfg, is_train=True, is_distributed=False, start_iter=0, random_number_generator=None, 
        seed=41, shapes=None, hybrid_dataloader=None, H_split=True,
        comm=None, master_rank=0
        ):
    dedicated_evaluation_ranks, num_training_ranks, images_per_batch_train, images_per_gpu_train, rank_train, rank_in_group_train, spatial_group_size_train, num_evaluation_ranks, images_per_batch_test, images_per_gpu_test, rank_test, rank_in_group_test, spatial_group_size_test = per_gpu_batch_size(cfg)

    if is_train:
        images_per_batch = images_per_batch_train
        assert (
            (images_per_batch * spatial_group_size_train) % num_training_ranks == 0
        ), "SOLVER.IMS_PER_BATCH ({}) must be divisible by the number "
        "of GPUs ({}) used.".format(images_per_batch, num_training_ranks)
        images_per_gpu = images_per_gpu_train
        shuffle = True
        num_iters = cfg.SOLVER.MAX_ITER
        rank = rank_train
        num_ranks = num_training_ranks
        spatial_group_size = spatial_group_size_train
    else:
        images_per_batch = images_per_batch_test
        assert (
            (images_per_batch * spatial_group_size_test) % num_evaluation_ranks == 0
        ), "TEST.IMS_PER_BATCH ({}) must be divisible by the number "
        "of GPUs ({}) used.".format(images_per_batch, num_evaluation_ranks)
        images_per_gpu = images_per_gpu_test
        shuffle = True if is_distributed else False
        num_iters = None
        start_iter = 0
        rank = rank_test
        num_ranks = num_evaluation_ranks
        spatial_group_size = spatial_group_size_test
    #shuffle = False # override for repeatability

    if images_per_gpu > 1:
        logger = logging.getLogger(__name__)
        logger.warning(
            "When using more than one image per GPU you may encounter "
            "an out-of-memory (OOM) error if your GPU does not have "
            "sufficient memory. If this happens, you can reduce "
            "SOLVER.IMS_PER_BATCH (for training) or "
            "TEST.IMS_PER_BATCH (for inference). For training, you must "
            "also adjust the learning rate and schedule length according "
            "to the linear scaling rule. See for example: "
            "https://github.com/facebookresearch/Detectron/blob/master/configs/getting_started/tutorial_1gpu_e2e_faster_rcnn_R-50-FPN.yaml#L14"
        )
    if is_train:
        assert(rank >= 0), "Evaluation rank initializing training data loader"
    else:
        assert(rank >= 0), "Training rank initializing evaluation data loader"

    # group images which have similar aspect ratio. In this case, we only
    # group in two cases: those with width / height > 1, and the other way around,
    # but the code supports more general grouping strategy
    aspect_grouping = [1] if cfg.DATALOADER.ASPECT_RATIO_GROUPING else []

    paths_catalog = import_file(
        "maskrcnn_benchmark.config.paths_catalog", cfg.PATHS_CATALOG, True
    )
    DatasetCatalog = paths_catalog.DatasetCatalog
    dataset_list = cfg.DATASETS.TRAIN if is_train else cfg.DATASETS.TEST

    is_hybrid_loader = cfg.DATALOADER.HYBRID

    is_fp16 = (cfg.DTYPE == "float16")
    transforms, transforms_properties = build_transforms(cfg, is_train, is_fp16, is_hybrid_loader)
    # NB! DO NOT PASS transforms to dataset if using hybrid loader.
    # Transforms will cache some small GPU side tensors in this case, which makes multiprocessing evaluation blow up.
    datasets, epoch_size = build_dataset(dataset_list, None if is_hybrid_loader else transforms, DatasetCatalog, is_train, cfg.DATALOADER.GLOBAL_TRANSFORMS, transforms_properties, comm, master_rank)

    data_loaders = []
    for dataset in datasets:
        sampler = make_data_sampler(dataset, shuffle, is_distributed, rank, num_ranks)
        batch_sampler = make_batch_data_sampler(
            dataset, sampler, aspect_grouping, images_per_gpu, num_iters, start_iter, random_number_generator,
        )
        if is_hybrid_loader:
            data_loader = HybridDataLoader3(cfg, images_per_gpu, cfg.DATALOADER.SIZE_DIVISIBILITY, shapes, spatial_group_size, rank_in_group_train, H_split) if hybrid_dataloader is None else hybrid_dataloader
            data_loader.load_dataset(cfg, batch_sampler, dataset, transforms)
        elif cfg.DATALOADER.DALI:
            data_loader = COCODALIDataloader(cfg, is_train, torch.cuda.current_device(), images_per_gpu, seed, batch_sampler, dataset, is_fp16, shapes)
        else:
            collator = BatchCollator(cfg.DATALOADER.SIZE_DIVISIBILITY, shapes, False)
            num_workers = cfg.DATALOADER.NUM_WORKERS
            data_loader = torch.utils.data.DataLoader(
                dataset,
                num_workers=num_workers,
                batch_sampler=batch_sampler,
                collate_fn=collator,
                pin_memory=True,
            )
        data_loaders.append(data_loader)
    if is_train:
        # during training, a single (possibly concatenated) data_loader is returned
        assert len(data_loaders) == 1
        iterations_per_epoch = epoch_size // images_per_batch + 1
        return data_loaders[0], iterations_per_epoch
    return data_loaders

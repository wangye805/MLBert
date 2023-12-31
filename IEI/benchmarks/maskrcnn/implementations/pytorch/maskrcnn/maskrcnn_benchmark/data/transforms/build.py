# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
from . import transforms as T

class TransformProperties:
    def __init__(self, min_size, max_size, flip_prob):
        self.min_size = min_size
        self.max_size = max_size
        self.flip_prob = flip_prob

def build_transforms(cfg, is_train=True, is_fp16=True, is_hybrid=False):
    if is_train:
        min_size = cfg.INPUT.MIN_SIZE_TRAIN
        max_size = cfg.INPUT.MAX_SIZE_TRAIN
        flip_prob = 0.5  # cfg.INPUT.FLIP_PROB_TRAIN
    else:
        min_size = cfg.INPUT.MIN_SIZE_TEST
        max_size = cfg.INPUT.MAX_SIZE_TEST
        flip_prob = 0

    to_bgr255 = cfg.INPUT.TO_BGR255
    normalize_transform = T.Normalize(
        mean=cfg.INPUT.PIXEL_MEAN, std=cfg.INPUT.PIXEL_STD, to_bgr255=to_bgr255
    )

    if is_hybrid:
        if is_train:
            if cfg.DATALOADER.GLOBAL_TRANSFORMS:
                ops = [
                          T.ToHalf(),
                          normalize_transform
                      ]
            else:
                ops = [
                          T.Resize(min_size, max_size),
                          T.RandomHorizontalFlip(flip_prob),
                          T.ToHalf(),
                          normalize_transform
                      ]
        else:
            if flip_prob > 0.0:
                ops = [
                          T.Resize(min_size, max_size),
                          T.RandomHorizontalFlip(flip_prob),
                          T.ToHalf(),
                          normalize_transform
                      ]
            else:
                ops = [
                          T.Resize(min_size, max_size),
                          T.ToHalf(),
                          normalize_transform
                      ]
    else:
        if flip_prob > 0.0:
            ops = [
                      T.Resize(min_size, max_size),
                      T.RandomHorizontalFlip(flip_prob),
                      T.ToTensor(),
                      normalize_transform
                  ]
        else:
            ops = [
                      T.Resize(min_size, max_size),
                      T.ToTensor(),
                      normalize_transform
                  ]
        if is_fp16:
            ops.append(T.ToHalf())
    transform = T.Compose(ops)

    return transform, TransformProperties(min_size, max_size, flip_prob)

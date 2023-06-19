#!/bin/bash

## DL params
#export BATCHSIZE=${BATCHSIZE:-32}
export BATCHSIZE=${BATCHSIZE:-64}
export NUMEPOCHS=${NUMEPOCHS:-8}
#export LR=${LR:-0.000085}
export LR=${LR:-0.0001}
export WARMUP_EPOCHS=${WARMUP_EPOCHS:-0}
export EXTRA_PARAMS=${EXTRA_PARAMS:-'--jit --frozen-bn-opt --frozen-bn-fp16 --apex-adam --apex-focal-loss --apex-backbone-fusion --apex-head-fusion --disable-ddp-broadcast-buffers --fp16-allreduce --reg-head-pad --cls-head-pad --cuda-graphs --dali --dali-matched-idxs --dali-eval --skip-metric-loss --cuda-graphs-syn --async-coco --dali-cpu-decode'}

## System run params
export DGXNNODES=1
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
WALLTIME_MINUTES=160
export WALLTIME=$((${NEXP:-1} * ${WALLTIME_MINUTES}))

## System config params
export DGXNGPU=4
export DGXSOCKETCORES=32
export DGXNSOCKET=2
export DGXHT=1  # HT is on is 2, HT off is 1

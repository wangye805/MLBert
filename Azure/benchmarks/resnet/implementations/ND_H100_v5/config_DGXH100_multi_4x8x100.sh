source $(dirname ${BASH_SOURCE[0]})/config_DGXA100_common.sh

## DL params -- 4k
export OPTIMIZER="sgdwfastlars"
export BATCHSIZE="100"
export KVSTORE="horovod"
export LR="10.7"
export WARMUP_EPOCHS="5"
export EVAL_OFFSET="2" # Targeting epoch no. 35
export EVAL_PERIOD="4"
export WD="5.0e-05"
export MOM="0.9"
export LARSETA="0.001"
export LABELSMOOTHING="0.1"
export LRSCHED="pow2"
export NUMEPOCHS=${NUMEPOCHS:-"37"}

export NETWORK="resnet-v1b-fl"
# export NETWORK="resnet-v1b-stats-fl"

export DALI_THREADS=6
export DALI_HW_DECODER_LOAD="0.99"
export DALI_PREFETCH_QUEUE="3"
export DALI_NVJPEG_MEMPADDING="256"
export DALI_CACHE_SIZE="42288"
#DALI buffer presizing hints
export DALI_PREALLOCATE_WIDTH="5980"
export DALI_PREALLOCATE_HEIGHT="6430"
export DALI_DECODER_BUFFER_HINT="1315942" #1196311*1.1
export DALI_CROP_BUFFER_HINT="165581" #150528*1.1
export DALI_TMP_BUFFER_HINT="355568328" #871491*batch_size
#export DALI_TMP_BUFFER_HINT="88892082"
export DALI_NORMALIZE_BUFFER_HINT="441549" #401408*1.1

# Default is no NCCL and BWD overlap
export HOROVOD_CYCLE_TIME=0.1
export HOROVOD_FUSION_THRESHOLD=67108864
export HOROVOD_NUM_NCCL_STREAMS=2
export MXNET_HOROVOD_NUM_GROUPS=1
export MXNET_EXEC_BULK_EXEC_MAX_NODE_TRAIN_FWD=999
export MXNET_EXEC_BULK_EXEC_MAX_NODE_TRAIN_BWD=999
export DALI_DONT_USE_MMAP=1
# export MXNET_EXTENDED_NORMCONV_SUPPORT=1

## System run parms
export DGXNNODES=4
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
export WALLTIME=$(( ${NEXP:-1} * 12 + 10 ))

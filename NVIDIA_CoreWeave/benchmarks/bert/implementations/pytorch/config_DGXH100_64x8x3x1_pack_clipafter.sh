## DL params
export BATCHSIZE=3
export GRADIENT_STEPS=1
export PACKING_FACTOR=2
export LR=0.002
export MAX_SAMPLES_TERMINATION=4500000
export MAX_STEPS=1024
export OPT_LAMB_BETA_1=0.62
export OPT_LAMB_BETA_2=0.90
export START_WARMUP_STEP=-9500
export WARMUP_STEPS=10000
export WEIGHT_DECAY_RATE=0.01
export INIT_LOSS_SCALE=1024.0

export SBATCH_NETWORK=sharp
export EXTRA_PARAMS="--use_cuda_graph --pad_fmha --cuda_graph_mode 'full_iteration' --max_iterations_per_graph 1 --fused_bias_fc --fused_bias_mha --fused_dropout_add --fused_bias_fc_loss_head --packed_samples --dwu-num-blocks=5 --dwu-overlap-reductions "
export PHASE=2
export EVAL_ITER_START_SAMPLES=175000
export EVAL_ITER_SAMPLES=175000

## System run parms
export DGXNNODES=64
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )

export WALLTIME_MINUTES=5
export WALLTIME=$(( ${NEXP:-1} * ${WALLTIME_MINUTES} + 5 ))

## System config params
source $(dirname ${BASH_SOURCE[0]})/config_DGXH100_common.sh

export CONTAINER_PRELOAD_LUSTRE=1

## force to use packed trainset
export DATADIR_PHASE2=${DATADIR_PHASE2_PACKED}


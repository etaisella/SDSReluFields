#!/bin/bash
echo "Starting Run!"

# Reading arguments:
gpu_num=0
while getopts g: flag
do
    case "${flag}" in
        g) gpu_num=${OPTARG};;
    esac
done

# Setting GPU:
echo "Running on GPU: $gpu_num";
export CUDA_VISIBLE_DEVICES=$gpu_num

# Rendering function template:
train_and_render() {
	# Train:
	echo "Starting Training..."
	python run_sds_on_high_res_model.py \
	-d ../data/${1}/ \
	-o logs/rf/${1}_sds_dir_${3}_dcl_${5}_${4}_lrs_${6}_${7}_${8}/ \
	-i logs/rf/${1}_ref_shdeg_0/saved_models/model_final.pth \
	-p "$2" \
	--directional_dataset=${3} \
	--density_correlation_weight=${5} \
	--lr_decay_start=${6} \
	--lr_gamma=${7} \
	--lr_freq=${8} \
	--sh_degree=0 # we currently only support diffuse

	# Rendering Output Video:
	echo "Starting Rendering..."
	python render_sh_based_voxel_grid.py \
	-i logs/rf/${1}_sds_dir_${3}_dcl_${5}_${4}_lrs_${6}_${7}_${8}/saved_models/model_final.pth \
	-o output_renders/${1}_sds_dir_${3}_dcl_${5}_${4}_lrs_${6}_${7}_${8}
}

# STARTING RUN:

scene=dog2
prompt="a cute light grey dog wearing big sunglasses"
directional=True
log_name="bigglasses" # 1-word description of the prompt for saving
dcl_weight=1500.0
lr_decay_start=3000
lr_gamma=0.8
lr_freq=400

train_and_render $scene "$prompt" $directional $log_name $dcl_weight $lr_decay_start $lr_gamma $lr_freq
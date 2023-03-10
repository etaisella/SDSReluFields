#!/bin/bash
echo "Starting Run!"

# Reading arguments:
gpu_num=0
while getopts g:d: flag
do
    case "${flag}" in
        g) gpu_num=${OPTARG};;
		d) sweep_name_in=${OPTARG};;
    esac
done

# Setting GPU:
echo "Running on GPU: $gpu_num";
export CUDA_VISIBLE_DEVICES=$gpu_num

# Rendering function template:
train_and_render() {
	# Rendering Output Video:
	#echo "Starting Rendering..."
	#python render_sh_based_voxel_grid.py \
	#-i /home/etaisella/voxelArt/SDSReluFields/logs/rf/sweep_160/dog2/ref/saved_models/model_final.pth \
	#--ref_path=/home/etaisella/voxelArt/SDSReluFields/logs/rf/sweep_160/dog2/ref/saved_models/model_final.pth \
	#-o output_renders/supp_results/dog2/ref/ \
	#--sds_prompt="$2" \
	#--save_freq=1

	python render_sh_based_voxel_grid.py \
	-i /home/etaisella/voxelArt/SDSReluFields/logs/rf/gingercat/ref/saved_models/gingercat_ref.pth \
	--ref_path=/home/etaisella/voxelArt/SDSReluFields/logs/rf/sweep_160_tv/gingercat/ref/saved_models/model_final.pth \
	-o output_renders/supp_results/gingercat/ref/ \
	--sds_prompt="$2" \
	--save_freq=1

	#python render_sh_based_voxel_grid.py \
	#-i /home/etaisella/voxelArt/SDSReluFields/logs/rf/sweep_160/kangaroo/ref/saved_models/model_final.pth \
	#--ref_path=/home/etaisella/voxelArt/SDSReluFields/logs/rf/sweep_160/kangaroo/ref/saved_models/model_final.pth \
	#-o output_renders/supp_results/kangaroo/ref/ \
	#--sds_prompt="$2" \
	#--save_freq=1
}

# STARTING RUN:

scene=gingercat
prompt="a render of a dog with a party hat"
directional=True
log_name="ref" # 1-word description of the prompt for saving
dcl_weight=200.0
sds_t_decay_start=4000
sds_t_gamma=0.75
sds_t_freq=500
sweep_name=$sweep_name_in

train_and_render $scene "$prompt" $directional $log_name $dcl_weight $sds_t_decay_start \
$sds_t_gamma $sds_t_freq $sweep_name
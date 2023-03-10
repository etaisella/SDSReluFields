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
train_default() {
	# Train:
	python edit_pretrained_relu_field.py \
	-d ../data/${1}/ \
	-o logs/rf/${2}/${1}/${4} \
	-i logs/rf/${2}/${1}/ref/saved_models/model_final.pth \
	-p "$3" \
	-eidx=${5} \
	--num_iterations_edit=8000 \
	--directional_dataset=True \
	--density_correlation_weight=80 \
	--tv_density_weight=50.0 \
	--tv_features_weight=100.0 \
	--learning_rate=0.025 \
	--do_refinement=True \
	--sh_degree=0 # we currently only support diffuse

	# Rendering Output Video:
	echo "Starting Rendering..."
	python render_sh_based_voxel_grid_attn.py \
	-i logs/rf/${2}/${1}/${4}/saved_models/model_final_refined.pth \
	-o output_renders/${2}/${1}/${4}/ \
	--ref_path=logs/rf/${2}/${1}/ref/saved_models/model_final.pth \
	--sds_prompt="$3" \
	--save_freq=10
}

# STARTING RUN:

# christmas sweater

#sweep_name=sweep_full_local
#scene=gingercat
#prompt="a render of a cat wearing a christmas sweater"
#log_name="christmas"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=dog2
#prompt="a render of a dog wearing a christmas sweater"
#log_name="christmas"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=kangaroo
#prompt="a render of a kangaroo wearing a christmas sweater"
#log_name="christmas"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx

# sunglasses

#sweep_name=sweep_full_local
#scene=alien
#prompt="a render of an alien wearing a tuxedo"
#log_name="tuxedo"
#eidx=8
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=alien
#prompt="a render of an alien wearing huge sunglasses"
#log_name="sunglasses"
#eidx=8
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=alien
#prompt="a render of an alien wearing a birthday hat"
#log_name="birthday"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=alien
#prompt="a render of an alien riding a hoverboard"
#log_name="hoverboard"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx#
#
#sweep_name=sweep_full_local#
#scene=gingercat
#prompt="a render of a cat wearing big sunglasses"
##log_name="sunglasses"
##eidx=8
##
##train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=kangaroo
#prompt="a render of a kangaroo wearing huge sunglasses"
#log_name="sunglasses"
#eidx=8
#oidx=5
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx $oidx
#
#sweep_name=sweep_full_local
#scene=dog2
#prompt="a render of a dog wearing huge sunglasses"
#log_name="sunglasses"
#eidx=8
#oidx=5
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx $oidx

# birthday hat

#sweep_name=sweep_full_local
#scene=gingercat
#prompt="a render of a cat wearing a birthday hat"
#log_name="birthday"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=dog2
#prompt="a render of a dog wearing a birthday hat"
#log_name="birthday"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=kangaroo
#prompt="a render of a kangaroo wearing a birthday hat"
#log_name="birthday"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=frog2
#prompt="a render of a frog wearing a birthday hat"
#log_name="birthday"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=frog2
#prompt="a render of a frog wearing a big sunglasses"
#log_name="sunglasses"
#eidx=8
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx
#
#sweep_name=sweep_full_local
#scene=frog2
#prompt="a render of a frog wearing a christmas sweater"
#log_name="christmas"
#eidx=9
#
#train_default $scene $sweep_name "$prompt" $log_name $eidx

sweep_name=sweep_full_local
scene=alien
prompt="a render of an alien in a scuba diving suit"
log_name="scuba"
eidx=10
oidx=5

train_default $scene $sweep_name "$prompt" $log_name $eidx $oidx

sweep_name=sweep_full_local
scene=dog2
prompt="a render of a dog in a scuba diving suit"
log_name="scuba"
eidx=10
oidx=5

train_default $scene $sweep_name "$prompt" $log_name $eidx $oidx

sweep_name=sweep_full_local
scene=gingercat
prompt="a render of a cat in a scuba diving suit"
log_name="scuba"
eidx=10
oidx=5

train_default $scene $sweep_name "$prompt" $log_name $eidx $oidx
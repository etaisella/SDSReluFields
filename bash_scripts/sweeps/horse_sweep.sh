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
	python run_sds_on_high_res_model.py \
	-d ../data/${1}/ \
	-o logs/rf/${2}/${1}/${4} \
	-i logs/rf/${2}/${1}/ref/saved_models/model_final.pth \
	-p "$3" \
	--directional_dataset=True \
	--density_correlation_weight=200 \
	--sds_t_start=4000 \
	--sds_t_gamma=0.75 \
	--sds_t_freq=800 \
	--num_iterations_per_stage=8000 \
	--sh_degree=0 # we currently only support diffuse

	# Rendering Output Video:
	echo "Starting Rendering..."
	python render_sh_based_voxel_grid.py \
	-i logs/rf/${2}/${1}/${4}/saved_models/model_final.pth \
	-o output_renders/${2}/${1}/${4}/ \
	--sds_prompt="$3" \
	--save_freq=10
}

# STARTING RUN:

sweep_name=horse_sweep
scene=horse
prompt="a render of a horse in an astronaut suit"
log_name="astronaut2"

train_default $scene $sweep_name "$prompt" $log_name

sweep_name=horse_sweep
scene=horse
prompt="a render of a brown minecraft horse"
log_name="brown_minecraft"

train_default $scene $sweep_name "$prompt" $log_name

sweep_name=horse_sweep
scene=horse
prompt="a render of a horse wearing a magician's hat"
log_name="magicianhat"

train_default $scene $sweep_name "$prompt" $log_name

#sweep_name=horse_sweep
#scene=horse
#prompt="a render of an astronaut horse"
#log_name="astronaut"
#
#train_default $scene $sweep_name "$prompt" $log_name
#
#sweep_name=horse_sweep
#scene=horse
#prompt="a render of wood carving of a horse"
#log_name="wood"
#
#train_default $scene $sweep_name "$prompt" $log_name
#
#sweep_name=horse_sweep
#scene=horse
#prompt="a render of a horse wearing a santa hat"
#log_name="santa"
#
#train_default $scene $sweep_name "$prompt" $log_name
#
#sweep_name=horse_sweep
#scene=horse
#prompt="a render of a horse on rollerskates"
#log_name="rollerskates"
#
#train_default $scene $sweep_name "$prompt" $log_name
#
#sweep_name=horse_sweep
#scene=horse
#prompt="a render of a balloon sculpture of a horse"
#log_name="balloon"
#
#train_default $scene $sweep_name "$prompt" $log_name
#
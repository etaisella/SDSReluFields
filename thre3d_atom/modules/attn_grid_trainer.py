import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional
from PIL import Image

import imageio
import torch
import numpy as np
from torch import Tensor
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import wandb

from thre3d_atom.data.datasets import PosedImagesDataset
from thre3d_atom.data.utils import infinite_dataloader
from thre3d_atom.modules.volumetric_model import VolumetricModel
from thre3d_atom.rendering.volumetric.utils.misc import (
    cast_rays,
    collate_rays_unflattened,
    sample_rays_and_pixels_synchronously,
    flatten_rays,
)

from thre3d_atom.thre3d_reprs.renderers import render_sh_voxel_grid_attn
from thre3d_atom.thre3d_reprs.voxels import (
    VoxelGrid,
    scale_voxel_grid_with_required_output_size_attn,
)
from thre3d_atom.thre3d_reprs.sd import StableDiffusion
from thre3d_atom.utils.constants import (
    CAMERA_BOUNDS,
    CAMERA_INTRINSICS,
    HEMISPHERICAL_RADIUS,
)
from thre3d_atom.utils.imaging_utils import CameraPose, to8b

# All the TrainProcedures below follow this function-type
from thre3d_atom.utils.logging import log
from thre3d_atom.utils.misc import compute_thre3d_grid_sizes
from thre3d_atom.visualizations.static import (
    visualize_sh_vox_grid_vol_mod_rendered_feedback,
    visualize_sh_vox_grid_vol_mod_rendered_feedback_attn,
)

from thre3d_atom.modules.refinement_functions import (
    visualize_and_log_attention_maps, 
    calc_loss_on_attn_grid,
    get_edit_region,
    log_and_vis_render_diff
)

dir_to_num_dict = {'side': 0, 'overhead': 1, 'back': 2, 'front': 3}
mse_loss = torch.nn.MSELoss(reduction='none')

# TrainProcedure = Callable[[VolumetricModel, Dataset, ...], VolumetricModel]


def refine_edited_relu_field(
        vol_mod_edit: VolumetricModel,
        vol_mod_object: VolumetricModel,
        vol_mod_output: VolumetricModel,
        vol_mod_ref: VolumetricModel,
        train_dataset: PosedImagesDataset,
        # required arguments:
        output_dir: Path,
        prompt: str,
        edit_idx: int,
        timestamp: int,
        object_idx: int = None,
        image_batch_cache_size: int = 8,
        ray_batch_size: int = 32768,
        num_stages: int = 1,
        num_iterations_per_stage: int = 2000,
        scale_factor: float = 2.0,
        # learning_rate and related arguments
        learning_rate: float = 0.03,
        lr_decay_gamma_per_stage: float = 0.1,
        lr_decay_steps_per_stage: int = 1000,
        stagewise_lr_decay_gamma: float = 0.9,
        # option to have a specific feedback_pose_for_visual feedback rendering
        render_feedback_pose: Optional[CameraPose] = None,
        # various training-loop frequencies
        save_freq: int = 1000,
        feedback_freq: int = 100,
        summary_freq: int = 10,
        # regularization option:
        apply_diffuse_render_regularization: bool = False,
        # miscellaneous options can be left untouched
        num_workers: int = 4,
        verbose_rendering: bool = True,
        directional_dataset: bool = False,
        attn_tv_weight: float = 0.001,
        kval: float = 5.0
) -> VolumetricModel:
    """
    ------------------------------------------------------------------------------------------------------
    |                               !!! :D LONG FUNCTION ALERT :D !!!                                    |
    ------------------------------------------------------------------------------------------------------
    trains a volumetric model given a dataset of images and corresponding poses
    Args:
        vol_mod: the volumetricModel to be trained with this procedure. Please note that it should have
                 an sh-based VoxelGrid as its underlying thre3d_repr.
        train_dataset: PosedImagesDataset used for training
        output_dir: path to the output directory where the assets of the training are to be written
        random_initializer: the pytorch initialization routine used for features of voxel_grid
        test_dataset: optional dataset of test images and poses :)
        image_batch_cache_size: batch of images from which rays are sampled per training iteration
        ray_batch_size: number of randomly sampled rays used per training iteration
        num_stages: number of stages in the training routine
        num_iterations_per_stage: iterations performed per stage
        scale_factor: factor by which the grid is up-scaled after each stage
        learning_rate: learning rate used for differential optimization
        lr_decay_gamma_per_stage: value of gamma for learning rate-decay in a single stage
        lr_decay_steps_per_stage: steps after which exponential learning rate decay is kicked in
        stagewise_lr_decay_gamma: gamma reduction of learning rate after each stage
        render_feedback_pose: optional feedback pose used for generating the rendered feedback
        save_freq: number of iterations after which checkpoints are saved
        test_freq: number of iterations after which testing scores are computed
        feedback_freq: number of iterations after which feedback is generated
        summary_freq: number of iterations after which current loss is logged to console
        apply_diffuse_render_regularization: whether to apply the diffuse render regularization
        num_workers: num_workers used by pytorch dataloader
        verbose_rendering: bool to control whether to show verbose details while generating rendered feedback
        fast_debug_mode: bool to control fast_debug_mode, skips testing and some other things
        diffuse_weight: weight for diffuse loss - used for regularization
        spcular_weight: weight for specular loss - used for regularization

    Returns: the trained version of the VolumetricModel. Also writes multiple assets to disk
    """
    # assertions about the VolumetricModel being used with this TrainProcedure :)
    assert isinstance(vol_mod_edit.thre3d_repr, VoxelGrid), (
        f"sorry, cannot use a {type(vol_mod_edit.thre3d_repr)} with this TrainProcedure :(; "
        f"only a {type(VoxelGrid)} can be used"
    )
    assert (
            vol_mod_edit._render_procedure_attn == render_sh_voxel_grid_attn
    ), f"sorry, non SH-based VoxelGrids cannot be used with this TrainProcedure"

    assert (
            prompt != "none"
    ), f"sorry, you have to supply a text prompt to use SDS"

    # init sds loss class
    sd_model = StableDiffusion(vol_mod_edit.device, "1.4")
    direction_batch = None
    selected_idx_in_batch = [0]

    # fix the sizes of the feature grids at different stages
    stagewise_voxel_grid_sizes = compute_thre3d_grid_sizes(
        final_required_resolution=vol_mod_edit.thre3d_repr.grid_dims,
        num_stages=num_stages,
        scale_factor=scale_factor,
    )

    # create downsampled versions of the train_dataset for lower training stages
    stagewise_train_datasets = [train_dataset]
    dataset_config_dict = train_dataset.get_config_dict()
    data_downsample_factor = dataset_config_dict["downsample_factor"]
    for stage in range(1, num_stages):
        dataset_config_dict.update(
            {"downsample_factor": data_downsample_factor * (scale_factor ** stage)}
        )
        stagewise_train_datasets.insert(0, PosedImagesDataset(**dataset_config_dict))

    # setup render_feedback_pose
    real_feedback_image = None
    if render_feedback_pose is None:
        feedback_dataset = train_dataset
        render_feedback_pose = CameraPose(
            rotation=feedback_dataset[-1][1][:, :3].cpu().numpy(),
            translation=feedback_dataset[-1][1][:, 3:].cpu().numpy(),
        )
        real_feedback_image = feedback_dataset[-1][0].permute(1, 2, 0).cpu().numpy()

    train_dl = _make_dataloader_from_dataset(
        train_dataset, image_batch_cache_size, num_workers
    )

    # dataset size aka number of total pixels
    dataset_size = (
            len(train_dl)
            * train_dataset.camera_intrinsics.height
            * train_dataset.camera_intrinsics.width
    )

    # setup output directories
    # fmt: off
    model_dir = output_dir / "saved_models"
    logs_dir = output_dir / "training_logs"
    tensorboard_dir = logs_dir / "tensorboard"
    render_dir = logs_dir / "rendered_output"
    for directory in (model_dir, logs_dir, tensorboard_dir,
                      render_dir):
        directory.mkdir(exist_ok=True, parents=True)
    # fmt: on

    # save the real_feedback_test_image if it exists:
    if real_feedback_image is not None:
        log.info(f"Logging real feedback image")
        imageio.imwrite(
            render_dir / f"1__real_log.png",
            to8b(real_feedback_image),
        )

    # extract the camera_bounds and camera_intrinsics for rest of the procedure
    camera_bounds, camera_intrinsics = (
        train_dataset.camera_bounds,
        train_dataset.camera_intrinsics,
    )

    # setup tensorboard writer
    tensorboard_writer = SummaryWriter(str(tensorboard_dir))

    # start actual training
    log.info("beginning training")
    time_spent_actually_training = 0

    # -----------------------------------------------------------------------------------------
    #  Main Training Loop                                                                     |
    # -----------------------------------------------------------------------------------------
    for stage in range(1, num_stages + 1):
        # setup the dataset for the current training stage
        # followed by creating an infinite training data-loader
        current_stage_train_dataset = stagewise_train_datasets[stage - 1]
        train_dl = _make_dataloader_from_dataset(
            current_stage_train_dataset, image_batch_cache_size, num_workers
        )
        infinite_train_dl = iter(infinite_dataloader(train_dl))

        # setup volumetric_model's optimizer
        current_stage_lr = learning_rate * (stagewise_lr_decay_gamma ** (stage - 1))

        # set optimizer edit
        params_edit = [{"params": vol_mod_edit.thre3d_repr.attn, "lr": current_stage_lr}]

        optimizer_edit = torch.optim.Adam(
            params=params_edit,
            betas=(0.9, 0.999),
        )

        # setup learning rate schedulers for the optimizer
        lr_scheduler_edit = torch.optim.lr_scheduler.ExponentialLR(
            optimizer_edit, gamma=lr_decay_gamma_per_stage
        )
        
        # set optimizer object
        params_object = [{"params": vol_mod_object.thre3d_repr.attn, "lr": current_stage_lr}]

        optimizer_object = torch.optim.Adam(
            params=params_object,
            betas=(0.9, 0.999),
        )

        # display logs related to this training stage:
        train_image_height, train_image_width = (
            current_stage_train_dataset.camera_intrinsics.height,
            current_stage_train_dataset.camera_intrinsics.width,
        )
        log.info(
            f"training stage: {stage}   "
            f"voxel grid resolution: {vol_mod_edit.thre3d_repr.grid_dims} "
            f"training images resolution: [{train_image_height} x {train_image_width}]"
        )
        current_stage_lrs = [
            param_group["lr"] for param_group in optimizer_edit.param_groups
        ]
        log_string = f"current stage learning rates: {current_stage_lrs} "
        log.info(log_string)
        last_time = time.perf_counter()
        # -------------------------------------------------------------------------------------
        #  Single Stage Training Loop                                                         |
        # -------------------------------------------------------------------------------------
        for stage_iteration in range(1, num_iterations_per_stage + 1):
            # ---------------------------------------------------------------------------------
            #  Main Operations Performed Per Iteration                                        |
            # ---------------------------------------------------------------------------------
            # sample a batch rays and pixels for a single iteration
            # load a batch of images and poses (These could already be cached on GPU)
            # please check the `data.datasets` module

            total_loss_edit = 0
            total_loss_object = 0

            global_step = ((stage - 1) * num_iterations_per_stage) + stage_iteration
            images, poses, indices = next(infinite_train_dl)
            # cast rays for all the loaded images:
            rays_list = []
            unflattened_rays_list = []
            for pose in poses:
                unflattened_rays = cast_rays(
                    current_stage_train_dataset.camera_intrinsics,
                    CameraPose(rotation=pose[:, :3], translation=pose[:, 3:]),
                    device=vol_mod_edit.device,
                )
                casted_rays = flatten_rays(unflattened_rays)
                rays_list.append(casted_rays)
                unflattened_rays_list.append(unflattened_rays)
            unflattened_rays = collate_rays_unflattened(unflattened_rays_list)
            # images are of shape [B x C x H x W] and pixels are [B * H * W x C]
            _, _, im_h, im_w = images.shape
            # sample a subset of rays and pixels synchronously
            batch_size_in_images = int(ray_batch_size / (im_h * im_w))
            rays_batch, pixels_batch, index_batch, selected_idx_in_batch = sample_rays_and_pixels_synchronously(
                unflattened_rays, images, indices, batch_size_in_images
            )
            pose = poses[selected_idx_in_batch]
            pose = CameraPose(
                rotation=pose[-1][:, :3].cpu().numpy(),
                translation=pose[-1][:, 3:].cpu().numpy(),
            )
            rendered_output = vol_mod_edit.render(
                pose,
                camera_intrinsics,
                gpu_render=False,
                verbose=False,
            )

            # log inputs
            if directional_dataset:
                direction_batch = get_dir_batch_from_poses(poses[selected_idx_in_batch])
                wandb.log({"Input Direction": dir_to_num_dict[direction_batch[0]]}  , step=global_step)

            # Get attention Maps
            out_imgs = rendered_output.colour.unsqueeze(0)
            out_imgs = out_imgs.permute((0, 3, 1, 2)).to(vol_mod_edit.device)
            m_prompt = prompt + f", {direction_batch[0]} view"
            wandb.log({"Input Image": wandb.Image(rendered_output.colour.numpy())}, step=global_step)

            # if no object idx is given (default) take the maximum between all non-edit tokens
            if object_idx == None:
                indices_to_fetch = list(range(1, edit_idx + 1))
            else:
                indices_to_fetch = [edit_idx, object_idx]
                
            gt, t = sd_model.get_attn_map(prompt=m_prompt, pred_rgb=out_imgs, timestamp=timestamp,
                                          indices_to_fetch=indices_to_fetch)
            visualize_and_log_attention_maps(gt, global_step)

            if object_idx == None:
                edit_attn_map = gt.pop(edit_idx - 1)
                rest_of_attn_maps = [t.unsqueeze(dim=-1) for t in gt]
                object_attn_map = torch.cat(rest_of_attn_maps, dim=-1)
                object_attn_map, _ = torch.max(object_attn_map, dim=-1)
                object_attn_map = object_attn_map.squeeze()
            else:
                edit_attn_map = gt[0]
                object_attn_map = gt[1]

            # render a small chunk of rays and compute a loss on it
            edit_attn_rendered_batch = vol_mod_edit.render_rays_attn(rays_batch)
            edit_attn_rendered_batch = edit_attn_rendered_batch.attn

            object_attn_rendered_batch = vol_mod_object.render_rays_attn(rays_batch)
            object_attn_rendered_batch = object_attn_rendered_batch.attn

            # calc losses
            edit_attn_loss = calc_loss_on_attn_grid(attn_render=edit_attn_rendered_batch, 
                                                    attn_map=edit_attn_map, 
                                                    token="edit", 
                                                    global_step=global_step)
            
            object_attn_loss = calc_loss_on_attn_grid(attn_render=object_attn_rendered_batch, 
                                                      attn_map=object_attn_map, 
                                                      token="object", 
                                                      global_step=global_step)
            
            edit_attn_render = edit_attn_rendered_batch.reshape(edit_attn_map.shape)
            object_attn_render = object_attn_rendered_batch.reshape(edit_attn_map.shape)
            log_and_vis_render_diff(edit_attn_render, object_attn_render, global_step)
            

            total_loss_edit = total_loss_edit + edit_attn_loss
            tv_loss_edit =_tv_loss_on_grid(vol_mod_edit.thre3d_repr.attn)
            total_loss_edit = total_loss_edit + tv_loss_edit * attn_tv_weight

            total_loss_object = total_loss_object + object_attn_loss
            tv_loss_object =_tv_loss_on_grid(vol_mod_object.thre3d_repr.attn)
            total_loss_object = total_loss_object + tv_loss_object * attn_tv_weight

            # optimization steps:
            total_loss_edit.backward()
            optimizer_edit.step()
            optimizer_edit.zero_grad()

            total_loss_object.backward()
            optimizer_object.step()
            optimizer_object.zero_grad()

            # wandb logging:
            wandb.log({"attn_loss_edit": edit_attn_loss}, step=global_step)
            wandb.log({"tv_loss_edit": tv_loss_edit}, step=global_step)
            wandb.log({"total_loss_edit": total_loss_edit}, step=global_step)

            wandb.log({"attn_loss_object": object_attn_loss}, step=global_step)
            wandb.log({"tv_loss_object": tv_loss_object}, step=global_step)
            wandb.log({"total_loss_object": total_loss_object}, step=global_step)

            wandb.log({"first selected indx in batch": index_batch[0]}, step=global_step)

            # ---------------------------------------------------------------------------------

            # rest of the code per iteration is related to saving/logging/feedback/testing
            time_spent_actually_training += time.perf_counter() - last_time

            # tensorboard summaries feedback
            if (
                    global_step % summary_freq == 0
                    or stage_iteration == 1
                    or stage_iteration == num_iterations_per_stage
            ):
                for summary_name, summary_value in (
                        ("attn_loss", edit_attn_loss),
                        ("total_loss", total_loss_edit),
                        ("num_epochs", (ray_batch_size * global_step) / dataset_size),
                ):
                    if summary_value is not None:
                        tensorboard_writer.add_scalar(
                            summary_name, summary_value, global_step=global_step
                        )

            # console loss feedback
            if (
                    global_step % summary_freq == 0
                    or stage_iteration == 1
                    or stage_iteration == num_iterations_per_stage
            ):
                loss_info_string = (
                    f"Stage: {stage} "
                    f"Global Iteration: {global_step} "
                    f"Stage Iteration: {stage_iteration} "
                    f"attn_loss: {edit_attn_loss.item(): .3f} "
                )
                log.info(loss_info_string)

            # step the learning rate schedulers
            if stage_iteration % lr_decay_steps_per_stage == 0:
                lr_scheduler_edit.step()
                new_lrs = [param_group["lr"] for param_group in optimizer_edit.param_groups]
                log_string = f"Adjusted learning rate | learning rates: {new_lrs} "
                log.info(log_string)

            # generated rendered feedback visualizations
            if (
                    global_step % feedback_freq == 0
                    or stage_iteration == 1
                    or stage_iteration == num_iterations_per_stage
            ):
                log.info(
                    f"TIME CHECK: time spent actually training "
                    f"till now: {timedelta(seconds=time_spent_actually_training)}"
                )
                with torch.no_grad():
                    render_feedback_pose = CameraPose(
                        rotation=train_dataset[index_batch[0]][1][:, :3].cpu().numpy(),
                        translation=train_dataset[index_batch[0]][1][:, 3:].cpu().numpy(),
                    )

                    visualize_sh_vox_grid_vol_mod_rendered_feedback_attn(
                        vol_mod=vol_mod_edit,
                        vol_mod_name="attn",
                        render_feedback_pose=render_feedback_pose,
                        camera_intrinsics=camera_intrinsics,
                        global_step=global_step,
                        feedback_logs_dir=render_dir,
                        parallel_rays_chunk_size=vol_mod_edit.render_config.parallel_rays_chunk_size,
                        training_time=time_spent_actually_training,
                        log_diffuse_rendered_version=apply_diffuse_render_regularization,
                        use_optimized_sampling_mode=False,  # testing how the optimized sampling mode rendering looks 🙂
                        overridden_num_samples_per_ray=vol_mod_edit.render_config.render_num_samples_per_ray,
                        verbose_rendering=verbose_rendering,
                        log_wandb=True,
                    )

            # save the model
            if (
                    global_step % save_freq == 0
                    or stage_iteration == 1
                    or stage_iteration == num_iterations_per_stage
            ):
                log.info(
                    f"saving model-snapshot at stage {stage}, global step {global_step}"
                )
                torch.save(
                    vol_mod_edit.get_save_info(
                        extra_info={
                            CAMERA_BOUNDS: camera_bounds,
                            CAMERA_INTRINSICS: camera_intrinsics,
                            HEMISPHERICAL_RADIUS: train_dataset.get_hemispherical_radius_estimate(),
                        }
                    ),
                    model_dir / f"model_edit_stage_{stage}_iter_{global_step}.pth",
                )
                torch.save(
                    vol_mod_object.get_save_info(
                        extra_info={
                            CAMERA_BOUNDS: camera_bounds,
                            CAMERA_INTRINSICS: camera_intrinsics,
                            HEMISPHERICAL_RADIUS: train_dataset.get_hemispherical_radius_estimate(),
                        }
                    ),
                    model_dir / f"model_pbject_stage_{stage}_iter_{global_step}.pth",
                )

            # ignore all the time spent doing verbose stuff :) and update
            # the last_time clock event
            last_time = time.perf_counter()

        # -------------------------------------------------------------------------------------

        log.info(f"Starting Grid Refinement!")
        get_edit_region(vol_mod_edit=vol_mod_edit, 
                        vol_mod_object=vol_mod_object,
                        vol_mod_output=vol_mod_output,
                        rays=rays_batch,
                        img_height=im_h, 
                        img_width=im_w,
                        step=global_step,
                        K=kval)

        # change densities and features without optimization:
        regular_density = vol_mod_ref.thre3d_repr._densities.detach()
        regular_features = vol_mod_ref.thre3d_repr._features.detach()
        keep_mask = vol_mod_output.thre3d_repr.attn != 0

        new_density = vol_mod_output.thre3d_repr._densities.detach()
        new_density[keep_mask.squeeze()] = regular_density[keep_mask.squeeze()]
        vol_mod_output.thre3d_repr._densities = torch.nn.Parameter(new_density)

        new_features = vol_mod_output.thre3d_repr.features.detach()
        new_features[keep_mask.squeeze()] = regular_features[keep_mask.squeeze()]
        vol_mod_output.thre3d_repr._features = torch.nn.Parameter(new_features)

        visualize_sh_vox_grid_vol_mod_rendered_feedback(
                    vol_mod=vol_mod_output,
                    vol_mod_name="sds_refined",
                    render_feedback_pose=render_feedback_pose,
                    camera_intrinsics=camera_intrinsics,
                    global_step=0,
                    feedback_logs_dir=render_dir,
                    parallel_rays_chunk_size=vol_mod_output.render_config.parallel_rays_chunk_size,
                    training_time=time_spent_actually_training,
                    log_diffuse_rendered_version=apply_diffuse_render_regularization,
                    use_optimized_sampling_mode=False,  # testing how the optimized sampling mode rendering looks 🙂
                    overridden_num_samples_per_ray=vol_mod_output.render_config.render_num_samples_per_ray,
                    verbose_rendering=verbose_rendering,
                    log_wandb=True,
                )

        # -------------------------------------------------------------------------------------

        # don't upsample the feature grid if the last stage is complete
        if stage != num_stages:
            # upsample the feature-grid after the completion of the stage:
            with torch.no_grad():
                # noinspection PyTypeChecker
                vol_mod_edit.thre3d_repr = scale_voxel_grid_with_required_output_size_attn(
                    vol_mod_edit.thre3d_repr,
                    output_size=stagewise_voxel_grid_sizes[stage],
                    mode="trilinear",
                )
    # -----------------------------------------------------------------------------------------

    # save the final trained model
    log.info(f"Saving the final model-snapshot :)! Almost there ... yay!")
    torch.save(
        vol_mod_edit.get_save_info(
            extra_info={
                "camera_bounds": camera_bounds,
                "camera_intrinsics": camera_intrinsics,
                "hemispherical_radius": train_dataset.get_hemispherical_radius_estimate(),
            }
        ),
        model_dir / f"model_final_attn_edit.pth",
    )
    torch.save(
        vol_mod_object.get_save_info(
            extra_info={
                "camera_bounds": camera_bounds,
                "camera_intrinsics": camera_intrinsics,
                "hemispherical_radius": train_dataset.get_hemispherical_radius_estimate(),
            }
        ),
        model_dir / f"model_final_attn_object.pth",
    )
    torch.save(
        vol_mod_output.get_save_info(
            extra_info={
                "camera_bounds": camera_bounds,
                "camera_intrinsics": camera_intrinsics,
                "hemispherical_radius": train_dataset.get_hemispherical_radius_estimate(),
            }
        ),
        model_dir / f"model_final_refined.pth",
    )

    # training complete yay! :)
    log.info("Training complete")
    log.info(
        f"Total actual training time: {timedelta(seconds=time_spent_actually_training)}"
    )

    return


def _make_dataloader_from_dataset(
        dataset: PosedImagesDataset, batch_size: int, num_workers: int = 0
) -> DataLoader:
    # setup the data_loader:
    # There are a bunch of fancy CPU-GPU configuration being done here.
    # Nothing too hard to understand, just refer the documentation page of PyTorch's
    # dataloader -> https://pytorch.org/docs/stable/data.html#torch.utils.data.DataLoader
    # And, read the book titled "CUDA_BY_EXAMPLE" https://developer.nvidia.com/cuda-example
    # Takes not long, just about 1-2 weeks :). But worth it :+1: :+1: :smile:!
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        drop_last=True,
        num_workers=0 if dataset.cached_data_mode else dataset,
        pin_memory=not dataset.cached_data_mode and num_workers > 0,
        prefetch_factor=num_workers
        if not dataset.cached_data_mode and num_workers > 0
        else 2,
        persistent_workers=not dataset.cached_data_mode and num_workers > 0,
    )

def get_dir_batch_from_poses(poses: Tensor):
    dir_batch = []
    num_poses = poses.shape[0]
    for i in range(num_poses):
        Rt = poses[i]
        pitch, yaw = _pitch_yaw_from_Rt(Rt)

        # determine view direction according to pitch, yaw
        dir = 'front'
        if yaw > 60.0:
            dir = 'side'
        if yaw > 120.0:
            dir = 'back'
        if pitch > 55.0:
            dir = 'overhead'

        dir_batch.append(dir)

    return dir_batch


def _pitch_yaw_from_Rt(rotation: Tensor):
    # pitch = np.arccos(rotation[1, 1].cpu().numpy()) * 180.0 / np.pi
    tx, ty, tz = rotation[:, -1].cpu().numpy()
    tr = np.sqrt(tx ** 2 + ty ** 2)
    pitch = np.arctan(tz / tr) * 180 / np.pi
    yaw = np.arccos(rotation[0, 0].cpu().numpy()) * 180.0 / np.pi
    return pitch, yaw

def _tv_loss_on_grid(grid: Tensor):
    tv0 = grid.diff(dim=0).abs()
    tv1 = grid.diff(dim=1).abs()
    tv2 = grid.diff(dim=2).abs()
    return (tv0.mean() + tv1.mean() + tv2.mean()) / 3

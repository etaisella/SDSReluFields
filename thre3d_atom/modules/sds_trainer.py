import time
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Callable, Optional

import imageio
import torch
import numpy as np
import matplotlib.pyplot as plt
from torch import Tensor
from torch.nn.functional import l1_loss, mse_loss
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import wandb

from thre3d_atom.data.datasets import PosedImagesDataset
from thre3d_atom.data.utils import infinite_dataloader
from thre3d_atom.modules.testers import test_sh_vox_grid_vol_mod_with_posed_images
from thre3d_atom.modules.volumetric_model import VolumetricModel
from thre3d_atom.rendering.volumetric.utils.misc import (
    cast_rays,
    collate_rays_unflattened,
    sample_rays_and_pixels_synchronously,
    sample_rays_directions_and_pixels_synchronously,
    flatten_rays,
)
from thre3d_atom.thre3d_reprs.renderers import render_sh_voxel_grid
from thre3d_atom.thre3d_reprs.voxels import (
    VoxelGrid,
    scale_voxel_grid_with_required_output_size,
)
from thre3d_atom.thre3d_reprs.sd import scoreDistillationLoss
from thre3d_atom.utils.constants import (
    CAMERA_BOUNDS,
    CAMERA_INTRINSICS,
    HEMISPHERICAL_RADIUS,
)
from thre3d_atom.utils.imaging_utils import CameraPose, to8b

# All the TrainProcedures below follow this function-type
from thre3d_atom.utils.logging import log
from thre3d_atom.utils.metric_utils import mse2psnr
from thre3d_atom.utils.misc import compute_thre3d_grid_sizes
from thre3d_atom.visualizations.static import (
    visualize_camera_rays,
    visualize_sh_vox_grid_vol_mod_rendered_feedback,
)

dir_to_num_dict = {'side':0, 'overhead':1, 'back':2, 'front':3}
# TrainProcedure = Callable[[VolumetricModel, Dataset, ...], VolumetricModel]


def train_sh_vox_grid_vol_mod_with_posed_images_and_sds(
    sds_vol_mod: VolumetricModel,
    pretrained_vol_mod: VolumetricModel,
    train_dataset: PosedImagesDataset,
    # required arguments:
    output_dir: Path,
    # optional arguments:)
    random_initializer: Callable[[Tensor], Tensor] = partial(
        torch.nn.init.uniform_, a=-1.0, b=1.0
    ),
    test_dataset: Optional[PosedImagesDataset] = None,
    image_batch_cache_size: int = 8,
    ray_batch_size: int = 32768,
    num_stages: int = 1,
    num_iterations_per_stage: int = 2000,
    scale_factor: float = 2.0,
    # learning_rate and related arguments
    learning_rate: float = 0.03,
    lr_decay_start: int = 5000,
    lr_freq: int = 400,
    lr_gamma: float = 0.8,
    # option to have a specific feedback_pose_for_visual feedback rendering
    render_feedback_pose: Optional[CameraPose] = None,
    # various training-loop frequencies
    save_freq: int = 1000,
    test_freq: int = 1000,
    feedback_freq: int = 100,
    summary_freq: int = 10,
    # regularization option:
    apply_diffuse_render_regularization: bool = True,
    # miscellaneous options can be left untouched
    num_workers: int = 4,
    verbose_rendering: bool = True,
    fast_debug_mode: bool = False,
    sds_prompt: str = "none",
    directional_dataset: bool = False,
    use_uncertainty: bool = False,
    new_frame_frequency: int = 1,
    density_correlation_weight: float = 0.0,
    feature_correlation_weight: float = 0.0,
    unet3d_mode: bool = False,
    tv_density_weight: float = 0.0,
    tv_features_weight: float = 0.0,
    do_sds: bool = True,
    sds_t_freq: int = 200,
    sds_t_start: int = 1500,
    sds_t_gamma: float = 1.0,
    uncoupled_mode: bool = False,
    uncoupled_l2_mode: bool = False,
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
    assert (
        sds_vol_mod.render_procedure == render_sh_voxel_grid
    ), f"sorry, non SH-based VoxelGrids cannot be used with this TrainProcedure"

    assert (
        sds_prompt != "none"
    ), f"sorry, you have to supply a text prompt to use SDS"

    # get regular density for dcl
    if unet3d_mode:
        regular_density, regular_features = pretrained_vol_mod.thre3d_repr.get_densities_and_features()
        regular_density = regular_density.detach()
        regular_features = regular_features.detach()
    else:
        regular_density = pretrained_vol_mod.thre3d_repr._densities.detach()
        regular_features = pretrained_vol_mod.thre3d_repr._features.detach()

    # init sds loss class
    sds_loss = scoreDistillationLoss(sds_vol_mod.device, 
                                     sds_prompt, 
                                     t_sched_start = sds_t_start,
                                     t_sched_freq = sds_t_freq,
                                     t_sched_gamma = sds_t_gamma,
                                     directional=directional_dataset)
    direction_batch = None
    selected_idx_in_batch = [0]
        

    # fix the sizes of the feature grids at different stages
    stagewise_voxel_grid_sizes = compute_thre3d_grid_sizes(
        final_required_resolution=sds_vol_mod.thre3d_repr.grid_dims,
        num_stages=num_stages,
        scale_factor=scale_factor,
    )

    # create downsampled versions of the train_dataset for lower training stages
    stagewise_train_datasets = [train_dataset]
    dataset_config_dict = train_dataset.get_config_dict()
    data_downsample_factor = dataset_config_dict["downsample_factor"]
    for stage in range(1, num_stages):
        dataset_config_dict.update(
            {"downsample_factor": data_downsample_factor * (scale_factor**stage)}
        )
        stagewise_train_datasets.insert(0, PosedImagesDataset(**dataset_config_dict))

    # setup render_feedback_pose
    real_feedback_image = None
    if render_feedback_pose is None:
        feedback_dataset = test_dataset if test_dataset is not None else train_dataset
        render_feedback_pose = CameraPose(
            rotation=feedback_dataset[-1][1][:, :3].cpu().numpy(),
            translation=feedback_dataset[-1][1][:, 3:].cpu().numpy(),
        )
        real_feedback_image = feedback_dataset[-1][0].permute(1, 2, 0).cpu().numpy()

    train_dl = _make_dataloader_from_dataset(
        train_dataset, image_batch_cache_size, num_workers
    )
    test_dl = (
        _make_dataloader_from_dataset(test_dataset, 1, num_workers)
        if test_dataset is not None
        else None
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

    # create camera-rays visualization:
    if not fast_debug_mode:
        log.info(
            "creating a camera-rays visualization... please wait... "
            "this is a slow operation :D"
        )
        visualize_camera_rays(
            train_dataset,
            output_dir,
            num_rays_per_image=1,
        )

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
        current_stage_lr = learning_rate

        params=[{"params": sds_vol_mod.thre3d_repr.parameters(), "lr": current_stage_lr}]
                    
        ## add logvars to optimizeable parameters if required
        if use_uncertainty:
            num_poses = len(train_dataset)
            logvars = torch.nn.Parameter(torch.zeros(num_poses, device=sds_vol_mod.device))
            params.append({{"params": logvars, "lr": current_stage_lr}})
    
        optimizer = torch.optim.Adam(
            params=params,
            betas=(0.9, 0.999),
        )

        # setup learning rate schedulers for the optimizer
        lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(
            optimizer, gamma=lr_gamma
        )

        # display logs related to this training stage:
        train_image_height, train_image_width = (
            current_stage_train_dataset.camera_intrinsics.height,
            current_stage_train_dataset.camera_intrinsics.width,
        )
        log.info(
            f"training stage: {stage}   "
            f"voxel grid resolution: {sds_vol_mod.thre3d_repr.grid_dims} "
            f"training images resolution: [{train_image_height} x {train_image_width}]"
        )
        current_stage_lrs = [
            param_group["lr"] for param_group in optimizer.param_groups
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
            total_loss = 0
            global_step = ((stage - 1) * num_iterations_per_stage) + stage_iteration

            if unet3d_mode:
                sds_density, sds_features = sds_vol_mod.thre3d_repr.get_densities_and_features()
            else:
                sds_density = sds_vol_mod.thre3d_repr._densities
                sds_features = sds_vol_mod.thre3d_repr._features

            if global_step % new_frame_frequency == 0 or global_step == 1:
                images, poses, indices = next(infinite_train_dl)

                # cast rays for all the loaded images:
                rays_list = []
                unflattened_rays_list = []
                for pose in poses:
                    unflattened_rays = cast_rays(
                            current_stage_train_dataset.camera_intrinsics,
                            CameraPose(rotation=pose[:, :3], translation=pose[:, 3:]),
                            device=sds_vol_mod.device,
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

                # log inputs
                wandb.log({"Input Image": wandb.Image(images[selected_idx_in_batch[0]])}, step=global_step)
                if directional_dataset:
                    direction_batch = _get_dir_batch_from_poses(poses[selected_idx_in_batch])
                    wandb.log({"Input Direction": dir_to_num_dict[direction_batch[0]]}, step=global_step)

            # render a small chunk of rays and compute a loss on it
            specular_rendered_batch_sds = sds_vol_mod.render_rays(rays_batch)
            specular_rendered_pixels_batch_sds = specular_rendered_batch_sds.colour

            # run sds loss training step!
            if use_uncertainty:
                logvars_batch = logvars[index_batch]
                total_loss = total_loss + torch.mean(logvars_batch)
            else:
                logvars_batch = None

            if do_sds:
                total_loss = total_loss + sds_loss.training_step(specular_rendered_pixels_batch_sds,
                                                                 im_h, im_w, 
                                                                 directions=direction_batch,
                                                                 global_step=global_step, 
                                                                 logvars=logvars_batch)
                current_sds_max_step = sds_loss.get_current_max_step_ratio()

            if uncoupled_mode:
                if uncoupled_l2_mode:
                    specular_loss = mse_loss(specular_rendered_pixels_batch_sds, pixels_batch)
                else:
                    specular_loss = l1_loss(specular_rendered_pixels_batch_sds, pixels_batch)
                total_loss = total_loss + specular_loss * density_correlation_weight
            else:
                ### insert losses that tie them together here ###
                density_correlation_loss, cov_grid = _density_correlation_loss(sds_density=sds_density,
                                                                     regular_density=regular_density)
                total_loss = total_loss + density_correlation_loss * density_correlation_weight

                if feature_correlation_weight > 0.0:
                    feature_correlation_loss = _feature_correlation_loss(sds_features=sds_features,
                                                                     regular_features=regular_features,
                                                                     density_cov_grid=cov_grid)
                    total_loss = total_loss + feature_correlation_loss * feature_correlation_weight

            ### insert other losses here ###
            if tv_density_weight > 0 and global_step % 1 == 0:
                activation = torch.nn.ReLU()
                activated_grid = activation(sds_vol_mod.thre3d_repr._densities)
                tv_density_loss = _tv_loss_on_grid(activated_grid)
                total_loss = total_loss + tv_density_loss * tv_density_weight
            
            if tv_features_weight > 0 and global_step % 1 == 0:
                tv_features_loss = _tv_loss_on_grid(sds_vol_mod.thre3d_repr._features)
                total_loss = total_loss + tv_features_loss * tv_features_weight

            # optimization steps:
            total_loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            # wandb logging:
            if use_uncertainty:
                _log_variances_in_wandb(logvars, global_step)
            if tv_density_weight > 0:
                wandb.log({"tv_density_loss" : tv_density_loss.item()}, step=global_step)
            if tv_features_weight > 0:
                wandb.log({"tv_features_loss" : tv_features_loss.item()}, step=global_step)
            if do_sds:
                wandb.log({"current_sds_max_step" : current_sds_max_step}, step=global_step)
            if not uncoupled_mode:
                if feature_correlation_weight > 0:
                    wandb.log({"feature_correlation_loss" : feature_correlation_loss.item()}, step=global_step)
                wandb.log({"density_correlation_loss" : density_correlation_loss.item()}, step=global_step)
            else:
                wandb.log({"specular_loss" : specular_loss.item()}, step=global_step)
            wandb.log({"total_loss" : total_loss}, step=global_step)
            wandb.log({"first selected indx in batch" : index_batch[0]}, step=global_step)
            lrs = [param_group["lr"] for param_group in optimizer.param_groups]
            wandb.log({"learning rate": lrs[0]}, step=global_step)

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
                    ("total_loss", total_loss),
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
                    f"total_loss: {total_loss.item(): .3f} "
                )

                log.info(loss_info_string)

            # step the learning rate schedulers
            if stage_iteration % lr_freq == 0 and global_step >= lr_decay_start:
                lr_scheduler.step()
                new_lrs = [param_group["lr"] for param_group in optimizer.param_groups]
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

                    if feedback_freq < global_step:
                        visualize_sh_vox_grid_vol_mod_rendered_feedback(
                            vol_mod=sds_vol_mod,
                            vol_mod_name="sds",
                            render_feedback_pose=render_feedback_pose,
                            camera_intrinsics=camera_intrinsics,
                            global_step=global_step,
                            feedback_logs_dir=render_dir,
                            parallel_rays_chunk_size=sds_vol_mod.render_config.parallel_rays_chunk_size,
                            training_time=time_spent_actually_training,
                            log_diffuse_rendered_version=apply_diffuse_render_regularization,
                            use_optimized_sampling_mode=False,  # testing how the optimized sampling mode rendering looks ????
                            overridden_num_samples_per_ray=sds_vol_mod.render_config.render_num_samples_per_ray,
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
                    sds_vol_mod.get_save_info(
                        extra_info={
                            CAMERA_BOUNDS: camera_bounds,
                            CAMERA_INTRINSICS: camera_intrinsics,
                            HEMISPHERICAL_RADIUS: train_dataset.get_hemispherical_radius_estimate(),
                        }
                    ),
                    model_dir / f"model_stage_{stage}_iter_{global_step}.pth",
                )

            # ignore all the time spent doing verbose stuff :) and update
            # the last_time clock event
            last_time = time.perf_counter()
        # -------------------------------------------------------------------------------------

        # don't upsample the feature grid if the last stage is complete
        if stage != num_stages:
            # upsample the feature-grid after the completion of the stage:
            with torch.no_grad():
                # noinspection PyTypeChecker
                sds_vol_mod.thre3d_repr = scale_voxel_grid_with_required_output_size(
                    sds_vol_mod.thre3d_repr,
                    output_size=stagewise_voxel_grid_sizes[stage],
                    mode="trilinear",
                )
    # -----------------------------------------------------------------------------------------

    # save the final trained model
    log.info(f"Saving the final model-snapshot :)! Almost there ... yay!")
    torch.save(
        sds_vol_mod.get_save_info(
            extra_info={
                "camera_bounds": camera_bounds,
                "camera_intrinsics": camera_intrinsics,
                "hemispherical_radius": train_dataset.get_hemispherical_radius_estimate(),
            }
        ),
        model_dir / f"model_final.pth",
    )

    # training complete yay! :)
    log.info("Training complete")
    log.info(
        f"Total actual training time: {timedelta(seconds=time_spent_actually_training)}"
    )
    return sds_vol_mod


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

def _log_variances_in_wandb(logvars, global_step):
    logvars_toplot = logvars.cpu().detach().numpy()
    variances = np.exp(logvars_toplot)
    indices = np.arange(variances.shape[0])
    fig = plt.figure(figsize=(5, 2.5), dpi=300)
    plt.bar(indices, variances)
    plt.title(f"Variance per Pose at step {global_step}")
    plt.xlabel("Pose index")
    plt.ylabel("Variance")
    plt.tight_layout()
    wandb.log({"Variances per Pose": wandb.Image(plt)}, step=global_step)
    plt.close(fig)

def _density_correlation_loss(sds_density: Tensor,
                              regular_density: Tensor):
    eps = 0.0000001 # for numerical stability

    # Calculate Denominator:
    sds_var = torch.mean((sds_density - torch.mean(sds_density))**2)
    regular_var = torch.mean((regular_density - torch.mean(regular_density))**2)
    denominator = torch.sqrt(sds_var * regular_var)

    # Calculate Covariance:
    covariance_grid = (sds_density - torch.mean(sds_density)) * \
        (regular_density - torch.mean(regular_density))
    #covariance = torch.mean(covariance_grid)

    # Return Result:
    correlation_grid = covariance_grid / (denominator + eps)
    correlation = torch.mean(correlation_grid)
    return 1.0 - correlation, correlation_grid.detach()

def _feature_correlation_loss(sds_features: Tensor,
                              regular_features: Tensor,
                              density_cov_grid):
    regular_features = regular_features.detach()
    sds_features_colors = torch.sigmoid(sds_features)
    regular_features_colors = torch.sigmoid(regular_features)
    l2_diffs = torch.sum(sds_features_colors - regular_features_colors, dim=-1) ** 2
    loss = torch.sum(l2_diffs)
    return loss


def _get_dir_batch_from_poses(poses: Tensor):
    dir_batch = []
    num_poses = poses.shape[0]
    for i in range(num_poses):
        Rt = poses[i]
        pitch, yaw = _pitch_yaw_from_Rt(Rt)

        # determine view direction according to pitch, yaw
        dir = 'front'
        if yaw > 45.0:
            dir = 'side'
        if yaw > 120.0:
            dir = 'back'
        if pitch > 55.0:
            dir = 'overhead'
        
        dir_batch.append(dir)
    
    return dir_batch
        
def _pitch_yaw_from_Rt(rotation: Tensor):
    tx, ty, tz = rotation[:,-1].cpu().numpy()
    tr = np.sqrt(tx**2 + ty**2)
    pitch = np.arctan(tz / tr) * 180 / np.pi
    yaw = np.arccos(rotation[0, 0].cpu().numpy()) * 180.0 / np.pi
    return pitch, yaw

def _tv_loss_on_grid(grid: Tensor):
    tv0 = grid.diff(dim=0).abs()
    tv1 = grid.diff(dim=1).abs()
    tv2 = grid.diff(dim=2).abs()
    return (tv0.mean() + tv1.mean() + tv2.mean()) / 3


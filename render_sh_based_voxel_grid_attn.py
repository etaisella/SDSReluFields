from pathlib import Path
import click
import imageio
import torch

from thre3d_atom.thre3d_reprs.sd_attn import StableDiffusion
from thre3d_atom.modules.volumetric_model import (
    create_volumetric_model_from_saved_model, create_volumetric_model_from_saved_model_attn
)
from thre3d_atom.thre3d_reprs.voxels import create_voxel_grid_from_saved_info_dict, \
    create_voxel_grid_from_saved_info_dict_attn
from thre3d_atom.utils.constants import HEMISPHERICAL_RADIUS, CAMERA_INTRINSICS
from thre3d_atom.utils.imaging_utils import (
    get_thre360_animation_poses,
    get_thre360_spiral_animation_poses,
)
from thre3d_atom.visualizations.animations import (
    render_camera_path_for_volumetric_model,
    render_camera_path_for_volumetric_model_attn,
    render_camera_path_for_volumetric_model_attn_blend
)
from easydict import EasyDict

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# -------------------------------------------------------------------------------------
#  Command line configuration for the script                                          |
# -------------------------------------------------------------------------------------
# fmt: off
# noinspection PyUnresolvedReferences
@click.command()
# Required arguments:
@click.option("-i", "--model_path", type=click.Path(file_okay=True, dir_okay=False),
              required=True, help="path to the trained (reconstructed) model")
@click.option("-o", "--output_path", type=click.Path(file_okay=False, dir_okay=True),
              required=True, help="path for saving rendered output")
@click.option("-r", "--ref_path", type=click.Path(file_okay=True, dir_okay=False), default=None,
              required=False, help="path for saving rendered output")

# Non-required Render configuration options:
@click.option("--overridden_num_samples_per_ray", type=click.IntRange(min=1), default=512,
              required=False, help="overridden (increased) num_samples_per_ray for beautiful renders :)")
@click.option("--render_scale_factor", type=click.FLOAT, default=2.0,
              required=False, help="overridden (increased) resolution (again :D) for beautiful renders :)")
@click.option("--camera_path", type=click.Choice(["thre360", "spiral"]), default="thre360",
              required=False, help="which camera path to use for rendering the animation")
# thre360_path options
@click.option("--camera_pitch", type=click.FLOAT, default=60.0,
              required=False, help="pitch-angle value for the camera for 360 path animation")
@click.option("--num_frames", type=click.IntRange(min=1), default=180,
              required=False, help="number of frames in the video")
# spiral path options
@click.option("--vertical_camera_height", type=click.FLOAT, default=3.0,
              required=False, help="height at which the camera spiralling will happen")
@click.option("--num_spiral_rounds", type=click.IntRange(min=1), default=2,
              required=False, help="number of rounds made while transitioning between spiral radii")
# Non-required video options:
@click.option("--fps", type=click.IntRange(min=1), default=60,
              required=False, help="frames per second of the video")
@click.option("--timestamp", type=click.INT, default=0,
              required=False, help="diffusion_timestamp")
@click.option("--use_sd", type=click.BOOL, default=False,
              required=False, help="render with stable diffusion")
@click.option("--load_attention", type=click.BOOL, default=True,
              required=False, help="render with attention features")
@click.option("--sds_prompt", type=click.STRING, required=False, default='',
              help="prompt for attention focus")
@click.option("--index_to_attn", type=click.INT, required=False, default=11,
              help="index to apply attention to", show_default=True)
@click.option("--save_freq", type=click.INT, default=None,
              required=False, help="frames per second of the video")



# fmt: on
# -------------------------------------------------------------------------------------
def main(**kwargs) -> None:
    # load the requested configuration for the training
    config = EasyDict(kwargs)

    # parse os-checked path-strings into Pathlike Paths :)
    model_path = Path(config.model_path)
    output_path = Path(config.output_path)
    sd_model = None
    if config.use_sd:
        sd_model = StableDiffusion(device, "1.4")
    # create the output path if it doesn't exist
    output_path.mkdir(exist_ok=True, parents=True)

    if config.load_attention:
        vol_mod, extra_info = create_volumetric_model_from_saved_model_attn(
            model_path=model_path,
            thre3d_repr_creator=create_voxel_grid_from_saved_info_dict_attn,
            device=device, load_attn=config.load_attention
        )
    # load volumetric_model from the model_path
    else:
        vol_mod, extra_info = create_volumetric_model_from_saved_model(
            model_path=model_path,
            thre3d_repr_creator=create_voxel_grid_from_saved_info_dict,
            device=device
        )

    # save prompt to text file if not None
    if config.sds_prompt != None:
        text_path = output_path / "prompt.txt"
        with open(text_path, 'w') as file:
            file.write(config.sds_prompt)

    # override extra info with ref's if given - raises quality
    if config.ref_path != None:
        ref_path = Path(config.ref_path)
        _, extra_info_ref = create_volumetric_model_from_saved_model(
            model_path=ref_path,
            thre3d_repr_creator=create_voxel_grid_from_saved_info_dict,
            device=device,
        )
        extra_info = extra_info_ref

    hemispherical_radius = extra_info[HEMISPHERICAL_RADIUS]
    camera_intrinsics = extra_info[CAMERA_INTRINSICS]

    # generate animation using the newly_created vol_mod :)
    if config.camera_path == "thre360":
        camera_pitch, num_frames = config.camera_pitch, config.num_frames
        animation_poses = get_thre360_animation_poses(
            hemispherical_radius=hemispherical_radius,
            camera_pitch=camera_pitch,
            num_poses=num_frames,
        )
    elif config.camera_path == "spiral":
        vertical_camera_height, num_frames = (
            config.vertical_camera_height,
            config.num_frames,
        )
        animation_poses = get_thre360_spiral_animation_poses(
            horizontal_radius_range=(hemispherical_radius / 8.0, hemispherical_radius),
            vertical_camera_height=vertical_camera_height,
            num_rounds=config.num_spiral_rounds,
            num_poses=num_frames,
        )
    else:
        raise ValueError(
            f"Unknown camera_path ``{config.camera_path}'' requested."
            f"Only available options are: ['thre360' and 'spiral']"
        )

    if config.load_attention:
        if config.use_sd:
            animation_frames, attn = render_camera_path_for_volumetric_model_attn_blend(
                vol_mod=vol_mod,
                camera_path=animation_poses,
                camera_intrinsics=camera_intrinsics,
                overridden_num_samples_per_ray=config.overridden_num_samples_per_ray,
                render_scale_factor=config.render_scale_factor,
                timestamp=config.timestamp
            )
        else:
            animation_frames, attn = render_camera_path_for_volumetric_model_attn_blend(
                vol_mod=vol_mod,
                camera_path=animation_poses,
                camera_intrinsics=camera_intrinsics,
                overridden_num_samples_per_ray=config.overridden_num_samples_per_ray,
                render_scale_factor=config.render_scale_factor,
                image_save_freq=config.save_freq,
                image_save_path=output_path,
            )
        #for i, f in enumerate(attn):
        #    imageio.imwrite(output_path / "{}.png".format(i), f)

    else:
        animation_frames = render_camera_path_for_volumetric_model(
            vol_mod=vol_mod,
            camera_path=animation_poses,
            camera_intrinsics=camera_intrinsics,
            overridden_num_samples_per_ray=config.overridden_num_samples_per_ray,
            render_scale_factor=config.render_scale_factor
        )

    imageio.mimwrite(
        output_path / "rendered_video.mp4",
        animation_frames,
        fps=config.fps,
    )


if __name__ == "__main__":
    main()
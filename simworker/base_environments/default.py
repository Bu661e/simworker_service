from __future__ import annotations

import logging
from dataclasses import dataclass

_TABLE_SIZE_M = 1.5
_TABLE_TOP_Z_M = _TABLE_SIZE_M
_TABLE_CENTER_Z_M = _TABLE_SIZE_M / 2.0
_TABLE_POSITION_XYZ = (0.0, 0.0, _TABLE_CENTER_Z_M)
_TABLE_COLOR_RGB = (0.56, 0.46, 0.36)
_GROUND_SIZE_XY_M = 8.0
_GROUND_THICKNESS_M = 0.05
_GROUND_CENTER_Z_M = -_GROUND_THICKNESS_M / 2.0
_GROUND_POSITION_XYZ = (0.0, 0.0, _GROUND_CENTER_Z_M)
_GROUND_COLOR_RGB = (0.42, 0.44, 0.48)
_ROBOT_POSITION_XYZ = (0.0, -0.4, _TABLE_TOP_Z_M)
_ROBOT_EULER_XYZ_DEG = (0.0, 0.0, 90.0)
_TOP_CAMERA_HEIGHT_M = 5.0
_OVERVIEW_CAMERA_POSITION_XYZ = (0.0, 3.3, 3.3)
_OVERVIEW_CAMERA_EULER_XYZ_DEG = (-60.0, 0.0, -180.0)
_CAMERA_RESOLUTION = (640, 640)
_TOP_CAMERA_ID = "table_top"
_TOP_CAMERA_PRIM_PATH = "/World/Cameras/TableTopCamera"
_OVERVIEW_CAMERA_ID = "table_overview"
_OVERVIEW_CAMERA_PRIM_PATH = "/World/Cameras/TableOverviewCamera"
_CAMERA_HORIZONTAL_APERTURE_M = 0.024
_CAMERA_FOCAL_LENGTH_M = 0.020
_SCENE_WARMUP_STEPS = 8
_KEY_LIGHT_INTENSITY = 650.0
_KEY_LIGHT_PRIM_PATH = "/World/Lights/KeyLight"
_FRANKA_PRIM_PATH = "/panda"
_FRANKA_END_EFFECTOR_PRIM_PATH = "/panda/panda_rightfinger"
_FRANKA_JOINT_PATHS = [
    "/panda/joints/panda_joint1",
    "/panda/joints/panda_joint2",
    "/panda/joints/panda_joint3",
    "/panda/joints/panda_joint4",
    "/panda/joints/panda_joint5",
    "/panda/joints/panda_joint6",
    "/panda/joints/panda_joint7",
]
@dataclass(slots=True)
class BaseEnvironmentHandles:
    world: object
    cameras: dict[str, object]
    camera_configs: dict[str, object]
    robot: object
    table: object


def create_default_tabletop_base_environment(logger: logging.Logger) -> BaseEnvironmentHandles:
    import isaacsim.core.utils.numpy.rotations as rot_utils
    import numpy as np
    from isaacsim.core.api.objects import FixedCuboid
    from isaacsim.core.api.world import World
    from isaacsim.core.utils.stage import create_new_stage, get_current_stage
    from isaacsim.sensors.camera import Camera
    from pxr import Sdf, UsdLux

    World.clear_instance()
    create_new_stage()
    world = World(stage_units_in_meters=1.0)
    cameras: dict[str, object] = {}
    # 运行时查询相机信息时除了 handle 本身，还需要知道 prim_path 和挂载方式。
    camera_configs: dict[str, object] = {}
    try:
        world.scene.add(
            FixedCuboid(
                prim_path="/World/Ground",
                name="ground",
                position=np.array(_GROUND_POSITION_XYZ),
                scale=np.array([_GROUND_SIZE_XY_M, _GROUND_SIZE_XY_M, _GROUND_THICKNESS_M]),
                size=1.0,
                color=np.array(_GROUND_COLOR_RGB),
            )
        )

        stage = get_current_stage()
        key_light = UsdLux.DistantLight.Define(stage, Sdf.Path(_KEY_LIGHT_PRIM_PATH))
        key_light.CreateIntensityAttr(_KEY_LIGHT_INTENSITY)

        table = world.scene.add(
            FixedCuboid(
                prim_path="/World/Furniture/Table",
                name="table",
                position=np.array(_TABLE_POSITION_XYZ),
                scale=np.array([_TABLE_SIZE_M, _TABLE_SIZE_M, _TABLE_SIZE_M]),
                size=1.0,
                color=np.array(_TABLE_COLOR_RGB),
            )
        )
        robot = _create_local_franka_robot(
            world,
            position_xyz=_ROBOT_POSITION_XYZ,
            orientation_wxyz=rot_utils.euler_angles_to_quats(np.array(_ROBOT_EULER_XYZ_DEG), degrees=True),
        )

        top_camera = world.scene.add(
            Camera(
                prim_path=_TOP_CAMERA_PRIM_PATH,
                name=_TOP_CAMERA_ID,
                resolution=_CAMERA_RESOLUTION,
            )
        )
        top_camera.set_world_pose(
            position=np.array((0.0, 0.0, _TOP_CAMERA_HEIGHT_M)),
            orientation=rot_utils.euler_angles_to_quats(np.array([0.0, 90.0, 0.0]), degrees=True),
            camera_axes="world",
        )
        cameras[_TOP_CAMERA_ID] = top_camera
        camera_configs[_TOP_CAMERA_ID] = {
            "prim_path": _TOP_CAMERA_PRIM_PATH,
            "mount_mode": "world",
        }

        overview_camera = world.scene.add(
            Camera(
                prim_path=_OVERVIEW_CAMERA_PRIM_PATH,
                name=_OVERVIEW_CAMERA_ID,
                resolution=_CAMERA_RESOLUTION,
            )
        )
        overview_camera.set_local_pose(
            translation=np.array(_OVERVIEW_CAMERA_POSITION_XYZ),
            orientation=rot_utils.euler_angles_to_quats(
                np.array(_OVERVIEW_CAMERA_EULER_XYZ_DEG),
                degrees=True,
                extrinsic=False,
            ),
            camera_axes="usd",
        )
        cameras[_OVERVIEW_CAMERA_ID] = overview_camera
        camera_configs[_OVERVIEW_CAMERA_ID] = {
            "prim_path": _OVERVIEW_CAMERA_PRIM_PATH,
            "mount_mode": "usd",
        }

        world.reset()

        top_camera.initialize()
        top_camera.set_lens_aperture(0.0)
        top_camera.set_horizontal_aperture(_CAMERA_HORIZONTAL_APERTURE_M)
        top_camera.set_focal_length(_CAMERA_FOCAL_LENGTH_M)
        top_camera.add_distance_to_image_plane_to_frame()
        top_camera.resume()

        overview_camera.initialize()
        overview_camera.set_lens_aperture(0.0)
        overview_camera.set_horizontal_aperture(_CAMERA_HORIZONTAL_APERTURE_M)
        overview_camera.set_focal_length(_CAMERA_FOCAL_LENGTH_M)
        overview_camera.add_distance_to_image_plane_to_frame()
        overview_camera.resume()

        _step_render_frames(world, _SCENE_WARMUP_STEPS)
    except Exception:
        World.clear_instance()
        raise

    logger.info("Isaac base environment created with ground, light, table, franka, and two cameras")
    return BaseEnvironmentHandles(
        world=world,
        cameras=cameras,
        camera_configs=camera_configs,
        robot=robot,
        table=table,
    )


def _step_render_frames(world: object, num_frames: int) -> None:
    for _ in range(num_frames):
        world.step(render=True)


def _create_local_franka_robot(world: object, *, position_xyz: tuple[float, float, float], orientation_wxyz) -> object:
    import omni.kit.app
    import omni.kit.commands
    import numpy as np
    from isaacsim.robot.manipulators import SingleManipulator
    from isaacsim.robot.manipulators.grippers.parallel_gripper import ParallelGripper

    status, import_config = omni.kit.commands.execute("URDFCreateImportConfig")
    if status is not True:
        raise RuntimeError("failed to create URDF import config for Franka")

    import_config.merge_fixed_joints = False
    import_config.import_inertia_tensor = True
    import_config.fix_base = True
    import_config.make_default_prim = True

    ext_manager = omni.kit.app.get_app().get_extension_manager()
    ext_id = ext_manager.get_enabled_extension_id("isaacsim.asset.importer.urdf")
    if not ext_id:
        raise RuntimeError("isaacsim.asset.importer.urdf extension is not enabled")
    extension_path = ext_manager.get_extension_path(ext_id)
    urdf_path = f"{extension_path}/data/urdf/robots/franka_description/robots/panda_arm_hand.urdf"

    # Use the Panda URDF bundled with Isaac Sim so headless servers do not depend on Nucleus assets root.
    omni.kit.commands.execute(
        "URDFParseAndImportFile",
        urdf_path=urdf_path,
        import_config=import_config,
    )

    gripper = ParallelGripper(
        end_effector_prim_path=_FRANKA_END_EFFECTOR_PRIM_PATH,
        joint_prim_names=["panda_finger_joint1", "panda_finger_joint2"],
        joint_opened_positions=np.array([0.05, 0.05]),
        joint_closed_positions=np.array([0.0, 0.0]),
    )
    robot = world.scene.add(
        SingleManipulator(
            prim_path=_FRANKA_PRIM_PATH,
            name="franka",
            end_effector_prim_path=_FRANKA_END_EFFECTOR_PRIM_PATH,
            position=np.array(position_xyz),
            orientation=np.asarray(orientation_wxyz, dtype=np.float64),
            gripper=gripper,
        )
    )
    _configure_local_franka_drives()
    return robot


def _configure_local_franka_drives() -> None:
    import math
    from isaacsim.core.utils.stage import get_current_stage
    from pxr import PhysxSchema, UsdPhysics

    stage = get_current_stage()

    articulation_api = PhysxSchema.PhysxArticulationAPI.Get(stage, _FRANKA_PRIM_PATH)
    articulation_api.CreateSolverPositionIterationCountAttr(64)
    articulation_api.CreateSolverVelocityIterationCountAttr(64)

    angular_stiffness = math.radians(1e8)
    angular_damping = math.radians(1e7)
    for joint_path in _FRANKA_JOINT_PATHS:
        drive = UsdPhysics.DriveAPI.Get(stage.GetPrimAtPath(joint_path), "angular")
        drive.GetTargetPositionAttr().Set(0.0)
        drive.GetStiffnessAttr().Set(angular_stiffness)
        drive.GetDampingAttr().Set(angular_damping)

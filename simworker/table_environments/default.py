from __future__ import annotations

from typing import TYPE_CHECKING

from simworker.table_environments.common import ensure_prim_path_is_available, finalize_loaded_object_handles, rollback_created_prims

if TYPE_CHECKING:
    from simworker.runtime import WorkerRuntime

_DEFAULT_CUBES = (
    {
        "object_id": "red_cube",
        "prim_path": "/World/Tabletop/red_cube",
        "position_xyz_m": (0.16, 0.22, 1.57),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.07, 0.07, 0.07),
        "color_rgb": (0.62, 0.06, 0.06),
    },
    {
        "object_id": "blue_cube",
        "prim_path": "/World/Tabletop/blue_cube",
        "position_xyz_m": (0.32, -0.14, 1.57),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.07, 0.07, 0.07),
        "color_rgb": (0.08, 0.24, 0.62),
    },
)


def load_default_table_environment(runtime: WorkerRuntime) -> list[object]:
    # 这里直接像 base_environment 一样用 Isaac Sim API 硬编码创建对象，
    # 不再经过额外的 Spec -> RuntimeState 转换层。
    if runtime.world is None:
        raise RuntimeError("Isaac world is not initialized")

    import isaacsim.core.utils.numpy.rotations as rot_utils
    import numpy as np
    from isaacsim.core.api.objects import DynamicCuboid

    created_prim_paths: list[str] = []
    loaded_handles: list[object] = []
    try:
        for cube in _DEFAULT_CUBES:
            ensure_prim_path_is_available(cube["prim_path"])

        for cube in _DEFAULT_CUBES:
            created_prim_paths.append(cube["prim_path"])
            handle = runtime.world.scene.add(
                DynamicCuboid(
                    prim_path=cube["prim_path"],
                    name=cube["object_id"],
                    position=np.array(cube["position_xyz_m"], dtype=float),
                    scale=np.array(cube["size_xyz_m"], dtype=float),
                    size=1.0,
                    color=np.array(cube["color_rgb"], dtype=float),
                )
            )
            handle.set_world_pose(
                position=np.array(cube["position_xyz_m"], dtype=float),
                orientation=rot_utils.euler_angles_to_quats(
                    np.array(cube["rotation_rpy_deg"], dtype=float),
                    degrees=True,
                    extrinsic=False,
                ),
            )
            runtime.register_table_object_metadata(
                cube["object_id"],
                bbox_size_xyz_m=cube["size_xyz_m"],
                geometry={
                    "type": "cuboid",
                    "size_xyz_m": [float(cube["size_xyz_m"][0]), float(cube["size_xyz_m"][1]), float(cube["size_xyz_m"][2])],
                },
                color=cube["color_rgb"],
            )
            loaded_handles.append(handle)

        finalize_loaded_object_handles(runtime.world, runtime.logger, loaded_handles)
    except Exception:
        rollback_created_prims(runtime.logger, created_prim_paths)
        raise
    return loaded_handles

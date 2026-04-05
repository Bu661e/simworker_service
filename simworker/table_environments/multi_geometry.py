from __future__ import annotations

from typing import TYPE_CHECKING

from simworker.table_environments.common import ensure_prim_path_is_available, finalize_loaded_object_handles, rollback_created_prims

if TYPE_CHECKING:
    from simworker.runtime import WorkerRuntime

_MULTI_GEOMETRY_OBJECTS = (
    {
        "kind": "fixed_cylinder",
        "object_id": "left_plate",
        "prim_path": "/World/Tabletop/left_plate",
        "position_xyz_m": (-0.5, 0.01, 1.5075),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "radius_m": 0.2,
        "height_m": 0.015,
        "color_rgb": (0.15, 0.75, 0.85),
    },
    {
        "kind": "fixed_cylinder",
        "object_id": "right_plate",
        "prim_path": "/World/Tabletop/right_plate",
        "position_xyz_m": (0.5, 0.01, 1.5075),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "radius_m": 0.2,
        "height_m": 0.015,
        "color_rgb": (0.95, 0.55, 0.75),
    },
    {
        "kind": "cuboid",
        "object_id": "red_cube",
        "prim_path": "/World/Tabletop/red_cube",
        "position_xyz_m": (-0.14, 0.12, 1.57),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.04, 0.04, 0.04),
        "color_rgb": (1.0, 0.0, 0.0),
    },
    {
        "kind": "cuboid",
        "object_id": "blue_cube",
        "prim_path": "/World/Tabletop/blue_cube",
        "position_xyz_m": (0.0, 0.12, 1.57),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.04, 0.04, 0.04),
        "color_rgb": (0.0, 0.0, 1.0),
    },
    {
        "kind": "cuboid",
        "object_id": "green_block",
        "prim_path": "/World/Tabletop/green_block",
        "position_xyz_m": (0.14, 0.12, 1.56),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.14, 0.04, 0.04),
        "color_rgb": (0.0, 1.0, 0.0),
    },
    {
        "kind": "cuboid",
        "object_id": "yellow_block",
        "prim_path": "/World/Tabletop/yellow_block",
        "position_xyz_m": (-0.14, -0.1, 1.56),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "size_xyz_m": (0.14, 0.04, 0.04),
        "color_rgb": (1.0, 1.0, 0.0),
    },
    {
        "kind": "cylinder",
        "object_id": "purple_cylinder",
        "prim_path": "/World/Tabletop/purple_cylinder",
        "position_xyz_m": (0.0, -0.1, 1.575),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "radius_m": 0.03,
        "height_m": 0.09,
        "color_rgb": (0.6, 0.0, 0.8),
    },
    {
        "kind": "cylinder",
        "object_id": "orange_cylinder",
        "prim_path": "/World/Tabletop/orange_cylinder",
        "position_xyz_m": (0.14, -0.1, 1.5725),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "radius_m": 0.03,
        "height_m": 0.085,
        "color_rgb": (1.0, 0.5, 0.0),
    },
)


def load_multi_geometry_table_environment(runtime: WorkerRuntime) -> list[object]:
    if runtime.world is None:
        raise RuntimeError("Isaac world is not initialized")

    import isaacsim.core.utils.numpy.rotations as rot_utils
    import numpy as np
    from isaacsim.core.api.objects import DynamicCuboid, DynamicCylinder, FixedCylinder

    created_prim_paths: list[str] = []
    loaded_handles: list[object] = []
    try:
        for scene_object in _MULTI_GEOMETRY_OBJECTS:
            ensure_prim_path_is_available(scene_object["prim_path"])

        for scene_object in _MULTI_GEOMETRY_OBJECTS:
            created_prim_paths.append(scene_object["prim_path"])
            handle = runtime.world.scene.add(
                _build_scene_object(
                    scene_object,
                    DynamicCuboid,
                    DynamicCylinder,
                    FixedCylinder,
                )
            )
            handle.set_world_pose(
                position=np.array(scene_object["position_xyz_m"], dtype=float),
                orientation=rot_utils.euler_angles_to_quats(
                    np.array(scene_object["rotation_rpy_deg"], dtype=float),
                    degrees=True,
                    extrinsic=False,
                ),
            )
            metadata = _build_scene_object_metadata(scene_object)
            runtime.register_table_object_metadata(
                scene_object["object_id"],
                bbox_size_xyz_m=metadata["bbox_size_xyz_m"],
                geometry=metadata["geometry"],
                color=scene_object["color_rgb"],
            )
            loaded_handles.append(handle)

        finalize_loaded_object_handles(runtime.world, runtime.logger, loaded_handles)
    except Exception:
        rollback_created_prims(runtime.logger, created_prim_paths)
        raise
    return loaded_handles


def _build_scene_object(
    scene_object: dict[str, object],
    dynamic_cuboid_cls: type[object],
    dynamic_cylinder_cls: type[object],
    fixed_cylinder_cls: type[object],
) -> object:
    import numpy as np

    common_kwargs = {
        "prim_path": str(scene_object["prim_path"]),
        "name": str(scene_object["object_id"]),
        "position": np.array(scene_object["position_xyz_m"], dtype=float),
        "color": np.array(scene_object["color_rgb"], dtype=float),
    }
    if scene_object["kind"] == "cuboid":
        return dynamic_cuboid_cls(
            **common_kwargs,
            scale=np.array(scene_object["size_xyz_m"], dtype=float),
            size=1.0,
        )
    if scene_object["kind"] == "cylinder":
        return dynamic_cylinder_cls(
            **common_kwargs,
            radius=float(scene_object["radius_m"]),
            height=float(scene_object["height_m"]),
        )
    if scene_object["kind"] == "fixed_cylinder":
        return fixed_cylinder_cls(
            **common_kwargs,
            radius=float(scene_object["radius_m"]),
            height=float(scene_object["height_m"]),
        )
    raise ValueError(f"unsupported multi_geometry object kind: {scene_object['kind']}")


def _build_scene_object_metadata(scene_object: dict[str, object]) -> dict[str, object]:
    if scene_object["kind"] == "cuboid":
        size_xyz_m = scene_object["size_xyz_m"]
        return {
            "bbox_size_xyz_m": [float(size_xyz_m[0]), float(size_xyz_m[1]), float(size_xyz_m[2])],
            "geometry": {
                "type": "cuboid",
                "size_xyz_m": [float(size_xyz_m[0]), float(size_xyz_m[1]), float(size_xyz_m[2])],
            },
        }

    if scene_object["kind"] in {"cylinder", "fixed_cylinder"}:
        radius_m = float(scene_object["radius_m"])
        height_m = float(scene_object["height_m"])
        return {
            "bbox_size_xyz_m": [2.0 * radius_m, 2.0 * radius_m, height_m],
            "geometry": {
                "type": "cylinder",
                "radius_m": radius_m,
                "height_m": height_m,
            },
        }

    raise ValueError(f"unsupported multi_geometry object kind: {scene_object['kind']}")

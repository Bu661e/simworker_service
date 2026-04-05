from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from simworker.table_environments.common import (
    ensure_prim_path_is_available,
    euler_xyz_deg_to_quaternion_wxyz,
    finalize_loaded_object_handles,
    rollback_created_prims,
)

if TYPE_CHECKING:
    from simworker.runtime import WorkerRuntime

# 线上主机当前按 ~/Download/YCB 部署，但为了兼容历史环境也接受 ~/Downloads/YCB。
_YCB_PHYSICS_ROOT_CANDIDATES = (
    Path("/root/Download/YCB/Axis_Aligned_Physics"),
    Path("/root/Downloads/YCB/Axis_Aligned_Physics"),
)
_YCB_OBJECTS = (
    {
        "object_id": "cracker_box_1",
        "prim_path": "/World/Tabletop/cracker_box_1",
        "asset_filename": "003_cracker_box.usd",
        "position_xyz_m": (0.20, 0.18, 1.68),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "asset_scale": (1.0, 1.0, 1.0),
        "semantic_label": "cracker_box",
    },
    {
        "object_id": "mustard_bottle_1",
        "prim_path": "/World/Tabletop/mustard_bottle_1",
        "asset_filename": "006_mustard_bottle.usd",
        "position_xyz_m": (0.34, -0.10, 1.68),
        "rotation_rpy_deg": (0.0, 0.0, 0.0),
        "asset_scale": (1.0, 1.0, 1.0),
        "semantic_label": "mustard_bottle",
    },
)


def load_ycb_table_environment(runtime: WorkerRuntime) -> list[object]:
    # ycb 桌面环境优先使用已经带 Physics 配置的资产版本，
    # 直接把 USD 引进 stage，再让物体落桌稳定。
    if runtime.world is None:
        raise RuntimeError("Isaac world is not initialized")

    from isaacsim.core.prims import SingleXFormPrim
    from isaacsim.core.utils.prims import get_prim_at_path
    from isaacsim.core.utils.semantics import add_labels
    from isaacsim.core.utils.stage import add_reference_to_stage

    created_prim_paths: list[str] = []
    loaded_handles: list[object] = []
    try:
        for scene_object in _YCB_OBJECTS:
            ensure_prim_path_is_available(scene_object["prim_path"])

        for scene_object in _YCB_OBJECTS:
            created_prim_paths.append(scene_object["prim_path"])
            root_prim = add_reference_to_stage(
                usd_path=str(_require_ycb_asset(scene_object["asset_filename"])),
                prim_path=scene_object["prim_path"],
            )
            handle = SingleXFormPrim(
                prim_path=scene_object["prim_path"],
                name=scene_object["object_id"],
                position=scene_object["position_xyz_m"],
                orientation=euler_xyz_deg_to_quaternion_wxyz(scene_object["rotation_rpy_deg"]),
                scale=scene_object["asset_scale"],
            )
            add_labels(
                get_prim_at_path(scene_object["prim_path"]),
                labels=[scene_object["semantic_label"]],
                instance_name="class",
            )
            _apply_usd_asset_dynamic_physics(
                root_prim,
                disable_gravity=False,
            )
            runtime.register_table_object_metadata(
                scene_object["object_id"],
                geometry={
                    "type": "mesh",
                    "asset_filename": scene_object["asset_filename"],
                    "semantic_label": scene_object["semantic_label"],
                },
                color=None,
            )
            loaded_handles.append(handle)

        finalize_loaded_object_handles(runtime.world, runtime.logger, loaded_handles)
    except Exception:
        rollback_created_prims(runtime.logger, created_prim_paths)
        raise
    return loaded_handles


def _require_ycb_asset(filename: str) -> Path:
    physics_root = _resolve_ycb_physics_root()
    asset_path = physics_root / filename
    if not asset_path.exists():
        raise ValueError(f"missing YCB asset: {asset_path}")
    if not asset_path.is_file():
        raise ValueError(f"YCB asset path is not a file: {asset_path}")
    return asset_path


def _resolve_ycb_physics_root() -> Path:
    for candidate in _YCB_PHYSICS_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate

    candidate_text = ", ".join(str(path) for path in _YCB_PHYSICS_ROOT_CANDIDATES)
    raise ValueError(f"missing YCB asset root; checked: {candidate_text}")


def _apply_usd_asset_dynamic_physics(root_prim: object, *, disable_gravity: bool) -> None:
    from pxr import PhysxSchema, Usd, UsdGeom, UsdPhysics

    for desc_prim in Usd.PrimRange(root_prim):
        if desc_prim.IsA(UsdGeom.Mesh) or desc_prim.IsA(UsdGeom.Gprim):
            collision_api = (
                UsdPhysics.CollisionAPI(desc_prim)
                if desc_prim.HasAPI(UsdPhysics.CollisionAPI)
                else UsdPhysics.CollisionAPI.Apply(desc_prim)
            )
            if collision_api.GetCollisionEnabledAttr().IsValid():
                collision_api.GetCollisionEnabledAttr().Set(True)
            else:
                collision_api.CreateCollisionEnabledAttr(True)

            physx_collision_api = (
                PhysxSchema.PhysxCollisionAPI(desc_prim)
                if desc_prim.HasAPI(PhysxSchema.PhysxCollisionAPI)
                else PhysxSchema.PhysxCollisionAPI.Apply(desc_prim)
            )
            physx_collision_api.CreateContactOffsetAttr(0.001)
            physx_collision_api.CreateRestOffsetAttr(0.0)

        if desc_prim.IsA(UsdGeom.Mesh):
            mesh_collision_api = (
                UsdPhysics.MeshCollisionAPI(desc_prim)
                if desc_prim.HasAPI(UsdPhysics.MeshCollisionAPI)
                else UsdPhysics.MeshCollisionAPI.Apply(desc_prim)
            )
            if mesh_collision_api.GetApproximationAttr().IsValid():
                mesh_collision_api.GetApproximationAttr().Set("convexHull")
            else:
                mesh_collision_api.CreateApproximationAttr().Set("convexHull")

    rigid_body_api = (
        UsdPhysics.RigidBodyAPI(root_prim)
        if root_prim.HasAPI(UsdPhysics.RigidBodyAPI)
        else UsdPhysics.RigidBodyAPI.Apply(root_prim)
    )
    if rigid_body_api.GetRigidBodyEnabledAttr().IsValid():
        rigid_body_api.GetRigidBodyEnabledAttr().Set(True)
    else:
        rigid_body_api.CreateRigidBodyEnabledAttr(True)

    physx_rigid_body_api = (
        PhysxSchema.PhysxRigidBodyAPI(root_prim)
        if root_prim.HasAPI(PhysxSchema.PhysxRigidBodyAPI)
        else PhysxSchema.PhysxRigidBodyAPI.Apply(root_prim)
    )
    physx_rigid_body_api.GetDisableGravityAttr().Set(disable_gravity)

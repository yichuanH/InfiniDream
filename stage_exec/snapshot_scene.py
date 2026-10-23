"""Export the scene facts used by the final-scene contract.

Run this script once per blend file.  Infinigen scenes are large enough that
opening populated and fine files in the same Blender process has caused
segfaults, so comparison deliberately lives in ``validate_final_scene.py``.

Usage:
  python snapshot_scene.py -- scene.blend snapshot.json --frames 1,24,48
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import bpy


SCHEMA_VERSION = 1
TERRAIN_BASES = (
    "OpaqueTerrain",
    "CollectiveTransparentTerrain",
    "liquid",
    "atmosphere",
    "water",
    "lava",
)
GROUND_MOTION_PRESETS = {
    "ground_wander", "ground_forward", "ground_hop",
    "terrain_wander", "directed_ground_traverse",
}


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("blend", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--frames",
        default=None,
        help="comma-separated contract frames; defaults to scene start/middle/end",
    )
    return parser.parse_args(argv)


def finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def matrix_values(matrix) -> list[float | None]:
    return [finite_float(matrix[row][col]) for row in range(4) for col in range(4)]


def action_snapshot(obj) -> dict | None:
    animation = getattr(obj, "animation_data", None)
    action = getattr(animation, "action", None) if animation else None
    if action is None:
        return None

    digest = hashlib.sha256()
    n_keys = 0
    fcurves = []
    for curve in sorted(action.fcurves, key=lambda c: (c.data_path, c.array_index)):
        curve_info = {
            "data_path": curve.data_path,
            "array_index": int(curve.array_index),
            "keyframes": len(curve.keyframe_points),
        }
        fcurves.append(curve_info)
        digest.update(
            f"{curve.data_path}\0{curve.array_index}\0{len(curve.keyframe_points)}\n".encode()
        )
        for point in curve.keyframe_points:
            n_keys += 1
            digest.update(
                (
                    f"{float(point.co.x):.17g},{float(point.co.y):.17g},"
                    f"{point.interpolation},{point.easing}\n"
                ).encode()
            )
    return {
        "name": action.name,
        "frame_range": [finite_float(v) for v in action.frame_range],
        "fcurve_count": len(fcurves),
        "keyframe_count": n_keys,
        "sha256": digest.hexdigest(),
        "fcurves": fcurves,
    }


def constraint_snapshot(obj) -> list[dict]:
    result = []
    for constraint in obj.constraints:
        target = getattr(constraint, "target", None)
        result.append(
            {
                "name": constraint.name,
                "type": constraint.type,
                "mute": bool(constraint.mute),
                "influence": finite_float(constraint.influence),
                "target": getattr(target, "name", None),
                "subtarget": getattr(constraint, "subtarget", None),
            }
        )
    return result


def parse_frames(raw: str | None, start: int, end: int) -> list[int]:
    if raw:
        values = [int(v.strip()) for v in raw.split(",") if v.strip()]
    else:
        values = [start, (start + end) // 2, end]
    values = sorted(set(values))
    if not values:
        raise ValueError("at least one contract frame is required")
    return values


def collection_render_visibility(scene) -> dict[str, bool]:
    """Whether a collection has at least one render-visible path from Scene Collection."""

    visible: dict[str, bool] = {}

    def walk(collection, ancestors_visible: bool):
        here = ancestors_visible and not bool(collection.hide_render)
        visible[collection.name] = visible.get(collection.name, False) or here
        for child in collection.children:
            walk(child, here)

    walk(scene.collection, True)
    return visible


def is_terrain_name(name: str) -> bool:
    return any(
        name == base
        or name.startswith(base + "_")
        or name.startswith(base + ".")
        for base in TERRAIN_BASES
    )


def mesh_stats(obj) -> dict | None:
    if obj.type != "MESH" or obj.data is None:
        return None
    return {
        "vertices": len(obj.data.vertices),
        "edges": len(obj.data.edges),
        "polygons": len(obj.data.polygons),
    }


def object_snapshot(
    obj,
    frame_matrices: dict[int, dict[str, list]],
    render_visible: bool,
) -> dict:
    return {
        "type": obj.type,
        "data_name": getattr(getattr(obj, "data", None), "name", None),
        "parent": getattr(obj.parent, "name", None),
        "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone or None,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "hide_render": bool(obj.hide_render),
        "hide_viewport": bool(obj.hide_viewport),
        "hide_get": bool(obj.hide_get()),
        "render_visible": render_visible,
        "mesh": mesh_stats(obj),
        "is_terrain": is_terrain_name(obj.name),
        "action": action_snapshot(obj),
        "constraints": constraint_snapshot(obj),
        "matrix_local_by_frame": {
            str(frame): frame_matrices[frame][obj.name] for frame in sorted(frame_matrices)
        },
    }


def particle_snapshot() -> dict[str, dict]:
    systems = {}
    setting_fields = (
        "type",
        "physics_type",
        "render_type",
        "emit_from",
        "distribution",
        "count",
        "frame_start",
        "frame_end",
        "lifetime",
        "particle_size",
        "instance_object",
        "instance_collection",
    )
    for emitter in sorted(bpy.data.objects, key=lambda obj: obj.name):
        particle_systems = getattr(emitter, "particle_systems", None)
        if not particle_systems:
            continue
        for index, system in enumerate(particle_systems):
            settings = system.settings
            values = {}
            for field in setting_fields:
                value = getattr(settings, field, None)
                if field in ("instance_object", "instance_collection"):
                    value = getattr(value, "name", None)
                elif isinstance(value, float):
                    value = finite_float(value)
                values[field] = value
            cache = system.point_cache
            key = f"{emitter.name}::{index}::{system.name}"
            systems[key] = {
                "emitter": emitter.name,
                "index": index,
                "name": system.name,
                "settings_name": settings.name,
                "settings": values,
                "cache": {
                    "frame_start": int(cache.frame_start),
                    "frame_end": int(cache.frame_end),
                    "is_baked": bool(cache.is_baked),
                    "is_outdated": bool(cache.is_outdated),
                    "use_disk_cache": bool(cache.use_disk_cache),
                    "use_external": bool(cache.use_external),
                },
            }
    return systems


def camera_snapshot(scene, frames: list[int]) -> dict:
    camera = scene.camera
    if camera is None:
        return {"active": None, "samples": {}, "rig_chain": []}

    samples = {}
    max_fov = 0.0
    for frame in frames:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = camera.evaluated_get(depsgraph)
        data = evaluated.data
        fovs = [finite_float(data.angle), finite_float(data.angle_x), finite_float(data.angle_y)]
        max_fov = max(max_fov, *(v or 0.0 for v in fovs))
        samples[str(frame)] = {
            "matrix_world": matrix_values(evaluated.matrix_world),
            "lens": finite_float(data.lens),
            "sensor_width": finite_float(data.sensor_width),
            "sensor_height": finite_float(data.sensor_height),
            "sensor_fit": data.sensor_fit,
            "clip_start": finite_float(data.clip_start),
            "clip_end": finite_float(data.clip_end),
            "fov_radians": fovs,
        }

    chain = []
    current = camera
    while current is not None:
        chain.append(
            {
                "name": current.name,
                "type": current.type,
                "parent": getattr(current.parent, "name", None),
                "action": action_snapshot(current),
                "constraints": constraint_snapshot(current),
            }
        )
        current = current.parent

    return {
        "active": camera.name,
        "samples": samples,
        "rig_chain": chain,
        "max_fov_degrees": math.degrees(max_fov),
    }


def fine_ground_motion_audit(scene) -> dict:
    """Raycast every ground-motion frame against terrain in this exact blend."""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import placement_modes as pm

    terrain = pm.terrain_candidates()
    if not terrain:
        return {"terrain": [], "anchors": {}, "failures": [], "skipped": "not a fine scene"}
    prepared = pm.prepare_terrains(terrain) if terrain else []
    result = {"terrain": [obj.name for obj in terrain], "anchors": {}, "failures": []}
    anchors = [
        obj for obj in bpy.data.objects
        if (obj.parent is None
            and obj.get("bi_world_motion_preset") in GROUND_MOTION_PRESETS
            and getattr(getattr(obj, "animation_data", None), "action", None) is not None
            and obj.animation_data.action.name.startswith("BI_WORLD_MOTION::"))
    ]
    original_frame = int(scene.frame_current)
    # ★外層跑幀、內層量每隻★(2026-09-13 coast：原本是「每隻 × 每幀都 frame_set 整個場景」，
    #   10 隻紅鶴 × 120 幀 = 1200 次整場景重算，3GB 的 fine 場景跑了 85 分鐘還沒完；
    #   arctic 20 隻企鵝會是 2400 次)。對調之後不管幾隻都只 frame_set 120 次，
    #   每隻每幀量到的東西、判定規則、失敗訊息完全不變。
    ordered = []
    for anchor in sorted(anchors, key=lambda obj: obj.name):
        measure = [child for child in anchor.children if ".spawn_asset(" in child.name]
        if not prepared or not measure:
            result["failures"].append(
                f"{anchor.name}: terrain={bool(prepared)} measure_children={len(measure)}")
            continue
        ordered.append((anchor, str(anchor.get("bi_world_motion_preset")), measure, []))
    try:
        for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
            if not ordered:
                break
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            for anchor, preset, measure, samples in ordered:
                if str(anchor.get("bi_support_surface") or "").lower().startswith("liquid"):
                    # ★站在水面/冰面的東西不打射線★(2026-09-13)：結果本來就不採用(下面同一個判斷會標
                    #   supported_by_liquid)，而且底下的地形在水深處，打到也是 10m 的「間隙」。
                    #   coast 10 隻紅鶴 × 120 幀 = 1200 次 contact_gap(每幀第一次約 1-2.5s)，
                    #   arctic 企鵝站在叫 liquid 的冰面上最多 2400 次。只少了 terrain ray miss 這道，
                    #   對站水面的東西沒有意義(beyondinfinigen-d8 同意)。
                    samples.append({"frame": frame, "gap_m": None, "hits": 0, "points": 0,
                                    "skipped": "supported_by_liquid"})
                    continue
                gap, hits, points = pm.contact_gap(measure, prepared)
                row = {"frame": frame, "gap_m": finite_float(gap),
                       "hits": int(hits), "points": int(points)}
                samples.append(row)
                if gap is None or hits <= 0:
                    result["failures"].append(f"{anchor.name}: terrain ray miss at frame {frame}")
                elif str(anchor.get("bi_support_surface") or "").lower().startswith("liquid"):
                    # ★站在水面的東西不對地形量接地★(2026-09-12 coast：紅鶴站淺水，
                    #   ground_wander 走到水深 0.4m 處就被判離地 ⇒ 契約 FAIL)。
                    row["skipped"] = "supported_by_liquid"
                elif preset != "ground_hop" and abs(float(gap)) > 0.30:
                    result["failures"].append(
                        f"{anchor.name}: fine-terrain gap {float(gap):+.4f}m at frame {frame}")
                elif preset == "ground_hop" and frame in {
                    int(scene.frame_start), int(scene.frame_end)
                } and abs(float(gap)) > 0.30:
                    result["failures"].append(
                        f"{anchor.name}: fine-terrain landing gap {float(gap):+.4f}m "
                        f"at frame {frame}")
        for anchor, preset, _measure, samples in ordered:
            result["anchors"][anchor.name] = {"preset": preset, "samples": samples}
    finally:
        scene.frame_set(original_frame)
        bpy.context.view_layer.update()
    return result


def main() -> int:
    args = parse_args()
    blend = args.blend.resolve()
    if not blend.is_file():
        raise SystemExit(f"snapshot input does not exist: {blend}")
    blend_stat = blend.stat()

    print(f"[snapshot] opening {blend}", flush=True)
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    scene = bpy.context.scene
    frames = parse_frames(args.frames, int(scene.frame_start), int(scene.frame_end))

    camera = camera_snapshot(scene, frames)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import camera_visibility as camera_vis
    requested_camera_visibility = camera_vis.snapshot_requested_visibility(scene)

    # Capture every object's local transform at the same explicit frames.  A
    # local matrix plus parent identity is stable across collection regrouping.
    frame_matrices = {}
    for frame in frames:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        frame_matrices[frame] = {
            obj.name: matrix_values(obj.matrix_local) for obj in bpy.data.objects
        }

    collection_visibility = collection_render_visibility(scene)
    objects = {}
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        object_visible = not bool(obj.hide_render) and any(
            collection_visibility.get(collection.name, False)
            for collection in obj.users_collection
        )
        objects[obj.name] = object_snapshot(obj, frame_matrices, object_visible)

    result = {
        "schema_version": SCHEMA_VERSION,
        "blend": str(blend),
        "file_stat": {
            "size": int(blend_stat.st_size),
            "mtime_ns": int(blend_stat.st_mtime_ns),
        },
        "scene": {
            "name": scene.name,
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "fps": int(scene.render.fps),
            "contract_frames": frames,
        },
        "camera": camera,
        "objects": objects,
        "particles": particle_snapshot(),
        "fine_ground_motion_audit": fine_ground_motion_audit(scene),
        "requested_camera_visibility": requested_camera_visibility,
        "summary": {
            "objects": len(objects),
            "meshes": sum(obj.type == "MESH" for obj in bpy.data.objects),
            "particle_systems": sum(
                len(obj.particle_systems)
                for obj in bpy.data.objects
                if getattr(obj, "particle_systems", None)
            ),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"[snapshot] {len(objects)} objects, {len(result['particles'])} particle systems "
        f"-> {args.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

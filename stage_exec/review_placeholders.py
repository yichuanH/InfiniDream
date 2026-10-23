"""Export a machine-readable Step-8 placement review contract.

Usage:
  blender-python review_placeholders.py -- work.blend manifest.json report.json \
      composer_edit_plan.json placement_review.json

The review agent is expected to edit the Composer JSON and rerun Step 8.  The
blend is an output, never an editable source of truth.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import camera_visibility as cv  # noqa: E402
import placeholder_colors as pc  # noqa: E402
import surface_raycast_cache as srcache  # noqa: E402

VOLUME_MAP_ANCHORINGS = {"submerged", "airborne"}


def _load_volume_maps(index_path: Path | None) -> dict[str, dict]:
    if index_path is None:
        return {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    result = {}
    for row in index.get("maps", []):
        payload = json.loads(
            (index_path.parent / row["map_json"]).read_text(encoding="utf-8"))
        if payload.get("anchoring") in VOLUME_MAP_ANCHORINGS:
            result[str(row["edit_id"])] = payload
    return result


def _outdoor_ultra_loose_metadata(placement: dict) -> dict | None:
    # Legacy ultra-loose metadata is diagnostic only. Outdoor Composer may widen
    # the search area, but review constraints remain hard.
    return None


def _violation_involves_ultra_loose(violation: dict, *,
                                    object_names: set[str],
                                    edit_ids: set[str]) -> bool:
    if any(str(obj) in object_names
           for obj in violation.get("objects", []) if obj is not None):
        return True
    return any(str(edit_id) in edit_ids
               for edit_id in violation.get("edit_ids", [])
               if edit_id is not None)


def volume_environment_collisions(scene, items, maps) -> dict:
    """Audit shifted volume proxies against map-captured 3D scene geometry."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from placement_region_contract import aabb_overlap_fraction

    violations = []
    for item in items:
        anchor = item["anchor"]
        edit_id = str(item["edit_id"])
        map_data = maps.get(edit_id)
        if map_data is None:
            continue
        bounds = cv.world_bounds(scene, anchor, visible_only=False)
        if bounds is None:
            continue
        base = [*bounds["min"], *bounds["max"]]
        padding = list(map(float, map_data.get(
            "motion_clearance_xyz", [0.0, 0.0, 0.0])))
        swept = [
            base[0] - padding[0], base[1] - padding[1], base[2] - padding[2],
            base[3] + padding[0], base[4] + padding[1], base[5] + padding[2],
        ]
        for obstacle in map_data.get("obstacles", []):
            obstacle_bounds = obstacle.get("world_bounds")
            overlap = aabb_overlap_fraction(swept, obstacle_bounds)
            if overlap <= 0.0:
                continue
            violations.append({
                "type": (
                    f"{map_data.get('anchoring')}_environment_aabb_collision"),
                "objects": [anchor.name, obstacle.get("object")],
                "edit_ids": [edit_id, None],
                "placement_indices": [
                    int(anchor.get("bi_instance_index", 0)), None],
                "obstacle": {
                    key: obstacle.get(key)
                    for key in ("object", "label", "source", "world_bounds")
                },
                "candidate_world_bounds": base,
                "swept_world_bounds": swept,
                "motion_clearance_xyz": padding,
                "max_overlap_of_smaller": float(overlap),
                "collision_frames": 1,
                "frames": [int(scene.frame_current)],
            })
    return {
        "proxy": ("shifted requested 3D-volume AABB plus conservative "
                  "planned-motion envelope versus map-captured environment AABBs"),
        "anchor_count": len(items),
        "violations": violations,
        "ok": not violations,
    }


def footprint_support(scene, anchor, policy):
    """Raycast four bottom box corners; semantics come only from policy names."""
    import placement_contract as plc
    corners = [anchor.matrix_world @ Vector(corner) for corner in anchor.bound_box]
    bottom_z = min(point.z for point in corners)
    top_z = max(point.z for point in corners)
    bottom = [point for point in corners if abs(point.z - bottom_z) < 1e-4]
    samples = []
    for point in bottom:
        origin = Vector((point.x, point.y, top_z + 0.25))
        cached_hit = srcache.raycast(
            scene, origin, Vector((0, 0, -1)), distance=1000.0)
        best = None
        if cached_hit is not None:
            distance, loc, object_name, normal = cached_hit
            slope = float(__import__("math").degrees(__import__("math").acos(
                max(0.0, min(1.0, abs(normal.z))))))
            best = (distance, object_name, slope, loc)
        if best is None:
            samples.append({"surface": None, "issue": "no support below footprint corner"})
            continue
        distance, name, slope, loc = best
        issue = plc.surface_policy_issue(
            policy, surface_name=name, slope_deg=slope)
        support_gap = abs(float(loc.z) - float(anchor.matrix_world.translation.z))
        # Stage A places the anchor origin on its center-ray support. A corner
        # whose surface height differs substantially is bridging a gap/slope.
        if issue is None and support_gap > 0.75:
            issue = f"support height differs by {support_gap:.3f}m across footprint"
        samples.append({
            "surface": name, "slope_deg": slope, "distance": float(distance),
            "support_height_gap": support_gap,
            "location": [float(v) for v in loc], "issue": issue,
        })
    return {"supported": bool(samples) and all(x.get("issue") is None for x in samples),
            "samples": samples}


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:]
    blend_path, manifest_path, report_path, plan_path, output_path = map(Path, argv[:5])
    review_frames = None
    if "--frame" in argv[5:]:
        review_frames = [int(argv[argv.index("--frame") + 1])]
    region_map_index = None
    if "--region-map-index" in argv[5:]:
        region_map_index = Path(argv[argv.index("--region-map-index") + 1])
    volume_maps = _load_volume_maps(region_map_index)
    # BI_HERO_SIMPLE=1(2026-09-12)：simple_volume_placement.py 放的 edit 沒有 region map。
    #   ⚠️ ★它放的不是只有空中/水下★(09-13 起也含 conform/upright/tilted/rooted/afloat)，
    #      但下面的支撐面豁免仍然★只對 volume anchoring 生效★(見 volume_map_owned:
    #      anchoring in VOLUME_MAP_ANCHORINGS 才成立) —— 水中/空中是明確 3D 座標，
    #      查支撐面會被判「射線沒打到可見表面」；地面的照常驗支撐面與 surface_policy。
    simple_routed_edits = set()
    try:
        simple_routed_edits = {
            str(edit.get("code_from") or "")
            for edit in json.loads(plan_path.read_text(encoding="utf-8")).get("hero_edits", [])
            if str(edit.get("placement_route") or "") == "simple_volume"}
    except (OSError, ValueError):
        pass
    preview_path = output_path.parent / "placement_preview.png"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reuse_open = "--reuse-open" in argv[5:]
    if not reuse_open:
        bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    scene = bpy.context.scene

    # Stage A stores the support/base point and Stage C applies altitude exactly
    # once. For review only, preview that future altitude without saving it into
    # the coarse blend. afloat uses the same path for its small waterline offset.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import placement_modes as pm
    shifted_objects = []
    for obj in scene.objects:
        anchoring = obj.get("bi_anchoring")
        if (obj.parent is None
                and (anchoring in pm.AIRBORNE_MODES or anchoring == "afloat")):
            _normal_fac, altitude = pm.params_for(anchoring)
            obj.location.z += float(altitude)
            shifted_objects.append((obj, float(altitude)))
    bpy.context.view_layer.update()

    added_names = set(report.get("added_placeholder_names") or [])
    requested_ids = [
        placement.get("code_from") or placement.get("factory")
        for placement in manifest.get("placements", [])
        if int(placement.get("added_this_plan") or 0) > 0
    ]
    requested_colors = pc.requested_colors(requested_ids)
    rows = []
    failures = []
    overlap_items = []
    requested_anchors = []
    volume_environment_items = []
    ultra_loose_objects = set()
    ultra_loose_edit_ids = set()
    for index, placement in enumerate(manifest.get("placements", [])):
        if int(placement.get("added_this_plan") or 0) <= 0:
            continue
        ultra_loose = _outdoor_ultra_loose_metadata(placement)
        # ★盡量模式 + 簡單路徑放的東西不退件★(使用者 2026-09-12：撒不下就算了，不要跳 error，要跑完)。
        #   simple_volume_placement 已經用分級放寬找過位置；審查再用 director 原條件退件只會進 5 輪
        #   composer 修訂然後 FAIL(arctic 實測)。這裡把它們當 ultra_loose：問題記成 warning，不算 failure。
        if (not ultra_loose and os.environ.get("BI_PLACEMENT_BEST_EFFORT") == "1"
                and str(placement.get("code_from") or "") in simple_routed_edits):
            ultra_loose = {"mode": "best_effort_simple_route",
                           "note": "simple_volume_placement placed it best-effort; review issues are warnings"}
        if ultra_loose:
            ultra_loose_edit_ids.add(str(
                placement.get("code_from") or placement.get("factory")))
        collection = bpy.data.collections.get(placement.get("collection", ""))
        anchors = sorted(
            (obj for obj in collection.objects
             if obj.parent is None and obj.name in added_names),
            key=lambda obj: (
                int(obj.get("bi_instance_index"))
                if obj.get("bi_instance_index") is not None else 1_000_000,
                obj.name,
            ),
        ) if collection is not None else []
        audits = [
            cv.audit_anchor(
                scene, anchor, frames=review_frames, visible_only=False)
            for anchor in anchors
        ]
        for fallback_index, (anchor, audit) in enumerate(zip(anchors, audits)):
            audit["instance_index"] = int(
                anchor.get("bi_instance_index", fallback_index))
            if anchor.get("bi_screen_x") is not None and anchor.get("bi_screen_y") is not None:
                audit["effective_screen_position"] = [
                    float(anchor["bi_screen_x"]), float(anchor["bi_screen_y"])]
            audit["selected_visible_surface"] = anchor.get("bi_support_surface")
            normal = anchor.get("bi_support_normal")
            audit["selected_surface_normal"] = (
                [float(value) for value in normal] if normal is not None else None)
            slope = anchor.get("bi_support_slope_deg")
            audit["selected_surface_slope_deg"] = (
                float(slope) if slope is not None else None)
            policy = (placement.get("placement_spec") or {}).get("surface_policy") or {}
            edit_id = str(placement.get("code_from") or placement.get("factory"))
            volume_map_owned = (
                placement.get("anchoring") in VOLUME_MAP_ANCHORINGS
                and (edit_id in volume_maps or edit_id in simple_routed_edits))
            if ultra_loose:
                ultra_loose_objects.add(str(anchor.name))
                audit["outdoor_ultra_loose_relaxation"] = ultra_loose
                audit["surface_policy_issue"] = None
                audit["surface_validation"] = (
                    "outdoor_ultra_loose_"
                    + (f"{placement.get('anchoring')}_3d_region_map"
                       if placement.get("anchoring") in VOLUME_MAP_ANCHORINGS
                       else "visible_surface"))
                audit["footprint_support"] = {
                    "supported": True,
                    "samples": [],
                    "validation": (
                        "outdoor_ultra_loose_footprint_support_advisory"),
                }
                audit["relaxed_review_failures"] = []
            elif volume_map_owned:
                # A map-owned volume voxel is an explicit point in water/open
                # air; unlike grounded placement it intentionally has no support
                # surface. Its hard contracts are the 3D environment map,
                # complete camera box and environment re-audit below.
                audit["surface_policy_issue"] = None
                audit["surface_validation"] = (
                    f"{placement.get('anchoring')}_3d_region_map")
                audit["footprint_support"] = {
                    "supported": True,
                    "samples": [],
                    "validation": "explicit_3d_region_map_no_ground_support",
                }
            else:
                audit["surface_policy_issue"] = __import__(
                    "placement_contract").surface_policy_issue(
                        policy, surface_name=audit["selected_visible_surface"],
                        slope_deg=audit["selected_surface_slope_deg"])
                audit["footprint_support"] = footprint_support(
                    scene, anchor, policy)
        row = {
            "placement": index,
            "factory": placement.get("factory"),
            "collection": placement.get("collection"),
            "code_from": placement.get("code_from"),
            "anchoring": placement.get("anchoring"),
            "world_motion": placement.get("world_motion"),
            "placement_spec": placement.get("placement_spec"),
            "outdoor_ultra_loose_relaxation": ultra_loose,
            "screen_position_source": "preview includes the planned anchoring altitude",
            "anchors": audits,
        }
        label = str(placement.get("code_from") or placement.get("factory"))
        rgb = requested_colors[label]
        row["preview_annotation"] = {
            "edit_id": placement.get("code_from"),
            "label": label.replace("_", " ").upper(),
            "color_rgb": [float(channel) for channel in rgb],
            "color_hex": "#%02X%02X%02X" % tuple(int(channel * 255) for channel in rgb),
        }
        if anchors:
            first_sample = [
                audit["samples"][0] for audit in audits if audit.get("samples")
            ]
            row["screen_centers"] = [
                {
                    "instance_index": int(audit.get(
                        "instance_index", instance_index)),
                    "object": audit["object"],
                    "xy_bottom_origin": [
                        float(sample["center_ndc"][0]),
                        float(sample["center_ndc"][1]),
                    ],
                }
                for instance_index, (audit, sample) in enumerate(
                    zip(audits, first_sample))
                if sample.get("center_ndc")
            ]
        rows.append(row)
        for audit in audits:
            overlap_items.append({
                "edit_id": placement.get("code_from"),
                "object": audit.get("object"),
                "audit": audit,
            })
        requested_anchors.extend(anchors)
        if placement.get("anchoring") in VOLUME_MAP_ANCHORINGS:
            volume_environment_items.extend({
                "anchor": anchor,
                "edit_id": placement.get("code_from") or placement.get("factory"),
            } for anchor in anchors)
        if not anchors:
            failures.append(f"placement #{index}: no added placeholder anchor found")
        for audit in audits:
            # Boundary is a full-box contract. A visible center is insufficient:
            # the Arctic benchmark had 72/72 visible centers but 0/72 complete
            # boxes and was incorrectly allowed to reach an Agent accept.
            required_visible_frames = cv.minimum_visible_frame_count(
                audit["frame_count"])
            if (audit["fully_visible_frames"] < required_visible_frames
                    and not cv.EXTENDED_GRID):
                message = (
                    f"complete placeholder box inside frame "
                    f"{audit['fully_visible_frames']}/"
                    f"{audit['frame_count']} frames "
                    f"(need >= {required_visible_frames})")
                # ★可見格數只警告不擋★(2026-10-19 使用者)：不再進 failures，
                #   照 ultra_loose 的方式記在 relaxed_review_failures。
                audit.setdefault("relaxed_review_failures", []).append({
                    "type": "full_box_visibility",
                    "message": message,
                })
                print(f"[review] ⚠️ 可見格數不足(只警告): placement #{index} "
                      f"{audit['object']}: {message}", flush=True)
            if audit.get("surface_policy_issue") and not ultra_loose:
                failures.append(
                    f"placement #{index} {audit['object']}: "
                    f"{audit['surface_policy_issue']}")
            policy = (placement.get("placement_spec") or {}).get("surface_policy") or {}
            if (policy.get("require_full_footprint_support", True)
                    and not audit.get("footprint_support", {}).get("supported")
                    and not ultra_loose):
                if os.environ.get("BI_HERO_SIMPLE", "1") == "1":
                    # ★寬鬆路線只警告★(2026-10-20：1019 重跑 13 五輪都只卡這一條)
                    audit.setdefault("relaxed_review_failures", []).append({
                        "type": "footprint_support",
                        "message": "full footprint is not supported"})
                    print(f"[review] ⚠️ 腳底未完全踩到地(只警告): placement #{index} {audit['object']}",
                          flush=True)
                else:
                    failures.append(
                        f"placement #{index} {audit['object']}: full footprint is not supported")

    composition_warnings = cv.projected_overlap_violations(overlap_items)
    collision_review = cv.audit_world_collisions(
        scene, requested_anchors, frames=review_frames)
    requested_violations = collision_review["violations"]
    requested_pair_keys = {
        tuple(sorted(str(obj) for obj in violation.get("objects", [])))
        for violation in requested_violations
    }
    same_factory_collision_review = cv.audit_world_collisions(
        scene, requested_anchors, frames=review_frames,
        threshold=0.0, same_factory_only=True)
    same_factory_violations = []
    strict_requested_violations = []
    relaxed_requested_violations = []
    for violation in requested_violations:
        if _violation_involves_ultra_loose(
                violation, object_names=ultra_loose_objects,
                edit_ids=ultra_loose_edit_ids):
            relaxed_requested_violations.append(violation)
            continue
        strict_requested_violations.append(violation)
        failures.append(
            "3D requested-box collision: "
            f"{violation['edit_ids']} in {violation['collision_frames']} frames "
            f"(max={violation['max_overlap_of_smaller']:.3f})")
    for violation in same_factory_collision_review["violations"]:
        pair_key = tuple(sorted(str(obj) for obj in violation.get("objects", [])))
        if pair_key in requested_pair_keys:
            continue
        same_factory_violations.append(violation)
        failures.append(
            "same-factory collision: "
            f"{violation.get('factory') or violation.get('factories')} "
            f"{violation['objects']} in {violation['collision_frames']} frames "
            f"(max={violation['max_overlap_of_smaller']:.3f})")
    same_factory_collision_review["filtered_violations"] = same_factory_violations
    collision_review["outdoor_ultra_loose_relaxed_violations"] = (
        relaxed_requested_violations)
    collision_review["strict_violation_count_after_outdoor_ultra_loose"] = len(
        strict_requested_violations)
    environment_collision_review = volume_environment_collisions(
        scene, volume_environment_items, volume_maps)
    strict_environment_violations = []
    relaxed_environment_violations = []
    for violation in environment_collision_review["violations"]:
        if _violation_involves_ultra_loose(
                violation, object_names=ultra_loose_objects,
                edit_ids=ultra_loose_edit_ids):
            relaxed_environment_violations.append(violation)
            continue
        strict_environment_violations.append(violation)
        failures.append(
            f"3D {violation['type'].split('_', 1)[0]} environment collision: "
            f"{violation['objects'][0]} overlaps "
            f"{violation['obstacle']['label'] or violation['objects'][1]} "
            f"(max={violation['max_overlap_of_smaller']:.3f})")
    environment_collision_review["outdoor_ultra_loose_relaxed_violations"] = (
        relaxed_environment_violations)
    environment_collision_review[
        "strict_violation_count_after_outdoor_ultra_loose"] = len(
            strict_environment_violations)
    violations = (requested_violations + same_factory_violations
                  + environment_collision_review["violations"])

    result = {
        "contract_version": 1,
        "status": "PASS" if not failures else "NEEDS_REVISION",
        "preview_image": str(preview_path.resolve()),
        "preview_blend": str(blend_path.resolve()),
        "editable_source": str(plan_path.resolve()),
        "derived_outputs": [str(blend_path.resolve()), str(manifest_path.resolve()),
                            str(report_path.resolve())],
        "review_loop": {
            "edit": ("Agent may update add.screen_positions and, only when the current "
                     "search area blocks a semantic correction, placement_spec.screen_bounds "
                     "in 3_composer/edit_plan.json."),
            "do_not_edit": "Do not patch work/scene.blend, manifest.json, or report.json directly.",
            "rerun_from_step": 8,
        },
        "frame_range": (
            [review_frames[0], review_frames[-1]]
            if review_frames else [int(scene.frame_start), int(scene.frame_end)]),
        "placements": rows,
        "collision_review": collision_review,
        "same_factory_collision_review": same_factory_collision_review,
        "environment_collision_review": environment_collision_review,
        "violations": violations,
        "outdoor_ultra_loose_review": {
            "edit_ids": sorted(ultra_loose_edit_ids),
            "object_count": len(ultra_loose_objects),
            "relaxed_requested_collision_count": len(
                relaxed_requested_violations),
            "relaxed_environment_collision_count": len(
                relaxed_environment_violations),
        },
        "composition_warnings": composition_warnings,
        "failures": failures,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    # This script may be embedded in a longer-lived Blender process.  Review
    # altitude is diagnostic only; never leak it into later renders or saves.
    for obj, altitude in shifted_objects:
        obj.location.z -= altitude
    bpy.context.view_layer.update()
    print(f"[placement-review] {result['status']}: {len(rows)} placements, "
          f"{len(failures)} issues -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

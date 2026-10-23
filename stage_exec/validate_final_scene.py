"""Validate that an E2E output is a genuine, invariant-preserving fine scene.

This is intentionally pure Python.  ``snapshot_scene.py`` opens each large
blend in its own Blender process; this script compares their compact JSON
snapshots and creates the render gate only when every required check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path


SCHEMA_VERSION = 1
SECONDARY_TERRAIN_BASES = {"liquid", "atmosphere", "water", "lava"}
PRIMARY_TERRAIN_BASES = {"OpaqueTerrain", "CollectiveTransparentTerrain"}
CORE_TERRAIN_BASES = PRIMARY_TERRAIN_BASES | SECONDARY_TERRAIN_BASES
FLOAT_TOLERANCE = 1e-5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--populated", type=Path, required=True)
    parser.add_argument("--fine", type=Path, required=True)
    parser.add_argument("--placer-report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ok-marker", type=Path, required=True)
    parser.add_argument("--mesher", default="unknown")
    return parser.parse_args()


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def close_values(left, right, tolerance=FLOAT_TOLERANCE) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), abs_tol=tolerance, rel_tol=tolerance)
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return all(close_values(a, b, tolerance) for a, b in zip(left, right))
    return left == right


def is_scaffolding(info: dict) -> bool:
    """Whether this object is placement scaffolding rather than final geometry.

    ``placeholders:`` holds the boxes (and, for trees/bushes, their skeleton
    children) that populate consumes. Placeholders the camera cull skipped are
    simply left behind, and the fine stage hides them -- correct behaviour, not
    evidence the scene changed. This shows up as soon as something creates many
    placeholders and populates only the visible ones (0911: official_scatter's
    christmas trees in snowy_mountain, white birches in desert, 20 objects each).
    """
    return any(str(name).startswith("placeholders:")
               for name in (info.get("collections") or []))


def is_particle_master(info: dict) -> bool:
    """Whether this object is a particle system's instancing source, not scene geometry.

    ``particleassets:`` collections hold the masters a particle system instances.
    creature_col_to_particle_col parks them at z-100 so they are never seen, and
    fine_terrain re-evaluation moves them again -- their transform is an
    implementation detail, not evidence the scene changed (0911 coral_reef: the
    fish masters we swapped in failed the contract and blocked the render).
    """
    return any(str(name).startswith("particleassets:")
               for name in (info.get("collections") or []))


def terrain_bases(populated: dict, fine: dict) -> set[str]:
    bases = set(CORE_TERRAIN_BASES)
    for snapshot in (populated, fine):
        for name in snapshot.get("objects", {}):
            if name.endswith("_fine"):
                bases.add(name[: -len("_fine")])
    return bases


def matches_terrain_base(name: str, base: str) -> bool:
    return (
        name == base
        or name.startswith(base + "_")
        or name.startswith(base + ".")
    )


def primary_terrain_bases(populated: dict, fine: dict) -> list[str]:
    pop_objects = populated.get("objects", {})
    fine_objects = fine.get("objects", {})
    fine_bases = []
    for name in fine_objects:
        if not name.endswith("_fine"):
            continue
        base = name[: -len("_fine")]
        if base in SECONDARY_TERRAIN_BASES:
            continue
        if any(matches_terrain_base(pop_name, base) for pop_name in pop_objects):
            fine_bases.append(base)
    if fine_bases:
        return sorted(set(fine_bases), key=lambda item: (item != "OpaqueTerrain", item))
    fallback = [
        base for base in sorted(PRIMARY_TERRAIN_BASES,
                                key=lambda item: (item != "OpaqueTerrain", item))
        if any(matches_terrain_base(pop_name, base) for pop_name in pop_objects)
    ]
    return fallback


def coarse_terrain_hidden(objects: dict, base: str) -> tuple[bool, dict]:
    coarse_rows = []
    for name, info in objects.items():
        if name == f"{base}_fine":
            continue
        if not matches_terrain_base(name, base):
            continue
        coarse_rows.append({
            "name": name,
            "hide_render": bool(info.get("hide_render")),
            "render_visible": bool(info.get("render_visible")),
        })
    visible = [
        row["name"] for row in coarse_rows
        if not (row["hide_render"] and not row["render_visible"])
    ]
    return bool(coarse_rows) and not visible, {
        "base": base,
        "coarse_objects": len(coarse_rows),
        "still_render_visible": visible[:30],
    }


def is_terrain_object(name: str, info: dict, bases: set[str]) -> bool:
    if info.get("is_terrain"):
        return True
    return any(
        name == base
        or name.startswith(base + "_")
        or name.startswith(base + ".")
        for base in bases
    )


def relevant_nonterrain_objects(snapshot: dict, bases: set[str]) -> dict[str, dict]:
    """Objects whose survival can affect the rendered final scene.

    Blender may retain unlinked objects from a temporary master pool in the first
    save and purge them in the next.  Those objects have no collection and are not
    render-visible; treating them as final-scene inventory creates a false failure.
    Protect every scene-linked object (including hidden helpers), plus particle
    references, then include parent and constraint dependencies.
    """
    objects = snapshot.get("objects", {})
    relevant = {
        name for name, info in objects.items()
        if info.get("collections") and not is_terrain_object(name, info, bases)
    }

    referenced_collections = set()
    for particle in snapshot.get("particles", {}).values():
        emitter = particle.get("emitter")
        if emitter in objects:
            relevant.add(emitter)
        settings = particle.get("settings", {})
        instance_object = settings.get("instance_object")
        if instance_object in objects:
            relevant.add(instance_object)
        instance_collection = settings.get("instance_collection")
        if instance_collection:
            referenced_collections.add(instance_collection)
    for name, info in objects.items():
        if referenced_collections.intersection(info.get("collections", [])):
            relevant.add(name)

    pending = list(relevant)
    while pending:
        name = pending.pop()
        info = objects.get(name, {})
        dependencies = [info.get("parent")]
        dependencies.extend(
            constraint.get("target") for constraint in info.get("constraints", [])
        )
        for dependency in dependencies:
            if dependency in objects and dependency not in relevant:
                relevant.add(dependency)
                pending.append(dependency)

    return {
        name: objects[name]
        for name in sorted(relevant)
        if not is_terrain_object(name, objects[name], bases)
    }


def requested_asset_visibility_audit(placer: dict, manifest: dict,
                                     fine_objects: dict) -> dict:
    """Prove generated populate deliveries still have visible roots after fine terrain."""
    results = {
        result.get("index"): result
        for result in placer.get("results", [])
        if isinstance(result, dict) and isinstance(result.get("index"), int)
    }
    failures = []
    collections = []
    for index, placement in enumerate(manifest.get("placements", [])):
        if not isinstance(placement, dict):
            continue
        generated = bool(placement.get("code"))
        added = int(placement.get("added_this_plan") or 0) > 0
        if (not generated and not added) or placement.get("edit_via") == "particle_col":
            continue
        if placement.get("realized_at", "populate") != "populate":
            continue
        result = results.get(index) or {}
        for processed in result.get("collections_processed", []):
            expected_roots = int(processed.get("built") or 0)
            placeholder_collection = processed.get("collection", "")
            if expected_roots <= 0 or not placeholder_collection.startswith("placeholders:"):
                continue
            suffix = placeholder_collection.split(":", 1)[1]
            unique_collection = f"unique_assets:{suffix}"
            visible_roots = sorted(
                name
                for name, info in fine_objects.items()
                if unique_collection in info.get("collections", [])
                and info.get("render_visible")
                and not info.get("hide_render")
                # Partial replacement moves an existing placeholder into the
                # new placeholder collection but deliberately preserves its
                # object name.  Its name can therefore contain the old factory
                # seed (for example TreeFactory(7836289)) while the generated
                # asset/collection uses the new seed (TreeFactory(53243660)).
                # Collection membership, not a name prefix, is the placement
                # contract.  Requiring the direct parent to be in that
                # collection selects asset roots without counting descendants.
                and placeholder_collection in fine_objects.get(
                    info.get("parent"), {}
                ).get("collections", [])
            )
            collections.append({
                "placement": index,
                "collection": unique_collection,
                "expected_roots": expected_roots,
                "visible_roots": len(visible_roots),
            })
            if len(visible_roots) != expected_roots:
                failures.append(
                    f"placement #{index} {unique_collection}: expected "
                    f"{expected_roots} visible roots, found {len(visible_roots)}"
                )
    return {"failures": failures, "collections": collections}


def nonempty_visible_mesh(objects: dict, name: str) -> tuple[bool, str]:
    info = objects.get(name)
    if info is None:
        return False, f"missing {name}"
    mesh = info.get("mesh") or {}
    vertices = int(mesh.get("vertices") or 0)
    polygons = int(mesh.get("polygons") or 0)
    if info.get("type") != "MESH" or vertices <= 0 or polygons <= 0:
        return False, f"{name} is not a non-empty mesh (v={vertices}, p={polygons})"
    if info.get("hide_render") or not info.get("render_visible"):
        return False, f"{name} is not render-visible"
    return True, f"{name}: {vertices} vertices / {polygons} polygons, render-visible"


def placer_delivery_audit(placer: dict, manifest: dict) -> dict:
    """Shared pre-fine/final audit for placer feasibility and requested assets."""

    results = placer.get("results")
    result_map = {
        result.get("index"): result
        for result in results or []
        if isinstance(result, dict) and isinstance(result.get("index"), int)
    }
    placer_failures = []
    if not isinstance(results, list):
        placer_failures.append("placer report has no results list")
        results = []
    else:
        for index, result in enumerate(results):
            if not isinstance(result, dict) or result.get("feasible") is not True:
                reason = result.get("blocked_reason") if isinstance(result, dict) else None
                placer_failures.append(f"result #{index} infeasible: {reason or 'no reason'}")

    delivery_failures = []
    placements = manifest.get("placements")
    if not isinstance(placements, list):
        delivery_failures.append("manifest has no placements list")
        placements = []
    expected_indices = set(range(len(placements)))
    actual_indices = set(result_map)
    if actual_indices != expected_indices:
        placer_failures.append(
            f"placer result indices differ from manifest: "
            f"missing={sorted(expected_indices - actual_indices)}, "
            f"extra={sorted(actual_indices - expected_indices)}"
        )
    checked_deliveries = 0
    for index, placement in enumerate(placements):
        if not isinstance(placement, dict):
            delivery_failures.append(f"placement #{index} is not an object")
            continue
        generated = bool(placement.get("code"))
        added = int(placement.get("added_this_plan") or 0) > 0
        result = result_map.get(index)
        if result and result.get("factory") != placement.get("factory"):
            placer_failures.append(
                f"placement #{index} factory {placement.get('factory')!r} != "
                f"report {result.get('factory')!r}"
            )
        if result and result.get("code") != placement.get("code"):
            placer_failures.append(f"placement #{index} generated code path differs from report")
        if not generated and not added:
            continue
        checked_deliveries += 1
        if not result or result.get("feasible") is not True:
            delivery_failures.append(f"placement #{index} has no feasible placer result")
            continue
        if generated and result.get("source") != "generated":
            delivery_failures.append(f"placement #{index} did not load generated code")
            continue
        if placement.get("edit_via") == "particle_col":
            swap = result.get("master_swap") or {}
            swap_ok = bool(
                swap.get("feasible")
                and swap.get("only_instance_changed")
                and swap.get("cache_ok")
                and int(swap.get("new_n_masters") or 0) > 0
            )
            if not swap_ok:
                delivery_failures.append(f"placement #{index} has no valid particle master swap")
            continue
        if placement.get("realized_at", "populate") != "populate":
            delivery_failures.append(
                f"placement #{index} is generated/added but was skipped at "
                f"realized_at={placement.get('realized_at')}"
            )
            continue
        if int(result.get("assets_built") or 0) <= 0:
            delivery_failures.append(f"placement #{index} built zero assets")

    return {
        "placer_failures": placer_failures,
        "delivery_failures": delivery_failures,
        "result_count": len(results),
        "checked_deliveries": checked_deliveries,
    }


def world_motion_audit(placer: dict, manifest: dict, snapshot: dict | None = None) -> dict:
    """Validate requested world-space motion independently from local rig animation."""
    result_map = {
        result.get("index"): result
        for result in placer.get("results", [])
        if isinstance(result, dict) and isinstance(result.get("index"), int)
    }
    failures = []
    checked = []
    objects = snapshot.get("objects", {}) if snapshot else {}
    for index, placement in enumerate(manifest.get("placements", [])):
        request = placement.get("world_motion") or {"preset": "none"}
        preset = request.get("preset", "none") if isinstance(request, dict) else request
        if preset == "none":
            continue
        if preset not in {
            "aquatic_bob",
            "aquatic_swim",
            "air_glide",
            "air_hover",
            "ground_wander",
            "ground_forward",
            "ground_hop",
            "aquatic_hover",
            "aquatic_wander",
            "airborne_wander",
            "directed_traverse",
            "airborne_glide",
            "orbit",
            "stationary_bob",
            "terrain_wander",
            "directed_ground_traverse",
            "afloat_forward",          # 2026-09-13 帆船/獨木舟往前漂
            "air_soar",                # 2026-09-13 鳥類快速滑翔
        }:
            failures.append(f"placement #{index}: unsupported world_motion {preset!r}")
            continue
        motion = (result_map.get(index) or {}).get("world_motion") or {}
        motion_objects = motion.get("objects") or []
        # ★地形太陡而改成靜止的不算失敗★(2026-09-12 forest：狐狸站在 45° 坡上)。
        #   world_motion 已經把 action 清掉、物件維持在原地，畫面上照樣看得到。
        skipped_objects = [x for x in motion_objects if x.get("skipped")]
        motion_objects = [x for x in motion_objects if not x.get("skipped")]
        if skipped_objects:
            checked.append({"placement": index, "preset": preset, "skipped": len(skipped_objects),
                            "reasons": sorted({str(x.get("skip_reason")) for x in skipped_objects})})
        if not motion_objects:
            continue
        if int(motion.get("applied_count") or 0) < len(motion_objects):
            failures.append(f"placement #{index}: no applied world-motion objects")
            continue
        for item in motion_objects:
            name = item.get("object")
            action = item.get("action")
            displacement = float(item.get("max_world_displacement_m") or 0.0)
            if not item.get("applied") or displacement < 0.01:   # 同 world_motion.MIN_WORLD_DISPLACEMENT_M(水下 ×0.2 後 bob 約 0.04m)
                failures.append(
                    f"placement #{index} {name}: reported displacement={displacement:.6f}m"
                )
                continue
            if not str(action or "").startswith(f"BI_WORLD_MOTION::{preset}::"):
                failures.append(f"placement #{index} {name}: invalid action {action!r}")
                continue
            contract_version = item.get("world_motion_contract_version")
            legacy_v1_hover = contract_version is None and preset == "aquatic_hover"
            reported_looping = item.get("looping")
            if reported_looping is None and legacy_v1_hover:
                reported_looping = True
            record = {
                "placement": index,
                "object": name,
                "preset": preset,
                "action": action,
                "reported_displacement_m": displacement,
                "world_motion_contract_version": contract_version or 1,
                "looping": bool(reported_looping),
            }
            final_displacement = float(item.get("final_world_displacement_m") or 0.0)
            should_loop = preset in {
                "aquatic_bob", "air_hover", "ground_wander", "ground_hop",
                "aquatic_hover", "aquatic_wander", "airborne_wander",
                "orbit", "stationary_bob",
                "terrain_wander",
            }
            if bool(reported_looping) != should_loop:
                failures.append(
                    f"placement #{index} {name}: looping={reported_looping!r} "
                    f"does not match preset {preset!r}"
                )
                continue
            if preset in {
                "ground_wander", "ground_forward", "ground_hop",
                "terrain_wander", "directed_ground_traverse",
            }:
                if not item.get("terrain_following"):
                    failures.append(
                        f"placement #{index} {name}: ground motion lacks terrain-following proof")
                    continue
                ray_misses = int(item.get("terrain_ray_miss_count") or 0)
                max_gap = float(item.get("max_abs_ground_contact_gap_m") or 0.0)
                max_slope = float(item.get("max_slope_deg") or 0.0)
                terrain_samples = item.get("terrain_samples") or []
                reported_frames = [int(frame) for frame in item.get("frames") or []]
                sampled_frames = [
                    int(sample.get("frame")) for sample in terrain_samples
                    if isinstance(sample, dict) and sample.get("frame") is not None
                ]
                expected_frames = (list(range(reported_frames[0], reported_frames[-1] + 1))
                                   if len(reported_frames) >= 2 else [])
                if (contract_version != 3 or not expected_frames
                        or reported_frames != expected_frames
                        or sampled_frames != expected_frames):
                    failures.append(
                        f"placement #{index} {name}: ground motion requires v3 "
                        f"every-frame terrain audit, got version={contract_version!r}, "
                        f"reported={len(reported_frames)}, terrain={len(sampled_frames)}")
                    continue
                landing_gap = float(item.get("max_abs_landing_contact_gap_m") or max_gap)
                unsafe_gap = landing_gap if preset == "ground_hop" else max_gap
                if ray_misses or unsafe_gap > 0.30 or max_slope > 35.0:
                    failures.append(
                        f"placement #{index} {name}: unsafe ground path "
                        f"ray_miss={ray_misses}, gap={unsafe_gap:.4f}m, slope={max_slope:.2f}deg")
                    continue
                if preset == "ground_hop" and int(item.get("airborne_frame_count") or 0) <= 0:
                    failures.append(
                        f"placement #{index} {name}: ground_hop has no airborne frames")
                    continue
                record.update(
                    terrain_following=True,
                    max_abs_ground_contact_gap_m=unsafe_gap,
                    max_slope_deg=max_slope,
                )
            if should_loop and final_displacement > 1e-4:
                failures.append(
                    f"placement #{index} {name}: closed-loop error="
                    f"{final_displacement:.6f}m"
                )
                continue
            if not should_loop and final_displacement < 0.01:   # 同 world_motion.MIN_WORLD_DISPLACEMENT_M
                failures.append(
                    f"placement #{index} {name}: one-way final displacement="
                    f"{final_displacement:.6f}m"
                )
                continue
            record["reported_final_displacement_m"] = final_displacement
            if snapshot is not None:
                info = objects.get(name)
                if info is None:
                    failures.append(f"placement #{index}: motion anchor missing from snapshot: {name}")
                    continue
                saved_action = (info.get("action") or {}).get("name")
                if saved_action != action:
                    failures.append(
                        f"placement #{index} {name}: action {action!r} -> {saved_action!r}"
                    )
                    continue
                # ★依幀號排序，不能用 dict 順序★(2026-09-13 under_water：快照的鍵是
                #   ['1','120','60']，取 values()[-1] 拿到的是中間幀 —— 環狀位移在中點
                #   離原點最遠，於是被誤判成「閉環誤差 0.386m」而整個 case FAIL)。
                _by_frame = (info.get("matrix_local_by_frame") or {})
                matrices = [matrix for _frame, matrix in
                            sorted(_by_frame.items(), key=lambda kv: int(kv[0]))]
                if len(matrices) < 2:
                    failures.append(f"placement #{index} {name}: fewer than 2 transform samples")
                    continue
                xyz = [(matrix[3], matrix[7], matrix[11]) for matrix in matrices]
                base = xyz[0]
                measured = max(math.sqrt(sum((a - b) ** 2 for a, b in zip(pos, base)))
                               for pos in xyz)
                record["snapshot_displacement_m"] = measured
                # A sparse start/middle/end snapshot can legitimately alias a
                # closed motion's zero crossings (aquatic_bob peaks at quarter
                # frames). The executor report already proves max displacement,
                # and the saved action plus endpoint check proves persistence.
                # One-way motion, however, must visibly separate in these
                # endpoint samples.
                if not should_loop and measured < 0.05:
                    failures.append(
                        f"placement #{index} {name}: snapshot displacement={measured:.6f}m"
                    )
                    continue
                snapshot_final = math.sqrt(
                    sum((a - b) ** 2 for a, b in zip(xyz[-1], base)))
                record["snapshot_final_displacement_m"] = snapshot_final
                if should_loop and snapshot_final > 1e-4:
                    failures.append(
                        f"placement #{index} {name}: snapshot closed-loop error="
                        f"{snapshot_final:.6f}m"
                    )
                    continue
                if not should_loop and snapshot_final < 0.05:
                    failures.append(
                        f"placement #{index} {name}: snapshot one-way final displacement="
                        f"{snapshot_final:.6f}m"
                    )
                    continue
            checked.append(record)
    return {"failures": failures, "checked": checked}


def requested_camera_visibility_audit(placer: dict, manifest: dict,
                                      snapshot: dict | None = None) -> dict:
    """Require every requested add asset to be in the final camera every frame."""
    source = ((snapshot or {}).get("requested_camera_visibility")
              if snapshot is not None else placer.get("camera_visibility")) or {}
    # Extended grid (BI_PLACEMENT_EXTENDED_GRID>1): off-screen placement is allowed,
    # so per-frame "not visible" failures are not a contract violation.
    extended = float(os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1) > 1.0
    # ★逐幀可見格數不足只警告★(2026-10-19 使用者)；缺 audit / frame range 不符仍是硬失敗。
    warnings = [] if extended else list(source.get("failures") or [])
    failures = []
    placement_rows = source.get("placements") or {}
    requested = {
        index for index, placement in enumerate(manifest.get("placements", []))
        if isinstance(placement, dict)
        and int(placement.get("added_this_plan") or 0) > 0
        and placement.get("realized_at", "populate") == "populate"
    }
    represented = {int(index) for index, rows in placement_rows.items() if rows}
    missing = sorted(requested - represented)
    failures.extend(f"placement #{index}: no requested camera anchor audit" for index in missing)
    if source.get("frame_range") != placer.get("frame_range"):
        failures.append(
            f"camera audit frame range={source.get('frame_range')} "
            f"expected={placer.get('frame_range')}")
    return {
        "failures": failures,
        "warnings": warnings,
        "requested": len(requested),
        "represented": len(represented & requested),
        "placements": placement_rows,
    }


def main() -> int:
    args = parse_args()
    if args.ok_marker.exists() or args.ok_marker.is_symlink():
        args.ok_marker.unlink()

    populated = read_json(args.populated)
    fine = read_json(args.fine)
    placer = read_json(args.placer_report)
    manifest = read_json(args.manifest)

    checks: list[dict] = []

    def record(name: str, passed: bool, detail, metrics=None):
        item = {"name": name, "passed": bool(passed), "detail": detail}
        if metrics is not None:
            item["metrics"] = metrics
        checks.append(item)

    # Snapshot and frame-range identity.
    schemas_ok = all(
        snapshot.get("schema_version") == SCHEMA_VERSION
        for snapshot in (populated, fine)
    )
    record("snapshot_schema", schemas_ok, f"required schema={SCHEMA_VERSION}")

    pop_frames = populated.get("scene", {}).get("contract_frames", [])
    fine_frames = fine.get("scene", {}).get("contract_frames", [])
    record(
        "contract_frames",
        bool(pop_frames) and pop_frames == fine_frames,
        {"populated": pop_frames, "fine": fine_frames},
    )
    fine_scene = fine.get("scene", {})
    range_ok = bool(fine_frames) and (
        int(fine_scene.get("frame_start", 0)) == int(min(fine_frames))
        and int(fine_scene.get("frame_end", 0)) == int(max(fine_frames))
    )
    record(
        "fine_frame_range",
        range_ok,
        {
            "scene": [fine_scene.get("frame_start"), fine_scene.get("frame_end")],
            "contract": [min(fine_frames), max(fine_frames)] if fine_frames else None,
        },
    )

    fine_ground = fine.get("fine_ground_motion_audit") or {}
    requested_ground_names = {
        item.get("object")
        for row in placer.get("world_motion", [])
        for item in (row.get("objects") or [])
        if item.get("preset") in {
            "ground_wander", "ground_forward", "ground_hop",
            "terrain_wander", "directed_ground_traverse",
        }
        # ★位移被收掉、改成原地不動的不用 fine 地形 audit★(2026-09-14 arctic：企鵝 #3 在 45° 坡上
        #   world_motion 回報 skipped ⇒ anchor 沒有 bi_world_motion_preset ⇒ snapshot 不會 audit 它 ⇒ 誤判缺 audit)
        and not item.get("skipped") and item.get("applied") is not False
    }
    audited_ground_names = set((fine_ground.get("anchors") or {}).keys())
    ground_failures = list(fine_ground.get("failures") or [])
    missing_ground = sorted(requested_ground_names - audited_ground_names)
    ground_failures.extend(f"missing final fine-terrain audit: {name}" for name in missing_ground)
    record(
        "fine_ground_motion_contact",
        not ground_failures,
        ground_failures[:50] or f"{len(audited_ground_names)} anchors audited every frame",
        {"requested": len(requested_ground_names), "audited": len(audited_ground_names)},
    )

    pop_objects = populated.get("objects", {})
    fine_objects = fine.get("objects", {})

    # Genuine terrain requirements.  A populated coarse renamed to fine fails
    # here because it has no non-empty, visible primary *_fine terrain.
    primary_bases = primary_terrain_bases(populated, fine)
    fine_details = {}
    fine_ok = bool(primary_bases)
    for base in primary_bases:
        ok, detail = nonempty_visible_mesh(fine_objects, f"{base}_fine")
        fine_details[base] = detail
        fine_ok = fine_ok and ok
    if not primary_bases:
        fine_details["missing"] = "missing primary fine terrain"
    record(
        "genuine_opaque_fine",
        fine_ok,
        {"primary_bases": primary_bases, "fine": fine_details},
    )
    coarse_details = {}
    coarse_hidden = bool(primary_bases)
    for base in primary_bases:
        ok, detail = coarse_terrain_hidden(fine_objects, base)
        coarse_details[base] = detail
        coarse_hidden = coarse_hidden and ok
    record(
        "coarse_opaque_hidden",
        coarse_hidden,
        coarse_details or "primary coarse terrain must be render-hidden after fine_terrain",
    )

    for base in ("liquid", "atmosphere"):
        if base not in pop_objects:
            record(f"{base}_fine", True, f"not required: populated scene has no {base}")
            continue
        fine_name = f"{base}_fine"
        fine_ok, fine_detail = nonempty_visible_mesh(fine_objects, fine_name)
        base_info = fine_objects.get(base)
        base_hidden = bool(
            base_info and base_info.get("hide_render") and not base_info.get("render_visible")
        )
        record(
            f"{base}_fine",
            fine_ok and base_hidden,
            {"fine": fine_detail, "coarse_hidden": base_hidden},
        )

    # Camera identity and sampled evaluated pose/lens.
    pop_camera = populated.get("camera", {})
    fine_camera = fine.get("camera", {})
    camera_errors = []
    if not pop_camera.get("active") or pop_camera.get("active") != fine_camera.get("active"):
        camera_errors.append(
            f"active camera {pop_camera.get('active')!r} -> {fine_camera.get('active')!r}"
        )
    if set(pop_camera.get("samples", {})) != set(fine_camera.get("samples", {})):
        camera_errors.append("camera sample frame keys differ")
    else:
        for frame, before in pop_camera.get("samples", {}).items():
            after = fine_camera["samples"][frame]
            for field in (
                "matrix_world",
                "lens",
                "sensor_width",
                "sensor_height",
                "sensor_fit",
                "clip_start",
                "clip_end",
            ):
                if not close_values(before.get(field), after.get(field)):
                    camera_errors.append(f"frame {frame}: camera {field} changed")
    if pop_camera.get("rig_chain") != fine_camera.get("rig_chain"):
        camera_errors.append("camera parent/action/constraint chain changed")
    record(
        "camera_invariance",
        not camera_errors,
        camera_errors[:30] or f"{len(pop_camera.get('samples', {}))} sampled poses unchanged",
        {"difference_count": len(camera_errors)},
    )

    # All render-relevant pre-existing non-terrain objects must survive fine
    # terrain unchanged. Unlinked, invisible master-pool orphans are allowed to
    # be purged by Blender when fine/scene.blend is saved.
    bases = terrain_bases(populated, fine)
    nonterrain = relevant_nonterrain_objects(populated, bases)
    all_pop_nonterrain = {
        name for name, info in pop_objects.items()
        if not is_terrain_object(name, info, bases)
    }
    missing_objects = sorted(set(nonterrain) - set(fine_objects))
    object_differences = []
    identity_fields = (
        "type",
        "parent",
        "parent_type",
        "parent_bone",
        "hide_render",
        "hide_viewport",
        "hide_get",
        "render_visible",
        "action",
        "constraints",
    )
    for name, before in nonterrain.items():
        after = fine_objects.get(name)
        if after is None:
            continue
        # ★鷹架不是最終幾何，連 identity 都不必比★(2026-09-12)
        #   placeholders: 底下的盒子與(樹/灌木的)骨架 ★polygons=0★，渲染不出東西；
        #   fine 階段把沒被 populate 的藏起來是正確行為(見 is_scaffolding docstring)。
        #   ⚠️ e1ae70f 就是為了這個加的豁免，但放在 identity_fields 迴圈★之後★ ⇒
        #      render_visible 早就被記成差異，豁免永遠來不及。0912 有四個 case
        #      (snowy_mountain/desert/river/plain)全死在這一行之差。
        if is_scaffolding(before):
            continue
        for field in identity_fields:
            if not close_values(before.get(field), after.get(field)):
                object_differences.append(f"{name}: {field} changed")
        # ★粒子母本不是場景幾何★(2026-09-11)：particleassets: 底下的東西是粒子系統的
        #   instance 來源，creature_col_to_particle_col 本來就把它們搬到 z-100 藏起來，
        #   fine_terrain 再求值時位置會再動。它們不會被直接 render，位置是實作細節。
        #   0911 coral_reef：我們換進去的魚群母本(swapped:...)害 step 11 判 FAIL、禁止出圖。
        if is_particle_master(before):
            continue
        # Batch opt-in: underwater baselines' own fine stage moves hidden creature
        # masters (-100/+100 root/armature, net zero) and re-evaluates keyframed
        # fish schools, which failed every coral_reef/under_water case (0911).
        # Identity fields above are still checked for these objects.
        # ★2026-10-20 起預設開★：1019 重跑 09/10 都死在「有 world_motion 的魚 transform changed at frame 1」，
        #   會游的魚本來就會動；BI_INVARIANCE_IGNORE_HIDDEN_ANIMATED=0 可退回嚴格比對。
        if os.environ.get("BI_INVARIANCE_IGNORE_HIDDEN_ANIMATED", "1") == "1" and (
                not before.get("render_visible")
                or (before.get("action") or {}).get("fcurve_count")):
            continue
        before_matrices = before.get("matrix_local_by_frame", {})
        after_matrices = after.get("matrix_local_by_frame", {})
        if set(before_matrices) != set(after_matrices):
            object_differences.append(f"{name}: sampled transform frames changed")
        else:
            for frame, before_matrix in before_matrices.items():
                if not close_values(before_matrix, after_matrices[frame]):
                    object_differences.append(f"{name}: transform changed at frame {frame}")
                    break
    inventory_errors = [f"missing: {name}" for name in missing_objects] + object_differences
    extra_nonterrain = sorted(
        name
        for name, info in fine_objects.items()
        if name not in pop_objects and not is_terrain_object(name, info, bases)
    )
    record(
        "nonterrain_invariance",
        not inventory_errors,
        inventory_errors[:50] or f"{len(nonterrain)} objects unchanged",
        {
            "compared": len(nonterrain),
            "missing": len(missing_objects),
            "different": len(object_differences),
            "ignored_nonrender_orphans": len(all_pop_nonterrain - set(nonterrain)),
            "fine_stage_additions": extra_nonterrain[:30],
        },
    )

    # Particle systems, including the master collection and baked cache window,
    # must survive fine terrain byte-for-fact at the contract level.
    pop_particles = populated.get("particles", {})
    fine_particles = fine.get("particles", {})
    particle_errors = []
    for key in sorted(set(pop_particles) - set(fine_particles)):
        particle_errors.append(f"missing particle system: {key}")
    for key in sorted(set(fine_particles) - set(pop_particles)):
        particle_errors.append(f"unexpected particle system: {key}")
    for key in sorted(set(pop_particles) & set(fine_particles)):
        before = pop_particles[key]
        after = fine_particles[key]
        if before.get("settings") != after.get("settings"):
            changed = sorted(
                field
                for field in set(before.get("settings", {})) | set(after.get("settings", {}))
                if not close_values(
                    before.get("settings", {}).get(field),
                    after.get("settings", {}).get(field),
                )
            )
            particle_errors.append(f"{key}: settings changed: {changed}")
        if before.get("cache") != after.get("cache"):
            particle_errors.append(f"{key}: cache changed")
    record(
        "particle_invariance",
        not particle_errors,
        particle_errors[:50] or f"{len(pop_particles)} systems unchanged",
        {"difference_count": len(particle_errors)},
    )

    # Placer feasibility is a hard gate.  The old xmas run deliberately has a
    # missing WoodenSleighFactory and must never render a white proxy box.
    placer_audit = placer_delivery_audit(placer, manifest)
    placer_failures = placer_audit["placer_failures"]
    record(
        "placer_feasibility",
        not placer_failures,
        placer_failures[:50] or f"{placer_audit['result_count']} results feasible",
        {"failure_count": len(placer_failures)},
    )

    try:
        report_out = Path(placer.get("out_blend", "")).resolve()
        snapshot_populated = Path(populated.get("blend", "")).resolve()
        report_manifest = Path(placer.get("manifest", "")).resolve()
        input_manifest = args.manifest.resolve()
        provenance = placer.get("provenance") or {}
        manifest_id = provenance.get("manifest") or {}
        output_id = provenance.get("output_blend") or {}
        populated_stat = snapshot_populated.stat()
        manifest_digest = hashlib.sha256(input_manifest.read_bytes()).hexdigest()
        provenance_ok = bool(
            not placer.get("dry_run")
            and report_out == snapshot_populated
            and report_manifest == input_manifest
            and manifest_id.get("path") == str(input_manifest)
            and manifest_id.get("sha256") == manifest_digest
            and output_id.get("path") == str(snapshot_populated)
            and output_id.get("size") == populated_stat.st_size
            and output_id.get("mtime_ns") == populated_stat.st_mtime_ns
            and bool(pop_frames)
            and placer.get("frame_range") == [min(pop_frames), max(pop_frames)]
        )
        provenance_detail = {
            "dry_run": placer.get("dry_run"),
            "report_out": str(report_out),
            "snapshot_populated": str(snapshot_populated),
            "report_manifest": str(report_manifest),
            "input_manifest": str(input_manifest),
            "frame_range": placer.get("frame_range"),
            "manifest_sha256": manifest_id.get("sha256"),
        }
    except (OSError, TypeError, ValueError) as error:
        provenance_ok = False
        provenance_detail = str(error)
    record("placer_provenance", provenance_ok, provenance_detail)

    visibility_audit = requested_asset_visibility_audit(placer, manifest, fine_objects)
    delivery_failures = placer_audit["delivery_failures"] + visibility_audit["failures"]
    checked_deliveries = placer_audit["checked_deliveries"]
    record(
        "requested_asset_delivery",
        not delivery_failures,
        delivery_failures[:50] or f"{checked_deliveries} generated/added placements delivered",
        {"checked": checked_deliveries, "failure_count": len(delivery_failures),
         "visible_collections": visibility_audit["collections"]},
    )

    motion_audit = world_motion_audit(placer, manifest, fine)
    record(
        "world_motion_delivery",
        not motion_audit["failures"],
        motion_audit["failures"][:50] or
        f"{len(motion_audit['checked'])} world-motion anchors validated",
        {"checked": motion_audit["checked"],
         "failure_count": len(motion_audit["failures"])},
    )

    camera_visibility = requested_camera_visibility_audit(placer, manifest, fine)
    record(
        "requested_camera_visibility",
        not camera_visibility["failures"],
        camera_visibility["failures"][:50] or
        f"{camera_visibility['represented']} requested placements audited "
        f"({len(camera_visibility['warnings'])} visibility warnings)",
        {"requested": camera_visibility["requested"],
         "represented": camera_visibility["represented"],
         "visibility_warnings": camera_visibility["warnings"][:50],
         "placements": camera_visibility["placements"]},
    )

    passed = all(check["passed"] for check in checks)
    result = {
        "contract_version": 1,
        "status": "PASS" if passed else "FAIL",
        "render_allowed": passed,
        "populated_scene": populated.get("blend"),
        "final_scene": fine.get("blend"),
        "final_scene_stat": fine.get("file_stat"),
        "mesher": args.mesher,
        "frames": fine_frames,
        "inputs": {
            "populated_snapshot": str(args.populated.resolve()),
            "fine_snapshot": str(args.fine.resolve()),
            "placer_report": str(args.placer_report.resolve()),
            "manifest": str(args.manifest.resolve()),
        },
        "summary": {
            "checks": len(checks),
            "passed": sum(check["passed"] for check in checks),
            "failed": sum(not check["passed"] for check in checks),
        },
        "checks": checks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if passed:
        args.ok_marker.parent.mkdir(parents=True, exist_ok=True)
        args.ok_marker.write_text(
            f"PASS\ncontract={args.out.resolve()}\nfinal_scene={fine.get('blend')}\n"
            f"size={fine.get('file_stat', {}).get('size')}\n"
            f"mtime_ns={fine.get('file_stat', {}).get('mtime_ns')}\n",
            encoding="utf-8",
        )
        print(f"[contract] PASS -> {args.ok_marker}")
        return 0

    print(f"[contract] FAIL ({result['summary']['failed']}/{len(checks)} checks)")
    for check in checks:
        if not check["passed"]:
            print(f"[contract]   FAIL {check['name']}: {check['detail']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

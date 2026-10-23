#!/usr/bin/env python
"""Build occlusion-free planning maps for surface and 3D volume placements.

The Blender process evaluates a deterministic screen grid with the canonical
visible-surface projection contract.  Each cell records both its final-camera
coordinate and actual world-space hit.  A pure Pillow renderer then draws the
camera fan, semantic feasibility raster, existing placeholder AABBs, current
requested boxes, and connected feasible-region labels.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from copy import deepcopy
from pathlib import Path

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stage_exec"))

import placement_contract as plc  # noqa: E402
import placement_modes as pm  # noqa: E402
from placement_region_contract import (  # noqa: E402
    AIRBORNE_MIN_DEPTH_M,
    aabb_overlap_fraction,
    ideal_depth_for_readable_size,
    all_map_managed_instances,
    best_effort_placement,
    all_conform_instances,
    conform_failed_instances,
    connected_feasible_regions,
    estimate_region_capacity,
    grid_points,
    is_soft_scene_obstacle,
    large_ground_relaxation_attempts,
    outdoor_broad_search_target_capacity,
    map_managed_failed_instances,
    placeholder_collection_claimed_by_plan,
    requires_full_footprint_structure_support,
    rotated_aabb_bounds,
    uses_3d_volume_map,
    uses_surface_map,
)
from placement_surface_evaluator import VisibleSurfaceEvaluator  # noqa: E402
import placeholder_population as php  # noqa: E402
import surface_raycast_cache as srcache  # noqa: E402
import surface_registry as sr  # noqa: E402
from render_placement_region_map import render_region_map  # noqa: E402


PH_COLL_RE = re.compile(r"placeholders:((.*)\((\d*)\))")
YAW_CANDIDATES = tuple(index * math.pi / 2.0 for index in range(4))
VOLUME_YAW_CANDIDATES = YAW_CANDIDATES
INDOOR_SCENE_NAMES = {
    "bedroom", "living-room", "kitchen", "bathroom", "dining-room"}
INDOOR_SURFACE_TYPES = (
    "floor", "tabletop", "countertop", "shelf", "bed_top", "seat")
INDOOR_SURFACE_FALLBACKS = {
    # Coarse semantic preference: related raised surfaces first, floor last so
    # every requested object can still enter the scene.  The selected fallback
    # category is written to the map/plan for review instead of being hidden.
    "shelf": ("tabletop", "countertop", "floor"),
    "seat": ("bed_top", "tabletop", "floor"),
    "countertop": ("tabletop", "shelf", "floor"),
    "tabletop": ("countertop", "shelf", "floor"),
    "bed_top": ("seat", "floor"),
    "floor": (),
}
FULL_FRAME_BOUNDS = [0.05, 0.05, 0.95, 0.90]
INDOOR_TOPDOWN_SUPPORT_CELL_SIZE_M = float(os.environ.get(
    "PLACEMENT_INDOOR_TOPDOWN_CELL_SIZE_M", "0.60"))
INDOOR_TOPDOWN_SUPPORT_MAX_CELLS = int(os.environ.get(
    "PLACEMENT_INDOOR_TOPDOWN_MAX_CELLS", "6"))


def _roots(collection):
    return [obj for obj in collection.objects if obj.parent is None]


def _proxy_dimensions_from_object(obj) -> tuple[float, float, float] | None:
    raw = obj.get("bi_proxy_dimensions_m") if hasattr(obj, "get") else None
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw)) if isinstance(raw, str) else raw
        dimensions = plc.normalize_proxy_dimensions(parsed)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if dimensions is None:
        return None
    return tuple(float(value) for value in dimensions)


def _placeholder_proxy_dimensions(
        classname: str, root) -> tuple[tuple[float, float, float] | None, str | None]:
    dimensions = _proxy_dimensions_from_object(root)
    if dimensions is not None:
        source = None
        if hasattr(root, "get"):
            source = root.get("bi_proxy_dimension_source")
        return dimensions, str(source or "existing_placeholder_metadata")
    dimensions = plc.inferred_placeholder_proxy_dimensions(factory=classname)
    if dimensions is None:
        return None, None
    return (
        tuple(float(value) for value in dimensions),
        plc.inferred_placeholder_proxy_dimension_source(factory=classname),
    )


def proxy_half_extents(factory: str, declared_dimensions=None,
                       fallback_dimensions=None
                       ) -> tuple[float, float, float]:
    """Measure an existing proxy, or use the Director's conservative estimate."""
    collections = [collection for collection in bpy.data.collections
                   if (match := PH_COLL_RE.fullmatch(collection.name))
                   and match.group(2) == factory]
    roots = _roots(collections[0]) if collections else []
    if not roots:
        declared = plc.normalize_proxy_dimensions(declared_dimensions)
        if declared is None:
            declared = plc.normalize_proxy_dimensions(fallback_dimensions)
        if declared is not None:
            return tuple(float(value) / 2.0 for value in declared)
        return (1.0, 1.0, 1.0)
    for root in roots:
        proxy_dimensions = _proxy_dimensions_from_object(root)
        if proxy_dimensions is not None:
            return tuple(max(float(value) / 2.0, 0.1)
                         for value in proxy_dimensions)
    points = [Vector(corner) for corner in roots[0].bound_box]
    half_x = max(abs(point.x) for point in points) * abs(roots[0].scale.x)
    half_y = max(abs(point.y) for point in points) * abs(roots[0].scale.y)
    half_z = max(abs(point.z) for point in points) * abs(roots[0].scale.z)
    return (
        max(float(half_x), 0.1),
        max(float(half_y), 0.1),
        max(float(half_z), 0.1),
    )


def footprint_half_extents(factory: str, declared_dimensions=None
                           ) -> tuple[float, float]:
    """Compatibility helper for the existing visible-surface map contract."""
    return proxy_half_extents(factory, declared_dimensions)[:2]


def scene_obstacles(*, include_render_scatter: bool = False,
                    plan: dict | None = None,
                    cameras: list | None = None) -> list[dict]:
    """Collect explicit placeholders and evaluated asset instances as boxes.

    Dense vegetation and rocks are often Geometry Nodes instances rather than
    ordinary scene objects.  The planning map must expose them without relying
    on their rendered appearance, so consume depsgraph instances directly.
    Terrain, particles, colliders, and hidden asset-library masters are not
    obstacles here.
    """
    obstacles = []
    seen = set()

    def append_box(*, obj, matrix, label, source, map_modes="all",
                   population_intent=None, population_cull=None,
                   proxy_dimensions=None, proxy_dimension_source=None):
        corners = None
        dimensions = None
        if proxy_dimensions is not None:
            try:
                dimensions = plc.normalize_proxy_dimensions(proxy_dimensions)
            except (TypeError, ValueError):
                dimensions = None
            if dimensions is not None:
                half = [float(value) / 2.0 for value in dimensions]
                corners = [
                    matrix @ Vector((sx * half[0], sy * half[1], sz * half[2]))
                    for sx in (-1, 1)
                    for sy in (-1, 1)
                    for sz in (-1, 1)
                ]
        if corners is None:
            try:
                corners = [matrix @ Vector(corner) for corner in obj.bound_box]
            except (AttributeError, TypeError, ValueError):
                return
        if not corners:
            return
        world_bounds = [
            min(point.x for point in corners),
            min(point.y for point in corners),
            min(point.z for point in corners),
            max(point.x for point in corners),
            max(point.y for point in corners),
            max(point.z for point in corners),
        ]
        if any(not math.isfinite(float(value)) for value in world_bounds):
            return
        key = tuple(round(float(value), 3) for value in world_bounds)
        if key in seen:
            return
        seen.add(key)
        row = {
            "object": obj.name,
            "label": label,
            "source": source,
            "map_modes": map_modes,
            "world_bounds": [float(value) for value in world_bounds],
            "world_xy_bounds": [float(world_bounds[index])
                                for index in (0, 1, 3, 4)],
        }
        if population_intent is not None:
            row["population_intent"] = population_intent
        if population_cull:
            row["population_cull"] = {
                key: population_cull.get(key)
                for key in ("will_populate", "dist_m", "vis_dist_m",
                            "coarse_cull_visible", "distance_source")
                if key in population_cull
            }
        if dimensions is not None:
            row["proxy_dimensions_m"] = [float(value) for value in dimensions]
            row["proxy_dimension_source"] = str(
                proxy_dimension_source or "placeholder_fallback")
        obstacles.append(row)

    placeholder_infos = []
    for collection in bpy.data.collections:
        match = PH_COLL_RE.fullmatch(collection.name)
        if not match:
            continue
        _full, classname, seed = match.groups()
        roots = _roots(collection)
        if not roots:
            continue
        placeholder_infos.append({
            "collection": collection,
            "classname": classname,
            "seed": seed,
            "roots": roots,
        })

    population_visible_rows_by_name = {}
    if cameras:
        population_groups = {}
        for info in placeholder_infos:
            cull = pm.populate_cull_for(info["classname"])
            key = (cull.get("dist_cull", 200), cull.get("vis_cull", 0))
            population_groups.setdefault(key, []).extend(info["roots"])
        for (dist_cull, vis_cull), roots in sorted(
                population_groups.items(), key=lambda item: str(item[0])):
            try:
                rows = php.population_target_metadata(
                    roots,
                    cameras,
                    dist_cull=dist_cull,
                    vis_cull=vis_cull,
                    include_all=False,
                )
            except Exception as error:
                print(
                    f"[region-map] ⚠️ placeholder cull failed for "
                    f"dist_cull={dist_cull!r}, vis_cull={vis_cull!r}: "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
                rows = {}
            population_visible_rows_by_name.update(rows)
        print(
            f"[region-map] coarse population cull: "
            f"{len(population_visible_rows_by_name)} visible target(s) across "
            f"{sum(len(info['roots']) for info in placeholder_infos)} "
            "placeholder root(s)",
            flush=True,
        )

    for info in placeholder_infos:
        collection = info["collection"]
        classname = info["classname"]
        seed = info["seed"]
        roots = info["roots"]
        claimed = placeholder_collection_claimed_by_plan(
            plan,
            collection=collection.name,
            factory=classname,
            seed=seed,
        )
        force_final_names = {
            root.name for root in roots
            if root.get("bi_code_from") or root.get("bi_requested_code_from")
        }
        population_rows = {
            root.name: dict(population_visible_rows_by_name[root.name])
            for root in roots
            if root.name in population_visible_rows_by_name
        }
        if claimed:
            if cameras:
                population_rows = php.force_population_target_metadata(
                    roots,
                    cameras,
                    population_rows,
                )
            else:
                population_rows = {
                    root.name: {
                        "object": root.name,
                        "will_populate": True,
                        "coarse_cull_visible": False,
                        "dist_m": None,
                        "vis_dist_m": None,
                        "distance_source": "no_camera_available",
                    }
                    for root in roots
                }
        for root in roots:
            row = population_rows.get(root.name)
            root_final = root.name in force_final_names
            if root_final and row is None:
                row = {
                    "object": root.name,
                    "will_populate": True,
                    "coarse_cull_visible": False,
                    "dist_m": None,
                    "vis_dist_m": None,
                    "distance_source": "requested_placeholder",
                }
            if not row and not claimed and not root_final:
                continue
            if (row and not row.get("will_populate", True)
                    and not claimed and not root_final):
                continue
            proxy_dimensions, proxy_dimension_source = _placeholder_proxy_dimensions(
                classname, root)
            intent = (
                "requested_add" if root_final else
                "requested_replace" if claimed else
                "baseline_visible")
            append_box(
                obj=root, matrix=root.matrix_world,
                label=classname, source=collection.name,
                population_intent=intent,
                population_cull=row,
                proxy_dimensions=proxy_dimensions,
                proxy_dimension_source=proxy_dimension_source,
            )

    restored_visibility = []
    if include_render_scatter:
        # Infinigen keeps the scatter collection hidden in the interactive
        # viewport but render-enabled.  A normal view-layer depsgraph therefore
        # contains only terrain/liquid and silently omits every reef instance.
        # Temporarily expose that collection only while extracting AABBs; restore
        # it before the canonical surface raycasts, and never save the source.
        for collection in bpy.data.collections:
            if collection.name == "scatter" and collection.hide_viewport:
                restored_visibility.append((collection, True))
                collection.hide_viewport = False
        if restored_visibility:
            bpy.context.view_layer.update()

    deps = bpy.context.evaluated_depsgraph_get()
    for instance in deps.object_instances:
        if not instance.is_instance:
            continue
        obj = instance.instance_object or instance.object
        if obj is None or obj.type != "MESH" or obj.hide_render:
            continue
        collections = {collection.name for collection in obj.users_collection}
        if collections & {"terrain", "colliders", "particles"}:
            continue
        if any(name.startswith("placeholders:") for name in collections):
            continue
        label = obj.name.split("(", 1)[0].split(".", 1)[0]
        append_box(
            obj=obj, matrix=instance.matrix_world,
            label=label, source="evaluated_instance")

    # Directly linked reef assets (individual corals/fish/rocks) are ordinary
    # depsgraph objects rather than instances.  They were intentionally absent
    # from the old ground map, so expose them only to the submerged 3D branch.
    for instance in deps.object_instances:
        if instance.is_instance:
            continue
        obj = instance.object
        if obj is None or obj.type != "MESH" or obj.hide_render:
            continue
        collections = {collection.name for collection in obj.users_collection}
        name_lower = obj.name.lower()
        if collections & {"terrain", "colliders", "particles"}:
            continue
        if any(name.startswith(("placeholders:", "assets:"))
               for name in collections):
            continue
        if (obj.name in {"OpaqueTerrain", "liquid", "atmosphere"}
                or name_lower.startswith(("scatter:", "liquid", "atmosphere"))):
            continue
        label = obj.name.split("(", 1)[0].split(".", 1)[0]
        append_box(
            obj=obj, matrix=instance.matrix_world,
            label=label, source="evaluated_scene_object",
            map_modes="submerged")
    for collection, was_hidden in restored_visibility:
        collection.hide_viewport = was_hidden
    if restored_visibility:
        bpy.context.view_layer.update()
    return obstacles


def _intersects(bounds, area) -> bool:
    return not (
        bounds[2] < area[0] or bounds[0] > area[2]
        or bounds[3] < area[1] or bounds[1] > area[3]
    )


def _proxy_intersects_obstacle(world_position, half_extents, obstacle) -> bool:
    half_x, half_y = map(float, half_extents)
    radius = math.hypot(half_x, half_y)
    bounds = obstacle["world_xy_bounds"]
    return not (
        world_position[0] + radius <= bounds[0]
        or world_position[0] - radius >= bounds[2]
        or world_position[1] + radius <= bounds[1]
        or world_position[1] - radius >= bounds[3]
    )


# 水下/空中位移已經整體放慢 0.2 倍(world_motion.AQUATIC_SPEED_FACTOR)，掃過範圍同步縮小，
#   否則 12cm 的蝴蝶會帶著 4m 的 envelope 被推到 20m 外(2026-09-12)。
# ⚠️ 2026-09-13：air_glide 已經不套慢速因子(鳥要真的往前飛) ⇒ 這裡改成逐 preset 問。
try:
    from world_motion import speed_factor_for as _motion_speed_factor
except ImportError:
    def _motion_speed_factor(preset: str) -> float:
        return 1.0 if str(preset) in {"air_glide", "airborne_glide"} else 0.2


def _submerged_motion_clearance(edit: dict) -> tuple[float, float, float]:
    """Conservative swept-envelope padding for supported underwater motion."""
    request = edit.get("world_motion") or {}
    preset = str(request.get("preset") or "none")
    intensity = str(request.get("intensity") or "subtle")
    params = request.get("params") or {}
    speed = float(params.get("speed_scale", 1.0))
    scale = {"subtle": 0.6, "normal": 1.0, "strong": 1.6}.get(
        intensity, 0.6) * 1.15 * speed * _motion_speed_factor(preset)
    if preset == "aquatic_swim":
        travel = 3.0 * scale
        return travel, travel, 0.18 * scale
    if preset == "aquatic_bob":
        return 0.0, 0.0, 0.32 * scale
    return 0.0, 0.0, 0.0


def _ground_motion_clearance(edit: dict) -> tuple[float, float, float]:
    """★地面位移也要預留掃過範圍★(2026-09-13 desert：兩隻穿山甲各自往前走 2.3m，
    第 34~58 幀撞在一起 ⇒ placer 的動態碰撞判死整個 case)。原本只有空中/水下有預留。
    """
    request = edit.get("world_motion") or {}
    preset = str(request.get("preset") or "none")
    intensity = str(request.get("intensity") or "subtle")
    params = request.get("params") or {}
    speed = float(params.get("speed_scale", 1.0))
    scale = {"subtle": 0.6, "normal": 1.0, "strong": 1.6}.get(intensity, 0.6) * 1.15 * speed
    if preset in {"ground_forward", "directed_ground_traverse"}:
        travel = 2.2 * scale
        return travel, travel, 0.0
    if preset in {"ground_wander", "terrain_wander"}:
        # 2026-09-14 改成原地慢慢轉(位置只繞 2cm)：不用再預留 2r 的路徑，只留轉身時身體掃過的一點餘量
        travel = 0.25
        return travel, travel, 0.0
    if preset == "ground_hop":
        return 0.0, 0.0, 0.45 * scale
    if preset == "afloat_forward":
        travel = 6.0 * scale
        return travel, travel, 0.0
    return 0.0, 0.0, 0.0


def _airborne_motion_clearance(edit: dict) -> tuple[float, float, float]:
    """Conservative swept-envelope padding for supported airborne motion."""
    request = edit.get("world_motion") or {}
    preset = str(request.get("preset") or "none")
    intensity = str(request.get("intensity") or "subtle")
    params = request.get("params") or {}
    speed = float(params.get("speed_scale", 1.0))
    scale = {"subtle": 0.6, "normal": 1.0, "strong": 1.6}.get(
        intensity, 0.6) * 1.15 * speed * _motion_speed_factor(preset)
    if preset == "air_soar":
        return 14.0 * scale, 14.0 * scale, 0.25 * scale
    if preset in {"air_glide", "airborne_glide"}:
        # world_motion 的 distance 基數(6.0) + 淨爬升 0.3×distance
        return 6.0 * scale, 6.0 * scale, (0.50 + 0.30 * 6.0) * scale
    if preset in {"airborne_wander", "directed_traverse"}:
        return 3.0 * scale, 3.0 * scale, 0.60 * scale
    if preset == "orbit":
        return 1.0 * scale, 1.0 * scale, 0.15 * scale
    if preset in {"air_hover", "stationary_bob"}:
        return 0.0, 0.0, 0.35 * scale
    return 0.0, 0.0, 0.0


def annotate_projected_size(scene, camera, data: dict) -> None:
    """★每個可行格子記下「放下去會佔畫面多少」★(2026-09-11)。

    先前只要落在畫面內就算過關，完全沒有大小概念，於是 0911 實測出現兩種壞構圖：
      ・太遠太小：石燈籠 45-52m 佔畫面 0.3%、野餐桌 85m 佔 0.5% —— 在畫面內但看不見
      ・太近擋鏡頭：帆船 13.7m 佔 32%、水晶柱 4.3m 佔 27.7%
    有了這個數字，選格子時才能挑「讀得出來又不擋鏡頭」的那一格。
    """
    proxy = data.get("proxy_dimensions_m")
    if not proxy or len(proxy) != 3:
        return
    extents = tuple(float(value) / 2.0 for value in proxy)
    for cell in data.get("cells", []):
        if cell.get("status") != "feasible":
            continue
        position = cell.get("candidate_world_position") or cell.get("world_position")
        if not position:
            continue
        yaws = cell.get("feasible_yaws") or [0.0]
        bounds = rotated_aabb_bounds(
            [float(value) for value in position], extents, yaw=float(yaws[0]))
        screen = _project_box_bounds(scene, camera, bounds)
        width = max(0.0, float(screen[2]) - float(screen[0]))
        height = max(0.0, float(screen[3]) - float(screen[1]))
        cell["projected_major_fraction"] = round(max(width, height), 6)
        cell["projected_area_fraction"] = round(width * height, 6)
        # ★地面/水面格子也要有投影盒★(2026-09-12 try_3 抓到)：volume 格子在深度階梯裡
        #   就寫了 candidate_screen_bounds，surface 格子沒有 ⇒ frame_tier 退回用「點」
        #   判斷 ⇒ x=0.01 算完全在畫面內 ⇒ 10 隻紅鶴 4 隻仍貼在極邊。這裡反正已經
        #   投影過了，存起來就好(volume 格子用 setdefault 保留原值)。
        cell.setdefault("candidate_screen_bounds",
                        [round(float(value), 6) for value in screen])


def _box_inside_camera(scene, camera, bounds) -> bool:
    """Check the final shifted proxy AABB, not its unshifted support hit."""
    from bpy_extras.object_utils import world_to_camera_view

    corners = [world_to_camera_view(scene, camera, Vector((x, y, z)))
               for x in (bounds[0], bounds[3])
               for y in (bounds[1], bounds[4])
               for z in (bounds[2], bounds[5])]
    # Extended grid: off-screen placement is allowed, only "in front of the camera".
    if plc.extended_search_bounds() is not None:
        return all(point.z > 0.0 for point in corners)
    # BI_PARTIAL_IN_FRAME_OK=1 (batch opt-in): being partly in frame is enough.
    if os.environ.get("BI_PARTIAL_IN_FRAME_OK") == "1":
        from placement_surface_evaluator import ndc_box_touches_frame
        return ndc_box_touches_frame(corners)
    for point in corners:
        if not (0.0 <= point.x <= 1.0
                and 0.0 <= point.y <= 1.0 and point.z > 0.0):
            return False
    return True


def _project_box_bounds(scene, camera, bounds) -> list[float]:
    from bpy_extras.object_utils import world_to_camera_view

    points = [world_to_camera_view(scene, camera, Vector((x, y, z)))
              for x in (bounds[0], bounds[3])
              for y in (bounds[1], bounds[4])
              for z in (bounds[2], bounds[5])]
    return [
        float(min(point.x for point in points)),
        float(min(point.y for point in points)),
        float(max(point.x for point in points)),
        float(max(point.y for point in points)),
    ]


class TerrainClearanceEvaluator:
    """Cached vertical clearance against the actual rendered terrain mesh."""

    def __init__(self, scene):
        self.deps = bpy.context.evaluated_depsgraph_get()
        self.objects = [
            obj for obj in bpy.data.objects
            if (obj.type == "MESH" and not obj.hide_render
                and obj.name.startswith("OpaqueTerrain")
                and "collider" not in obj.name.lower())
        ]
        self.cache = {}

    def height(self, x: float, y: float) -> float | None:
        key = (round(float(x), 3), round(float(y), 3))
        if key in self.cache:
            return self.cache[key]
        best = None
        origin = Vector((float(x), float(y), 1000.0))
        direction = Vector((0.0, 0.0, -1.0))
        for obj in self.objects:
            evaluated = obj.evaluated_get(self.deps)
            inverse = obj.matrix_world.inverted()
            local_origin = inverse @ origin
            local_direction = (inverse.to_3x3() @ direction).normalized()
            try:
                ok, location, _normal, _face = evaluated.ray_cast(
                    local_origin, local_direction, distance=2000.0)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if not ok:
                continue
            world = obj.matrix_world @ location
            best = float(world.z) if best is None else max(best, float(world.z))
        self.cache[key] = best
        return best

    def clears(self, bounds, *, margin: float = 0.25) -> bool:
        bottom = float(bounds[2])
        for x in (bounds[0], bounds[3]):
            for y in (bounds[1], bounds[4]):
                height = self.height(x, y)
                if height is not None and bottom <= height + float(margin):
                    return False
        return True


def _submerged_depths(*, surface_distance, surface_name, reference_depth,
                      proxy_extents, motion_clearance) -> list[float]:
    """Rank deterministic water-column samples from the camera outward."""
    reserve = math.sqrt(sum((float(proxy_extents[index])
                             + float(motion_clearance[index])) ** 2
                            for index in range(3)))
    # ★水下那條也有自己的 8.0 下限★(2026-09-12 try_3 抓到)：只改了空中的，
    #   coral_reef 河豚理想 6.2m、海馬理想 3.0m，階梯排好序了卻只剩 [8.0] 一階可試。
    minimum = max(AIRBORNE_MIN_DEPTH_M, reserve + 2.0)
    maximum = 70.0
    if surface_distance is not None and surface_name != "liquid":
        maximum = min(maximum, float(surface_distance) - reserve - 0.5)
    elif surface_distance is not None:
        maximum = min(maximum, float(surface_distance) - reserve - 0.5)
    if maximum < minimum:
        return []
    step = 2.0
    count = int(math.floor((maximum - minimum) / step))
    depths = [minimum + step * index for index in range(count + 1)]
    return sorted(depths)


def _airborne_depths(*, surface_distance, reference_depth,
                     proxy_extents, motion_clearance) -> list[float]:
    """Rank open-air samples before the first rendered cliff/terrain hit."""
    reserve = math.sqrt(sum((float(proxy_extents[index])
                             + float(motion_clearance[index])) ** 2
                            for index in range(3)))
    # ★下限跟著物體大小★(2026-09-12)：以前寫死 8.0，小動物永遠 8m 遠、幾個 pixel。
    #   reserve 本來就是物體半徑(含動作淨空)，絕對下限只留 AIRBORNE_MIN_DEPTH_M 防穿鏡。
    minimum = max(AIRBORNE_MIN_DEPTH_M, reserve + 2.0)
    maximum = 80.0
    if surface_distance is not None:
        maximum = min(maximum, float(surface_distance) - reserve - 0.75)
    if maximum < minimum:
        return []
    step = 2.0
    count = int(math.floor((maximum - minimum) / step))
    depths = [minimum + step * index for index in range(count + 1)]
    return sorted(depths)


def _attach_volume_cell_polygons(*, evaluator, cells: list[dict],
                                 screen_bounds, columns: int, rows: int) -> None:
    """Draw each 2D cell at the exact depth selected for its 3D voxel."""
    xmin, ymin, xmax, ymax = map(float, screen_bounds)
    step_x = (xmax - xmin) / int(columns)
    step_y = (ymax - ymin) / int(rows)
    for cell in cells:
        depth = cell.get("candidate_depth_m")
        if depth is None:
            cell["world_xy_polygon"] = None
            continue
        column, row = int(cell["column"]), int(cell["row"])
        polygon = []
        for nx, ny in (
            (xmin + column * step_x, ymin + row * step_y),
            (xmin + (column + 1) * step_x, ymin + row * step_y),
            (xmin + (column + 1) * step_x, ymin + (row + 1) * step_y),
            (xmin + column * step_x, ymin + (row + 1) * step_y),
        ):
            point = evaluator.origin + evaluator.camera_direction(nx, ny) * float(depth)
            polygon.append([float(point.x), float(point.y)])
        cell["world_xy_polygon"] = polygon


def _median_world_spacing(cells: list[dict]) -> float:
    by_grid = {(cell["column"], cell["row"]): cell for cell in cells
               if cell.get("world_position") is not None}
    distances = []
    for (column, row), cell in by_grid.items():
        for neighbour in ((column + 1, row), (column, row + 1)):
            other = by_grid.get(neighbour)
            if other is None:
                continue
            distances.append(math.dist(
                cell["world_position"][:2], other["world_position"][:2]))
    return float(statistics.median(distances)) if distances else 0.4


def _attach_surface_plane_inventory(cells: list[dict], scene_name: str
                                    ) -> list[dict]:
    """Group visible horizontal grid hits into world-space 3D plane patches.

    Each patch is a 2D grid at one world elevation.  Multiple elevations on the
    same furniture object (for example the lower and upper boards of a TV
    stand) remain separate patches, so their union is a queryable 3D inventory
    rather than one flattened screen mask.
    """
    candidates = {}
    for cell in cells:
        world = cell.get("world_position")
        slope = cell.get("slope_deg")
        surface = cell.get("hit_surface")
        if world is None or slope is None or surface is None:
            continue
        surface_types = [value for value in sr.types_for_mesh(
            scene_name, str(surface))
            if value in plc.INDOOR_PLANAR_SURFACE_TYPES]
        if not surface_types:
            continue
        slope_cap = (45.0 if set(surface_types)
                     & plc.INDOOR_SOFT_PLANAR_SURFACE_TYPES else 20.0)
        if float(slope) > slope_cap:
            continue
        cell["surface_types"] = surface_types
        candidates[(int(cell["column"]), int(cell["row"]))] = cell

    components = []
    remaining = set(candidates)
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = {seed}
        stack = [seed]
        while stack:
            key = stack.pop()
            current = candidates[key]
            current_types = set(current.get("surface_types") or [])
            height_tolerance = (
                0.35 if current_types & plc.INDOOR_SOFT_PLANAR_SURFACE_TYPES
                else 0.20)
            for neighbour in (
                    (key[0] - 1, key[1]), (key[0] + 1, key[1]),
                    (key[0], key[1] - 1), (key[0], key[1] + 1)):
                other = candidates.get(neighbour)
                if neighbour not in remaining or other is None:
                    continue
                other_types = set(other.get("surface_types") or [])
                same_semantic_plane = bool(current_types & other_types)
                if (not same_semantic_plane
                        and str(other.get("hit_surface")) != str(
                            current.get("hit_surface"))):
                    continue
                if abs(float(other["world_position"][2])
                       - float(current["world_position"][2])) > height_tolerance:
                    continue
                remaining.remove(neighbour)
                component.add(neighbour)
                stack.append(neighbour)
        components.append(component)

    spacing = _median_world_spacing(cells)
    inventory = []
    for index, component in enumerate(sorted(
            components, key=lambda part: min(part)), start=1):
        plane_id = f"P{index:02d}"
        rows = [candidates[key] for key in sorted(component)]
        for cell in rows:
            cell["support_plane_id"] = plane_id
        world = [cell["world_position"] for cell in rows]
        heights = sorted(float(point[2]) for point in world)
        slopes = [float(cell["slope_deg"]) for cell in rows]
        qualities = [float(cell["plane_quality_score"])
                     for cell in rows
                     if cell.get("plane_quality_score") is not None
                     and cell.get("status") == "feasible"]
        usable = [cell for cell in rows if cell.get("status") == "feasible"]
        inventory.append({
            "plane_id": plane_id,
            "surface_object": str(rows[0]["hit_surface"]),
            "surface_objects": sorted({
                str(cell["hit_surface"]) for cell in rows}, key=str.casefold),
            "surface_types": sorted({value for cell in rows
                                     for value in cell.get("surface_types", [])}),
            "placement_category": (
                "floor" if any("floor" in cell.get("surface_types", [])
                               for cell in rows)
                else "bed" if any("bed_top" in cell.get("surface_types", [])
                                  for cell in rows)
                else "seat" if any("seat" in cell.get("surface_types", [])
                                   for cell in rows)
                else "raised_surface"),
            "median_elevation_m": float(statistics.median(heights)),
            "elevation_range_m": [heights[0], heights[-1]],
            "max_slope_deg": max(slopes),
            "visible_cell_count": len(rows),
            "usable_cell_count": len(usable),
            "world_xy_bounds": [
                min(point[0] for point in world),
                min(point[1] for point in world),
                max(point[0] for point in world),
                max(point[1] for point in world),
            ],
            "estimated_visible_area_m2": float(
                len(rows) * spacing * spacing),
            "estimated_usable_area_m2": float(
                len(usable) * spacing * spacing),
            "median_plane_quality_score": (
                float(statistics.median(qualities)) if qualities else None),
            "max_plane_quality_score": max(qualities) if qualities else None,
            "cell_ids": [cell["cell_id"] for cell in rows],
            "usable_cell_ids": [cell["cell_id"] for cell in usable],
        })
    return inventory


def _visible_registered_topdown_support_cells(*, scene, camera,
                                              scene_name: str,
                                              screen_bounds,
                                              start_column: int,
                                              start_row: int) -> list[dict]:
    """Add indoor support cells that are visible in top-down geometry space.

    The regular semantic grid is intentionally camera-ray based.  That is
    correct for deterministic screen placement, but indoor furniture often
    presents a vertical face to the final camera while the usable seat/table top
    is only obvious from above.  These cells keep the experiment honest: they
    are created only from registered support meshes and validated by vertical
    raycasts against the same mesh, then later machine review can reject any
    candidate whose full footprint is not actually supported.
    """
    from bpy_extras.object_utils import world_to_camera_view

    depsgraph = bpy.context.evaluated_depsgraph_get()
    sx0, sy0, sx1, sy1 = map(float, screen_bounds)
    cells = []
    support_index = 0
    for obj in sorted(bpy.data.objects, key=lambda item: item.name.casefold()):
        if obj.type != "MESH" or not obj.visible_get():
            continue
        object_types = [
            value for value in sr.types_for_mesh(scene_name, obj.name)
            if value in plc.INDOOR_PLANAR_SURFACE_TYPES
        ]
        if not object_types or object_types == ["floor"]:
            continue
        try:
            bounds_points = [
                obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        except (AttributeError, TypeError, ValueError):
            continue
        if not bounds_points:
            continue
        xmin, ymin, zmin = (
            min(point.x for point in bounds_points),
            min(point.y for point in bounds_points),
            min(point.z for point in bounds_points),
        )
        xmax, ymax, zmax = (
            max(point.x for point in bounds_points),
            max(point.y for point in bounds_points),
            max(point.z for point in bounds_points),
        )
        width, depth, height = xmax - xmin, ymax - ymin, zmax - zmin
        if width < 0.18 or depth < 0.18 or height < 0.05:
            continue
        cell_size = max(0.10, float(INDOOR_TOPDOWN_SUPPORT_CELL_SIZE_M))
        max_cells = max(1, int(INDOOR_TOPDOWN_SUPPORT_MAX_CELLS))
        columns = max(1, min(max_cells, int(math.ceil(width / cell_size))))
        rows = max(1, min(max_cells, int(math.ceil(depth / cell_size))))
        object_column = int(start_column) + support_index * 100
        object_row = int(start_row) + support_index * 100
        support_index += 1
        for row in range(rows):
            y0 = ymin + depth * row / rows
            y1 = ymin + depth * (row + 1) / rows
            cy = (y0 + y1) * 0.5
            for column in range(columns):
                x0 = xmin + width * column / columns
                x1 = xmin + width * (column + 1) / columns
                cx = (x0 + x1) * 0.5
                hit = srcache.raycast(
                    scene,
                    Vector((cx, cy, zmax + 1.0)),
                    Vector((0.0, 0.0, -1.0)),
                    distance=max(2.0, height + 2.0),
                )
                if hit is None:
                    continue
                distance, loc, hit_surface, normal = hit
                if str(hit_surface) != obj.name:
                    continue
                surface_types = [
                    value for value in sr.types_for_mesh(
                        scene_name, str(hit_surface))
                    if value in plc.INDOOR_PLANAR_SURFACE_TYPES
                ]
                if not surface_types:
                    continue
                slope = float(math.degrees(math.acos(
                    max(0.0, min(1.0, abs(float(normal.z)))))))
                slope_cap = (
                    45.0 if set(surface_types)
                    & plc.INDOOR_SOFT_PLANAR_SURFACE_TYPES else 20.0)
                if slope > slope_cap:
                    continue
                ndc = world_to_camera_view(scene, camera, loc)
                if not (sx0 <= ndc.x <= sx1 and sy0 <= ndc.y <= sy1
                        and ndc.z > 0.0):
                    continue
                world_polygon = [[float(x0), float(y0)], [float(x1), float(y0)],
                                 [float(x1), float(y1)], [float(x0), float(y1)]]
                screen_polygon = []
                for px, py in world_polygon:
                    projected = world_to_camera_view(
                        scene, camera, Vector((px, py, float(loc.z))))
                    screen_polygon.append([float(projected.x), float(projected.y)])
                cell_id = f"T{support_index:03d}_{column:02d}_{row:02d}"
                cells.append({
                    "cell_id": cell_id,
                    "column": object_column + column,
                    "row": object_row + row,
                    "screen_position": [
                        round(float(ndc.x), 8),
                        round(float(ndc.y), 8),
                    ],
                    "screen_polygon": screen_polygon,
                    "status": "feasible",
                    "reason": None,
                    "hit_surface": str(hit_surface),
                    "world_position": [float(loc.x), float(loc.y), float(loc.z)],
                    "candidate_world_position": [
                        float(loc.x), float(loc.y), float(loc.z)],
                    "distance": float((Vector(loc) - camera.matrix_world.translation).length),
                    "surface_normal": [float(value) for value in normal],
                    "slope_deg": slope,
                    "footprint_samples": [],
                    "support_summary": {
                        "strict_planar_support": False,
                        "sample_count": 1,
                        "expected_sample_count": 1,
                        "support_coverage_ratio": 1.0,
                        "support_height_range_m": 0.0,
                        "maximum_sample_slope_deg": slope,
                        "plane_quality_score": max(
                            0.0, min(1.0, 1.0 - slope / max(slope_cap, 1e-6))),
                        "source": "registered_object_topdown_raycast",
                    },
                    "plane_quality_score": max(
                        0.0, min(1.0, 1.0 - slope / max(slope_cap, 1e-6))),
                    "surface_types": surface_types,
                    "semantic_source": "registered_object_topdown_raycast",
                    "world_xy_polygon": world_polygon,
                })
    return cells


def _camera_frustum_world_xy(scene, evaluator) -> list[list[float]]:
    hits = []
    directions = []
    for nx, ny in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)):
        direction = evaluator.camera_direction(nx, ny)
        directions.append(direction)
        hit = srcache.raycast(scene, evaluator.origin, direction)
        hits.append(hit)
    distances = [float(hit[0]) for hit in hits if hit is not None]
    fallback = statistics.median(distances) if distances else 40.0
    result = []
    for direction, hit in zip(directions, hits):
        point = hit[1] if hit is not None else evaluator.origin + direction * fallback
        result.append([float(point.x), float(point.y)])
    return result


def _camera_frustum_world_xy_at_depth(evaluator, depth: float) -> list[list[float]]:
    return [[float(point.x), float(point.y)] for point in (
        evaluator.origin + evaluator.camera_direction(nx, ny) * float(depth)
        for nx, ny in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    )]


def _attach_world_cell_polygons(*, scene, evaluator, cells: list[dict],
                                screen_bounds, columns: int, rows: int) -> None:
    """Attach top-map quadrilaterals derived from exact screen-cell corner rays.

    The feasibility decision still comes only from the canonical center and
    footprint evaluator.  These shared grid vertices are visualization
    geometry: filling their quadrilaterals turns the old perspective-warped
    point cloud into a readable semantic world-XY map without inventing terrain
    boundaries.
    """
    xmin, ymin, xmax, ymax = map(float, screen_bounds)
    step_x = (xmax - xmin) / int(columns)
    step_y = (ymax - ymin) / int(rows)
    vertices = {}
    for row in range(rows + 1):
        ny = ymin + row * step_y
        for column in range(columns + 1):
            nx = xmin + column * step_x
            direction = evaluator.camera_direction(nx, ny)
            hit = srcache.raycast(scene, evaluator.origin, direction)
            vertices[(column, row)] = None if hit is None else [
                float(hit[1].x), float(hit[1].y)]
    for cell in cells:
        column, row = int(cell["column"]), int(cell["row"])
        polygon = [
            vertices[(column, row)],
            vertices[(column + 1, row)],
            vertices[(column + 1, row + 1)],
            vertices[(column, row + 1)],
        ]
        cell["world_xy_polygon"] = (
            None if any(point is None for point in polygon) else polygon)


def _infer_scene_name(source: str | Path, explicit_scene: str | None) -> str:
    if explicit_scene:
        return str(explicit_scene)
    aliases = {
        *sr.SCENE_ALIASES.keys(),
        *sr.SCENE_ALIASES.values(),
        *INDOOR_SCENE_NAMES,
    }
    for part in reversed(Path(source).parts):
        if part in aliases:
            return part
    return ""


def _is_indoor_scene(*, source: str | Path, scene_name: str,
                     scene_location: str | None) -> bool:
    if str(scene_location or "").casefold() == "indoor":
        return True
    if sr.canonical_scene_name(scene_name) in INDOOR_SCENE_NAMES:
        return True
    return any(part == "scenes_inside" for part in Path(source).parts)


def _harden_indoor_edit(edit: dict) -> dict | None:
    """Apply indoor-only planar support and semantic size minimums in-place."""
    spec = deepcopy(edit.get("placement_spec") or {})
    original_policy = plc.normalize_surface_policy(
        spec.get("surface_policy"))
    hardened_policy = plc.harden_indoor_surface_policy(original_policy)
    original_dimensions = plc.normalize_proxy_dimensions(
        spec.get("proxy_dimensions_m"))
    hardened_dimensions = plc.indoor_proxy_dimensions(
        edit.get("factory"), original_dimensions)
    if (hardened_policy == original_policy
            and hardened_dimensions == original_dimensions):
        return None
    spec["surface_policy"] = hardened_policy
    if hardened_dimensions is not None:
        spec["proxy_dimensions_m"] = hardened_dimensions
    edit["placement_spec"] = spec
    metadata = {
        "applied": True,
        "scope": "indoor_only",
        "original_surface_policy": original_policy,
        "selected_surface_policy": hardened_policy,
        "original_proxy_dimensions_m": original_dimensions,
        "selected_proxy_dimensions_m": hardened_dimensions,
        "reason": (
            "dense planar footprint support and indoor semantic size minimums"),
    }
    edit["indoor_planar_hardening"] = metadata
    return metadata


def _enable_indoor_semantic_surface_choice(
        edit: dict, scene_name: str) -> dict | None:
    """Let image placement choose among all registered indoor support types.

    Director/Composer often emits an early coarse preference such as
    ``floor``.  In image-guided semantic mode that should be a hint, not a
    hard lock: the Agent sees the render and chooses the actual support patch.
    Keep explicit forbidden surfaces so prompts like "not on the bed" still
    constrain the search.
    """
    spec = deepcopy(edit.get("placement_spec") or {})
    original_policy = plc.normalize_surface_policy(
        spec.get("surface_policy"))
    declared_types = {
        str(value)
        for value in original_policy.get("required_surface_types") or []
        if str(value) in INDOOR_SURFACE_TYPES
    }
    for name in original_policy.get("required_surface_names") or []:
        declared_types.update(
            surface_type
            for surface_type in sr.types_for_mesh(scene_name, str(name))
            if surface_type in INDOOR_SURFACE_TYPES
        )
    if any(surface_type != "floor" for surface_type in declared_types):
        return None
    forbidden_types = {
        str(value) for value in original_policy.get(
            "forbidden_surface_types") or []
    }
    names_by_type = _surface_type_names(scene_name)
    allowed_types = [
        surface_type for surface_type in INDOOR_SURFACE_TYPES
        if names_by_type.get(surface_type)
        and surface_type not in forbidden_types
    ]
    if not allowed_types:
        return None
    selected_policy = deepcopy(original_policy)
    selected_policy["required_surface_types"] = allowed_types
    selected_policy["required_surface_names"] = []
    selected_policy["forbidden_surface_types"] = [
        value for value in selected_policy.get("forbidden_surface_types") or []
        if value not in allowed_types
    ]
    selected_policy["require_full_footprint_support"] = True
    if selected_policy == original_policy:
        return None
    spec["surface_policy"] = selected_policy
    edit["placement_spec"] = spec
    metadata = {
        "applied": True,
        "scope": "composer_image_placement_only",
        "original_surface_policy": original_policy,
        "selected_surface_policy": selected_policy,
        "available_surface_types": allowed_types,
        "reason": (
            "image-guided Composer should choose a semantic support plane "
            "from the room inventory instead of inheriting the early coarse "
            "surface lock"),
    }
    edit["indoor_semantic_surface_choice"] = metadata
    return metadata


def _surface_proxy_projected_major_fraction(
        *, scene, camera, cell: dict, proxy_half_extents) -> float | None:
    """Project an upright proxy resting on one sampled indoor surface cell."""
    position = cell.get("world_position")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        return None
    center = [float(value) for value in position]
    center[2] += float(proxy_half_extents[2])
    bounds = rotated_aabb_bounds(center, proxy_half_extents, yaw=0.0)
    screen = _project_box_bounds(scene, camera, bounds)
    major = max(float(screen[2]) - float(screen[0]),
                float(screen[3]) - float(screen[1]))
    return major if math.isfinite(major) and major > 0.0 else None


def _apply_indoor_visibility_scaling(
        *, scene, camera, edit: dict, map_data: dict) -> dict | None:
    """Enlarge a small indoor proxy before region selection and population.

    The provisional map supplies real candidate-surface depths.  Measure the
    proxy there, choose one coarse category-capped scale, then let the caller
    rebuild the complete map so support, obstacle, and capacity checks all see
    the enlarged footprint.
    """
    spec = deepcopy(edit.get("placement_spec") or {})
    original_dimensions = plc.normalize_proxy_dimensions(
        spec.get("proxy_dimensions_m") or map_data.get("proxy_dimensions_m"))
    if original_dimensions is None:
        return None
    half_extents = tuple(float(value) * 0.5
                         for value in original_dimensions)
    projected = [
        fraction
        for cell in map_data.get("cells", [])
        if cell.get("hit_surface") and cell.get("world_position") is not None
        for fraction in [_surface_proxy_projected_major_fraction(
            scene=scene,
            camera=camera,
            cell=cell,
            proxy_half_extents=half_extents,
        )]
        if fraction is not None
    ]
    if not projected:
        return None
    projected_median = float(statistics.median(projected))
    surface_types = ((spec.get("surface_policy") or {})
                     .get("required_surface_types") or [])
    selected_dimensions, scale = plc.indoor_visibility_proxy_dimensions(
        original_dimensions,
        projected_median,
        surface_types,
    )
    metadata = {
        "applied": scale > 1.0,
        "scope": "indoor_only",
        "target_projected_major_fraction": (
            plc.INDOOR_VISIBILITY_TARGET_MAJOR_FRACTION),
        "measured_projected_major_fraction": projected_median,
        "measurement_sample_count": len(projected),
        "surface_types": list(surface_types),
        "scale_factor": float(scale),
        "original_proxy_dimensions_m": original_dimensions,
        "selected_proxy_dimensions_m": selected_dimensions,
        "reason": (
            "camera-visible proxy was below the indoor review target"
            if scale > 1.0 else
            "camera-visible proxy already met the indoor review target"),
    }
    edit["indoor_visibility_scaling"] = metadata
    if scale <= 1.0:
        return metadata
    spec["proxy_dimensions_m"] = selected_dimensions
    edit["placement_spec"] = spec
    return metadata


def _map_required_capacity(data: dict, failed_indices: set[int]) -> int:
    return max(1, len(failed_indices))


def _map_total_capacity(data: dict) -> int:
    return sum(int(region.get("estimated_capacity", 0))
               for region in data.get("regions", []))


def _map_capacity_sufficient(data: dict, failed_indices: set[int]) -> bool:
    return _map_total_capacity(data) >= _map_required_capacity(
        data, failed_indices)


def _map_status_counts(data: dict) -> dict[str, int]:
    result: dict[str, int] = {}
    for cell in data.get("cells", []):
        status = str(cell.get("status") or "unknown")
        result[status] = result.get(status, 0) + 1
    return dict(sorted(result.items(), key=lambda item: (-item[1], item[0])))


def _surface_type_names(scene_name: str) -> dict[str, list[str]]:
    result = {surface_type: [] for surface_type in INDOOR_SURFACE_TYPES}
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.hide_render or "collider" in obj.name.lower():
            continue
        if any(PH_COLL_RE.fullmatch(collection.name)
               for collection in obj.users_collection):
            continue
        types = [surface_type for surface_type in sr.types_for_mesh(
            scene_name, obj.name) if surface_type in result]
        if not types:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = getattr(evaluated, "data", None)
        if mesh is None or not hasattr(mesh, "vertices") or len(mesh.vertices) == 0:
            continue
        try:
            evaluated.matrix_world.inverted()
            evaluated.ray_cast(
                Vector((0.0, 0.0, 0.0)),
                Vector((0.0, 0.0, 1.0)),
                distance=1e-6,
            )
        except (AttributeError, RuntimeError, ValueError, ReferenceError):
            continue
        for surface_type in types:
            result[surface_type].append(obj.name)
    return {
        surface_type: sorted(set(names), key=str.casefold)
        for surface_type, names in result.items()
    }


def _original_surface_types(edit: dict, scene_name: str) -> list[str]:
    policy = (((edit.get("placement_spec") or {}).get("surface_policy"))
              or {})
    ordered = []
    for surface_type in policy.get("required_surface_types") or []:
        surface_type = str(surface_type)
        if surface_type in INDOOR_SURFACE_TYPES and surface_type not in ordered:
            ordered.append(surface_type)
    for name in policy.get("required_surface_names") or []:
        for surface_type in sr.types_for_mesh(scene_name, str(name)):
            if surface_type in INDOOR_SURFACE_TYPES and surface_type not in ordered:
                ordered.append(surface_type)
    return ordered


def _fallback_surface_type_sequence(original_types: list[str]) -> list[str]:
    sequence = []
    for surface_type in original_types:
        if surface_type in INDOOR_SURFACE_TYPES and surface_type not in sequence:
            sequence.append(surface_type)
        for fallback in INDOOR_SURFACE_FALLBACKS.get(surface_type, ()):
            if fallback not in sequence:
                sequence.append(fallback)
    return sequence


def _limited_surface_names(names: list[str]) -> list[str]:
    return list(names[:20])


def _clone_with_surface_policy(edit: dict, *, surface_type: str,
                               surface_names: list[str],
                               require_full_footprint_support: bool,
                               full_frame: bool) -> dict:
    cloned = deepcopy(edit)
    spec = deepcopy(cloned.get("placement_spec") or {})
    policy = deepcopy(spec.get("surface_policy") or {})
    policy["required_surface_types"] = [surface_type]
    policy["required_surface_names"] = list(surface_names)
    policy["forbidden_surface_types"] = [
        str(value)
        for value in policy.get("forbidden_surface_types") or []
    ]
    policy["forbidden_surface_names"] = [
        str(value)
        for value in policy.get("forbidden_surface_names") or []
    ]
    policy["max_slope_deg"] = (
        20.0 if policy.get("max_slope_deg") is None
        else policy.get("max_slope_deg"))
    policy["require_full_footprint_support"] = bool(
        require_full_footprint_support)
    spec["surface_policy"] = policy
    if full_frame:
        spec["screen_region"] = "full_frame"
        spec["screen_bounds"] = list(FULL_FRAME_BOUNDS)
    cloned["placement_spec"] = spec
    return cloned


def _fallback_attempt_summary(*, label: str, surface_type: str,
                              surface_names: list[str], full_frame: bool,
                              require_full_footprint_support: bool,
                              data: dict, failed_indices: set[int]) -> dict:
    return {
        "label": label,
        "surface_type": surface_type,
        "surface_names": list(surface_names),
        "full_frame": bool(full_frame),
        "require_full_footprint_support": bool(
            require_full_footprint_support),
        "required_instance_count": _map_required_capacity(data, failed_indices),
        "feasible_cell_count": sum(
            cell.get("status") == "feasible" for cell in data.get("cells", [])),
        "region_count": len(data.get("regions", [])),
        "estimated_total_capacity": _map_total_capacity(data),
        "status_counts": _map_status_counts(data),
    }


def indoor_surface_fallback_map(*, scene, camera, plan: dict, edit: dict,
                                original_map: dict, failed_indices: set[int],
                                columns: int, rows: int,
                                obstacles: list[dict],
                                include_current_instances: bool,
                                scene_name: str,
                                enabled: bool) -> tuple[dict, dict | None, dict | None]:
    """Try deterministic indoor support fallbacks when the semantic map is empty."""
    if (not enabled
            or not uses_surface_map(edit.get("anchoring"))
            or _map_capacity_sufficient(original_map, failed_indices)):
        return original_map, None, None
    original_types = _original_surface_types(edit, scene_name)
    if not original_types:
        return original_map, None, None
    names_by_type = _surface_type_names(scene_name)
    original_policy = deepcopy(original_map.get("surface_policy") or {})
    forbidden_types = {
        str(value)
        for value in original_policy.get("forbidden_surface_types") or []
    }
    forbidden_names = {
        str(value).casefold()
        for value in original_policy.get("forbidden_surface_names") or []
    }

    def allowed_surface_names(surface_type: str) -> list[str]:
        if str(surface_type) in forbidden_types:
            return []
        return _limited_surface_names([
            name for name in names_by_type.get(surface_type) or []
            if str(name).casefold() not in forbidden_names
        ])

    original_summary = _fallback_attempt_summary(
        label="original",
        surface_type=",".join(original_types),
        surface_names=original_policy.get("required_surface_names") or [],
        full_frame=False,
        require_full_footprint_support=bool(
            original_policy.get("require_full_footprint_support", True)),
        data=original_map,
        failed_indices=failed_indices,
    )
    attempts = [original_summary]

    candidates = []
    original_required_names = [
        name for name in original_policy.get("required_surface_names") or []
        if str(name).casefold() not in forbidden_names
    ]
    if original_required_names:
        for surface_type in original_types:
            if surface_type in forbidden_types:
                continue
            same_type_names = [
                name for name in original_required_names
                if surface_type in sr.types_for_mesh(scene_name, name)
            ]
            if not same_type_names:
                same_type_names = (
                    original_required_names if len(original_types) == 1 else [])
            if not same_type_names:
                continue
            candidates.append({
                "label": "same_names_strict_planar_support",
                "surface_type": surface_type,
                "surface_names": _limited_surface_names(same_type_names),
                "require_full_footprint_support": True,
                "full_frame": False,
            })
    for surface_type in _fallback_surface_type_sequence(original_types):
        surface_names = allowed_surface_names(surface_type)
        if not surface_names:
            continue
        is_original_type = surface_type in original_types
        candidates.append({
            "label": ("same_type_all_names"
                      if is_original_type else "semantic_fallback"),
            "surface_type": surface_type,
            "surface_names": surface_names,
            "require_full_footprint_support": True,
            "full_frame": False,
        })
    for surface_type in _fallback_surface_type_sequence(original_types):
        surface_names = allowed_surface_names(surface_type)
        if not surface_names:
            continue
        candidates.append({
            "label": "full_frame_fallback",
            "surface_type": surface_type,
            "surface_names": surface_names,
            "require_full_footprint_support": True,
            "full_frame": True,
        })

    seen = set()
    for candidate in candidates:
        key = (
            candidate["surface_type"],
            tuple(candidate["surface_names"]),
            candidate["require_full_footprint_support"],
            candidate["full_frame"],
        )
        if key in seen:
            continue
        seen.add(key)
        fallback_edit = _clone_with_surface_policy(
            edit,
            surface_type=candidate["surface_type"],
            surface_names=candidate["surface_names"],
            require_full_footprint_support=(
                candidate["require_full_footprint_support"]),
            full_frame=candidate["full_frame"],
        )
        data = build_one_map(
            scene=scene,
            camera=camera,
            scene_name=scene_name,
            plan=plan,
            edit=fallback_edit,
            failed_indices=failed_indices,
            columns=columns,
            rows=rows,
            obstacles=obstacles,
            include_current_instances=include_current_instances,
        )
        attempts.append(_fallback_attempt_summary(
            label=candidate["label"],
            surface_type=candidate["surface_type"],
            surface_names=candidate["surface_names"],
            full_frame=candidate["full_frame"],
            require_full_footprint_support=(
                candidate["require_full_footprint_support"]),
            data=data,
            failed_indices=failed_indices,
        ))
        if not _map_capacity_sufficient(data, failed_indices):
            continue
        fallback_metadata = {
            "applied": True,
            "reason": (
                "original indoor surface map had insufficient "
                "machine-feasible capacity"),
            "original_surface_policy": original_policy,
            "selected_surface_policy": data.get("surface_policy"),
            "original_surface_types": original_types,
            "selected_surface_type": candidate["surface_type"],
            "selected_surface_names": candidate["surface_names"],
            "full_frame": candidate["full_frame"],
            "required_instance_count": _map_required_capacity(
                data, failed_indices),
            "original_estimated_total_capacity": _map_total_capacity(
                original_map),
            "selected_estimated_total_capacity": _map_total_capacity(data),
            "attempts": attempts,
        }
        data["surface_fallback"] = fallback_metadata
        return data, fallback_edit, fallback_metadata
    original_map["surface_fallback"] = {
        "applied": False,
        "reason": "no indoor fallback surface reached required capacity",
        "original_surface_policy": original_policy,
        "original_surface_types": original_types,
        "attempts": attempts,
    }
    return original_map, None, original_map["surface_fallback"]


def afloat_surface_fallback_map(*, scene, camera, plan: dict, edit: dict,
                                original_map: dict, failed_indices: set[int],
                                columns: int, rows: int,
                                obstacles: list[dict],
                                include_current_instances: bool,
                                enabled: bool) -> tuple[dict, dict | None,
                                                        dict | None]:
    """Try a tiny full-frame relaxation for afloat edits when water is sparse."""
    if (not enabled
            or str(edit.get("anchoring") or "") != "afloat"
            or _map_capacity_sufficient(original_map, failed_indices)):
        return original_map, None, None
    original_policy = deepcopy(original_map.get("surface_policy") or {})
    surface_types = list(original_policy.get("required_surface_types") or [])
    surface_names = list(original_policy.get("required_surface_names") or [])
    if not surface_types:
        surface_types = ["liquid"]
    if not surface_names:
        surface_names = list(surface_types)
    attempts = []
    original_summary = _fallback_attempt_summary(
        label="original",
        surface_type=",".join(surface_types),
        surface_names=surface_names,
        full_frame=False,
        require_full_footprint_support=bool(
            original_policy.get("require_full_footprint_support", True)),
        data=original_map,
        failed_indices=failed_indices,
    )
    attempts.append(original_summary)
    for attempt in (
        {"label": "full_frame_support_relaxed",
         "require_full_footprint_support": False,
         "full_frame": True},
        {"label": "full_frame_support_strict",
         "require_full_footprint_support": True,
         "full_frame": True},
    ):
        fallback_edit = _clone_with_surface_policy(
            edit,
            surface_type=surface_types[0],
            surface_names=surface_names,
            require_full_footprint_support=(
                attempt["require_full_footprint_support"]),
            full_frame=attempt["full_frame"],
        )
        data = build_one_map(
            scene=scene,
            camera=camera,
            scene_name="",
            plan=plan,
            edit=fallback_edit,
            failed_indices=failed_indices,
            columns=columns,
            rows=rows,
            obstacles=obstacles,
            include_current_instances=include_current_instances,
        )
        attempts.append(_fallback_attempt_summary(
            label=attempt["label"],
            surface_type=surface_types[0],
            surface_names=surface_names,
            full_frame=attempt["full_frame"],
            require_full_footprint_support=(
                attempt["require_full_footprint_support"]),
            data=data,
            failed_indices=failed_indices,
        ))
        if not _map_capacity_sufficient(data, failed_indices):
            continue
        fallback_metadata = {
            "applied": True,
            "reason": "afloat water surface needed a wider full-frame search",
            "original_surface_policy": original_policy,
            "selected_surface_policy": data.get("surface_policy"),
            "original_surface_types": surface_types,
            "selected_surface_type": surface_types[0],
            "selected_surface_names": surface_names,
            "full_frame": attempt["full_frame"],
            "required_instance_count": _map_required_capacity(
                data, failed_indices),
            "original_estimated_total_capacity": _map_total_capacity(
                original_map),
            "selected_estimated_total_capacity": _map_total_capacity(data),
            "attempts": attempts,
        }
        data["surface_fallback"] = fallback_metadata
        return data, fallback_edit, fallback_metadata
    original_map["surface_fallback"] = {
        "applied": False,
        "reason": "no afloat fallback surface reached required capacity",
        "original_surface_policy": original_policy,
        "original_surface_types": surface_types,
        "attempts": attempts,
    }
    return original_map, None, original_map["surface_fallback"]


def _clone_with_large_ground_relaxation(edit: dict, attempt: dict) -> dict:
    cloned = deepcopy(edit)
    spec = deepcopy(cloned.get("placement_spec") or {})
    policy = deepcopy(spec.get("surface_policy") or {})
    policy["max_slope_deg"] = float(attempt["max_slope_deg"])
    policy["require_full_footprint_support"] = bool(
        attempt["require_full_footprint_support"])
    spec["surface_policy"] = policy
    if attempt.get("full_frame"):
        spec["screen_region"] = "full_frame"
        spec["screen_bounds"] = list(FULL_FRAME_BOUNDS)
    cloned["placement_spec"] = spec
    return cloned


def _obstacles_for_large_ground_attempt(
        obstacles: list[dict], obstacle_policy: str) -> tuple[list[dict], dict]:
    if obstacle_policy != "hard_scene_obstacles":
        return list(obstacles), {
            "obstacle_policy": "all",
            "input_obstacle_count": len(obstacles),
            "ignored_soft_obstacle_count": 0,
            "remaining_obstacle_count": len(obstacles),
        }
    filtered = [
        row for row in obstacles
        if not is_soft_scene_obstacle(
            label=row.get("label"),
            source=row.get("source"),
            object_name=row.get("object"),
        )
    ]
    return filtered, {
        "obstacle_policy": "hard_scene_obstacles",
        "input_obstacle_count": len(obstacles),
        "ignored_soft_obstacle_count": len(obstacles) - len(filtered),
        "remaining_obstacle_count": len(filtered),
    }


def _large_ground_attempt_summary(*, label: str, attempt: dict | None,
                                  data: dict, failed_indices: set[int],
                                  obstacle_summary: dict) -> dict:
    policy = data.get("surface_policy") or {}
    return {
        "label": label,
        "max_slope_deg": policy.get("max_slope_deg"),
        "require_full_footprint_support": bool(
            policy.get("require_full_footprint_support", True)),
        "full_frame": bool(attempt.get("full_frame")) if attempt else False,
        "screen_bounds": data.get("screen_bounds"),
        "obstacle_policy": obstacle_summary.get("obstacle_policy", "all"),
        "input_obstacle_count": obstacle_summary.get(
            "input_obstacle_count", 0),
        "ignored_soft_obstacle_count": obstacle_summary.get(
            "ignored_soft_obstacle_count", 0),
        "remaining_obstacle_count": obstacle_summary.get(
            "remaining_obstacle_count", 0),
        "visible_obstacle_count": len(data.get("obstacles", [])),
        "required_instance_count": _map_required_capacity(
            data, failed_indices),
        "feasible_cell_count": sum(
            cell.get("status") == "feasible" for cell in data.get("cells", [])),
        "region_count": len(data.get("regions", [])),
        "estimated_total_capacity": _map_total_capacity(data),
        "status_counts": _map_status_counts(data),
    }


def _relaxation_target_capacity(data: dict, failed_indices: set[int]) -> int:
    required = _map_required_capacity(data, failed_indices)
    if required <= 1:
        return required
    return max(required + 2, int(math.ceil(required * 1.5)))


def _outdoor_broad_search_target_capacity(data: dict,
                                          failed_indices: set[int]) -> int:
    return outdoor_broad_search_target_capacity(
        required_capacity=_map_required_capacity(data, failed_indices),
        arrangement=str(data.get("arrangement") or ""),
    )


def _large_ground_relaxation_metadata(
        *, original_map: dict, selected_map: dict, attempt: dict,
        obstacle_summary: dict, attempts: list[dict],
        failed_indices: set[int], target_capacity: int,
        reached_target: bool) -> dict:
    return {
        "applied": True,
        "reason": (
            "original large grounded map had insufficient "
            "machine-feasible capacity" if reached_target else
            "no relaxation reached the preferred slack target; selected "
            "the best sufficient large-ground map"),
        "original_surface_policy": deepcopy(
            original_map.get("surface_policy") or {}),
        "selected_surface_policy": selected_map.get("surface_policy"),
        "original_screen_bounds": original_map.get("screen_bounds"),
        "selected_screen_bounds": selected_map.get("screen_bounds"),
        "selected_label": str(attempt["label"]),
        "selected_obstacle_policy": str(
            attempt.get("obstacle_policy") or "all"),
        "ignored_soft_obstacle_count": obstacle_summary.get(
            "ignored_soft_obstacle_count", 0),
        "required_instance_count": _map_required_capacity(
            selected_map, failed_indices),
        "target_estimated_total_capacity": int(target_capacity),
        "target_capacity_reached": bool(reached_target),
        "original_estimated_total_capacity": _map_total_capacity(
            original_map),
        "selected_estimated_total_capacity": _map_total_capacity(
            selected_map),
        "attempts": attempts,
    }


def large_ground_relaxation_map(
        *, scene, camera, plan: dict, edit: dict, original_map: dict,
        failed_indices: set[int], columns: int, rows: int,
        obstacles: list[dict], include_current_instances: bool,
        enabled: bool) -> tuple[dict, dict | None, dict | None]:
    """Try deterministic outdoor relaxations for large grounded proxies."""
    if (not enabled
            or not uses_surface_map(edit.get("anchoring"))):
        return original_map, None, None
    attempts = large_ground_relaxation_attempts(
        factory=edit.get("factory"),
        code_from=edit.get("code_from"),
        anchoring=edit.get("anchoring"),
        semantic_target=original_map.get("semantic_target"),
        proxy_dimensions_m=original_map.get("proxy_dimensions_m"),
        surface_policy=original_map.get("surface_policy"),
    )
    if not attempts:
        return original_map, None, None

    original_policy = deepcopy(original_map.get("surface_policy") or {})
    target_capacity = _relaxation_target_capacity(
        original_map, failed_indices)
    if (_map_capacity_sufficient(original_map, failed_indices)
            and _map_total_capacity(original_map) >= target_capacity):
        return original_map, None, None
    _, original_obstacles = _obstacles_for_large_ground_attempt(
        obstacles, "all")
    attempt_summaries = [_large_ground_attempt_summary(
        label="original",
        attempt=None,
        data=original_map,
        failed_indices=failed_indices,
        obstacle_summary=original_obstacles,
    )]

    original_key = (
        original_policy.get("max_slope_deg"),
        bool(original_policy.get("require_full_footprint_support", True)),
        tuple(original_map.get("screen_bounds") or []),
        "all",
    )
    seen = {original_key}
    best_sufficient = None
    for attempt in attempts:
        attempt_edit = _clone_with_large_ground_relaxation(edit, attempt)
        spec = plc.normalize_placement_spec(
            attempt_edit.get("placement_spec"),
            count=int(attempt_edit["n"]),
            anchoring=attempt_edit.get("anchoring"),
            factory=attempt_edit.get("factory"),
            code_from=attempt_edit.get("code_from"),
        )
        obstacle_policy = str(attempt.get("obstacle_policy") or "all")
        filtered_obstacles, obstacle_summary = (
            _obstacles_for_large_ground_attempt(obstacles, obstacle_policy))
        key = (
            spec["surface_policy"].get("max_slope_deg"),
            bool(spec["surface_policy"].get(
                "require_full_footprint_support", True)),
            tuple(spec["screen_bounds"]),
            obstacle_policy,
        )
        if key in seen:
            continue
        seen.add(key)
        data = build_one_map(
            scene=scene,
            camera=camera,
            scene_name="",
            plan=plan,
            edit=attempt_edit,
            failed_indices=failed_indices,
            columns=columns,
            rows=rows,
            obstacles=filtered_obstacles,
            include_current_instances=include_current_instances,
        )
        attempt_summaries.append(_large_ground_attempt_summary(
            label=str(attempt["label"]),
            attempt=attempt,
            data=data,
            failed_indices=failed_indices,
            obstacle_summary=obstacle_summary,
        ))
        if not _map_capacity_sufficient(data, failed_indices):
            continue
        candidate = (data, attempt_edit, attempt, obstacle_summary)
        if (best_sufficient is None
                or _map_total_capacity(data) > _map_total_capacity(
                    best_sufficient[0])):
            best_sufficient = candidate
        if _map_total_capacity(data) < target_capacity:
            continue
        metadata = _large_ground_relaxation_metadata(
            original_map=original_map,
            selected_map=data,
            attempt=attempt,
            obstacle_summary=obstacle_summary,
            attempts=attempt_summaries,
            failed_indices=failed_indices,
            target_capacity=target_capacity,
            reached_target=True,
        )
        data["large_ground_relaxation"] = metadata
        return data, attempt_edit, metadata

    if best_sufficient is not None:
        data, attempt_edit, attempt, obstacle_summary = best_sufficient
        metadata = _large_ground_relaxation_metadata(
            original_map=original_map,
            selected_map=data,
            attempt=attempt,
            obstacle_summary=obstacle_summary,
            attempts=attempt_summaries,
            failed_indices=failed_indices,
            target_capacity=target_capacity,
            reached_target=False,
        )
        data["large_ground_relaxation"] = metadata
        return data, attempt_edit, metadata

    original_map["large_ground_relaxation"] = {
        "applied": False,
        "reason": "no large grounded relaxation reached required capacity",
        "original_surface_policy": original_policy,
        "original_screen_bounds": original_map.get("screen_bounds"),
        "target_estimated_total_capacity": int(target_capacity),
        "attempts": attempt_summaries,
    }
    return original_map, None, original_map["large_ground_relaxation"]


def _clone_with_outdoor_broad_search(edit: dict) -> dict:
    cloned = deepcopy(edit)
    plc.recover_legacy_outdoor_ultra_loose_edit(cloned)
    spec = deepcopy(cloned.get("placement_spec") or {})
    spec["screen_region"] = "full_frame"
    spec["screen_bounds"] = list(FULL_FRAME_BOUNDS)
    spec["outdoor_broad_search_relaxation"] = {
        "applied": True,
        "scope": "outdoor_composer_only",
        "mode": "broad_search_strict_review",
    }
    cloned["placement_spec"] = spec
    cloned["outdoor_broad_search_relaxation"] = deepcopy(
        spec["outdoor_broad_search_relaxation"])
    return cloned


def _outdoor_broad_search_attempt_summary(*, label: str, data: dict,
                                          failed_indices: set[int],
                                          obstacle_policy: str) -> dict:
    return {
        "label": label,
        "screen_bounds": data.get("screen_bounds"),
        "surface_policy": deepcopy(data.get("surface_policy") or {}),
        "proxy_dimensions_m": data.get("proxy_dimensions_m"),
        "footprint_half_extents": data.get("footprint_half_extents"),
        "proxy_half_extents": data.get("proxy_half_extents"),
        "min_sep": data.get("min_sep"),
        "obstacle_policy": obstacle_policy,
        "visible_obstacle_count": len(data.get("obstacles", [])),
        "required_instance_count": _map_required_capacity(
            data, failed_indices),
        "feasible_cell_count": sum(
            cell.get("status") == "feasible"
            for cell in data.get("cells", [])),
        "region_count": len(data.get("regions", [])),
        "estimated_total_capacity": _map_total_capacity(data),
        "status_counts": _map_status_counts(data),
    }


def _outdoor_broad_search_metadata(
        *, original_map: dict, selected_map: dict,
        failed_indices: set[int], attempts: list[dict],
        target_capacity: int) -> dict:
    return {
        "applied": True,
        "scope": "outdoor_composer_only",
        "mode": "broad_search_strict_review",
        "reason": (
            "outdoor Composer broadens the search area when the requested "
            "screen region lacks capacity, while keeping surface policy, "
            "footprint support, spacing, collision, and full-box review hard"),
        "search_rules_relaxed": ["screen_bounds"],
        "hard_rules_preserved": [
            "required_surface_types",
            "required_surface_names",
            "forbidden_surface_types",
            "forbidden_surface_names",
            "max_slope_deg",
            "require_full_footprint_support",
            "scene_obstacles",
            "min_sep",
            "packing_capacity",
            "apply_projection_rejections",
            "review_full_box_visibility",
            "review_requested_box_collisions",
            "review_environment_collisions",
        ],
        "original_surface_policy": deepcopy(
            original_map.get("surface_policy") or {}),
        "selected_surface_policy": deepcopy(
            selected_map.get("surface_policy") or {}),
        "original_screen_bounds": original_map.get("screen_bounds"),
        "selected_screen_bounds": selected_map.get("screen_bounds"),
        "original_proxy_dimensions_m": original_map.get("proxy_dimensions_m"),
        "selected_proxy_dimensions_m": selected_map.get("proxy_dimensions_m"),
        "original_min_sep": original_map.get("min_sep"),
        "selected_min_sep": selected_map.get("min_sep"),
        "selected_obstacle_policy": "original",
        "required_instance_count": _map_required_capacity(
            selected_map, failed_indices),
        "target_estimated_total_capacity": int(target_capacity),
        "target_capacity_reached": (
            _map_total_capacity(selected_map) >= int(target_capacity)),
        "original_estimated_total_capacity": _map_total_capacity(original_map),
        "selected_estimated_total_capacity": _map_total_capacity(selected_map),
        "selected_feasible_cell_count": sum(
            cell.get("status") == "feasible"
            for cell in selected_map.get("cells", [])),
        "attempts": attempts,
    }


def outdoor_broad_search_map(
        *, scene, camera, plan: dict, edit: dict, original_map: dict,
        failed_indices: set[int], columns: int, rows: int,
        obstacles: list[dict], include_current_instances: bool,
        scene_name: str, enabled: bool) -> tuple[dict, dict | None, dict | None]:
    """Expand outdoor search bounds without weakening placement hard rules."""
    anchoring = edit.get("anchoring")
    if (not enabled
            or not (uses_surface_map(anchoring)
                    or uses_3d_volume_map(anchoring))):
        return original_map, None, None
    target_capacity = _outdoor_broad_search_target_capacity(
        original_map, failed_indices)
    if _map_total_capacity(original_map) >= target_capacity:
        return original_map, None, None
    plan_edit = _clone_with_outdoor_broad_search(edit)
    map_edit = _clone_with_outdoor_broad_search(edit)
    attempts = [_outdoor_broad_search_attempt_summary(
        label="original", data=original_map,
        failed_indices=failed_indices,
        obstacle_policy="original")]
    data = build_one_map(
        scene=scene,
        camera=camera,
        scene_name=scene_name,
        plan=plan,
        edit=map_edit,
        failed_indices=failed_indices,
        columns=columns,
        rows=rows,
        obstacles=obstacles,
        include_current_instances=include_current_instances,
    )
    if _map_total_capacity(data) <= _map_total_capacity(original_map):
        return original_map, None, None
    attempts.append(_outdoor_broad_search_attempt_summary(
        label="outdoor_broad_search_full_frame", data=data,
        failed_indices=failed_indices,
        obstacle_policy="original"))
    metadata = _outdoor_broad_search_metadata(
        original_map=original_map,
        selected_map=data,
        failed_indices=failed_indices,
        attempts=attempts,
        target_capacity=target_capacity,
    )
    for key in ("surface_fallback", "large_ground_relaxation"):
        if original_map.get(key) is not None:
            data[key] = deepcopy(original_map[key])
    data["outdoor_broad_search_relaxation"] = metadata
    plan_edit["placement_spec"]["outdoor_broad_search_relaxation"] = deepcopy(
        metadata)
    plan_edit["outdoor_broad_search_relaxation"] = deepcopy(metadata)
    return data, plan_edit, metadata


def build_one_map(*, scene, camera, scene_name: str, plan: dict, edit: dict,
                  failed_indices: set[int], columns: int, rows: int,
                  obstacles: list[dict], include_current_instances: bool = True
                  ) -> dict:
    count = int(edit["n"])
    spec = plc.normalize_placement_spec(
        edit.get("placement_spec"), count=count,
        anchoring=edit.get("anchoring"),
        factory=edit.get("factory"),
        code_from=edit.get("code_from"))
    declared_dimensions = spec.get("proxy_dimensions_m")
    factory = str(edit["factory"])
    anchoring = str(edit.get("anchoring") or "")
    if (uses_surface_map(anchoring)
            and requires_full_footprint_structure_support(
                factory=factory,
                code_from=edit.get("code_from"),
                semantic_target=spec.get("semantic_target"))):
        policy = deepcopy(spec["surface_policy"])
        policy["require_full_footprint_support"] = True
        spec["surface_policy"] = policy
    inferred_dimensions = (
        None if declared_dimensions is not None else
        plc.inferred_proxy_dimensions(
            factory=factory,
            code_from=edit.get("code_from"),
            anchoring=anchoring,
            semantic_target=spec.get("semantic_target"),
        ))
    proxy_extents = proxy_half_extents(
        factory, declared_dimensions, inferred_dimensions)
    proxy_source = ("measured_existing_placeholder"
                    if any((match := PH_COLL_RE.fullmatch(collection.name))
                           and match.group(2) == factory
                           and _roots(collection)
                           for collection in bpy.data.collections)
                    else "director_estimate"
                    if declared_dimensions is not None
                    else plc.inferred_proxy_dimension_source(
                        factory=factory,
                        code_from=edit.get("code_from"),
                        anchoring=anchoring,
                        semantic_target=spec.get("semantic_target"),
                    )
                    if inferred_dimensions is not None
                    else "legacy_2m_cube_fallback")
    half_extents = proxy_extents[:2]
    submerged = anchoring == "submerged"
    airborne = anchoring == "airborne"
    volume = uses_3d_volume_map(anchoring)
    altitude = float(pm.params_for(anchoring)[1]) if volume else 0.0
    motion_clearance = (
        _submerged_motion_clearance(edit) if submerged
        else _airborne_motion_clearance(edit) if airborne
        else (0.0, 0.0, 0.0))
    evaluator = VisibleSurfaceEvaluator.from_camera(
        scene=scene,
        camera=camera,
        scene_name=scene_name,
        surface_policy=spec["surface_policy"],
        footprint_half_extents=half_extents,
        box_half_height=proxy_extents[2],
    )
    # Extended grid (BI_PLACEMENT_EXTENDED_GRID): probe a larger-than-frame
    # screen area; spec["screen_bounds"] stays the Director's target for scoring.
    search_bounds = plc.extended_search_bounds() or spec["screen_bounds"]
    if search_bounds is not spec["screen_bounds"]:
        scale = 0.75 * plc.extended_grid_factor()
        columns, rows = max(columns, round(columns * scale)), max(rows, round(rows * scale))
    cells = []
    for cell in grid_points(
            search_bounds, columns=columns, rows=rows):
        evaluations = ([(0.0, evaluator.evaluate(*cell["screen_position"]))]
                       if volume else [
                           (yaw, evaluator.evaluate(
                               *cell["screen_position"], yaw=yaw))
                           for yaw in YAW_CANDIDATES
                       ])
        feasible_yaws = [yaw for yaw, result in evaluations if result.feasible]
        representative = (next(
            result for yaw, result in evaluations if result.feasible)
            if feasible_yaws else evaluations[0][1])
        cells.append({
            **cell,
            **representative.as_json(),
            "feasible_yaws": [round(float(yaw), 8) for yaw in feasible_yaws],
            "surface_feasible_yaws": [
                round(float(yaw), 8) for yaw in feasible_yaws],
        })
    if not volume:
        _attach_world_cell_polygons(
            scene=scene,
            evaluator=evaluator,
            cells=cells,
            screen_bounds=search_bounds,
            columns=columns,
            rows=rows,
        )

    hit_world = [cell["world_position"] for cell in cells
                 if cell.get("world_position") is not None]
    if volume:
        from bpy_extras.object_utils import world_to_camera_view

        sx0, sy0, sx1, sy1 = map(float, spec["screen_bounds"])
        screen_pad = 0.10
        visible_obstacles = []
        for row in obstacles:
            if row.get("map_modes", "all") not in {"all", anchoring}:
                continue
            bounds = row["world_bounds"]
            center = Vector(tuple(
                (float(bounds[axis]) + float(bounds[axis + 3])) * 0.5
                for axis in range(3)))
            distance = (center - evaluator.origin).length
            projected = world_to_camera_view(scene, camera, center)
            if (8.0 <= distance <= 80.0 and projected.z > 0.0
                    and sx0 - screen_pad <= projected.x <= sx1 + screen_pad
                    and sy0 - screen_pad <= projected.y <= sy1 + screen_pad):
                visible_obstacles.append(row)
        visible_obstacles.sort(key=lambda row: (
            math.dist(list(evaluator.origin), [
                (row["world_bounds"][axis]
                 + row["world_bounds"][axis + 3]) * 0.5
                for axis in range(3)]),
            row.get("label", ""), row["object"],
        ))
        visible_obstacles = visible_obstacles[:500]
    elif hit_world:
        xmin = min(point[0] for point in hit_world)
        ymin = min(point[1] for point in hit_world)
        xmax = max(point[0] for point in hit_world)
        ymax = max(point[1] for point in hit_world)
        pad = max(5.0, max(half_extents) * 3.0)
        relevant_area = [xmin - pad, ymin - pad, xmax + pad, ymax + pad]
        visible_obstacles = [
            row for row in obstacles
            if _intersects(row["world_xy_bounds"], relevant_area)
            and row.get("map_modes", "all") == "all"
        ]
        visible_obstacles.sort(key=lambda row: (
            (row["world_xy_bounds"][2] - row["world_xy_bounds"][0])
            * (row["world_xy_bounds"][3] - row["world_xy_bounds"][1]),
            row["object"],
        ))
        visible_obstacles = visible_obstacles[:500]
    else:
        visible_obstacles = []

    # Keep a proxy-size-aware reference depth for map metadata/framing, but the
    # actual volume samples now walk near-to-far. Collision and full-frustum
    # checks still reject anything too close or overlapping.
    reference_depth = max(
        12.0 if submerged else 16.0,
        min(32.0, 6.0 * max(proxy_extents)),
    )
    terrain_clearance = TerrainClearanceEvaluator(scene) if volume else None

    # Surface-anchored modes use the original 2D footprint test.  Volume cells
    # instead own one explicit 3D voxel selected before the first rendered hit.
    for cell in cells:
        if volume:
            cell["visible_surface_status"] = cell.get("status")
            cell["visible_surface_reason"] = cell.get("reason")
            cell["visible_surface_world_position"] = cell.get("world_position")
            cell["visible_surface_distance"] = cell.get("distance")
            cell["status"] = (
                "no_water_voxel" if submerged else "no_air_voxel")
            cell["reason"] = (
                "no collision-free 3D water-column sample" if submerged
                else "no collision-free camera-frustum air sample")
            cell["world_position"] = None
            cell["feasible_yaws"] = []
            first_collision = None
            boundary_rejected = False
            terrain_rejected = False
            tested_depths = []
            obstacle_depths = set()
            terrain_depths = set()
            boundary_depths = set()
            rejected_obstacles = set()
            direction = evaluator.camera_direction(*cell["screen_position"])
            per_cell_reference = reference_depth + float(
                ((int(cell["column"]) * 17
                  + int(cell["row"]) * 31) % 7) - 3)
            if submerged:
                depths = _submerged_depths(
                    surface_distance=cell.get("visible_surface_distance"),
                    surface_name=cell.get("hit_surface"),
                    reference_depth=per_cell_reference,
                    proxy_extents=proxy_extents,
                    motion_clearance=motion_clearance,
                )
            else:
                depths = _airborne_depths(
                    surface_distance=cell.get("visible_surface_distance"),
                    reference_depth=per_cell_reference,
                    proxy_extents=proxy_extents,
                    motion_clearance=motion_clearance,
                )
            # ★深度不再「由近到遠取第一個可行的」★(2026-09-12)：那會讓每個飛行物
            #   都停在最近可行深度(螢火蟲/老鷹/蝴蝶全都剛好 8.0m)，評分完全管不到。
            #   改成：用第一個深度當探針量一次投影面積，推出「佔畫面剛好 READABLE」
            #   的理想距離，深度階梯照★離理想值的遠近★排序 —— 小東西自然往前、
            #   大東西自然往後，可行性檢查照舊、第一個可行的就停(便宜)。
            ideal_depth = None
            if depths:
                probe_depth = float(depths[0])
                probe_bounds = rotated_aabb_bounds(
                    evaluator.origin + direction * probe_depth, proxy_extents,
                    yaw=0.0)
                px0, py0, px1, py1 = _project_box_bounds(scene, camera, probe_bounds)
                ideal_depth = ideal_depth_for_readable_size(
                    probe_depth, max(0.0, px1 - px0) * max(0.0, py1 - py0))
            if ideal_depth is not None:
                depths = sorted(depths, key=lambda d: (abs(float(d) - ideal_depth), float(d)))
            cell["ideal_depth_m"] = ideal_depth
            yaw_candidates = (
                VOLUME_YAW_CANDIDATES if volume else YAW_CANDIDATES)
            for depth in depths:
                tested_depths.append(float(depth))
                candidate = evaluator.origin + direction * float(depth)
                candidate_position = [float(value) for value in candidate]
                passing_yaws = []
                first_passing_bounds = None
                first_actual_bounds = None
                for yaw in yaw_candidates:
                    actual_bounds = rotated_aabb_bounds(
                        candidate_position, proxy_extents, yaw=yaw)
                    swept_bounds = rotated_aabb_bounds(
                        candidate_position, proxy_extents, yaw=yaw,
                        clearance=motion_clearance)
                    if not _box_inside_camera(scene, camera, actual_bounds):
                        boundary_rejected = True
                        boundary_depths.add(float(depth))
                        continue
                    collision = next((
                        obstacle for obstacle in visible_obstacles
                        if aabb_overlap_fraction(
                            swept_bounds, obstacle.get("world_bounds")) > 0.0
                    ), None)
                    if collision is not None:
                        if first_collision is None:
                            first_collision = collision
                        obstacle_depths.add(float(depth))
                        rejected_obstacles.add(str(collision.get("object")))
                        continue
                    if not terrain_clearance.clears(swept_bounds):
                        terrain_rejected = True
                        terrain_depths.add(float(depth))
                        continue
                    passing_yaws.append(float(yaw))
                    if first_passing_bounds is None:
                        first_passing_bounds = swept_bounds
                        first_actual_bounds = actual_bounds
                if not passing_yaws:
                    continue
                base_position = list(candidate_position)
                base_position[2] -= altitude
                cell.update({
                    "status": "feasible",
                    "reason": None,
                    "world_position": base_position,
                    "candidate_world_position": candidate_position,
                    "candidate_world_bounds": first_passing_bounds,
                    "candidate_proxy_world_bounds": rotated_aabb_bounds(
                        candidate_position, proxy_extents,
                        yaw=passing_yaws[0]),
                    "candidate_screen_bounds": _project_box_bounds(
                        scene, camera, first_actual_bounds),
                    "candidate_depth_m": float(depth),
                    "feasible_yaws": [round(yaw, 8) for yaw in passing_yaws],
                })
                break
            cell["motion_clearance_xyz"] = list(motion_clearance)
            cell["depth_search"] = {
                "tested_depths_m": tested_depths,
                "obstacle_rejected_depths_m": sorted(obstacle_depths),
                "terrain_rejected_depths_m": sorted(terrain_depths),
                "boundary_rejected_depths_m": sorted(boundary_depths),
                "rejected_obstacles": sorted(rejected_obstacles),
                "selected_depth_m": cell.get("candidate_depth_m"),
            }
            if cell["status"] != "feasible":
                if first_collision is not None:
                    cell["status"] = "obstacle"
                    cell["reason"] = (
                        f"{anchoring} 3D proxy/swept envelope overlaps "
                        f"{first_collision['object']}")
                    cell["environment_obstacle"] = {
                        key: first_collision.get(key)
                        for key in ("object", "label", "source", "world_bounds")
                    }
                elif boundary_rejected:
                    cell["status"] = "boundary"
                    cell["reason"] = (
                        f"shifted {anchoring} proxy would cross the camera boundary")
                elif terrain_rejected:
                    cell["status"] = "terrain"
                    cell["reason"] = (
                        f"{anchoring} 3D proxy/swept envelope intersects terrain")
        elif cell["status"] == "feasible" and any(_proxy_intersects_obstacle(
                cell["world_position"], half_extents, obstacle)
                for obstacle in visible_obstacles):
            cell["status"] = "obstacle"
            cell["reason"] = (
                "proxy footprint overlaps an existing scene object")

    surface_inventory = (
        [] if volume else _attach_surface_plane_inventory(cells, scene_name))

    if volume:
        _attach_volume_cell_polygons(
            evaluator=evaluator,
            cells=cells,
            screen_bounds=search_bounds,
            columns=columns,
            rows=rows,
        )

    cells, regions = connected_feasible_regions(cells)
    by_region = {}
    for cell in cells:
        if cell.get("region_id"):
            by_region.setdefault(cell["region_id"], []).append(cell)
    min_sep_value = edit.get("min_sep")
    if min_sep_value is None:
        min_sep_value = plc.default_min_sep(
            factory=factory,
            code_from=edit.get("code_from"),
            anchoring=anchoring,
            semantic_target=spec.get("semantic_target"),
            arrangement=spec.get("arrangement"),
            count=int(edit.get("n", 1)),
        )
    min_sep = float(min_sep_value)
    for region in regions:
        region["estimated_capacity"] = estimate_region_capacity(
            by_region[region["region_id"]],
            half_extents=proxy_extents if volume else half_extents,
            min_sep=min_sep,
            use_3d=volume,
            arrangement=spec["arrangement"],
        )

    current_instances = []
    if include_current_instances:
        planned_yaws = edit.get("placement_yaws") or []
        for instance_index, point in enumerate(edit.get("screen_positions") or []):
            yaw = (float(planned_yaws[instance_index])
                   if instance_index < len(planned_yaws)
                   and planned_yaws[instance_index] is not None else 0.0)
            evaluation = evaluator.evaluate(*point, yaw=yaw)
            row = evaluation.as_json()
            if volume and row.get("world_position") is not None:
                row["candidate_world_position"] = [
                    float(row["world_position"][0]),
                    float(row["world_position"][1]),
                    float(row["world_position"][2]) + altitude,
                ]
            current_instances.append({
                "instance_index": instance_index,
                "failed": instance_index in failed_indices,
                "planned_yaw": yaw,
                **row,
            })

    step_x = (spec["screen_bounds"][2] - spec["screen_bounds"][0]) / columns
    step_y = (spec["screen_bounds"][3] - spec["screen_bounds"][1]) / rows
    return {
        "contract_version": 1,
        "kind": (f"{anchoring}_3d_environment_region_map" if volume
                 else f"{anchoring}_visible_surface_region_map"),
        "coordinate_system": {
            "camera_screen": "normalized x/y, bottom-left origin",
            "planning_map": "world X right, world Y up",
        },
        "edit_id": str(edit.get("code_from") or edit["factory"]),
        "factory": str(edit["factory"]),
        "anchoring": edit.get("anchoring"),
        "arrangement": spec["arrangement"],
        "semantic_target": spec["semantic_target"],
        "surface_policy": spec["surface_policy"],
        "screen_bounds": spec["screen_bounds"],
        "search_bounds": list(search_bounds),
        "footprint_half_extents": list(half_extents),
        "proxy_half_extents": list(proxy_extents),
        "proxy_dimensions_m": [2.0 * value for value in proxy_extents],
        "proxy_dimension_source": proxy_source,
        "submerged_altitude_m": altitude if submerged else None,
        "airborne_altitude_m": altitude if airborne else None,
        "volume_reference_depth_m": reference_depth if volume else None,
        "motion_clearance_xyz": list(motion_clearance),
        "min_sep": min_sep,
        "yaw_candidates_rad": [round(float(yaw), 8)
                               for yaw in YAW_CANDIDATES],
        "failed_instances": sorted(failed_indices),
        "grid": {
            "columns": int(columns),
            "rows": int(rows),
            "screen_step": [step_x, step_y],
            "median_world_spacing": _median_world_spacing(cells),
        },
        "camera": {
            "name": camera.name,
            "frame": int(scene.frame_current),
            "world_position": [float(v) for v in evaluator.origin],
            "frustum_world_xy": (
                _camera_frustum_world_xy_at_depth(
                    evaluator, min(70.0, reference_depth + 12.0))
                if volume else _camera_frustum_world_xy(scene, evaluator)),
        },
        "current_instances": current_instances,
        "surface_inventory": surface_inventory,
        "regions": regions,
        "cells": cells,
        "obstacles": visible_obstacles,
    }


def build_scene_semantic_support_grid(*, scene, camera, scene_name: str,
                                      plan: dict, columns: int, rows: int,
                                      obstacles: list[dict]) -> dict:
    """Build a scene-level grid of all visible indoor planar support cells."""
    probe_edit = {
        "op": "add",
        "factory": "SemanticSupportProbeFactory",
        "code_from": "__semantic_support_grid",
        "anchoring": "conform",
        "n": 1,
        "placement_spec": {
            "arrangement": "single",
            "semantic_target": "all visible indoor planar support surfaces",
            "screen_region": "full_frame",
            "screen_bounds": list(FULL_FRAME_BOUNDS),
            "proxy_dimensions_m": [0.2, 0.2, 0.2],
            "surface_policy": {
                "required_surface_types": list(INDOOR_SURFACE_TYPES),
                "forbidden_surface_types": [],
                "required_surface_names": [],
                "forbidden_surface_names": [],
                "max_slope_deg": 45.0,
                "require_full_footprint_support": False,
            },
        },
    }
    data = build_one_map(
        scene=scene,
        camera=camera,
        scene_name=scene_name,
        plan=plan,
        edit=probe_edit,
        failed_indices=set(),
        columns=columns,
        rows=rows,
        obstacles=obstacles,
        include_current_instances=False,
    )
    data["kind"] = "scene_indoor_semantic_support_grid"
    data["selection_scope"] = "scene_all_visible_indoor_planar_supports"
    topdown_cells = _visible_registered_topdown_support_cells(
        scene=scene,
        camera=camera,
        scene_name=scene_name,
        screen_bounds=probe_edit["placement_spec"]["screen_bounds"],
        start_column=int(columns) + 1000,
        start_row=int(rows) + 1000,
    )
    if topdown_cells:
        data["cells"].extend(topdown_cells)
        data["surface_inventory"] = _attach_surface_plane_inventory(
            data["cells"], scene_name)
        data["cells"], data["regions"] = connected_feasible_regions(
            data["cells"])
        by_region = {}
        for cell in data["cells"]:
            if cell.get("region_id"):
                by_region.setdefault(cell["region_id"], []).append(cell)
        half_extents = tuple(map(float, data.get(
            "footprint_half_extents") or [0.1, 0.1]))
        min_sep = float(data.get("min_sep") or 2.5)
        for region in data["regions"]:
            region["estimated_capacity"] = estimate_region_capacity(
                by_region.get(region["region_id"], []),
                half_extents=half_extents,
                min_sep=min_sep,
            )
        data["semantic_topdown_support"] = {
            "enabled": True,
            "cell_count": len(topdown_cells),
            "source": "registered_object_topdown_raycast",
        }
    else:
        data["semantic_topdown_support"] = {
            "enabled": True,
            "cell_count": 0,
            "source": "registered_object_topdown_raycast",
        }
    for cell in data.get("cells", []):
        if not cell.get("support_plane_id"):
            cell["status"] = "surface"
            cell["reason"] = (
                cell.get("reason")
                or "visible hit is not a registered indoor planar support")
    semantic_cells = sorted(
        (cell for cell in data.get("cells", [])
         if cell.get("status") == "feasible"
         and cell.get("support_plane_id")),
        key=lambda cell: (
            str(cell.get("support_plane_id") or ""),
            str(cell.get("cell_id") or ""),
        ))
    for index, cell in enumerate(semantic_cells, start=1):
        cell["semantic_cell_id"] = f"S{index:03d}"
        cell["semantic_label"] = cell["semantic_cell_id"]
    support_cell_ids = {
        str(cell.get("cell_id") or "") for cell in semantic_cells}
    data["regions"] = [
        region for region in data.get("regions", [])
        if set(map(str, region.get("cell_ids") or [])) & support_cell_ids
    ]
    return data


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:]
    if len(argv) < 7:
        raise SystemExit(
            "usage: build_placement_region_map.py -- SOURCE PLAN REVIEW OUT_DIR "
            "COLUMNS ROWS FRAME [--all-map-managed|--all-conform] "
            "[--scene SCENE] [--scene-location indoor|outdoor] "
            "[--no-indoor-surface-fallback] [--semantic-support-grid] "
            "[--indoor-semantic-surface-choice]")
    source, plan_path, review_path, out_dir, columns, rows, frame = argv[:7]
    rest = argv[7:]
    explicit_scene = (
        rest[rest.index("--scene") + 1] if "--scene" in rest
        and rest.index("--scene") + 1 < len(rest) else None)
    scene_location = (
        rest[rest.index("--scene-location") + 1]
        if "--scene-location" in rest
        and rest.index("--scene-location") + 1 < len(rest) else None)
    scene_name = _infer_scene_name(source, explicit_scene)
    out_dir = Path(out_dir)
    plan_path = Path(plan_path)
    columns, rows, frame = int(columns), int(rows), int(frame)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    all_conform = "--all-conform" in rest
    all_map_managed = "--all-map-managed" in rest
    is_indoor = _is_indoor_scene(
        source=source, scene_name=scene_name,
        scene_location=scene_location)
    indoor_fallback_enabled = (
        "--no-indoor-surface-fallback" not in rest
        and is_indoor)
    semantic_support_enabled = "--semantic-support-grid" in rest and is_indoor
    indoor_semantic_surface_choice_enabled = (
        "--indoor-semantic-surface-choice" in rest and is_indoor)
    large_ground_relaxation_enabled = not is_indoor
    outdoor_broad_search_enabled = not is_indoor
    if all_conform and all_map_managed:
        raise SystemExit("choose only one all-instance map scope")
    review = ({"status": "MAP_FIRST_INITIAL"} if review_path == "-" else
              json.loads(Path(review_path).read_text(encoding="utf-8")))
    failed = (all_map_managed_instances(plan) if all_map_managed
              else all_conform_instances(plan) if all_conform
              else map_managed_failed_instances(plan, review))
    if not failed:
        raise SystemExit("region map did not identify map-managed instances")

    bpy.ops.wm.open_mainfile(filepath=str(Path(source).resolve()))
    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    camera = scene.camera
    if camera is None:
        raise SystemExit("region map source scene has no active camera")
    srcache.clear()
    edits = {str(edit.get("code_from") or ""): edit
             for edit in plan.get("hero_edits", []) if edit.get("op") == "add"}
    indoor_hardening = {}
    indoor_semantic_surface_choice = {}
    indoor_visibility_scaling = {}
    legacy_ultra_recoveries = {}
    if is_indoor:
        for edit_id in sorted(failed):
            if indoor_semantic_surface_choice_enabled:
                choice_metadata = _enable_indoor_semantic_surface_choice(
                    edits[edit_id], scene_name)
                if choice_metadata is not None:
                    indoor_semantic_surface_choice[edit_id] = choice_metadata
            metadata = _harden_indoor_edit(edits[edit_id])
            if metadata is not None:
                indoor_hardening[edit_id] = metadata
    obstacles = scene_obstacles(
        include_render_scatter=any(
            edits[edit_id].get("anchoring") == "submerged"
            for edit_id in failed),
        plan=plan,
        cameras=[camera],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    semantic_support_index = None
    if semantic_support_enabled:
        support_data = build_scene_semantic_support_grid(
            scene=scene,
            camera=camera,
            scene_name=scene_name,
            plan=plan,
            columns=columns,
            rows=rows,
            obstacles=obstacles,
        )
        support_json = out_dir / "semantic_support_grid.json"
        support_preview = out_dir / "semantic_support_grid.png"
        support_json.write_text(
            json.dumps(support_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        render_region_map(support_data, support_preview)
        semantic_support_index = {
            "edit_id": support_data.get("edit_id"),
            "region_count": len(support_data.get("regions", [])),
            "feasible_cell_count": sum(
                bool(cell.get("status") == "feasible"
                     and cell.get("support_plane_id"))
                for cell in support_data.get("cells", [])),
            "surface_inventory_count": len(
                support_data.get("surface_inventory", [])),
            "map_json": support_json.name,
            "map_preview": support_preview.name,
        }
        print(
            "[region-map] semantic support grid: "
            f"planes={semantic_support_index['surface_inventory_count']} "
            f"feasible={semantic_support_index['feasible_cell_count']}/"
            f"{len(support_data.get('cells', []))}",
            flush=True)
    index_rows = []
    for edit_id in sorted(failed):
        edit = edits[edit_id]
        if not is_indoor:
            recovery = plc.recover_legacy_outdoor_ultra_loose_edit(edit)
            if recovery is not None:
                legacy_ultra_recoveries[edit_id] = recovery
        data = build_one_map(
            scene=scene,
            camera=camera,
            scene_name=scene_name,
            plan=plan,
            edit=edit,
            failed_indices=failed[edit_id],
            columns=columns,
            rows=rows,
            obstacles=obstacles,
            include_current_instances=not (all_conform or all_map_managed),
        )
        if edit_id in indoor_semantic_surface_choice:
            data["indoor_semantic_surface_choice"] = (
                indoor_semantic_surface_choice[edit_id])
        if edit_id in indoor_hardening:
            data["indoor_planar_hardening"] = indoor_hardening[edit_id]
        if is_indoor:
            pre_visibility_data = data
            pre_visibility_spec = deepcopy(edit.get("placement_spec") or {})
            visibility_metadata = _apply_indoor_visibility_scaling(
                scene=scene,
                camera=camera,
                edit=edit,
                map_data=data,
            )
            if visibility_metadata is not None:
                if visibility_metadata.get("applied"):
                    scaled_data = build_one_map(
                        scene=scene,
                        camera=camera,
                        scene_name=scene_name,
                        plan=plan,
                        edit=edit,
                        failed_indices=failed[edit_id],
                        columns=columns,
                        rows=rows,
                        obstacles=obstacles,
                        include_current_instances=not (
                            all_conform or all_map_managed),
                    )
                    if (not _map_capacity_sufficient(
                            scaled_data, failed[edit_id])
                            and _map_capacity_sufficient(
                                pre_visibility_data, failed[edit_id])):
                        rejected_dimensions = (
                            visibility_metadata.get(
                                "selected_proxy_dimensions_m"))
                        requested_scale = visibility_metadata.get(
                            "scale_factor")
                        edit["placement_spec"] = pre_visibility_spec
                        visibility_metadata = deepcopy(visibility_metadata)
                        visibility_metadata["applied"] = False
                        visibility_metadata["capacity_guard_applied"] = True
                        visibility_metadata["requested_scale_factor"] = (
                            requested_scale)
                        visibility_metadata["rejected_proxy_dimensions_m"] = (
                            rejected_dimensions)
                        visibility_metadata["scale_factor"] = 1.0
                        visibility_metadata["selected_proxy_dimensions_m"] = (
                            visibility_metadata.get(
                                "original_proxy_dimensions_m"))
                        visibility_metadata["reason"] = (
                            "visibility scaling would eliminate the required "
                            "surface capacity")
                        data = pre_visibility_data
                    else:
                        data = scaled_data
                indoor_visibility_scaling[edit_id] = visibility_metadata
                if edit_id in indoor_semantic_surface_choice:
                    data["indoor_semantic_surface_choice"] = (
                        indoor_semantic_surface_choice[edit_id])
                data["indoor_visibility_scaling"] = visibility_metadata
        if edit_id in indoor_semantic_surface_choice:
            data["indoor_semantic_surface_choice"] = (
                indoor_semantic_surface_choice[edit_id])
        if edit_id in indoor_hardening:
            data["indoor_planar_hardening"] = indoor_hardening[edit_id]
        data, fallback_edit, fallback_metadata = indoor_surface_fallback_map(
            scene=scene,
            camera=camera,
            plan=plan,
            edit=edit,
            original_map=data,
            failed_indices=failed[edit_id],
            columns=columns,
            rows=rows,
            obstacles=obstacles,
            include_current_instances=not (all_conform or all_map_managed),
            scene_name=scene_name,
            enabled=indoor_fallback_enabled,
        )
        if edit_id in indoor_hardening:
            data["indoor_planar_hardening"] = indoor_hardening[edit_id]
        if edit_id in indoor_visibility_scaling:
            data["indoor_visibility_scaling"] = indoor_visibility_scaling[edit_id]
        if fallback_edit is not None:
            edit["placement_spec"] = deepcopy(
                fallback_edit.get("placement_spec") or {})
            edit["surface_fallback"] = fallback_metadata
        active_edit = fallback_edit if fallback_edit is not None else edit
        data, afloat_fallback_edit, afloat_fallback_metadata = (
            afloat_surface_fallback_map(
                scene=scene,
                camera=camera,
                plan=plan,
                edit=active_edit,
                original_map=data,
                failed_indices=failed[edit_id],
                columns=columns,
                rows=rows,
                obstacles=obstacles,
                include_current_instances=not (
                    all_conform or all_map_managed),
                enabled=True,
            ))
        if afloat_fallback_edit is not None:
            edit["placement_spec"] = deepcopy(
                afloat_fallback_edit.get("placement_spec") or {})
            edit["surface_fallback"] = afloat_fallback_metadata
            active_edit = afloat_fallback_edit
        data, relaxation_edit, relaxation_metadata = (
            large_ground_relaxation_map(
                scene=scene,
                camera=camera,
                plan=plan,
                edit=active_edit,
                original_map=data,
                failed_indices=failed[edit_id],
                columns=columns,
                rows=rows,
                obstacles=obstacles,
                include_current_instances=not (
                    all_conform or all_map_managed),
                enabled=large_ground_relaxation_enabled,
            ))
        if relaxation_edit is not None:
            edit["placement_spec"] = deepcopy(
                relaxation_edit.get("placement_spec") or {})
            if relaxation_edit.get("min_sep") is not None:
                edit["min_sep"] = relaxation_edit.get("min_sep")
            edit["large_ground_relaxation"] = relaxation_metadata
            active_edit = relaxation_edit
        data, broad_search_edit, broad_search_metadata = outdoor_broad_search_map(
            scene=scene,
            camera=camera,
            plan=plan,
            edit=active_edit,
            original_map=data,
            failed_indices=failed[edit_id],
            columns=columns,
            rows=rows,
            obstacles=obstacles,
            include_current_instances=not (
                all_conform or all_map_managed),
            scene_name=scene_name,
            enabled=outdoor_broad_search_enabled,
        )
        if broad_search_edit is not None:
            edit["placement_spec"] = deepcopy(
                broad_search_edit.get("placement_spec") or {})
            edit["outdoor_broad_search_relaxation"] = broad_search_metadata
        annotate_projected_size(scene, camera, data)
        json_path = out_dir / f"{edit_id}_region_map.json"
        preview_path = out_dir / f"{edit_id}_region_map.png"
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        render_region_map(data, preview_path)
        index_rows.append({
            "edit_id": edit_id,
            "failed_instances": sorted(failed[edit_id]),
            "required_instance_count": len(failed[edit_id]),
            "region_count": len(data["regions"]),
            "feasible_cell_count": sum(
                cell.get("status") == "feasible" for cell in data["cells"]),
            "estimated_total_capacity": sum(
                int(region.get("estimated_capacity", 0))
                for region in data["regions"]),
            "surface_fallback": data.get("surface_fallback"),
            "indoor_semantic_surface_choice": data.get(
                "indoor_semantic_surface_choice"),
            "indoor_planar_hardening": data.get(
                "indoor_planar_hardening"),
            "indoor_visibility_scaling": data.get(
                "indoor_visibility_scaling"),
            "large_ground_relaxation": data.get("large_ground_relaxation"),
            "legacy_outdoor_ultra_loose_recovery": (
                legacy_ultra_recoveries.get(edit_id)),
            "outdoor_broad_search_relaxation": data.get(
                "outdoor_broad_search_relaxation"),
            "map_json": json_path.name,
            "map_preview": preview_path.name,
        })
        print(
            f"[region-map] {edit_id}: regions={len(data['regions'])} "
            f"feasible={index_rows[-1]['feasible_cell_count']}/"
            f"{len(data['cells'])}", flush=True)
        fallback = data.get("surface_fallback") or {}
        if fallback.get("applied"):
            print(
                f"[region-map] {edit_id}: indoor surface fallback "
                f"{fallback.get('original_surface_types')} → "
                f"{fallback.get('selected_surface_type')} "
                f"capacity={fallback.get('selected_estimated_total_capacity')}",
                flush=True)
        visibility = data.get("indoor_visibility_scaling") or {}
        if visibility.get("applied"):
            print(
                f"[region-map] {edit_id}: indoor visibility scale "
                f"{visibility.get('scale_factor')}x "
                f"projected={visibility.get('measured_projected_major_fraction'):.3f} "
                f"target={visibility.get('target_projected_major_fraction'):.3f}",
                flush=True)
        relaxation = data.get("large_ground_relaxation") or {}
        if relaxation.get("applied"):
            print(
                f"[region-map] {edit_id}: large ground relaxation "
                f"{relaxation.get('selected_label')} "
                f"capacity={relaxation.get('selected_estimated_total_capacity')}",
                flush=True)
        broad_search = data.get("outdoor_broad_search_relaxation") or {}
        if broad_search.get("applied"):
            print(
                f"[region-map] {edit_id}: outdoor broad search "
                f"capacity={broad_search.get('selected_estimated_total_capacity')} "
                f"min_sep={broad_search.get('selected_min_sep')}",
                flush=True)
        recovery = legacy_ultra_recoveries.get(edit_id) or {}
        if recovery.get("applied"):
            print(
                f"[region-map] {edit_id}: restored legacy outdoor ultra-loose "
                f"fields={recovery.get('restored_fields')}",
                flush=True)
    index = {
        "contract_version": 1,
        "source_blend": str(Path(source).resolve()),
        "plan": str(plan_path.resolve()),
        "review": None if review_path == "-" else str(Path(review_path).resolve()),
        "selection_scope": (
            "all_map_managed" if all_map_managed
            else "all_conform" if all_conform
            else "failed_map_managed"),
        "scene_name": scene_name,
        "scene_location": scene_location,
        "indoor_surface_fallback_enabled": indoor_fallback_enabled,
        "indoor_semantic_surface_choice_enabled": (
            indoor_semantic_surface_choice_enabled),
        "semantic_support_grid": semantic_support_index,
        "indoor_semantic_surface_choice": indoor_semantic_surface_choice,
        "indoor_planar_hardening": indoor_hardening,
        "indoor_visibility_scaling": indoor_visibility_scaling,
        "legacy_outdoor_ultra_loose_recovery": legacy_ultra_recoveries,
        "large_ground_relaxation_enabled": large_ground_relaxation_enabled,
        "outdoor_broad_search_relaxation_enabled": (
            outdoor_broad_search_enabled),
        "outdoor_ultra_loose_relaxation_enabled": False,
        "maps": index_rows,
    }
    index_path = out_dir / "region_map_index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    applied_fallbacks = [
        row["surface_fallback"] for row in index_rows
        if (row.get("surface_fallback") or {}).get("applied")
    ]
    applied_relaxations = [
        row["large_ground_relaxation"] for row in index_rows
        if (row.get("large_ground_relaxation") or {}).get("applied")
    ]
    applied_broad_search = [
        row["outdoor_broad_search_relaxation"] for row in index_rows
        if (row.get("outdoor_broad_search_relaxation") or {}).get("applied")
    ]
    if (indoor_semantic_surface_choice or indoor_hardening
            or indoor_visibility_scaling
            or legacy_ultra_recoveries
            or applied_fallbacks or applied_relaxations
            or applied_broad_search):
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    if applied_fallbacks:
        fallback_path = out_dir / "surface_fallbacks.json"
        fallback_path.write_text(
            json.dumps({
                "contract_version": 1,
                "scene_name": scene_name,
                "scene_location": scene_location,
                "applied": applied_fallbacks,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"INDOOR_SURFACE_FALLBACK_APPLIED → {fallback_path}")
    if applied_relaxations:
        relaxation_path = out_dir / "large_ground_relaxations.json"
        relaxation_path.write_text(
            json.dumps({
                "contract_version": 1,
                "scene_name": scene_name,
                "scene_location": scene_location,
                "applied": applied_relaxations,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"LARGE_GROUND_RELAXATION_APPLIED -> {relaxation_path}")
    if applied_broad_search:
        broad_search_path = out_dir / "outdoor_broad_search_relaxations.json"
        broad_search_path.write_text(
            json.dumps({
                "contract_version": 1,
                "scene_name": scene_name,
                "scene_location": scene_location,
                "applied": applied_broad_search,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"OUTDOOR_BROAD_SEARCH_RELAXATION_APPLIED -> {broad_search_path}")
    # ★盡量模式★(2026-09-11)：容量不足時把需求降到容量，只有★一個都放不下★
    #   才算 INFEASIBLE。先前只要少一個格子就整個 case 失敗，而 Step 8 的失敗大宗
    #   正是這個(0910-0911 兩批 50 個 case)。
    best_effort = best_effort_placement()
    reduced = []
    for row in index_rows:
        capacity = row["estimated_total_capacity"]
        if not best_effort or capacity >= row["required_instance_count"] or capacity <= 0:
            continue
        reduced.append({"edit_id": row["edit_id"],
                        "requested": row["required_instance_count"],
                        "capacity": capacity})
        print(f"  ⚠️ {row['edit_id']}: 容量 {capacity} < 需求 "
              f"{row['required_instance_count']} ⇒ 降成 {capacity} 個(盡量模式)")
        row["required_instance_count"] = capacity
    infeasible = [{
        "edit_id": row["edit_id"],
        "required_instance_count": row["required_instance_count"],
        "estimated_total_capacity": row["estimated_total_capacity"],
        "reason": "insufficient machine-feasible region capacity",
    } for row in index_rows
        if row["estimated_total_capacity"] < row["required_instance_count"]]
    feasibility = {
        "contract_version": 1,
        "status": "INFEASIBLE" if infeasible else "PASS",
        "infeasible_edits": infeasible,
        "best_effort_reductions": reduced,
    }
    feasibility_path = out_dir / "region_map_feasibility.json"
    feasibility_path.write_text(
        json.dumps(feasibility, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"PLACEMENT_REGION_MAP_READY → {index_path}")
    if infeasible:
        print(f"PLACEMENT_REGION_MAP_INFEASIBLE → {feasibility_path}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

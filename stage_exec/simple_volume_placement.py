#!/usr/bin/env python
"""簡單版空中/水下 hero 擺放(2026-09-12 B 方案；BI_HERO_SIMPLE=1 才啟用)。

只做三件事，對應使用者的三條需求：
  1. 小的靠近：深度階梯由近到遠，第一個「不擋鏡頭」(單隻投影 ≤ MAX_FRAME_FRACTION)
     的深度就用。蝴蝶/蜂鳥在 2m 就只佔 1~2%，自然停在最近。
  2. 大的推遠：帆船在 2m 佔滿畫面，自然一路退到幾十公尺才過關。
  3. 放看得到的空位：在那個深度把畫面框切成粗網格，跳過「相機到落點的射線先撞到
     地形/物件」「箱子壓到既有 placeholder」「離其他 hero 太近」的點。★每隻各自★從最近
     的深度往後找第一個放得下的點(同組不必同深度，第二隻自然稍遠一點)；退到底還放不下
     的放到框外側邊；再放不下才少放幾隻。印警告，不報錯。

地面物件(conform/upright/tilted/rooted/afloat)同一套規則：格點的相機射線打到的表面就是落點，
深度=打到的距離，一樣是「最近、不擋鏡頭、空位」；表面種類照 surface_policy(紅鶴要 liquid、桌子要沙)。

原本的 region map 路徑(build_placement_region_map + apply_placement_region_selection)
完整保留。被這支處理過的 edit 會標 placement_route="simple_volume"，region map 的
all_map_managed_instances / map_managed_failed_instances 會略過它。
下游不變：positions 寫成「不含 anchoring 高度」的錨點(跟 region map 一樣，高度由階段 C 加)，
screen_position_projection="explicit_3d_region_map"，apply_plan 直接照座標放。

用法：simple_volume_placement.py -- SOURCE_BLEND PLAN_JSON OUT_DIR FRAME [--scene NAME]
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import sys
from copy import deepcopy
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "stage_exec"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import placement_contract as plc  # noqa: E402
import placement_modes as pm  # noqa: E402
import surface_raycast_cache as srcache  # noqa: E402
from placement_region_contract import (  # noqa: E402
    aabb_overlap_fraction,
    is_soft_scene_obstacle,
    rotated_aabb_bounds,
    uses_3d_volume_map,
    uses_surface_map,
)
from placement_surface_evaluator import VisibleSurfaceEvaluator  # noqa: E402
from build_placement_region_map import (  # noqa: E402
    TerrainClearanceEvaluator,
    _airborne_motion_clearance,
    _ground_motion_clearance,
    _infer_scene_name,
    _project_box_bounds,
    _submerged_motion_clearance,
    proxy_half_extents,
    scene_obstacles,
)

try:
    from world_motion import PARALLEL_MOTION_PRESETS  # noqa: E402  placer 共用 seed 的同一份清單
except ImportError:
    PARALLEL_MOTION_PRESETS = {"air_glide", "airborne_glide", "air_soar", "aquatic_swim",
                               "ground_forward", "directed_ground_traverse", "afloat_forward"}

ROUTE = "simple_volume"
# ★0.08→0.14★(2026-09-13 使用者：東西都太小，地面動物被推到 23~93m)。仍遠低於 BLOCKING_FRACTION
MAX_FRAME_FRACTION = 0.14   # 地面物件：先找佔畫面 ≤14% 的位置(小的自然近、大的自然遠)
AIR_FRAME_FRACTION = 0.20   # 空中/水下的小東西(≤SMALL_AIR_M)：盡量靠鏡頭，佔到 1/5 都可以(使用者 2026-09-12)
SMALL_AIR_FRAME_FRACTION = 0.08  # 空中的大東西(燈籠花 0.8m)照地面規則，不然 3.8m 處就佔 18% 貼在臉上
SMALL_AIR_M = 0.30          # 「小東西」的門檻：最長邊 ≤ 30cm(蝴蝶/蜂鳥/螢火蟲)
BLOCKING_FRACTION = 0.25    # 上面的找不到(大東西如橋)才放寬到 25%；超過 25% 才算真的擋鏡頭
MIN_SURFACE_FRACTION = 0.002   # 地面物在框內至少佔 0.2% 畫面(1600x900 約 40x30 px)，太小等於沒放
# ★先要求「看得出是什麼」★(2026-09-13)：第三輪實測狐狸 23m 佔 0.7%、馴鹿 93m 佔 0.2%、
#   企鵝 112m 佔 0.0% —— 都「在畫面內」但等於沒放。所以整組等級先跑一遍「≥1.2%」，
#   全部失敗才降回 MIN_SURFACE_FRACTION(寧可小也要放，不報錯)。
PREFERRED_MIN_FRACTION = 0.012
RELAXED_SLOPE_DEG = 60.0    # 地面物件：director 給的坡度上限內找不到才放寬(forest seed0 相機前全是 45° 以上的坡，狐狸被推到 23m；使用者：看得到比站得正重要)
# ★大東西不能放到陡壁上★(2026-09-13 使用者：冰屋跟北極熊掛在旁邊超陡的冰壁上很怪，
#   但企鵝在上面沒關係)。小動物趴在斜坡上看起來合理，冰屋/大型動物不行。
BIG_FOOTPRINT_M = 0.6       # 水平半徑超過這個就算「大東西」
BIG_RELAXED_SLOPE_DEG = 30.0


def relaxed_slope_for(half) -> float:
    return (RELAXED_SLOPE_DEG if max(float(half[0]), float(half[1])) <= BIG_FOOTPRINT_M
            else BIG_RELAXED_SLOPE_DEG)
FOOTPRINT_GAP_M = 0.75      # 四角支撐高度差上限(跟 review_placeholders 的 footprint_support 同值)
MIN_DEPTH_M = 0.6           # 防穿鏡；12cm 的蝴蝶在 0.6m 約佔 10%
NEAR_SIZE_FACTOR = 5.0      # 空中/水中：最近距離至少是最長邊的 5 倍(2026-09-13 blue_tang 0.67m 塞滿畫面)
MAX_DEPTH_M = 80.0
DEPTH_STEP = 1.3            # 階梯每階 ×1.3：0.6, 0.78, 1.0, 1.3, 1.7, 2.2, 2.9 … 80
GRID_COLUMNS, GRID_ROWS = 9, 5
SIDE_BANDS = ((-0.30, -0.08), (1.08, 1.30))   # 框外側邊(畫面 x 座標)
TERRAIN_MARGIN_M = 0.25


def _hash01(*parts) -> float:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _depth_ladder(start: float) -> list[float]:
    depths, depth = [], max(MIN_DEPTH_M, float(start))
    while depth <= MAX_DEPTH_M:
        depths.append(round(depth, 3))
        depth *= DEPTH_STEP
    return depths


def _grid_points(bounds, columns: int, rows: int) -> list[list[float]]:
    x0, y0, x1, y1 = map(float, bounds)
    return [[x0 + (x1 - x0) * (c + 0.5) / columns,
             y0 + (y1 - y0) * (r + 0.5) / rows]
            for r in range(rows) for c in range(columns)]


def _side_points(bounds, rows: int) -> list[list[float]]:
    y0, y1 = float(bounds[1]), float(bounds[3])
    points = []
    for low, high in SIDE_BANDS:
        for i in range(3):
            x = low + (high - low) * (i + 0.5) / 3
            points.extend([x, y0 + (y1 - y0) * (r + 0.5) / rows] for r in range(rows))
    return points


def _is_liquid(name) -> bool:
    return "liquid" in str(name or "").lower()


def _ray_clear(scene, origin, direction, depth: float, reserve: float,
               *, submerged: bool):
    """相機到落點之間不能先撞到東西。水下的落點可以穿過水面(只有水面不算)。"""
    start, travelled = Vector(origin), 0.0
    for _ in range(4):
        hit = srcache.raycast(scene, start, direction,
                              distance=depth + reserve - travelled + 0.01)
        if hit is None:
            return True, None
        distance = travelled + float(hit[0])
        if submerged and _is_liquid(hit[2]) and distance < depth - reserve:
            travelled = distance + 0.05
            start = Vector(hit[1]) + Vector(direction) * 0.05
            continue
        return False, f"ray_blocked_by:{hit[2]}"
    return False, "ray_blocked:too_many_surfaces"


def _underwater(scene, point) -> bool:
    hit = srcache.raycast(scene, point, Vector((0.0, 0.0, 1.0)), distance=500.0)
    return hit is not None and _is_liquid(hit[2])


class Placer:
    def __init__(self, *, scene, camera, scene_name: str, plan: dict):
        self.scene, self.camera, self.scene_name = scene, camera, scene_name
        self.evaluator = VisibleSurfaceEvaluator.from_camera(
            scene=scene, camera=camera, scene_name=scene_name,
            surface_policy={}, footprint_half_extents=(0.5, 0.5),
            box_half_height=0.5)
        self.origin = Vector(self.evaluator.origin)
        self.terrain = TerrainClearanceEvaluator(scene)
        self.all_obstacles = scene_obstacles(
            include_render_scatter=any(
                edit.get("anchoring") == "submerged"
                for edit in plan.get("hero_edits", [])),
            plan=plan, cameras=[camera])
        self.placed: list[dict] = []     # 跨 edit 共用：世界座標、箱子、min_sep

    def obstacles_for(self, anchoring: str) -> list[dict]:
        """跟 region map 一樣只看這個模式的障礙物；雲層/地形這種超大箱子不算。
        地面物件不把 GN scatter 實例(珊瑚/草/石頭)當障礙——陸地場景本來就不會載入它們，
        水下場景載入是給魚用的；礁石上的海葵本來就長在珊瑚之間。"""
        rows = []
        surface = uses_surface_map(anchoring)
        for row in self.all_obstacles:
            if row.get("map_modes", "all") not in {"all", anchoring}:
                continue
            if surface and row.get("source") == "evaluated_instance":
                continue
            bounds = row["world_bounds"]
            if max(bounds[axis + 3] - bounds[axis] for axis in range(3)) > 50.0:
                continue
            center = Vector(tuple((bounds[axis] + bounds[axis + 3]) * 0.5 for axis in range(3)))
            if (center - self.origin).length > MAX_DEPTH_M + 40.0:
                continue
            rows.append(row)
        return rows

    # ── 單點可行性 ────────────────────────────────────────────────
    def _probe(self, spec, point, depth: float) -> tuple[dict | None, str | None]:
        direction = self.evaluator.camera_direction(*point)
        world = self.origin + direction * depth
        ok, why = _ray_clear(self.scene, self.origin, direction, depth,
                             spec["body_reserve"], submerged=spec["submerged"])
        if not ok:
            return None, why
        # 碰撞/地形用「物體 + 位移掃過範圍」的箱子；投影(擋不擋鏡頭)只看物體本身——
        #   air_glide 的掃過範圍 4m×4m，算進去 12cm 的蝴蝶會被當成 8m 推到 20m 外(12_cliff_anim 實測)
        box = rotated_aabb_bounds(world, spec["half"], clearance=spec["clearance"])
        body_box = rotated_aabb_bounds(world, spec["half"])
        if not self.terrain.clears(box, margin=TERRAIN_MARGIN_M):
            return None, "terrain_too_close"
        under = _underwater(self.scene, world)
        if spec["submerged"] and not under:
            return None, "not_underwater"
        if not spec["submerged"] and under:
            return None, "underwater"
        for obstacle in spec["obstacles"]:
            if aabb_overlap_fraction(box, obstacle["world_bounds"]) > 0.0:
                return None, f"obstacle:{obstacle.get('label') or obstacle.get('object')}"
        ndc = _project_box_bounds(self.scene, self.camera, body_box)
        area = max(0.0, ndc[2] - ndc[0]) * max(0.0, ndc[3] - ndc[1])
        return {"world": [float(v) for v in world], "box": box, "body_box": body_box,
                "screen": [float(point[0]), float(point[1])],
                "depth": float(depth), "area": float(area)}, None

    def _separated(self, spec, row, chosen: list[dict]) -> bool:
        for other in [*self.placed, *chosen]:
            # 同一筆 edit「只比本體」的前提是平行運動(placer 共用 seed)；地面走動不是平行的，照樣比掃過範圍
            same_edit = (str(other.get("edit_id") or spec["edit_id"]) == str(spec["edit_id"])
                         and spec.get("parallel_motion", False))
            # ★水平外接半徑相加★(2026-09-13 coast：5m 的 kayak 與紅鶴只隔 2.5m，擺放時
            #   箱子用的是某個 yaw，placer 隨機轉另一個 yaw ⇒ 轉過去就疊在一起，
            #   整個 case 被動態碰撞判死)。用半徑和就跟 yaw 無關。
            radius = float(spec["radius"]) + float(other.get("radius", spec["radius"]))
            gap = max(spec["min_sep"], float(other.get("min_sep", spec["min_sep"])),
                      radius + 0.30)
            if math.dist(row["world"], other["world"]) < gap:
                return False
            # ★互不重疊看「含位移掃過範圍」的箱子★(2026-09-12 canyon：只比本體，
            #   兩隻蝴蝶的滑行路徑在第 22~29 幀交叉 ⇒ placer 的動畫碰撞判死整個 case)。
            #   掃過範圍已隨 0.2 倍慢速縮小，不會再把蝴蝶推遠。
            # 同一筆 edit 的個體是平行運動(placer 用同一個 seed) ⇒ 只比本體，不比掃過範圍；
            #   跨 edit / 地形 / 場景障礙照樣用含掃過範圍的箱子。
            a = row["body_box"] if (same_edit and "body_box" in row) else row["box"]
            b = (other.get("body_box") if same_edit else None) or other["box"]
            if aabb_overlap_fraction(a, b) > 0.0:
                return False
        return True

    # ── 地面物件：格點射線打到的表面就是落點 ──────────────────────
    def _probe_surface(self, spec, point) -> tuple[dict | None, str | None]:
        cache = spec["cache"].setdefault(("surface", id(spec)), {})
        key = (round(point[0], 4), round(point[1], 4), spec["policy"].get("max_slope_deg"),
               bool(spec["policy"].get("require_full_footprint_support", True)),
               bool(spec.get("ignore_soft")),
               tuple(spec["policy"].get("required_surface_types") or ()),
               tuple(spec["policy"].get("required_surface_names") or ()))
        if key in cache:
            return cache[key]
        direction = self.evaluator.camera_direction(*point)
        hit = srcache.raycast(self.scene, self.origin, direction, distance=400.0)
        if hit is None:
            cache[key] = (None, "ray_no_hit")
            return cache[key]
        distance, location, surface, normal = hit
        slope = math.degrees(math.acos(max(0.0, min(1.0, abs(float(normal.z))))))
        issue = plc.surface_policy_issue(spec["policy"], surface_name=surface, slope_deg=slope)
        if issue:
            cache[key] = (None, f"surface:{issue[:40]}")
            return cache[key]
        half = spec["half"]
        if spec["policy"].get("require_full_footprint_support", True):
            # 四角支撐：跟審查同一條規則(每角往下打到同種表面、坡度合格、高度差 ≤ 0.75m)
            top = float(location.z) + max(2.0, 2.0 * float(half[2]))
            for sx in (-1.0, 1.0):
                for sy in (-1.0, 1.0):
                    corner = Vector((float(location.x) + sx * float(half[0]),
                                     float(location.y) + sy * float(half[1]), top))
                    below = srcache.raycast(self.scene, corner, Vector((0.0, 0.0, -1.0)), distance=1000.0)
                    if below is None:
                        cache[key] = (None, "footprint:no_support")
                        return cache[key]
                    c_slope = math.degrees(math.acos(max(0.0, min(1.0, abs(float(below[3].z))))))
                    c_issue = plc.surface_policy_issue(spec["policy"], surface_name=below[2], slope_deg=c_slope)
                    if c_issue or abs(float(below[1].z) - float(location.z)) > FOOTPRINT_GAP_M:
                        cache[key] = (None, "footprint:" + (c_issue or "height_gap")[:30])
                        return cache[key]
        center = Vector(location) + Vector((0.0, 0.0, float(half[2])))
        box = rotated_aabb_bounds(center, half, clearance=spec["clearance"])
        body_box = rotated_aabb_bounds(center, half)
        for obstacle in spec["obstacles"]:
            if spec.get("ignore_soft") and is_soft_scene_obstacle(
                    label=obstacle.get("label"), source=obstacle.get("source"),
                    object_name=obstacle.get("object")):
                continue    # 最後一級：軟植被(跟舊路徑 hard_scene_obstacles 同一份定義)不算障礙
            if aabb_overlap_fraction(box, obstacle["world_bounds"]) > 0.0:
                cache[key] = (None, f"obstacle:{obstacle.get('label') or obstacle.get('object')}")
                return cache[key]
        # 投影只看物體本身(跟空中那條一樣)：掃過範圍算進去的話，會走動的動物就被推遠
        ndc = _project_box_bounds(self.scene, self.camera, body_box)
        area = max(0.0, ndc[2] - ndc[0]) * max(0.0, ndc[3] - ndc[1])
        cache[key] = ({"world": [float(v) for v in location], "box": box,
                       "body_box": body_box,
                       "screen": [float(point[0]), float(point[1])],
                       "depth": float(distance), "area": float(area),
                       "surface": str(surface),
                       "surface_normal": [float(v) for v in normal]}, None)
        return cache[key]

    def _fill_surface(self, spec, points, count: int, chosen: list[dict],
                      reasons: dict) -> list[dict]:
        """最近的可行點先放(小東西自然靠鏡頭；大東西近處會被 blocks_camera 擋掉、自然落遠)。

        ★太小就不算可行★(2026-09-12 snowy：雪兔全落在 91m，投影 0.0% 等於沒放)。
        地面物在框內至少要佔 MIN_SURFACE_FRACTION，不到就當這個點不可行，換下一級條件。
        """
        result: list[dict] = []
        rows = []
        for index, point in enumerate(points):
            row, why = self._probe_surface(spec, point)
            if row is None:
                reasons[why] = reasons.get(why, 0) + 1
            elif row["area"] > spec["max_fraction"]:
                reasons["blocks_camera"] = reasons.get("blocks_camera", 0) + 1
            elif row["area"] < spec.get("min_fraction", 0.0):
                reasons["too_small"] = reasons.get("too_small", 0) + 1
            else:
                rows.append((row["depth"], _hash01(spec["edit_id"], index), row))
        rows.sort(key=lambda item: (item[0], item[1]))
        for _ in range(count):
            picked = None
            for _depth, _tie, row in rows:
                if any(row is other for other in [*chosen, *result]):
                    continue
                if not self._separated(spec, row, [*chosen, *result]):
                    continue
                picked = dict(row)
                break
            if picked is None:
                break
            result.append(picked)
        return result

    # ── 一個深度、一組點、放 count 隻 ─────────────────────────────
    def _fill(self, spec, points, depth: float, count: int, chosen: list[dict],
              reasons: dict) -> list[dict]:
        scatter = spec["arrangement"] == "scatter"
        center = spec["center"]
        cache = spec["cache"].setdefault((round(depth, 3), id(points)), {})
        result: list[dict] = []
        for _ in range(count):
            def rank(item):
                index, point = item
                if scatter and (result or chosen):
                    spread = min(math.dist(point, row["screen"])
                                 for row in [*chosen, *result])
                    return (-spread, _hash01(spec["edit_id"], index))
                return (math.dist(point, center), _hash01(spec["edit_id"], index))
            picked = None
            for index, point in sorted(enumerate(points), key=rank):
                if index not in cache:
                    cache[index] = self._probe(spec, point, depth)
                row, why = cache[index]
                if row is None:
                    reasons[why] = reasons.get(why, 0) + 1
                    continue
                if not self._separated(spec, row, [*chosen, *result]):
                    reasons["too_close_to_other_hero"] = reasons.get("too_close_to_other_hero", 0) + 1
                    continue
                picked = dict(row)
                break
            if picked is None:
                break
            result.append(picked)
        return result

    # ── 一個 edit ──────────────────────────────────────────────
    def place_edit(self, edit: dict) -> dict:
        original_edit = deepcopy(edit)
        count = int(edit.get("n", 1))
        factory = str(edit["factory"])
        anchoring = str(edit.get("anchoring") or "")
        code_from = str(edit.get("code_from") or factory)
        norm = plc.normalize_placement_spec(
            edit.get("placement_spec"), count=count, anchoring=anchoring,
            factory=factory, code_from=code_from)
        declared = norm.get("proxy_dimensions_m")
        inferred = None if declared is not None else plc.inferred_proxy_dimensions(
            factory=factory, code_from=code_from, anchoring=anchoring,
            semantic_target=norm.get("semantic_target"))
        # ★尺寸以 director 給的為準★(2026-09-12 forest)：placer 會把真資產縮到 proxy_dimensions_m，
        #   但場景若已有同 factory 的佔位框(forest 原生蜻蜓 2m 立方)，proxy_half_extents 會量那個框，
        #   12cm 的蝴蝶就被當成 2m 的東西推到 12m 外，縮成 3cm 後根本看不見。
        sized = declared or inferred
        half = (tuple(float(v) / 2.0 for v in sized) if sized
                else proxy_half_extents(factory, None, None))
        submerged = anchoring == "submerged"
        clearance = (_submerged_motion_clearance(edit) if submerged
                     else _ground_motion_clearance(edit) if uses_surface_map(anchoring)
                     else _airborne_motion_clearance(edit))
        # ★一次性滑翔只預留前 35% 的路徑★(2026-09-13)：air_glide 改成真的往前飛 6m 之後，
        #   整段掃過範圍 ±7m 會讓 12cm 的蝴蝶「離地形/樹 7m 內都不能放」⇒ 又被推遠。
        #   hero 對 hero 的碰撞審查看的是彼此的掃過箱(這裡同一份)，地形那邊只保證起飛段。
        _preset = str((edit.get("world_motion") or {}).get("preset") or "none")
        if _preset in ("air_glide", "airborne_glide", "aquatic_swim"):
            # aquatic_swim 也一樣(2026-09-13 改成不套 0.2 之後，整段 2m 預留會把魚推遠)；
            #   同一群平行游、彼此只比本體(placer 同 seed)。
            clearance = tuple(float(v) * 0.35 for v in clearance)
        elif _preset == "air_soar":
            # 鳥是★橫越畫面★飛(placer 把 heading 設成相機右軸)，整段 13m 的球形預留會把牠推到天邊；
            #   只預留起飛段，路徑上的地形碰撞由 placer 的動態碰撞審查把關。
            clearance = tuple(float(v) * 0.18 for v in clearance)
        reserve = math.sqrt(sum((float(half[i]) + float(clearance[i])) ** 2
                                for i in range(3)))
        body_reserve = math.sqrt(sum(float(half[i]) ** 2 for i in range(3)))
        bounds = norm["screen_bounds"]
        spec = {
            "edit_id": code_from, "half": half, "clearance": clearance,
            "reserve": reserve, "body_reserve": body_reserve, "submerged": submerged,
            # 間距跟著物體大小：地面物件 3 倍最長邊(真資產可能比 proxy 寬，11_coral_anim 海葵 1.5 倍就撞)，
            #   空中/水下 1.5 倍(蝴蝶要能擠在鏡頭前)；最小 0.3m，最大維持 2.5m(箱子重疊另有檢查)
            # 地面 3 倍最長邊(真資產常比 proxy 寬)，空中/水下 2 倍(蝴蝶要能擠在鏡頭前但不能互穿)
            # ★一群就要擠★(2026-09-13 使用者：企鵝要 9 隻以上)：count≥6 時間距砍半，
            #   20 隻企鵝才塞得進鏡頭前那片冰(箱子重疊另有檢查，不會互穿)。
            # 真資產是「等比 fit 到 proxy 的最長軸」，其他軸可能比 proxy 寬(企鵝 proxy 0.5 寬、
            #   fit 完 0.85 寬) ⇒ 外接半徑補一個以最長軸為準的下限。
            "parallel_motion": str((edit.get("world_motion") or {}).get("preset") or "none")
                               in PARALLEL_MOTION_PRESETS,
            "radius": max(math.hypot(float(half[0]), float(half[1])),
                          0.7 * max(float(v) for v in half)),
            # ⚠️ 群體下限 1.2→1.5m(14:30 arctic：1.2m 斜對角剛好貼到，被動態碰撞判死)
            "min_sep": min(1.5 if count >= 6 else 2.5,
                           max(0.3, (3.0 if uses_surface_map(anchoring) else 2.0)
                               * (1.0 if count >= 6 else 2.0) * max(half))),
            "arrangement": str(norm.get("arrangement") or "scatter"),
            "obstacles": self.obstacles_for(anchoring),
            "policy": norm.get("surface_policy") or {},
            "surface": uses_surface_map(anchoring),
            "center": [(bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0],
        }
        altitude = float(pm.params_for(anchoring)[1])
        spec["cache"] = {}
        # 要放一群(≥6)就把網格加密，不然 45 個格點扣掉不可行的就不夠站 20 隻企鵝
        columns, rows = ((GRID_COLUMNS * 2 - 1, GRID_ROWS * 2 - 1) if count >= 6
                         else (GRID_COLUMNS, GRID_ROWS))
        grid = _grid_points(bounds, columns, rows)
        full_frame_grid = _grid_points((0.06, 0.08, 0.94, 0.92), columns, rows)
        side_points = _side_points(bounds, rows)
        reasons: dict[str, int] = {}
        # 規則 1+2：階梯由近到遠，單隻投影 > MAX_FRAME_FRACTION 的深度算擋鏡頭，跳過
        ladder_log, ladder_area = [], {}
        # ★小東西也不能貼到鏡頭上★(2026-09-13 under_water：最近的 blue_tang 在 0.67m，投影箱算 15%、
        #   實際佔畫面六成以上 —— 等比 fit 後真資產比 proxy 寬，貼近鏡頭時投影箱又低估)。
        #   階梯起點至少是「最長邊的 NEAR_SIZE_FACTOR 倍」：30cm 的魚從 1.5m 起、12cm 的蝴蝶仍是 0.6m。
        ladder_start = max(body_reserve + 0.5, NEAR_SIZE_FACTOR * 2.0 * max(float(v) for v in half))
        for depth in ([] if spec["surface"] else _depth_ladder(ladder_start)):
            center_world = self.origin + self.evaluator.camera_direction(*spec["center"]) * depth
            ndc = _project_box_bounds(self.scene, self.camera, rotated_aabb_bounds(
                center_world, half))
            area = max(0.0, ndc[2] - ndc[0]) * max(0.0, ndc[3] - ndc[1])
            ladder_area[depth] = area
            ladder_log.append({"depth_m": depth, "area": round(area, 4),
                               "result": "blocks_camera" if area > BLOCKING_FRACTION
                               else "usable" if area <= AIR_FRAME_FRACTION else "large"})
        # 規則 3：每隻各自從最近的深度開始，框內第一個放得下的點就停；框內都不行才去側邊。
        #   門檻兩級：先找 ≤8% 的位置；整個框都沒有(橋這種大東西)才放寬到 25%。
        best: list[dict] = []
        off_screen = 0
        base_policy = dict(spec["policy"])
        base_slope = base_policy.get("max_slope_deg")
        base_footprint = bool(base_policy.get("require_full_footprint_support", True))
        # 等級由嚴到鬆(比照舊路徑 large_ground_relaxation 的尾段)：
        #   門檻 8%→25%；坡度 director 給的→45°；四角支撐 要→不要；軟植被障礙 算→不算
        first_fraction = (
            MAX_FRAME_FRACTION if spec["surface"]
            else AIR_FRAME_FRACTION if max(half) * 2.0 <= SMALL_AIR_M
            else SMALL_AIR_FRAME_FRACTION)
        # 每一級都帶「最小可見大小」；最後一級才放掉(寧可小也要放)
        min_frac = MIN_SURFACE_FRACTION if spec["surface"] else 0.0
        tiers = [dict(slope=base_slope, fraction=first_fraction, footprint=base_footprint, soft=False, minf=min_frac),
                 dict(slope=base_slope, fraction=BLOCKING_FRACTION, footprint=base_footprint, soft=False, minf=min_frac)]
        relaxed_slope_cap = relaxed_slope_for(half)
        if spec["surface"] and base_slope is not None and float(base_slope) < relaxed_slope_cap:
            tiers += [dict(slope=relaxed_slope_cap, fraction=MAX_FRAME_FRACTION, footprint=base_footprint, soft=False, minf=min_frac),
                      dict(slope=relaxed_slope_cap, fraction=BLOCKING_FRACTION, footprint=base_footprint, soft=False, minf=min_frac)]
        # ★最後一級放寬「一定要踩哪種表面」★(2026-09-13)：arctic 的冰面在 blend 裡叫
        #   liquid，企鵝的 required OpaqueTerrain 把 20 隻擋成 3 隻。只對「不是水上/水中
        #   的東西」放寬(afloat 的荷花、submerged 的魚照樣必須在水裡)。
        land_anchoring = anchoring not in ("afloat", "submerged")
        if spec["surface"]:
            last_slope = tiers[-1]["slope"]
            tiers += [dict(slope=last_slope, fraction=BLOCKING_FRACTION, footprint=False, soft=False, minf=min_frac),
                      dict(slope=last_slope, fraction=BLOCKING_FRACTION, footprint=False, soft=True, minf=min_frac),
                      # 最後一級：放掉最小大小，遠也要放(不然整組掉光)
                      dict(slope=last_slope, fraction=BLOCKING_FRACTION, footprint=False, soft=True, minf=0.0)]
            if land_anchoring:
                tiers += [dict(slope=last_slope, fraction=BLOCKING_FRACTION, footprint=False,
                               soft=True, minf=min_frac, any_surface=True),
                          dict(slope=last_slope, fraction=BLOCKING_FRACTION, footprint=False,
                               soft=True, minf=0.0, any_surface=True)]
        # ★兩輪★：先整組等級要求「看得出來」(≥1.2% 畫面)，都放不下才降回原本的最小值。
        if spec["surface"]:
            tiers = [dict(t, minf=max(float(t.get("minf") or 0.0), PREFERRED_MIN_FRACTION))
                     for t in tiers] + tiers
        relaxed_slope = None
        relaxed_footprint = False
        for _ in range(count):
            picked = None
            # 先把框內的所有等級試完，都不行才去側邊；側邊不限大小(反正在框外，不會擋鏡頭)
            side_tiers = tiers + [dict(tiers[-1], fraction=1e9)]
            # ★空中/水中：先找「看得出來」的位置，director 的範圍不行就換整個畫面★
            #   (2026-09-13 under_water：螃蟹 director 給中下方，水下相機往那裡看全是海床/珊瑚，
            #    近處被擋 213 次 ⇒ 只剩 57m 的縫、投影 0.4%。上方明明是開闊水域。)
            #   第三個欄位 = 深度只挑投影 ≥ PREFERRED_MIN_FRACTION 的；最後兩組照舊(可以遠、可以放旁邊)。
            # ★地面也在框外側邊之前先試整個畫面★(2026-09-13 arctic：北極熊的框 y 0.06–0.45 全是水，
            #   唯一的浮冰在 y≈0.47 ⇒ 一隻都放不下，交回 region map，它不知道企鵝已經放了就疊上去)
            point_sets = ((grid, False, False), (full_frame_grid, False, False), (side_points, True, False))
            if not spec["surface"]:
                point_sets = ((grid, False, True), (full_frame_grid, False, True)) + point_sets
            for points, is_side, need_visible in point_sets:
                for tier in (side_tiers if is_side else tiers):
                    slope_limit, fraction = tier["slope"], tier["fraction"]
                    spec["policy"] = {**base_policy, "max_slope_deg": slope_limit,
                                      "require_full_footprint_support": tier["footprint"]}
                    if tier.get("any_surface"):
                        spec["policy"].update(required_surface_types=[],
                                              required_surface_names=[])
                    spec["ignore_soft"] = tier["soft"]
                    spec["max_fraction"] = fraction
                    spec["min_fraction"] = tier.get("minf", 0.0)
                    if spec["surface"]:
                        rows = self._fill_surface(spec, points, 1, best, reasons)
                    else:
                        # ★同組分散深度★(使用者 2026-09-12 cliff：8 隻蝴蝶全貼在 0.6~1.3m 同一個近平面，
                        #   像貼在畫面前的圖層)。第 k 隻從階梯第 k 階開始找(仍限投影 ≥0.5% 看得見)，
                        #   近處找不到再回頭找更近的。
                        depths = [d for d, a in ladder_area.items()
                                  if a <= fraction
                                  and (not need_visible or a >= PREFERRED_MIN_FRACTION)]
                        if not depths:
                            continue
                        visible = [d for d in depths if ladder_area[d] >= 0.005]
                        start = min(len(best), max(0, len(visible) - 1))
                        rows = []
                        for depth in depths[start:] + depths[:start]:
                            rows = self._fill(spec, points, depth, 1, best, reasons)
                            if rows:
                                break
                    if rows:
                        picked = rows[0]
                        picked["off_screen"] = is_side
                        if fraction > first_fraction:
                            picked["relaxed_to_blocking"] = True
                        if slope_limit != base_slope:
                            picked["relaxed_slope_deg"] = slope_limit
                            relaxed_slope = slope_limit
                        if base_footprint and not tier["footprint"]:
                            picked["relaxed_footprint"] = True
                            relaxed_footprint = True
                        if tier.get("any_surface"):
                            picked["relaxed_any_surface"] = True
                        break
                if picked is not None:
                    break
            if picked is None:
                break
            off_screen += int(picked["off_screen"])
            best.append(picked)
        if off_screen:
            print(f"[simple] ⚠️ {code_from}: 框內只放得下 {len(best) - off_screen}/{count}，"
                  f"{off_screen} 隻放到框外側邊", flush=True)
        if len(best) < count:
            print(f"[simple] ⚠️ {code_from}: 側邊也放不下，少放 {count - len(best)} 隻 "
                  f"(n {count} → {len(best)})；原因統計 {reasons}", flush=True)
        best_depth = [row["depth"] for row in best]
        if not best:
            # ★一隻都放不下：不動這個 edit★(2026-09-12 river 的橋：n 寫成 0 讓 apply_plan 炸掉)。
            #   還原成 composer 給的原樣，交回原本的 region map 路徑處理，這支只印警告。
            edit.clear()
            edit.update(original_edit)
            print(f"[simple] ⚠️ {code_from}: 交回原路徑(region map)處理", flush=True)
            return {"edit_id": code_from, "factory": factory, "anchoring": anchoring,
                    "requested": count, "placed": 0, "off_screen": 0, "depths_m": [],
                    "min_sep_m": spec["min_sep"], "proxy_half_extents_m": [float(v) for v in half],
                    "areas": [], "ladder": ladder_log, "reject_reasons": reasons,
                    "fallback": "original_route"}
        for index, row in enumerate(best):
            row["min_sep"] = spec["min_sep"]
            row["radius"] = spec["radius"]
            row["edit_id"] = code_from
            if spec["surface"]:
                row["yaw"] = _hash01(code_from, index, "yaw") * math.tau
            else:
                # 飛行/水中物側面對鏡頭(±90° 再抖 ±30°)：正對鏡頭的鳥/蝴蝶像一張平貼的圖
                fwd = self.evaluator.forward
                azimuth = math.atan2(float(fwd.y), float(fwd.x))
                side = 1.0 if _hash01(code_from, index, "side") < 0.5 else -1.0
                jitter = (_hash01(code_from, index, "yaw") - 0.5) * math.radians(60.0)
                row["yaw"] = (azimuth + side * math.pi / 2.0 + jitter) % math.tau
            self.placed.append(row)

        edit["n"] = len(best)
        if relaxed_slope is not None or relaxed_footprint:
            policy = dict((edit.get("placement_spec") or {}).get("surface_policy") or {})
            if relaxed_slope is not None:
                policy["max_slope_deg"] = relaxed_slope
            if relaxed_footprint:
                policy["require_full_footprint_support"] = False
            edit.setdefault("placement_spec", {})["surface_policy"] = policy
            print(f"[simple] ℹ️ {code_from}: 框內用 director 的條件沒有合格點，放寬 "
                  f"{'坡度→%g° ' % relaxed_slope if relaxed_slope is not None else ''}"
                  f"{'四角支撐→不要求' if relaxed_footprint else ''}(已寫回 plan 讓審查用同一組)", flush=True)
        drop = 0.0 if spec["surface"] else altitude
        edit["positions"] = [[row["world"][0], row["world"][1], row["world"][2] - drop]
                             for row in best]
        edit["screen_positions"] = [row["screen"] for row in best]
        edit["placement_yaws"] = [row["yaw"] for row in best]
        edit["explicit_position_supports"] = [
            {"off_screen": bool(row.get("off_screen")), "simple_volume_depth_m": row["depth"],
             **({"surface": row["surface"], "surface_normal": row["surface_normal"]}
                if spec["surface"] else {})}
            for row in best]
        edit["screen_position_projection"] = "explicit_3d_region_map"
        edit["screen_positions_source"] = ROUTE
        edit["placement_route"] = ROUTE
        summary = {
            "edit_id": code_from, "factory": factory, "anchoring": anchoring,
            "requested": count, "placed": len(best), "off_screen": off_screen,
            "depths_m": best_depth, "min_sep_m": spec["min_sep"],
            "proxy_half_extents_m": [float(v) for v in half],
            "areas": [round(row["area"], 4) for row in best],
            "ladder": ladder_log, "reject_reasons": reasons,
        }
        depth_text = "/".join(f"{d:g}" for d in best_depth[:6]) + "m" if best_depth else "-"
        areas = ", ".join(f"{row['area'] * 100:.1f}%" for row in best[:6])
        print(f"[simple] {code_from}: {len(best)}/{count} 隻 @ {depth_text}"
              f"(框外 {off_screen})  min_sep={spec['min_sep']:.2f}m  投影 {areas}", flush=True)
        return summary


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:]
    if len(argv) < 4:
        raise SystemExit("usage: simple_volume_placement.py -- SOURCE PLAN OUT_DIR FRAME [--scene NAME]")
    source, plan_path, out_dir, frame = argv[:4]
    explicit_scene = (argv[argv.index("--scene") + 1]
                      if "--scene" in argv and argv.index("--scene") + 1 < len(argv) else None)
    plan_path, out_dir = Path(plan_path), Path(out_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # ★尺寸釘選要在擺放之前★(2026-09-13)：不然 6m 的條紋獏用 2.4m 的框找位置，
    #   畫面大小沒變、還會跟旁邊的東西疊。記號讓 apply_plan 不會再乘一次。
    import asset_scale_pins as pins
    _pinned = pins.apply_all(plan.get("hero_edits", []),
                             log=lambda m: print(m, flush=True))
    if _pinned["scales"] or _pinned["motions"] or _pinned.get("flying"):
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    targets = [edit for edit in plan.get("hero_edits", [])
               if edit.get("op") == "add"
               and (uses_3d_volume_map(edit.get("anchoring"))
                    or uses_surface_map(edit.get("anchoring")))]
    # ★沿用既有座標★(BI_ABLATION_REUSE_POSITIONS=1，2026-09-21)
    #   給 SA1(拿掉 anchoring)這類★只想改階段 C 貼地、不想改擺放★的對照用。
    #   plan 裡已經有 positions 的 edit 原封不動跳過 ⇒ 擺放與歸檔那一輪
    #   ★依定義完全相同★，差異只剩 placer 套不套 anchoring。
    #   這樣歸檔的 FULL 就是完美對照，不必用今天的程式碼重跑一份。
    #   ⚠️ 只對「不改擺放」的條件有效 —— SA1b / SA2 / SA23 的效果本來就發生在
    #      擺放階段，開了這個旗標它們會變成沒有作用。
    if os.environ.get("BI_ABLATION_REUSE_POSITIONS") == "1":
        reused = [e for e in targets if e.get("positions")]
        targets = [e for e in targets if not e.get("positions")]
        print(f"[simple] ★沿用既有座標★ {len(reused)} 筆 edit 不重算 "
              f"({', '.join(str(e.get('code_from')) for e in reused)})；"
              f"仍需計算 {len(targets)} 筆", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not targets:
        print("[simple] 沒有 hero add，什麼都不做", flush=True)
        (out_dir / "simple_volume_placement.json").write_text(
            json.dumps({"edits": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    bpy.ops.wm.open_mainfile(filepath=str(Path(source).resolve()))
    scene = bpy.context.scene
    scene.frame_set(int(frame))
    bpy.context.view_layer.update()
    camera = scene.camera
    if camera is None:
        raise SystemExit("source scene has no active camera")
    srcache.clear()
    scene_name = _infer_scene_name(source, explicit_scene)
    placer = Placer(scene=scene, camera=camera, scene_name=scene_name, plan=plan)

    def size_key(edit):
        dims = (edit.get("placement_spec") or {}).get("proxy_dimensions_m") or [1, 1, 1]
        return -math.prod(float(v) for v in dims)

    summaries = [placer.place_edit(edit) for edit in sorted(targets, key=size_key)]  # 大的先放
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "simple_volume_placement.json").write_text(
        json.dumps({"route": ROUTE, "max_frame_fraction": MAX_FRAME_FRACTION,
                    "edits": summaries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"SIMPLE_VOLUME_PLACED → {plan_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

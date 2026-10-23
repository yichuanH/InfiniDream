"""Pure-data contracts for deterministic map-first placement.

Code ranks machine-feasible regions, owns the exact grid cells, and preserves
every passing sibling.  This module has no Blender or API dependencies so its
contracts are unit-testable.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import math
import re


VOLUME_MAP_ANCHORINGS = frozenset({"submerged", "airborne"})
SURFACE_MAP_ANCHORINGS = frozenset(
    {"conform", "upright", "tilted", "rooted", "afloat"})
MAP_FIRST_ANCHORINGS = frozenset({
    *SURFACE_MAP_ANCHORINGS,
    *VOLUME_MAP_ANCHORINGS,
})
INDOOR_PLANAR_COLLISION_TYPES = frozenset({
    "floor", "tabletop", "countertop", "shelf", "bed_top", "seat",
})
LARGE_GROUND_RELAXATION_ANCHORINGS = frozenset({
    "conform", "upright", "tilted", "rooted"})
LARGE_GROUND_POSITIVE_TOKENS = frozenset({
    "bear", "bears", "boar", "boars", "buffalo", "camel", "camels",
    "cattle", "cow", "cows", "deer", "elephant", "elephants", "elk",
    "giraffe", "giraffes", "herd", "horse", "horses", "mammoth",
    "mammoths", "moose", "musk", "muskox", "muskoxen", "ox", "oxen",
    "reindeer", "rhino", "rhinos", "sleigh", "sled", "tent", "torii",
    "gate", "igloo", "tree", "trees",
})
LARGE_GROUND_EXCLUDED_TOKENS = frozenset({
    "bee", "bees", "beehive", "beehives", "bird", "birds", "butterfly",
    "butterflies", "crab", "crabs", "eagle", "eagles", "flower",
    "flowers", "hive", "hives", "insect", "insects", "lantern",
    "lanterns", "orchid", "orchids", "owl", "owls", "penguin",
    "penguins", "toucan", "toucans",
})
FULL_FOOTPRINT_STRUCTURE_TOKENS = frozenset({
    "building", "buildings", "cabin", "cabins", "gate", "gates", "house",
    "houses", "hut", "huts", "igloo", "igloos", "shelter", "shelters",
    "tent", "tents", "torii",
})
FULL_FOOTPRINT_STRUCTURE_FACTORIES = frozenset({
    "BuildingFactory", "CabinFactory", "GateFactory", "IglooFactory",
    "JapaneseToriiGateFactory", "ShelterFactory", "TentFactory",
    "ToriiGateFactory",
})
SOFT_OBSTACLE_TOKENS = frozenset({
    "blossom", "blossoms", "branch", "branches", "bush", "bushes",
    "canopy", "fern", "ferns", "flower", "flowers", "foliage", "grass",
    "leaf", "leaves", "moss", "orchid", "orchids", "plant", "plants",
    "reed", "reeds", "sapling", "saplings", "shrub", "shrubs", "tree",
    "trees", "twig", "twigs", "vine", "vines",
})
HARD_OBSTACLE_TOKENS = frozenset({
    "boulder", "boulders", "cliff", "cliffs", "ledge", "ledges", "rock",
    "rocks", "stone", "stones", "terrain", "trunk", "wall", "walls",
})
PLACEHOLDER_POPULATION_INTENT_REQUESTED_ADD = "requested_add"
PLACEHOLDER_POPULATION_INTENT_REQUESTED_REPLACE = "requested_replace"
PLACEHOLDER_POPULATION_INTENT_BASELINE_VISIBLE = "baseline_visible"
PLACEHOLDER_POPULATION_INTENT_COARSE_REALIZED = "coarse_realized"
PLACEHOLDER_POPULATION_INTENT_LEGACY_POPULATE = "legacy_populate"
PLACEHOLDER_POPULATION_INTENT_LATENT_SKIP = "latent_unmodified_skip"
PLACEHOLDER_POPULATION_MATERIALIZE_INTENTS = frozenset({
    PLACEHOLDER_POPULATION_INTENT_REQUESTED_ADD,
    PLACEHOLDER_POPULATION_INTENT_REQUESTED_REPLACE,
    PLACEHOLDER_POPULATION_INTENT_BASELINE_VISIBLE,
    PLACEHOLDER_POPULATION_INTENT_COARSE_REALIZED,
    PLACEHOLDER_POPULATION_INTENT_LEGACY_POPULATE,
})


_PLACEHOLDER_REF_RE = re.compile(
    r"^(?:placeholders:)?([A-Za-z_][A-Za-z0-9_]*)\((\d+)\)$")


def _semantic_tokens(*values) -> set[str]:
    tokens = set()
    for value in values:
        token = []
        for char in str(value or ""):
            if char.isalnum():
                if (char.isupper() and token
                        and not token[-1].isupper()):
                    tokens.add("".join(token).casefold())
                    token = []
                token.append(char)
            elif token:
                tokens.add("".join(token).casefold())
                token = []
        if token:
            tokens.add("".join(token).casefold())
    return tokens


def normalize_placeholder_collection_ref(value) -> str | None:
    """Return ``Factory(seed)``/``Factory`` for a placeholder target reference."""
    if value is None:
        return None
    ref = str(value).strip()
    if not ref:
        return None
    if ref.startswith("placeholders:"):
        ref = ref.split(":", 1)[1]
    return ref


def placeholder_collection_parts(value) -> tuple[str, str, str] | None:
    """Parse ``placeholders:Factory(seed)`` or ``Factory(seed)``."""
    ref = normalize_placeholder_collection_ref(value)
    if ref is None:
        return None
    match = _PLACEHOLDER_REF_RE.fullmatch(ref)
    if not match:
        return None
    factory, seed = match.groups()
    return factory, seed, f"{factory}({seed})"


def plan_claimed_placeholder_refs(plan: dict | None) -> set[str]:
    """Return placeholder collections/factories explicitly claimed by Composer."""
    refs = set()
    for edit in (plan or {}).get("hero_edits", []):
        if not isinstance(edit, dict):
            continue
        if edit.get("op") not in {"attrs", "replace"}:
            continue
        if not (edit.get("code_from") or edit.get("code")):
            continue
        target = (edit.get("target_factory") or edit.get("target")
                  or edit.get("collection") or edit.get("factory"))
        ref = normalize_placeholder_collection_ref(target)
        if ref:
            refs.add(ref)
    return refs


def placeholder_collection_claimed_by_plan(
        plan: dict | None, *,
        collection: str | None = None,
        factory: str | None = None,
        seed: int | str | None = None) -> bool:
    """Return whether Composer explicitly turns this placeholder into final geometry."""
    refs = plan_claimed_placeholder_refs(plan)
    if not refs:
        return False
    collection_ref = normalize_placeholder_collection_ref(collection)
    if collection_ref and collection_ref in refs:
        return True
    parts = placeholder_collection_parts(collection_ref)
    if parts is not None:
        parsed_factory, parsed_seed, parsed_ref = parts
        if parsed_ref in refs or parsed_factory in refs:
            return True
        if factory is None:
            factory = parsed_factory
        if seed is None:
            seed = parsed_seed
    if factory is not None and str(factory) in refs:
        return True
    if factory is not None and seed is not None:
        return f"{factory}({seed})" in refs
    return False


def _manifest_population_prediction_count(placement: dict) -> tuple[bool, int]:
    for key in ("will_populate_count", "populate_target_count",
                "population_target_count"):
        if key not in placement:
            continue
        try:
            return True, max(0, int(placement.get(key) or 0))
        except (TypeError, ValueError):
            return True, 0
    targets = (placement.get("populate_targets")
               if "populate_targets" in placement
               else placement.get("population_targets"))
    if isinstance(targets, list):
        dict_targets = [row for row in targets if isinstance(row, dict)]
        if dict_targets and any("will_populate" in row for row in dict_targets):
            return True, sum(
                1 for row in dict_targets
                if row.get("will_populate", True)
            )
        return True, len(targets)
    return False, 0


def classify_placeholder_population_intent(
        *, realized_at=None, added_this_plan=0, code_from=None, code=None,
        proxy_for=None, will_populate_count: int | None = None,
        has_population_prediction: bool = False) -> str:
    """Classify whether a manifest row represents expected final geometry."""
    if (realized_at or "populate") != "populate":
        return PLACEHOLDER_POPULATION_INTENT_COARSE_REALIZED
    try:
        added = int(added_this_plan or 0)
    except (TypeError, ValueError):
        added = 0
    if added > 0:
        return PLACEHOLDER_POPULATION_INTENT_REQUESTED_ADD
    if code_from or code or proxy_for:
        return PLACEHOLDER_POPULATION_INTENT_REQUESTED_REPLACE
    if has_population_prediction:
        return (
            PLACEHOLDER_POPULATION_INTENT_BASELINE_VISIBLE
            if int(will_populate_count or 0) > 0
            else PLACEHOLDER_POPULATION_INTENT_LATENT_SKIP)
    return PLACEHOLDER_POPULATION_INTENT_LEGACY_POPULATE


def placement_population_intent(placement: dict | None) -> str:
    """Return the manifest row's final-geometry intent with legacy fallback."""
    placement = placement or {}
    explicit = (placement.get("population_intent")
                or placement.get("final_geometry_intent"))
    if explicit:
        return str(explicit)
    has_prediction, count = _manifest_population_prediction_count(placement)
    return classify_placeholder_population_intent(
        realized_at=placement.get("realized_at"),
        added_this_plan=placement.get("added_this_plan"),
        code_from=placement.get("code_from"),
        code=placement.get("code"),
        proxy_for=placement.get("proxy_for"),
        will_populate_count=count,
        has_population_prediction=has_prediction,
    )


def placement_should_materialize(placement: dict | None) -> bool:
    """Return whether placer/review should expect this row in final geometry."""
    return (placement_population_intent(placement)
            in PLACEHOLDER_POPULATION_MATERIALIZE_INTENTS)


def _dimensions_or_none(value) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        dimensions = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(item) or item <= 0.0 for item in dimensions):
        return None
    return dimensions


def _no_volume_placement() -> bool:
    """SA1b ablation：拿掉體積擺放(2026-09-20)。

    infinigen 對魚群/蟲群的做法是 scatter_placeholders_mesh(..., altitude=2)
    —— ★地表取點再抬固定高度★，沒有「在開闊水域/空中挑一個讀得出來的深度」
    這回事(generate_nature.py:448/467)。開了這個旗標，airborne/submerged 就走
    surface 路徑：落點變成地表 raycast 的命中點，再由 placer 加 anchoring 的
    +2/+3m ⇒ ★魚貼著海床上方 2m、鳥貼著地面上方 3m★，而不是游在鏡頭前。
    """
    import os
    return os.environ.get("BI_ABLATION_NO_VOLUME_PLACEMENT") == "1"


def uses_3d_volume_map(value) -> bool:
    """Return whether an anchoring/map owns an explicit camera-frustum voxel."""
    anchoring = value.get("anchoring") if isinstance(value, dict) else value
    if _no_volume_placement():
        return False                       # SA1b：體積模式改走地表
    return str(anchoring or "") in VOLUME_MAP_ANCHORINGS


def uses_surface_map(value) -> bool:
    """Return whether an anchoring can be selected on a visible surface grid."""
    anchoring = value.get("anchoring") if isinstance(value, dict) else value
    if _no_volume_placement():
        return str(anchoring or "") in (SURFACE_MAP_ANCHORINGS | VOLUME_MAP_ANCHORINGS)
    return str(anchoring or "") in SURFACE_MAP_ANCHORINGS


def is_outdoor_ultra_loose_map(map_data: dict) -> bool:
    """Legacy ultra-loose maps are diagnostic only, never relaxed geometry."""
    return False


def is_large_ground_proxy(*, factory: str | None = None,
                          code_from: str | None = None,
                          anchoring: str | None = None,
                          semantic_target: str | None = None,
                          proxy_dimensions_m=None) -> bool:
    """Return whether a surface edit is eligible for terrain-only relaxation."""
    if str(anchoring or "") not in LARGE_GROUND_RELAXATION_ANCHORINGS:
        return False
    words = _semantic_tokens(factory, code_from, semantic_target)
    if words & LARGE_GROUND_EXCLUDED_TOKENS:
        return False
    dimensions = _dimensions_or_none(proxy_dimensions_m)
    if dimensions is None:
        return False
    length, width, height = dimensions
    footprint_area = length * width
    footprint_major = max(length, width)
    footprint_minor = min(length, width)
    if footprint_major >= 2.0 or footprint_area >= 1.5:
        return True
    if (height >= 2.5 and footprint_major >= 1.0
            and footprint_minor >= 0.35):
        return True
    return bool(words & LARGE_GROUND_POSITIVE_TOKENS
                and footprint_major >= 1.2)


def requires_full_footprint_structure_support(
        *, factory: str | None = None, code_from: str | None = None,
        semantic_target: str | None = None) -> bool:
    """Return whether relaxation should keep rectangular footprint support."""
    if str(factory or "") in FULL_FOOTPRINT_STRUCTURE_FACTORIES:
        return True
    return bool(_semantic_tokens(code_from, semantic_target)
                & FULL_FOOTPRINT_STRUCTURE_TOKENS)


def large_ground_relaxation_attempts(
        *, factory: str | None = None, code_from: str | None = None,
        anchoring: str | None = None, semantic_target: str | None = None,
        proxy_dimensions_m=None, surface_policy: dict | None = None) -> list[dict]:
    """Return deterministic relaxation attempts for large grounded add edits."""
    if not is_large_ground_proxy(
            factory=factory, code_from=code_from, anchoring=anchoring,
            semantic_target=semantic_target,
            proxy_dimensions_m=proxy_dimensions_m):
        return []
    policy = surface_policy if isinstance(surface_policy, dict) else {}
    original_slope = policy.get("max_slope_deg")
    try:
        original_slope = (
            None if original_slope is None else float(original_slope))
    except (TypeError, ValueError):
        original_slope = None

    def relaxed_slope(value: float) -> float:
        if original_slope is None or not math.isfinite(original_slope):
            return float(value)
        return max(float(value), original_slope)

    structure_support = requires_full_footprint_structure_support(
        factory=factory, code_from=code_from,
        semantic_target=semantic_target)
    original_full_support = bool(
        policy.get("require_full_footprint_support", True)) or structure_support
    attempts = [
        {
            "label": "slope_25_full_support_all_obstacles",
            "max_slope_deg": relaxed_slope(25.0),
            "require_full_footprint_support": original_full_support,
            "full_frame": False,
            "obstacle_policy": "all",
        },
        {
            "label": "slope_30_full_support_all_obstacles",
            "max_slope_deg": relaxed_slope(30.0),
            "require_full_footprint_support": original_full_support,
            "full_frame": False,
            "obstacle_policy": "all",
        },
        {
            "label": "slope_30_full_frame_all_obstacles",
            "max_slope_deg": relaxed_slope(30.0),
            "require_full_footprint_support": original_full_support,
            "full_frame": True,
            "obstacle_policy": "all",
        },
        {
            "label": "slope_35_full_frame_hard_obstacles",
            "max_slope_deg": relaxed_slope(35.0),
            "require_full_footprint_support": original_full_support,
            "full_frame": True,
            "obstacle_policy": "hard_scene_obstacles",
        },
    ]
    if original_full_support and not structure_support:
        attempts.extend([
            {
                "label": "slope_35_center_support_hard_obstacles",
                "max_slope_deg": relaxed_slope(35.0),
                "require_full_footprint_support": False,
                "full_frame": True,
                "obstacle_policy": "hard_scene_obstacles",
            },
            {
                "label": "slope_40_center_support_hard_obstacles",
                "max_slope_deg": relaxed_slope(40.0),
                "require_full_footprint_support": False,
                "full_frame": True,
                "obstacle_policy": "hard_scene_obstacles",
            },
        ])
    return attempts


def is_soft_scene_obstacle(*, label: str | None = None,
                           source: str | None = None,
                           object_name: str | None = None) -> bool:
    """Return whether a scene obstacle is soft vegetation for relaxed maps."""
    words = _semantic_tokens(label, object_name)
    if words & HARD_OBSTACLE_TOKENS:
        return False
    return bool(words & SOFT_OBSTACLE_TOKENS)


# ★構圖大小門檻★(2026-09-11)：先前只要落在畫面內就算過關，沒有大小概念。
#   0911 實測的兩種壞構圖：石燈籠 45-52m 只佔 0.3%、野餐桌 85m 佔 0.5%(看不見)；
#   帆船 13.7m 佔 32%、水晶柱 4.3m 佔 27.7%(擋住鏡頭)。
BLOCKING_FRAME_FRACTION = 0.25      # 超過這個比例算擋鏡頭
# ★到這個比例就算「讀得出來」，再大不加分★
#   ⚠️ 這個值就是「東西會停在多遠」的旋鈕：≥ 它一律滿分，所以物體會停在
#      ★剛好達標的最遠處★(平手之後由構圖分決定，而構圖分對遠近沒有意見)。
#   0.01 太鬆：coast 帆船停在 73.3m/1.77%，達標了但太小太遠(使用者 0912 回報)。
#   投影面積 ∝ 1/距離² ⇒ 提到 0.05 後帆船約落在 73.3×√(1.77/5) ≈ 44m。
#   影響：老鷹 8m/19.9% 本來就 ≥5% 不受影響；螢火蟲會再靠近一點(本來就該近)。
READABLE_FRAME_FRACTION = 0.05


# ★飛行物的深度下限★(2026-09-12)：以前 _airborne_depths 是 max(8.0, reserve+2)，
#   跟物體大小無關 —— 16 公分的蝴蝶和 1.2 公尺的蜜蜂都從 8m 起跳，然後取第一個
#   可行深度就停 ⇒ 小動物永遠 8m、永遠幾個 pixel(canyon 蝴蝶 0.167%、蜂鳥 12cm)。
#   「正常大小＋近」= 下限跟著物體(reserve+2 已經跟著)，絕對下限只留 2m 防穿鏡。
AIRBORNE_MIN_DEPTH_M = 2.0


def ideal_depth_for_readable_size(probe_depth_m: float,
                                  probe_area_fraction: float,
                                  target_fraction: float | None = None) -> float | None:
    """由一次探針投影推出「佔畫面剛好 target」的距離。投影面積 ∝ 1/距離²。

    深度階梯照這個值排序(最接近的先試)，取代「由近到遠取第一個可行的」——
    小東西自然往前、大東西自然往後，跟評分那邊用的是同一個 READABLE 門檻。
    """
    target = READABLE_FRAME_FRACTION if target_fraction is None else float(target_fraction)
    try:
        depth = float(probe_depth_m)
        area = float(probe_area_fraction)
    except (TypeError, ValueError):
        return None
    if not (depth > 0.0 and area > 0.0 and target > 0.0):
        return None
    return depth * math.sqrt(area / target)


def projected_size_score(cell) -> float:
    """0..1：讀得出來又不擋鏡頭的給高分；沒有資料時給中性分。"""
    value = cell.get("projected_area_fraction")
    if value is None:
        return 0.5
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return 0.5
    if fraction >= BLOCKING_FRAME_FRACTION:
        # 擋鏡頭：越大越差，但不要變成硬性禁止(盡量模式下可能只剩這種格子)
        return max(0.0, 0.2 * (1.0 - min(1.0, fraction)))
    return min(1.0, fraction / READABLE_FRAME_FRACTION)


def best_effort_placement() -> bool:
    """Place as many instances as fit instead of failing the whole edit.

    In-frame cells are still preferred (see the extended-grid selection above); this
    only decides what happens when the frame genuinely runs out of room.
    Batch opt-in (BI_PLACEMENT_BEST_EFFORT=1), like the other fixed-asset switches: the
    default stays all-or-nothing so the placement contract keeps its guarantees.
    """
    import os
    return os.environ.get("BI_PLACEMENT_BEST_EFFORT") == "1"


def is_map_managed_anchoring(value) -> bool:
    """Return whether map-first placement owns an added edit's coordinates."""
    anchoring = value.get("anchoring") if isinstance(value, dict) else value
    return str(anchoring or "") in MAP_FIRST_ANCHORINGS


def grid_points(bounds, *, columns: int, rows: int) -> list[dict]:
    """Return stable cell-center probes in bottom-left-origin screen space."""
    columns, rows = int(columns), int(rows)
    if columns < 2 or rows < 2:
        raise ValueError("planning grid must be at least 2x2")
    xmin, ymin, xmax, ymax = map(float, bounds)
    import os
    extended = float(os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1) > 1.0
    in_range = (xmin < xmax and ymin < ymax if extended
                else 0.0 <= xmin < xmax <= 1.0 and 0.0 <= ymin < ymax <= 1.0)
    if not in_range:
        raise ValueError(f"invalid normalized screen bounds: {list(bounds)}")
    step_x = (xmax - xmin) / columns
    step_y = (ymax - ymin) / rows
    return [{
        "cell_id": f"C{column:02d}_{row:02d}",
        "column": column,
        "row": row,
        "screen_position": [
            round(xmin + (column + 0.5) * step_x, 8),
            round(ymin + (row + 0.5) * step_y, 8),
        ],
    } for row in range(rows) for column in range(columns)]


def _component_clearance(component: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Four-neighbour distance from each cell to the component boundary."""
    distance = {}
    queue = deque()
    for cell in sorted(component):
        column, row = cell
        neighbours = (
            (column - 1, row), (column + 1, row),
            (column, row - 1), (column, row + 1),
        )
        if any(neighbour not in component for neighbour in neighbours):
            distance[cell] = 1
            queue.append(cell)
    while queue:
        column, row = queue.popleft()
        for neighbour in (
            (column - 1, row), (column + 1, row),
            (column, row - 1), (column, row + 1),
        ):
            if neighbour not in component or neighbour in distance:
                continue
            distance[neighbour] = distance[(column, row)] + 1
            queue.append(neighbour)
    return distance


def connected_feasible_regions(cells: list[dict]) -> tuple[list[dict], list[dict]]:
    """Label four-connected feasible cells and summarize Agent-selectable regions."""
    copied = deepcopy(cells)
    by_grid = {
        (int(cell["column"]), int(cell["row"])): cell
        for cell in copied if cell.get("status") == "feasible"
    }
    remaining = set(by_grid)
    components = []
    while remaining:
        start = min(remaining)
        queue = deque([start])
        component = set()
        remaining.remove(start)
        while queue:
            column, row = queue.popleft()
            component.add((column, row))
            current_plane = by_grid[(column, row)].get("support_plane_id")
            for neighbour in (
                (column - 1, row), (column + 1, row),
                (column, row - 1), (column, row + 1),
            ):
                neighbour_plane = (by_grid[neighbour].get("support_plane_id")
                                   if neighbour in by_grid else None)
                # New indoor maps identify each elevation explicitly.  Do not
                # connect two adjacent screen pixels when they belong to
                # different shelves/planes.  Legacy/outdoor maps omit the key
                # and retain the original four-connected behaviour.
                same_plane = (
                    current_plane is None or neighbour_plane is None
                    or current_plane == neighbour_plane)
                if neighbour in remaining and same_plane:
                    remaining.remove(neighbour)
                    queue.append(neighbour)
        components.append(component)
    components.sort(key=lambda component: (-len(component), min(component)))

    regions = []
    for region_index, component in enumerate(components, start=1):
        region_id = f"R{region_index:02d}"
        clearance = _component_clearance(component)
        region_cells = [by_grid[key] for key in sorted(component)]
        for key, steps in clearance.items():
            by_grid[key]["region_id"] = region_id
            by_grid[key]["clearance_steps"] = int(steps)
        screen = [cell["screen_position"] for cell in region_cells]
        world = [cell.get("world_position") for cell in region_cells
                 if cell.get("world_position") is not None]
        regions.append({
            "region_id": region_id,
            "support_plane_ids": sorted({
                str(cell["support_plane_id"]) for cell in region_cells
                if cell.get("support_plane_id") is not None}),
            "cell_count": len(region_cells),
            "cell_ids": [cell["cell_id"] for cell in region_cells],
            "screen_bounds": [
                min(point[0] for point in screen),
                min(point[1] for point in screen),
                max(point[0] for point in screen),
                max(point[1] for point in screen),
            ],
            "screen_centroid": [
                sum(point[0] for point in screen) / len(screen),
                sum(point[1] for point in screen) / len(screen),
            ],
            "world_xy_bounds": None if not world else [
                min(point[0] for point in world),
                min(point[1] for point in world),
                max(point[0] for point in world),
                max(point[1] for point in world),
            ],
            "world_xy_centroid": None if not world else [
                sum(point[0] for point in world) / len(world),
                sum(point[1] for point in world) / len(world),
            ],
            "max_clearance_steps": max(clearance.values(), default=0),
        })
    return copied, regions


def collect_failed_instances(review: dict) -> dict[str, set[int]]:
    """Return exactly the projection or Machine-Review instances allowed to move."""
    failed: dict[str, set[int]] = {}
    for row in review.get("failed_instances", []):
        edit_id = str(row.get("edit_id") or "")
        if edit_id and row.get("instance_index") is not None:
            failed.setdefault(edit_id, set()).add(int(row["instance_index"]))

    object_to_instance = {}
    for placement in review.get("placements", []):
        edit_id = str(placement.get("code_from") or "")
        policy = (placement.get("placement_spec") or {}).get(
            "surface_policy") or {}
        require_support = policy.get("require_full_footprint_support", True)
        for anchor in placement.get("anchors", []):
            index = int(anchor.get("instance_index", 0))
            object_to_instance[str(anchor.get("object"))] = (edit_id, index)
            hard_failure = (
                not anchor.get("all_frames_fully_visible", False)
                or bool(anchor.get("surface_policy_issue"))
                or (require_support and not anchor.get(
                    "footprint_support", {}).get("supported", False))
            )
            if hard_failure and edit_id:
                failed.setdefault(edit_id, set()).add(index)
    for violation in review.get("violations", []):
        for object_name in violation.get("objects", []):
            mapped = object_to_instance.get(str(object_name))
            if mapped and mapped[0]:
                failed.setdefault(mapped[0], set()).add(mapped[1])
    return failed


def is_simple_routed(edit: dict) -> bool:
    """BI_HERO_SIMPLE=1 時 simple_volume_placement.py 已放好的 edit。

    ★含地面物件★(2026-09-13 起)：該支的 targets 是 volume(airborne/submerged)
    ★或★ surface(conform/upright/tilted/rooted/afloat) 的 hero add，不是只有空中/
    水下。凡是標了 placement_route="simple_volume" 的都不再由 region map 接手。
    """
    return str((edit or {}).get("placement_route") or "") == "simple_volume"


def map_managed_failed_instances(plan: dict, review: dict) -> dict[str, set[int]]:
    """Return failed surface/volume instances and reject mixed ownership."""
    failed = collect_failed_instances(review)
    edits = {
        str(edit.get("code_from") or ""): edit
        for edit in plan.get("hero_edits", []) if edit.get("op") == "add"
    }
    unsupported = []
    result = {}
    for edit_id, indices in failed.items():
        edit = edits.get(edit_id)
        if edit is not None and is_simple_routed(edit):
            continue    # BI_HERO_SIMPLE 放好的：沒有 region map，也不再搬
        if edit is None or edit.get("anchoring") not in MAP_FIRST_ANCHORINGS:
            unsupported.append(edit_id)
        else:
            result[edit_id] = set(indices)
    if unsupported:
        raise ValueError(
            "map-first placement supports only failed surface/volume "
            "add edits; "
            f"unsupported: {sorted(unsupported)}")
    return result


def all_map_managed_instances(plan: dict) -> dict[str, set[int]]:
    """Return every add whose coordinates are owned by map-first placement."""
    result = {}
    for edit in plan.get("hero_edits", []):
        if (edit.get("op") != "add"
                or edit.get("anchoring") not in MAP_FIRST_ANCHORINGS
                or is_simple_routed(edit)):
            continue
        edit_id = str(edit.get("code_from") or "")
        count = int(edit.get("n", 0))
        if not edit_id or count < 1:
            raise ValueError(
                "map-first edits require code_from and positive n")
        result[edit_id] = set(range(count))
    return result


def conform_failed_instances(plan: dict, review: dict) -> dict[str, set[int]]:
    """Reject mixed unsupported modes instead of silently partially applying a map."""
    failed = collect_failed_instances(review)
    edits = {
        str(edit.get("code_from") or ""): edit
        for edit in plan.get("hero_edits", []) if edit.get("op") == "add"
    }
    unsupported = []
    result = {}
    for edit_id, indices in failed.items():
        edit = edits.get(edit_id)
        if edit is None or edit.get("anchoring") != "conform":
            unsupported.append(edit_id)
        else:
            result[edit_id] = set(indices)
    if unsupported:
        raise ValueError(
            "region map MVP supports only failed conform add edits; unsupported: "
            f"{sorted(unsupported)}")
    return result


def all_conform_instances(plan: dict) -> dict[str, set[int]]:
    """Return every instance owned by map-first deterministic placement."""
    result = {}
    for edit in plan.get("hero_edits", []):
        if edit.get("op") != "add" or edit.get("anchoring") != "conform":
            continue
        edit_id = str(edit.get("code_from") or "")
        count = int(edit.get("n", 0))
        if not edit_id or count < 1:
            raise ValueError(
                "map-first conform edits require code_from and positive n")
        result[edit_id] = set(range(count))
    return result


def rotated_aabb_bounds(center, half_extents, *, yaw: float = 0.0,
                        clearance=(0.0, 0.0, 0.0)) -> list[float]:
    """Return a conservative world AABB for a Z-yawed 3D proxy box."""
    cx, cy, cz = map(float, center)
    hx, hy, hz = map(float, half_extents)
    px, py, pz = map(float, clearance)
    cosine, sine = abs(math.cos(float(yaw))), abs(math.sin(float(yaw)))
    world_hx = cosine * hx + sine * hy + px
    world_hy = sine * hx + cosine * hy + py
    world_hz = hz + pz
    return [
        cx - world_hx, cy - world_hy, cz - world_hz,
        cx + world_hx, cy + world_hy, cz + world_hz,
    ]


def aabb_overlap_fraction(left, right) -> float:
    """Intersection volume divided by the smaller six-value AABB volume."""
    if not left or not right or len(left) != 6 or len(right) != 6:
        return 0.0
    left = list(map(float, left))
    right = list(map(float, right))
    left_size = [max(0.0, left[axis + 3] - left[axis]) for axis in range(3)]
    right_size = [max(0.0, right[axis + 3] - right[axis]) for axis in range(3)]
    smaller = min(math.prod(left_size), math.prod(right_size))
    if smaller <= 1e-12:
        return 0.0
    intersection = math.prod(max(
        0.0,
        min(left[axis + 3], right[axis + 3])
        - max(left[axis], right[axis]),
    ) for axis in range(3))
    return float(intersection / smaller)


def _screen_bounds_for_scoring(edit: dict, map_data: dict) -> tuple[float, float, float, float]:
    raw = (map_data.get("screen_bounds")
           or (edit.get("placement_spec") or {}).get("screen_bounds")
           or [0.0, 0.0, 1.0, 1.0])
    try:
        xmin, ymin, xmax, ymax = map(float, raw[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 1.0, 1.0)
    if not all(math.isfinite(value) for value in (xmin, ymin, xmax, ymax)):
        return (0.0, 0.0, 1.0, 1.0)
    if xmax <= xmin or ymax <= ymin:
        return (0.0, 0.0, 1.0, 1.0)
    return (xmin, ymin, xmax, ymax)


def _screen_point_or_none(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        x, y = map(float, value)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def _preferred_screen_point(edit: dict, map_data: dict) -> tuple[float, float]:
    """Translate a Director composition hint into one deterministic point."""
    xmin, ymin, xmax, ymax = _screen_bounds_for_scoring(edit, map_data)
    region = str((edit.get("placement_spec") or {}).get(
        "screen_region") or "auto")
    x_fraction = 0.5
    y_fraction = 0.5
    if "left" in region:
        x_fraction = 0.25
    elif "right" in region:
        x_fraction = 0.75
    if "lower" in region:
        y_fraction = 0.30
    elif "upper" in region:
        y_fraction = 0.70
    return (
        xmin + (xmax - xmin) * x_fraction,
        ymin + (ymax - ymin) * y_fraction,
    )


def _target_screen_point(edit: dict, map_data: dict,
                         instance_index: int | None = None
                         ) -> tuple[tuple[float, float], str]:
    """Return the composition point that legal cells should be ranked around."""
    points = edit.get("screen_positions")
    if instance_index is not None:
        if isinstance(points, list) and 0 <= instance_index < len(points):
            point = _screen_point_or_none(points[instance_index])
            if point is not None:
                return point, "composer_screen_position"
    if isinstance(points, list):
        valid = [
            point for point in (_screen_point_or_none(row) for row in points)
            if point is not None
        ]
        if valid:
            centroid = (
                sum(point[0] for point in valid) / len(valid),
                sum(point[1] for point in valid) / len(valid),
            )
            if str((edit.get("placement_spec") or {}).get("arrangement") or "") == "field":
                xmin, ymin, xmax, ymax = _screen_bounds_for_scoring(edit, map_data)
                field_center = (
                    xmin + (xmax - xmin) * 0.5,
                    ymin + (ymax - ymin) * 0.5,
                )
                return (
                    0.65 * centroid[0] + 0.35 * field_center[0],
                    0.65 * centroid[1] + 0.35 * field_center[1],
                ), "composer_field_centroid"
            return centroid, "composer_screen_positions_centroid"
    return _preferred_screen_point(edit, map_data), "placement_spec_region"


def _screen_target_score(point: tuple[float, float],
                         target: tuple[float, float],
                         bounds: tuple[float, float, float, float]) -> float:
    xmin, ymin, xmax, ymax = bounds
    diagonal = max(math.hypot(xmax - xmin, ymax - ymin), 1e-9)
    return max(0.0, min(1.0, 1.0 - math.dist(point, target) / diagonal))


def _frame_margin_score(cell: dict) -> float:
    bounds = cell.get("candidate_screen_bounds")
    if isinstance(bounds, list) and len(bounds) == 4:
        frame_margin = max(0.0, min(
            float(bounds[0]), float(bounds[1]),
            1.0 - float(bounds[2]), 1.0 - float(bounds[3])))
    else:
        screen = _screen_point_or_none(cell.get("screen_position")) or (0.5, 0.5)
        frame_margin = max(0.0, min(
            float(screen[0]), float(screen[1]),
            1.0 - float(screen[0]), 1.0 - float(screen[1])))
    return min(1.0, frame_margin / 0.20)


FRAME_TIER_POINT_MARGIN = 0.05


def frame_tier(cell: dict) -> int:
    """0 = 整個在畫面內，1 = 壓到邊緣，2 = 整個在畫面外。★越小越好★。

    ★為什麼要分層而不是加權★(2026-09-12)：_frame_margin_score 本來就有算，但它在
    composition(0.15)裡、composition 又只佔 proximity 的 0.28 ⇒ ★實際權重 4.2%★，
    被投影大小的 0.72 輕鬆蓋過。而 scatter 的第一順位是「離兄弟越遠越好」，
    會主動把東西推到可用格子的四個極端。
    0912 coast：director 要 5 隻紅鶴、placer 也★確實放了 5 隻★，但螢幕座標是
    [0.431,0.025] [0.014,0.375] [0.847,0.375] [0.264,0.325] [0.986,0.275]
    —— 三隻貼在畫面極邊，被切掉或被棕櫚擋住，使用者只看到一隻。
    ⇒ 先在「看得見的格子」裡挑，挑不到才往邊緣、再往畫面外。
      盡量模式仍然保底(有格子就放)，但只在真的沒有選擇時才犧牲可見性。
    """
    bounds = cell.get("candidate_screen_bounds")
    if isinstance(bounds, list) and len(bounds) == 4:
        x0, y0, x1, y1 = (float(value) for value in bounds)
        if x0 >= 0.0 and y0 >= 0.0 and x1 <= 1.0 and y1 <= 1.0:
            return 0
        if x1 > 0.0 and y1 > 0.0 and x0 < 1.0 and y0 < 1.0:
            return 1
        return 2
    # 沒有投影盒時退回用點：★留 5% 邊★，不然 x=0.01 也算「完整在畫面內」
    #   (0912 try_3：surface 格子沒有盒子，10 隻紅鶴 4 隻仍貼在 x=0.01/0.99)。
    screen = _screen_point_or_none(cell.get("screen_position"))
    if screen is None:
        return 1
    x, y = float(screen[0]), float(screen[1])
    if FRAME_TIER_POINT_MARGIN <= x <= 1.0 - FRAME_TIER_POINT_MARGIN \
            and FRAME_TIER_POINT_MARGIN <= y <= 1.0 - FRAME_TIER_POINT_MARGIN:
        return 0
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        return 1
    return 2


def _region_cell_count_map(map_data: dict) -> dict[str, int]:
    counts = {}
    for region in map_data.get("regions", []):
        region_id = str(region.get("region_id") or "")
        if not region_id:
            continue
        counts[region_id] = max(0, int(region.get("cell_count", 0)))
    if not any(counts.values()):
        for cell in map_data.get("cells", []):
            if cell.get("status") != "feasible":
                continue
            region_id = str(cell.get("region_id") or "")
            if region_id:
                counts[region_id] = counts.get(region_id, 0) + 1
    return counts


def _bounds_overlap(left, right, *, margin: float = 0.0) -> bool:
    if not left or not right or len(left) != 6 or len(right) != 6:
        return False
    left = list(map(float, left))
    right = list(map(float, right))
    margin = float(margin)
    return all(
        left[axis] < right[axis + 3] + margin
        and right[axis] < left[axis + 3] + margin
        for axis in range(3)
    )


def automatic_region_assignments(
        plan: dict, *, maps: dict[str, dict], expected: dict[str, set[int]],
        rejected_positions: dict[tuple[str, int], list[list[float]]] | None = None,
) -> list[dict]:
    """Assign failed instances to feasible regions without an Agent/API call.

    Prefer keeping one arrangement together in a capacity-sufficient region,
    then use a reproducible pseudo-random ordering.  The ordering seed includes
    prior rejected points, so a retry gets a fresh draw without an API call.
    """
    rejected_positions = rejected_positions or {}
    edits = {
        str(edit.get("code_from") or ""): edit
        for edit in plan.get("hero_edits", []) if edit.get("op") == "add"
    }
    assignments = []
    for edit_id in sorted(expected):
        indices = sorted(expected[edit_id])
        if not indices:
            continue
        edit = edits.get(edit_id)
        map_data = maps.get(edit_id)
        if edit is None or map_data is None:
            raise ValueError(f"missing edit or region map for {edit_id!r}")
        rejected_for_edit = [
            point
            for (prior_edit_id, _), points in rejected_positions.items()
            if prior_edit_id == edit_id
            for point in points
        ]
        step = max(map(float, map_data.get(
            "grid", {}).get("screen_step", [0.0, 0.0])))
        fresh_cells = [
            cell for cell in map_data.get("cells", [])
            if cell.get("status") == "feasible"
            and all(math.dist(cell["screen_position"], point) > step * 0.75
                    for point in rejected_for_edit)
        ]
        fresh_by_region = {}
        for cell in fresh_cells:
            fresh_by_region.setdefault(str(cell.get("region_id")), []).append(cell)
        has_cell_inventory = bool(map_data.get("cells"))
        regions = []
        for region in map_data.get("regions", []):
            region_id = str(region["region_id"])
            region_cells = fresh_by_region.get(region_id, [])
            if has_cell_inventory:
                capacity = estimate_region_capacity(
                    region_cells,
                    half_extents=(
                        map_data.get("proxy_half_extents", [1.0, 1.0, 1.0])
                        if uses_3d_volume_map(map_data)
                        else map_data.get("footprint_half_extents", [1.0, 1.0])),
                    min_sep=float(
                        map_data.get("min_sep")
                        if map_data.get("min_sep") is not None
                        else edit.get("min_sep")
                        if edit.get("min_sep") is not None
                        else 2.5),
                    use_3d=uses_3d_volume_map(map_data),
                    arrangement=str(map_data.get("arrangement") or
                                    (edit.get("placement_spec") or {}).get(
                                        "arrangement") or ""),
                )
            else:
                capacity = int(region.get("estimated_capacity", 0))
            centroid = region.get("screen_centroid")
            if capacity < 1 or centroid is None:
                continue
            regions.append({
                "region_id": region_id,
                "capacity": capacity,
                "max_clearance_steps": int(
                    region.get("max_clearance_steps", 0)),
                "cell_count": int(region.get("cell_count", 0)),
                "screen_centroid": centroid,
            })
        capacity_total = sum(row["capacity"] for row in regions)
        if capacity_total < len(indices):
            # ★盡量模式★(2026-09-11)：少一格就整個 case 失敗太脆弱 —— 0911 arctic
            #   「10 隻企鵝、只有 9 個位置」直接 INFEASIBLE，而且企鵝數量是 config 釘死的，
            #   retry 也降不下來 ⇒ 那個場景永遠跑不完。改成照樣把所有實例指派出去(容量
            #   超額的那幾個會在下面選格子時找不到位置)，由 apply_region_selection 的
            #   盡量模式丟掉放不下的那幾個並收斂數量。
            if not best_effort_placement():
                raise ValueError(
                    f"insufficient feasible capacity for {edit_id}: "
                    f"required={len(indices)} available={capacity_total}")
            print(f"[region] ⚠️ {edit_id}: 可行容量 {capacity_total} < 需要 "
                  f"{len(indices)} ⇒ 先全部指派，放不下的之後丟掉(盡量模式)")

        history_key = repr(sorted(
            tuple(map(float, point)) for point in rejected_for_edit))
        # ★畫面內優先是在「指派區域」這一層決定的★(2026-09-11)
        #   擴大網格把搜尋範圍變成 2x2 畫面，畫面外的連通區域自然又大又完整，而畫面內
        #   的可行格子常常零碎(0911 canyon: 平衡石畫面內只有 3 格、分散在不同區域)。
        #   底下的 `capacity >= 需要數量` 捷徑會把這些小區域整批排除 ⇒ 三個大物件全被
        #   指派到畫面外的大區域，之後選格子那層再怎麼「畫面內優先」也選不回來。
        #   ⇒ 擴大網格開著時：含畫面內格子的區域排在最前面，且一律走逐實例分配，
        #     畫面內填滿了才溢出去。
        import os as _os
        extended = float(
            _os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1) > 1.0
        in_frame_cells = {}
        if extended:
            for cell in map_data.get("cells", []):
                if cell.get("status") != "feasible":
                    continue
                point = _screen_point_or_none(cell.get("screen_position"))
                if point is not None and 0.0 < point[0] < 1.0 and 0.0 < point[1] < 1.0:
                    key = str(cell.get("region_id"))
                    in_frame_cells[key] = in_frame_cells.get(key, 0) + 1
        target, _target_source = _target_screen_point(edit, map_data)
        bounds = _screen_bounds_for_scoring(edit, map_data)
        max_clearance = max(
            (row["max_clearance_steps"] for row in regions), default=0) or 1
        max_cells = max((row["cell_count"] for row in regions), default=0) or 1
        required_count = max(len(indices), 1)

        def rank(row):
            digest = hashlib.sha256(
                f"{edit_id}|{history_key}|{row['region_id']}".encode("utf-8")
            ).hexdigest()
            centroid = _screen_point_or_none(row.get("screen_centroid"))
            target_score = (_screen_target_score(centroid, target, bounds)
                            if centroid is not None else 0.0)
            clearance_score = max(
                0.0, float(row["max_clearance_steps"])) / max_clearance
            size_score = max(0.0, float(row["cell_count"])) / max_cells
            capacity_score = min(
                1.0, max(0.0, float(row["capacity"])) / required_count)
            score = (0.52 * target_score
                     + 0.20 * clearance_score
                     + 0.18 * size_score
                     + 0.10 * capacity_score)
            # 畫面內的區域一律排在畫面外之前(tuple 由左至右比較)
            has_in_frame = 1 if in_frame_cells.get(row["region_id"]) else 0
            return (has_in_frame, score, row["capacity"],
                    row["max_clearance_steps"],
                    row["cell_count"], digest, row["region_id"])

        sufficient = [row for row in regions
                      if row["capacity"] >= len(indices)]
        if sufficient and not (extended and in_frame_cells):
            chosen = max(sufficient, key=rank)
            assignments.extend({
                "edit_id": edit_id,
                "instance_index": index,
                "region_id": chosen["region_id"],
            } for index in indices)
            continue

        available = {row["region_id"]: row["capacity"] for row in regions}
        ordered = sorted(regions, key=rank, reverse=True)
        for index in indices:
            # 容量用完時退回排名最前的區域(盡量模式才會走到；選格子那層會再擋一次)
            chosen = next((row for row in ordered
                           if available[row["region_id"]] > 0), ordered[0])
            available[chosen["region_id"]] -= 1
            assignments.append({
                "edit_id": edit_id,
                "instance_index": index,
                "region_id": chosen["region_id"],
            })
    return assignments


def estimate_region_capacity(region_cells: list[dict], *, half_extents,
                             min_sep: float, use_3d: bool = False,
                             arrangement: str | None = None) -> int:
    """Deterministic approximate XY packing capacity for one proxy footprint.

    A single greedy ordering can badly underestimate a long or concave region.
    Try several stable spatial/clearance orderings and report their maximum.
    This is still an estimate; exact application rechecks every chosen cell
    against fixed and newly placed proxy boxes.
    """
    half_x, half_y = map(float, half_extents[:2])
    min_sep = float(min_sep)
    if use_3d:
        min_sep = max(min_sep, 2.0 * max(half_x, half_y) + 0.1)
    elif str(arrangement or "") == "field":
        # Dense flower fields want row-to-row packing driven by the actual
        # stem/base footprint, not the looser circular hull used for scattered
        # hero props.
        min_sep = max(min_sep, 0.30)
    else:
        footprint_radius = math.hypot(half_x, half_y)
        min_sep = max(min_sep, 2.0 * footprint_radius + 0.1)
    usable = [cell for cell in region_cells
              if cell.get("world_position") is not None]

    def packed_count(ordered):
        selected = []
        for cell in ordered:
            world = cell["world_position"]
            compatible = True
            for prior in selected:
                other = prior["world_position"]
                dimensions = 3 if use_3d else 2
                if math.dist(world[:dimensions], other[:dimensions]) < min_sep:
                    compatible = False
                    break
                if use_3d and aabb_overlap_fraction(
                        cell.get("candidate_proxy_world_bounds"),
                        prior.get("candidate_proxy_world_bounds")) > 0.0:
                    compatible = False
                    break
            if compatible:
                selected.append(cell)
        return len(selected)

    order_keys = (
        lambda cell: (-int(cell.get("clearance_steps", 0)), cell["cell_id"]),
        lambda cell: (int(cell.get("clearance_steps", 0)), cell["cell_id"]),
        lambda cell: (cell["world_position"][0], cell["world_position"][1]),
        lambda cell: (cell["world_position"][1], cell["world_position"][0]),
        lambda cell: (cell["world_position"][2], cell["world_position"][0],
                      cell["world_position"][1]),
    )
    orderings = []
    for key in order_keys:
        ordered = sorted(usable, key=key)
        orderings.extend((ordered, list(reversed(ordered))))
    return max((packed_count(ordered) for ordered in orderings), default=0)


def outdoor_broad_search_target_capacity(*, required_capacity: int,
                                         arrangement: str | None = None) -> int:
    """Return the slack target used before broadening outdoor search bounds."""
    required = max(0, int(required_capacity))
    if required <= 1:
        return required
    if str(arrangement or "") == "field":
        # Dense botanical fields already want a compact patch.  Do not demand
        # extra slack just to widen the search box; that inflates the patch and
        # makes the field read as sparse.
        return max(required, 3)
    return max(required + 2, int(math.ceil(required * 1.5)), 3)


def validate_region_selection(selection: dict, *, maps: dict[str, dict],
                              expected: dict[str, set[int]]) -> list[dict]:
    """Validate one region assignment for every and only selected instance."""
    if selection.get("decision") != "select_regions":
        raise ValueError("region selection must use decision='select_regions'")
    assignments = selection.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("region assignments must be a list")
    normalized = []
    seen = set()
    counts = {}
    for row in assignments:
        edit_id = str(row.get("edit_id") or "")
        if row.get("instance_index") is None:
            raise ValueError("region assignment is missing instance_index")
        index = int(row["instance_index"])
        key = (edit_id, index)
        if key in seen:
            raise ValueError(f"duplicate region assignment for {key}")
        seen.add(key)
        if index not in expected.get(edit_id, set()):
            raise ValueError(f"region assignment moved a passing instance: {key}")
        region_id = str(row.get("region_id") or "")
        map_data = maps.get(edit_id)
        if map_data is None:
            raise ValueError(f"no planning map for {edit_id!r}")
        regions = {region["region_id"]: region
                   for region in map_data.get("regions", [])}
        if region_id not in regions:
            raise ValueError(
                f"unknown or infeasible region {region_id!r} for {edit_id}")
        counts[(edit_id, region_id)] = counts.get((edit_id, region_id), 0) + 1
        normalized.append({
            "edit_id": edit_id,
            "instance_index": index,
            "region_id": region_id,
        })
    required = {(edit_id, index) for edit_id, indices in expected.items()
                for index in indices}
    if seen != required:
        raise ValueError(
            f"region assignments must cover exactly selected instances; "
            f"missing={sorted(required - seen)} extra={sorted(seen - required)}")
    over_subscribed = best_effort_placement()
    for (edit_id, region_id), count in counts.items():
        region = next(region for region in maps[edit_id]["regions"]
                      if region["region_id"] == region_id)
        capacity = int(region.get("estimated_capacity", 0))
        if count > capacity and not over_subscribed:
            raise ValueError(
                f"region {edit_id}/{region_id} capacity={capacity}, assigned={count}")
        if count > capacity:
            # 盡量模式：容量不足時本來就是「先全部指派，放不下的在選格子時丟掉」
            print(f"[region] ⚠️ {edit_id}/{region_id} 指派 {count} > 容量 {capacity}"
                  f"，多出來的會在選格子時被丟掉(盡量模式)")
    return sorted(normalized, key=lambda row: (
        row["edit_id"], row["instance_index"]))


def _boxes_overlap(left, right, *, margin=0.1) -> bool:
    left_bounds = left.get("world_bounds")
    right_bounds = right.get("world_bounds")
    if left_bounds is not None and right_bounds is not None:
        return _bounds_overlap(left_bounds, right_bounds, margin=margin)
    left_3d = bool(left.get("collision_use_3d", left.get("use_3d")))
    right_3d = bool(right.get("collision_use_3d", right.get("use_3d")))
    if left_3d and right_3d:
        return all(
            abs(float(left["world_position"][axis])
                - float(right["world_position"][axis]))
            < (float(left["half_extents"][axis])
               + float(right["half_extents"][axis]) + float(margin))
            for axis in range(3)
        )
    left_radius = math.hypot(*map(float, left["half_extents"][:2]))
    right_radius = math.hypot(*map(float, right["half_extents"][:2]))
    return math.dist(
        left["world_position"][:2], right["world_position"][:2]
    ) < left_radius + right_radius + margin


def _screen_overlap_of_smaller(left, right) -> float:
    if not left or not right:
        return 0.0
    lx0, ly0, lx1, ly1 = map(float, left)
    rx0, ry0, rx1, ry1 = map(float, right)
    left_area = max(0.0, lx1 - lx0) * max(0.0, ly1 - ly0)
    right_area = max(0.0, rx1 - rx0) * max(0.0, ry1 - ry0)
    smaller = min(left_area, right_area)
    if smaller <= 1e-12:
        return 0.0
    intersection = (
        max(0.0, min(lx1, rx1) - max(lx0, rx0))
        * max(0.0, min(ly1, ry1) - max(ly0, ry0))
    )
    return float(intersection / smaller)


def _cell_collision_position(cell: dict, use_3d: bool) -> list[float]:
    """Return the proxy centre used by collision tests for one map cell."""
    if use_3d and cell.get("candidate_world_position") is not None:
        return list(map(float, cell["candidate_world_position"]))
    return list(map(float, cell["world_position"]))


def _collision_uses_3d(map_data: dict, use_3d: bool) -> bool:
    """Use vertical separation for indoor floor/table/bed/shelf proxies only."""
    if use_3d:
        return True
    policy = map_data.get("surface_policy") or {}
    surface_types = {
        str(value) for value in policy.get("required_surface_types") or []}
    return bool(surface_types & INDOOR_PLANAR_COLLISION_TYPES)


def _collision_half_extents(map_data: dict, use_3d: bool) -> list[float]:
    if _collision_uses_3d(map_data, use_3d):
        return list(map(float, map_data.get(
            "proxy_half_extents", [1.0, 1.0, 1.0])))
    return list(map(float, map_data.get(
        "footprint_half_extents", [1.0, 1.0])))


def _map_cell_collision_position(cell: dict, map_data: dict,
                                 use_3d: bool) -> list[float]:
    position = _cell_collision_position(cell, use_3d)
    if _collision_uses_3d(map_data, use_3d) and not use_3d:
        # Surface maps store the support point.  The collision box rests on
        # that point, so its centre is half a proxy height above it.
        position[2] += _collision_half_extents(map_data, use_3d)[2]
    return position


def _screen_step(map_data: dict) -> float:
    step = (map_data.get("grid") or {}).get("screen_step") or [0.0, 0.0]
    try:
        values = [abs(float(value)) for value in step[:2]]
    except (TypeError, ValueError):
        values = []
    return max(values or [0.0])


def _outdoor_ultra_loose_screen_sep(edit: dict, map_data: dict,
                                    *, same_edit: bool) -> float:
    """Return a soft visual spacing floor for ultra-loose placement.

    Ultra-loose outdoor placement keeps strict geometry advisory, but exact
    cell reuse is still a bad visual result.  Use screen-space spacing as a
    preference, and let apply_region_selection fall back only if a scene truly
    has no distinct feasible cells.
    """
    step = _screen_step(map_data)
    arrangement = str((edit.get("placement_spec") or {}).get(
        "arrangement") or ("single" if int(edit.get("n", 1)) == 1
                            else "scatter"))
    if same_edit and arrangement in {"cluster", "field", "school"}:
        return max(2.0 * step, 0.04)
    if same_edit and arrangement == "row":
        return max(3.0 * step, 0.07)
    if same_edit:
        return max(4.0 * step, 0.085)
    return max(5.0 * step, 0.10)


def _outdoor_ultra_loose_visual_conflict(
        cell: dict, *, edit: dict, edit_id: str, map_data: dict,
        occupied: list[dict]) -> bool:
    screen = cell.get("screen_position")
    if not isinstance(screen, (list, tuple)) or len(screen) != 2:
        return False
    screen = list(map(float, screen))
    cell_id = str(cell.get("cell_id") or "")
    for other in occupied:
        other_screen = other.get("screen_position")
        same_edit = str(other.get("edit_id") or "") == edit_id
        if cell_id and str(other.get("cell_id") or "") == cell_id:
            return True
        if isinstance(other_screen, (list, tuple)) and len(other_screen) == 2:
            required_sep = _outdoor_ultra_loose_screen_sep(
                edit, map_data, same_edit=same_edit)
            if math.dist(screen, list(map(float, other_screen))) < required_sep:
                return True
        if _screen_overlap_of_smaller(
                cell.get("candidate_screen_bounds"),
                other.get("screen_bounds")) > 0.20:
            return True
    return False


def cross_edit_reserved_cells(map_data: dict, reservations: list[dict]
                              ) -> list[dict]:
    """Mark this edit's feasible cells consumed by other selected edits."""
    edit_id = str(map_data.get("edit_id") or "")
    use_3d = uses_3d_volume_map(map_data)
    collision_use_3d = _collision_uses_3d(map_data, use_3d)
    half_extents = _collision_half_extents(map_data, use_3d)
    min_sep = float(map_data.get("min_sep", 2.5))
    result = []
    for cell in map_data.get("cells", []):
        if cell.get("status") != "feasible" or cell.get("world_position") is None:
            continue
        box = {
            "world_position": _map_cell_collision_position(
                cell, map_data, use_3d),
            "half_extents": half_extents,
            "use_3d": use_3d,
            "collision_use_3d": collision_use_3d,
            "world_bounds": cell.get("candidate_proxy_world_bounds"),
        }
        for other in reservations:
            if str(other.get("edit_id") or "") == edit_id:
                continue
            other_position = (other.get("collision_world_position")
                              or other.get("candidate_world_position")
                              or other.get("world_position"))
            if other_position is None or other.get("half_extents") is None:
                continue
            other_box = {
                "world_position": list(map(float, other_position)),
                "half_extents": list(map(float, other["half_extents"])),
                "use_3d": bool(other.get("use_3d")),
                "collision_use_3d": bool(other.get(
                    "collision_use_3d", other.get("use_3d"))),
                "world_bounds": (other.get("candidate_proxy_world_bounds")
                                 or other.get("world_bounds")),
            }
            dimensions = (
                3 if collision_use_3d
                and other_box["collision_use_3d"] else 2)
            too_close = math.dist(
                box["world_position"][:dimensions],
                other_box["world_position"][:dimensions],
            ) < max(min_sep, float(other.get("min_sep", 2.5)))
            if not _boxes_overlap(box, other_box) and not too_close:
                continue
            result.append({
                "cell_id": str(cell["cell_id"]),
                "reserved_by_edit_id": str(other.get("edit_id") or ""),
                "reserved_by_instance_index": int(
                    other.get("instance_index", 0)),
            })
            break
    return result


def _assignment_priority(row: dict, maps: dict[str, dict]) -> tuple:
    """Allocate large proxies first so small objects cannot strand them."""
    edit_id = str(row["edit_id"])
    map_data = maps[edit_id]
    use_3d = uses_3d_volume_map(map_data)
    half_extents = list(map(float, (
        map_data.get("proxy_half_extents", [1.0, 1.0, 1.0])
        if use_3d else map_data.get(
            "footprint_half_extents", [1.0, 1.0]))))
    size = math.prod(2.0 * value for value in half_extents)
    return (-size, edit_id, int(row["instance_index"]))


def _choose_arrangement_cell(*, compatible: list[dict], edit: dict,
                             edit_id: str, instance_index: int,
                             map_data: dict, occupied: list[dict],
                             use_3d: bool,
                             history_key: str) -> tuple[dict, str, dict]:
    """Apply the edit's deterministic packing strategy to feasible cells."""
    import os
    if float(os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1) > 1.0:
        # Extended grid: fill the real frame first; only when no in-frame cell is
        # left spill off-screen (then the usual near-camera / near-target ranking
        # picks the off-screen cells closest to the camera and the frame).
        in_frame = [
            cell for cell in compatible
            if (point := _screen_point_or_none(cell.get("screen_position"))) is not None
            and 0.0 <= point[0] <= 1.0 and 0.0 <= point[1] <= 1.0
        ]
        if in_frame:
            compatible = in_frame
    arrangement = str((edit.get("placement_spec") or {}).get(
        "arrangement") or ("single" if int(edit.get("n", 1)) == 1
                            else "scatter"))
    siblings = [row for row in occupied if row.get("edit_id") == edit_id]
    dimensions = 3 if use_3d else 2
    target, target_source = _target_screen_point(
        edit, map_data, instance_index)
    screen_bounds = _screen_bounds_for_scoring(edit, map_data)
    max_clearance = max(
        (max(0.0, float(cell.get("clearance_steps") or 0.0))
         for cell in compatible),
        default=0.0,
    ) or 1.0
    region_counts = _region_cell_count_map(map_data)
    max_region_cells = max(region_counts.values(), default=0) or 1

    def tie(cell):
        return hashlib.sha256(
            f"{edit_id}|{instance_index}|{history_key}|{cell['cell_id']}".encode(
                "utf-8")
        ).hexdigest()

    def camera_distance(cell):
        value = (cell.get("candidate_depth_m") if use_3d
                 else cell.get("distance"))
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return None

    measurable_cells = [cell for cell in compatible
                        if camera_distance(cell) is not None]
    measurable_distances = [camera_distance(cell) for cell in measurable_cells]
    depth_min = min(measurable_distances) if measurable_distances else None
    depth_max = max(measurable_distances) if measurable_distances else None
    depth_span = (
        max(float(depth_max) - float(depth_min), 1e-9)
        if depth_min is not None and depth_max is not None else None)

    def camera_depth_score(cell):
        distance = camera_distance(cell)
        if distance is None or depth_min is None or depth_span is None:
            return None
        score = 1.0 - (distance - float(depth_min)) / float(depth_span)
        return max(0.0, min(1.0, float(score)))

    def camera_scores(cell):
        screen = _screen_point_or_none(cell.get("screen_position")) or target
        target_score = _screen_target_score(screen, target, screen_bounds)
        clearance_score = (
            max(0.0, float(cell.get("clearance_steps") or 0.0))
            / max_clearance)
        frame_score = _frame_margin_score(cell)
        size_score = projected_size_score(cell)
        region_score = (
            max(0.0, float(region_counts.get(
                str(cell.get("region_id") or ""), 0)))
            / max_region_cells)
        composition_score = (0.55 * target_score
                             + 0.25 * clearance_score
                             + 0.15 * frame_score
                             + 0.05 * region_score)
        depth_score = camera_depth_score(cell)
        # ★量得到投影大小時，用它取代「越近越好」★(2026-09-11)
        #   深度本來只是「看起來夠大」的替代指標，而它★只會把東西往鏡頭前拉★：
        #   0911 coast 的帆船被放到 13.7m、佔畫面 32% 擋住鏡頭。投影大小才是真正要的
        #   量——小東西唯有靠近才夠大，大東西太近就爆框，一個分數同時管兩端。
        #   沒有量測值(舊地圖)時維持原本的深度偏好。
        measured_size = cell.get("projected_area_fraction") is not None
        if measured_size:
            proximity_score = 0.72 * size_score + 0.28 * composition_score
        elif depth_score is None:
            proximity_score = composition_score
        else:
            proximity_score = 0.72 * depth_score + 0.28 * composition_score
        plane_quality = cell.get("plane_quality_score")
        plane_score = None
        if isinstance(plane_quality, (int, float)) and math.isfinite(
                float(plane_quality)):
            plane_score = max(0.0, min(1.0, float(plane_quality)))
        return {
            "screen_target": [float(target[0]), float(target[1])],
            "screen_target_source": target_source,
            "screen_target_score": target_score,
            "clearance_score": clearance_score,
            "frame_score": frame_score,
            "region_size_score": region_score,
            "plane_quality_score": plane_score,
            "camera_composition_score": composition_score,
            "projected_size_score": size_score,
            "projected_area_fraction": cell.get("projected_area_fraction"),
            "camera_depth_score": depth_score,
            "camera_proximity_score": proximity_score,
            "camera_proximity_policy": (
                "hard_feasible_cells_prefer_near_camera_then_composition"),
            "camera_composition_policy": (
                "hard_feasible_cells_ranked_by_camera_target_clearance_"
                "frame_margin_and_region_size"),
        }

    if not siblings or arrangement == "single":
        # Prefer the nearest feasible depth first, then let screen centering
        # and clearance break ties. We still keep a small depth ladder so the
        # map can report a meaningful band instead of one exact slice.
        measurable = measurable_cells
        if measurable:
            depths = [camera_distance(cell) for cell in measurable]
            prior_edits = {
                str(row.get("edit_id") or "") for row in occupied
                if str(row.get("edit_id") or "") != edit_id
            }
            depth_stage_index = len(prior_edits)
            target_quantile = min(0.18, 0.04 + 0.03 * depth_stage_index)
            sorted_depths = sorted(depths)
            target_depth = sorted_depths[round(
                target_quantile * (len(sorted_depths) - 1))]

            def depth_camera_scores(cell):
                scores = camera_scores(cell)
                depth_score = scores.get("camera_depth_score")
                if depth_score is None:
                    depth_score = 0.0
                if scores.get("projected_area_fraction") is not None:
                    # ★同 camera_scores 的規則：量得到投影大小就取代深度★
                    #   n=1 的單體走這條路，0911 的帆船就是這樣被拉到 13.7m 的。
                    depth_score = scores["projected_size_score"]
                plane_score = scores.get("plane_quality_score")
                if plane_score is None:
                    soft_score = (0.76 * depth_score
                                  + 0.24 * scores[
                                      "camera_composition_score"])
                else:
                    # Indoor plane patches carry dense support/height/slope
                    # evidence.  Keep camera composition meaningful without
                    # selecting a narrow or irregular patch merely by depth.
                    soft_score = (0.64 * depth_score
                                  + 0.21 * scores[
                                      "camera_composition_score"]
                                  + 0.15 * plane_score)
                scores["camera_soft_score"] = soft_score
                return scores

            def soft_near_rank(cell):
                scores = depth_camera_scores(cell)
                return (
                    -frame_tier(cell),      # ★看得見優先★
                    scores["camera_soft_score"],
                    scores["screen_target_score"],
                    scores["clearance_score"],
                    tie(cell),
                    cell["cell_id"],
                )

            chosen = max(measurable, key=soft_near_rank)
            chosen_depth = camera_distance(chosen)
            scores = depth_camera_scores(chosen)
            return chosen, (
                "single_depth_staged_camera_safe_cell"
                if arrangement == "single"
                else f"{arrangement}_depth_staged_camera_group_origin"), {
                    "camera_distance_m": chosen_depth,
                    "available_camera_distance_range_m": [depth_min, depth_max],
                    "depth_target_quantile": target_quantile,
                    "depth_target_distance_m": target_depth,
                    "depth_stage_index": depth_stage_index,
                    "depth_policy": (
                        "near_camera_preference_with_light_cross_edit_staging"),
                    **scores,
                }

        chosen = max(
            compatible,
            key=lambda cell: (
                -frame_tier(cell),          # ★看得見優先★
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                camera_scores(cell)["screen_target_score"],
                tie(cell),
                cell["cell_id"],
            ))
        return chosen, (
            "single_camera_safe_cell" if arrangement == "single"
            else f"{arrangement}_camera_group_origin"), {
                "depth_policy": "unavailable_in_legacy_map",
                **camera_scores(chosen),
            }

    sibling_world = [row["world_position"][:dimensions] for row in siblings]

    if arrangement == "scatter":
        def scatter_rank(cell):
            point = _cell_collision_position(cell, use_3d)[:dimensions]
            nearest = min(math.dist(point, other) for other in sibling_world)
            return (
                -frame_tier(cell),          # ★看得見優先，再談散開★
                nearest,
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                tie(cell),
                cell["cell_id"],
            )
        return max(compatible, key=scatter_rank), (
            "scatter_farthest_camera_tiebreak_safe_cell"), {}

    if arrangement == "field":
        centroid = [
            sum(float(point[axis]) for point in sibling_world) / len(sibling_world)
            for axis in range(dimensions)
        ]

        def field_rank(cell):
            point = _cell_collision_position(cell, use_3d)[:dimensions]
            return (
                -frame_tier(cell),          # ★看得見優先★
                -math.dist(point, centroid),
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                tie(cell),
                cell["cell_id"],
            )

        return max(compatible, key=field_rank), (
            "field_compact_camera_tiebreak_safe_cell"), {}

    if arrangement == "row":
        axis = str((edit.get("placement_spec") or {}).get(
            "row_axis") or "horizontal")
        along_index, cross_index = ((0, 1) if axis == "horizontal" else (1, 0))
        sibling_screen = [row.get("screen_position") for row in siblings
                          if row.get("screen_position") is not None]
        bounds = (edit.get("placement_spec") or {}).get(
            "screen_bounds", [0.0, 0.0, 1.0, 1.0])
        target_cross = (sum(float(point[cross_index]) for point in sibling_screen)
                        / len(sibling_screen) if sibling_screen else
                        (float(bounds[cross_index])
                         + float(bounds[cross_index + 2])) / 2.0)

        def row_rank(cell):
            screen = cell["screen_position"]
            cross_error = abs(float(screen[cross_index]) - target_cross)
            along_gap = min(
                abs(float(screen[along_index]) - float(other[along_index]))
                for other in sibling_screen) if sibling_screen else 0.0
            return (
                -frame_tier(cell),          # ★看得見優先★
                -cross_error,
                along_gap,
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                tie(cell),
                cell["cell_id"],
            )
        return (max(compatible, key=row_rank),
                "row_axis_aligned_camera_tiebreak_safe_cell", {})

    if arrangement in {"cluster", "school"}:
        centroid = [
            sum(float(point[axis]) for point in sibling_world)
            / len(sibling_world)
            for axis in range(dimensions)
        ]

        def cluster_rank(cell):
            point = _cell_collision_position(cell, use_3d)[:dimensions]
            # Compactness is primary.  The hash only breaks equal-grid ties,
            # so a fish school is never accidentally treated as scatter.
            return (
                -frame_tier(cell),          # ★看得見優先★
                -math.dist(point, centroid),
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                tie(cell),
                cell["cell_id"],
            )
        policy = ("school_compact_3d_camera_tiebreak_safe_cell"
                  if arrangement == "school"
                  else "cluster_compact_camera_tiebreak_safe_cell")
        return max(compatible, key=cluster_rank), policy, {}

    return (max(compatible, key=lambda cell: (
                camera_scores(cell)["camera_proximity_score"],
                camera_scores(cell)["camera_composition_score"],
                tie(cell),
                cell["cell_id"])),
            "camera_safe_cell", {})


def apply_region_selection(plan: dict, *, maps: dict[str, dict],
                           assignments: list[dict],
                           rejected_positions: dict[tuple[str, int], list[list[float]]] | None = None
                           ) -> tuple[dict, dict]:
    """Choose exact feasible cells deterministically and move only assigned instances."""
    revised = deepcopy(plan)
    rejected_positions = rejected_positions or {}
    edits = {str(edit.get("code_from") or ""): edit
             for edit in revised.get("hero_edits", []) if edit.get("op") == "add"}
    moving = {(row["edit_id"], int(row["instance_index"])) for row in assignments}
    occupied = []
    unplaced: dict[str, set[int]] = {}

    # Approximate passing sibling world locations by their nearest evaluated map
    # cell.  Machine Review remains the final collision authority.
    for edit_id, edit in edits.items():
        map_data = maps.get(edit_id)
        if map_data is None:
            continue
        use_3d = uses_3d_volume_map(map_data)
        collision_use_3d = _collision_uses_3d(map_data, use_3d)
        hit_cells = [cell for cell in map_data.get("cells", [])
                     if cell.get("world_position") is not None]
        current_world = {
            int(row["instance_index"]): (
                row.get("candidate_world_position")
                if use_3d and row.get("candidate_world_position") is not None
                else row.get("world_position"))
            for row in map_data.get("current_instances", [])
            if row.get("instance_index") is not None
            and row.get("world_position") is not None
        }
        explicit_positions = edit.get("positions")
        half_extents = _collision_half_extents(map_data, use_3d)
        for index, point in enumerate(edit.get("screen_positions") or []):
            if (edit_id, index) in moving or not hit_cells:
                continue
            nearest = min(hit_cells, key=lambda cell: math.dist(
                list(map(float, point)), cell["screen_position"]))
            world_position = current_world.get(index)
            if (world_position is None
                    and isinstance(explicit_positions, list)
                    and index < len(explicit_positions)
                    and isinstance(explicit_positions[index], list)
                    and len(explicit_positions[index]) == 3):
                world_position = explicit_positions[index]
            if world_position is None:
                world_position = _map_cell_collision_position(
                    nearest, map_data, use_3d)
            elif collision_use_3d and not use_3d:
                world_position = list(map(float, world_position))
                world_position[2] += float(half_extents[2])
            occupied.append({
                "edit_id": edit_id,
                "instance_index": index,
                "cell_id": nearest.get("cell_id"),
                "world_position": world_position,
                "half_extents": half_extents,
                "use_3d": use_3d,
                "collision_use_3d": collision_use_3d,
                "world_bounds": nearest.get("candidate_proxy_world_bounds"),
                "screen_bounds": nearest.get("candidate_screen_bounds"),
                "screen_position": list(map(float, point)),
                "min_sep": float(
                    map_data.get("min_sep")
                    if map_data.get("min_sep") is not None
                    else edit.get("min_sep")
                    if edit.get("min_sep") is not None
                    else 2.5),
            })

    applied = []
    ordered_assignments = sorted(
        assignments, key=lambda row: _assignment_priority(row, maps))
    for assignment in ordered_assignments:
        edit_id = assignment["edit_id"]
        index = int(assignment["instance_index"])
        region_id = assignment["region_id"]
        edit = edits[edit_id]
        map_data = maps[edit_id]
        use_3d = uses_3d_volume_map(map_data)
        collision_use_3d = _collision_uses_3d(map_data, use_3d)
        half_extents = _collision_half_extents(map_data, use_3d)
        all_candidates = [cell for cell in map_data.get("cells", [])
                          if cell.get("status") == "feasible"]
        candidates = [cell for cell in all_candidates
                      if cell.get("region_id") == region_id]
        rejected_for_edit = [
            point
            for (prior_edit_id, _), points in rejected_positions.items()
            if prior_edit_id == edit_id
            for point in points
        ]
        def fresh_cells(rows):
            if not rejected_for_edit:
                return rows
            cell_step = max(
                float(map_data["grid"]["screen_step"][0]),
                float(map_data["grid"]["screen_step"][1]),
            )
            return [cell for cell in rows if all(
                math.dist(cell["screen_position"], point) > cell_step * 0.75
                for point in rejected_for_edit)]

        candidates = fresh_cells(candidates)
        min_sep = float(
            map_data.get("min_sep")
            if map_data.get("min_sep") is not None
            else edit.get("min_sep")
            if edit.get("min_sep") is not None
            else 2.5)
        def compatible_cells(rows):
            result = []
            for cell in rows:
                collision_position = _map_cell_collision_position(
                    cell, map_data, use_3d)
                box = {
                    "world_position": collision_position,
                    "half_extents": half_extents,
                    "use_3d": use_3d,
                    "collision_use_3d": collision_use_3d,
                    "world_bounds": cell.get("candidate_proxy_world_bounds"),
                    "screen_bounds": cell.get("candidate_screen_bounds"),
                }
                if any(_boxes_overlap(box, other) for other in occupied):
                    continue
                if any(math.dist(
                        collision_position[
                            :3 if collision_use_3d and other.get(
                                "collision_use_3d", other.get("use_3d")) else 2],
                        other["world_position"][
                            :3 if collision_use_3d and other.get(
                                "collision_use_3d", other.get("use_3d")) else 2])
                        < max(min_sep, float(other.get("min_sep", 2.5)))
                        for other in occupied):
                    continue
                result.append(cell)
            return result

        compatible = compatible_cells(candidates)
        cross_region_fallback = False
        if not compatible:
            # Region assignment groups an edit compositionally, but a prior
            # large edit can consume that component.  The global allocator may
            # transparently use another green component; it records both ids.
            fallback = fresh_cells([
                cell for cell in all_candidates
                if cell.get("region_id") != region_id])
            compatible = compatible_cells(fallback)
            cross_region_fallback = bool(compatible)
        if not compatible:
            if best_effort_placement():
                # ★盡量，不是全有全無★(2026-09-11)：畫面塞不下第 k 個實例時，
                #   少放一個比整個 case 失敗好(Step 8 的失敗大宗就是這個)。
                #   收斂數量在迴圈結束後一次做完，才不會打亂 instance_index。
                unplaced.setdefault(edit_id, set()).add(index)
                continue
            raise ValueError(
                f"all feasible regions for {edit_id} have no remaining "
                f"collision-free cell for instance {index} "
                f"(requested region {region_id})")

        history_key = repr(sorted(
            tuple(map(float, point)) for point in rejected_for_edit))

        chosen, selection_policy, depth_selection = _choose_arrangement_cell(
            compatible=compatible,
            edit=edit,
            edit_id=edit_id,
            instance_index=index,
            map_data=map_data,
            occupied=occupied,
            use_3d=use_3d,
            history_key=history_key,
        )
        feasible_yaws = [float(value) for value in (
            chosen.get("feasible_yaws") or [0.0])]
        chosen_yaw = max(feasible_yaws, key=lambda yaw: hashlib.sha256(
            f"{edit_id}|{index}|{history_key}|{chosen['cell_id']}|{yaw}".encode(
                "utf-8")
        ).hexdigest())
        before = deepcopy(edit["screen_positions"][index])
        edit["screen_positions"][index] = deepcopy(chosen["screen_position"])
        placement_yaws = edit.get("placement_yaws")
        if not isinstance(placement_yaws, list) or len(placement_yaws) != len(
                edit["screen_positions"]):
            placement_yaws = [None] * len(edit["screen_positions"])
        placement_yaws[index] = chosen_yaw
        edit["placement_yaws"] = placement_yaws
        if uses_3d_volume_map(map_data):
            positions = edit.get("positions")
            if not isinstance(positions, list) or len(positions) != len(
                    edit["screen_positions"]):
                positions = [None] * len(edit["screen_positions"])
            positions[index] = deepcopy(chosen["world_position"])
            edit["positions"] = positions
            edit["screen_position_projection"] = "explicit_3d_region_map"
        elif not (0.0 < chosen["screen_position"][0] < 1.0
                  and 0.0 < chosen["screen_position"][1] < 1.0):
            # ★畫面外的格子要交世界座標★(2026-09-11)：擴大網格(BI_PLACEMENT_EXTENDED_GRID>1)
            #   的搜尋範圍是 2x2 畫面，種子座標本來就會是負的或 >1。這種點★無法再投影回
            #   「可見表面」★(可見表面只由畫面內的取樣建出來)，apply_plan 會直接擋下整個
            #   case(0911 canyon: screen_positions[0] 超出畫面 [0.486, -0.425])。
            #   地圖自己就有這個格子的世界座標與法線，直接交下去，不要重投影。
            positions = edit.get("positions")
            if not isinstance(positions, list) or len(positions) != len(
                    edit["screen_positions"]):
                positions = [None] * len(edit["screen_positions"])
            supports = edit.get("explicit_position_supports")
            if not isinstance(supports, list) or len(supports) != len(
                    edit["screen_positions"]):
                supports = [None] * len(edit["screen_positions"])
            positions[index] = deepcopy(chosen["world_position"])
            supports[index] = {
                "surface": chosen.get("hit_surface"),
                "surface_normal": chosen.get("surface_normal"),
                "cell_id": chosen.get("cell_id"),
                "off_screen": True,
            }
            edit["positions"] = positions
            edit["explicit_position_supports"] = supports
            edit["screen_position_projection"] = "explicit_3d_region_map"
        else:
            edit["screen_position_projection"] = "visible_surface"
            edit.pop("positions", None)
        occupied.append({
            "edit_id": edit_id,
            "instance_index": index,
            "cell_id": chosen.get("cell_id"),
            "world_position": _map_cell_collision_position(
                chosen, map_data, use_3d),
            "candidate_world_position": chosen.get("candidate_world_position"),
            "candidate_depth_m": chosen.get("candidate_depth_m"),
            "half_extents": half_extents,
            "use_3d": use_3d,
            "collision_use_3d": collision_use_3d,
            "world_bounds": chosen.get("candidate_proxy_world_bounds"),
            "screen_bounds": chosen.get("candidate_screen_bounds"),
            "screen_position": chosen.get("screen_position"),
            "min_sep": min_sep,
        })
        applied.append({
            **assignment,
            "requested_region_id": region_id,
            "region_id": str(chosen.get("region_id") or region_id),
            "cross_region_fallback": cross_region_fallback,
            "outdoor_ultra_loose": False,
            "ultra_loose_collision_fallback": False,
            "cell_id": chosen["cell_id"],
            "screen_position_before": before,
            "screen_position_after": chosen["screen_position"],
            "world_position": chosen["world_position"],
            "candidate_world_position": chosen.get("candidate_world_position"),
            "candidate_depth_m": chosen.get("candidate_depth_m"),
            "candidate_proxy_world_bounds": chosen.get(
                "candidate_proxy_world_bounds"),
            "placement_yaw": chosen_yaw,
            "clearance_steps": chosen.get("clearance_steps"),
            "collision_world_position": _map_cell_collision_position(
                chosen, map_data, use_3d),
            "half_extents": half_extents,
            "use_3d": use_3d,
            "collision_use_3d": collision_use_3d,
            "min_sep": min_sep,
            "arrangement": (edit.get("placement_spec") or {}).get(
                "arrangement"),
            "selection_policy": selection_policy,
            **depth_selection,
        })
    # ★數量收斂★：把塞不下的實例從 plan 拿掉(全部塞不下就原樣留著，讓 apply_plan
    #   自己取樣，行為與這個開關關掉時相同)。
    dropped = []
    for edit_id, indices in unplaced.items():
        edit = edits.get(edit_id)
        points = (edit or {}).get("screen_positions")
        if not edit or not isinstance(points, list):
            continue
        keep = [i for i in range(len(points)) if i not in indices]
        if not keep:
            continue
        for field in ("screen_positions", "placement_yaws", "positions"):
            values = edit.get(field)
            if isinstance(values, list) and len(values) == len(points):
                edit[field] = [values[i] for i in keep]
        edit["n"] = len(keep)
        dropped.append({"edit_id": edit_id, "requested": len(points),
                        "placed": len(keep),
                        "unplaced_instances": sorted(indices)})
        print(f"[region] ⚠️ {edit_id}: 畫面塞不下 {len(indices)} 個 ⇒ "
              f"放 {len(keep)}/{len(points)} 個(盡量模式)")
    return revised, {"contract_version": 1, "applied": applied,
                     "best_effort_reductions": dropped}

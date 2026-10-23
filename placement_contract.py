"""Pure-data contract for semantic hero-add placement.

The agent proposes a layout in Composer JSON.  Blender executes only the
geometric portion of the contract; semantic targets (for example ``ice floe``
or ``river bank``) are reviewed from the rendered Step-8 preview.  Geometry
validators report violations and never silently choose replacement positions.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


ARRANGEMENTS = {"single", "scatter", "row", "cluster", "field", "school"}
ROW_AXES = {"horizontal", "vertical"}
ORIENTATIONS = {"random", "parallel"}
PLACEMENT_MODES = {"center", "edge", "back_to_wall", "wall_mount", "row"}
DEFAULT_SURFACE_POLICY = {
    "required_surface_types": [],
    "forbidden_surface_types": [],
    "required_surface_names": [],
    "forbidden_surface_names": [],
    "max_slope_deg": None,
    "require_full_footprint_support": True,
}
SMALL_INSECT_PROXY_DIMENSIONS_M = [0.5, 0.5, 0.35]
SMALL_INSECT_MIN_SEP_M = 0.8
SMALL_INSECT_TOKENS = frozenset({
    "bee", "bees", "wasp", "wasps", "hornet", "hornets",
    "insect", "insects", "butterfly", "butterflies",
    "dragonfly", "dragonflies", "firefly", "fireflies",
})
SMALL_INSECT_FACTORIES = frozenset({
    "BeeFactory", "BeetleFactory", "ButterflyFactory", "DragonflyFactory",
    "FireflyFactory", "FlyingInsectFactory", "InsectFactory",
})
DENSE_FIELD_OBJECT_TOKENS = frozenset({
    "blossom", "blossoms", "bloom", "blooms", "flower", "flowers",
    "grass", "plant", "plants",
    "sunflower", "sunflowers", "wildflower", "wildflowers",
})
DENSE_FIELD_TARGET_TOKENS = DENSE_FIELD_OBJECT_TOKENS - {"grass"}
DENSE_FIELD_FACTORIES = frozenset({
    "FlowerFactory",
})
# ★會動的東西不是「一片植物」★(2026-09-11)：dense-field 的下限 60 和 0.22x0.22x1.8 的
#   細長 proxy 都是為花海設計的。0911 forest 的蝴蝶因為 semantic_target 提到 blossom
#   就被放大成 60 隻，然後撞上 fixed-asset 的 hero 上限 12，連退四次讓整個 case 失敗。
CREATURE_FACTORY_TOKENS = ("Fish", "Bird", "Dragonfly", "Beetle", "Crustacean",
                           "Carnivore", "Herbivore", "Snake", "Jellyfish",
                           "Lobster", "Crab", "Creature")
CREATURE_OBJECT_TOKENS = frozenset({
    "butterfly", "butterflies", "moth", "moths", "bee", "bees", "bird", "birds",
    "fish", "dragonfly", "dragonflies", "beetle", "beetles", "firefly",
    "fireflies", "insect", "insects", "wasp", "hummingbird", "hummingbirds",
})


def is_creature(factory: str | None, code_from: str | None = None) -> bool:
    """Whether this object is an animal, and so never a botanical dense field."""
    name = str(factory or "")
    if any(token in name for token in CREATURE_FACTORY_TOKENS):
        return True
    return bool(_semantic_words(factory, code_from) & CREATURE_OBJECT_TOKENS)
DENSE_FIELD_PROXY_DIMENSIONS_M = [0.22, 0.22, 1.80]
DENSE_FIELD_VISUAL_FIT_DIMENSIONS_M = [1.20, 1.20, 0.95]
DENSE_FIELD_INSTANCE_FLOOR = 60
PLACEHOLDER_PROXY_DIMENSIONS_M = {
    "SeaweedFactory": [0.55, 0.55, 3.40],
    "KelpMonocotFactory": [0.75, 0.75, 10.80],
}

# These names exist only in the deterministic indoor surface registry.  They
# deliberately do not include outdoor terrain types (snow, rock, soil, ...),
# so tightening this contract cannot change the outdoor map-first policy.
INDOOR_PLANAR_SURFACE_TYPES = frozenset({
    "floor", "tabletop", "countertop", "shelf", "bed_top", "seat",
})
INDOOR_RIGID_PLANAR_SURFACE_TYPES = frozenset({
    "floor", "tabletop", "countertop", "shelf",
})
INDOOR_SOFT_PLANAR_SURFACE_TYPES = frozenset({"bed_top", "seat"})

# The Director estimate remains authoritative except for known semantic
# minimums.  PlushToyFactory currently uses PillowFactory as its Stage-C
# substitute; the previous 0.32m proxy forced that pillow to be nearly
# invisible.  This minimum produces a four-times larger realized asset while
# reserving the same footprint before deterministic placement selection.
INDOOR_PROXY_DIMENSION_MINIMUMS_M = {
    "PlushToyFactory": [1.28, 0.96, 0.60],
}

# Indoor review assets should be legible in the final scene camera, not merely
# physically plausible in metres.  The map builder measures a provisional
# proxy at the actual candidate-surface depths and asks this pure-data policy
# for a coarse uniform scale.  Keeping the policy here makes the indoor guard
# and category caps independently testable; outdoor surface types always
# return 1x.
INDOOR_VISIBILITY_TARGET_MAJOR_FRACTION = 0.12
INDOOR_VISIBILITY_SCALE_STEPS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
INDOOR_VISIBILITY_MAX_SCALE_BY_SURFACE = {
    "floor": 3.0,
    "tabletop": 2.5,
    "countertop": 2.5,
    "shelf": 2.0,
    "bed_top": 4.0,
    "seat": 4.0,
}


def blender_safe_placeholder_name(factory: str, seed: int, instance_index: int,
                                  *, max_bytes: int = 63) -> str:
    """Return a parseable placeholder name that Blender will not truncate.

    Blender object IDs are capped at 63 UTF-8 bytes.  Long generated factory
    names can therefore lose the ``spawn_placeholder(index)`` suffix required
    by Infinigen's population parser.  Preserve the readable canonical name
    whenever it fits; otherwise use a deterministic factory digest while
    retaining the same seed/index grammar.
    """
    canonical = f"{factory}({int(seed)}).spawn_placeholder({int(instance_index)})"
    if len(canonical.encode("utf-8")) <= int(max_bytes):
        return canonical
    digest = hashlib.sha1(str(factory).encode("utf-8")).hexdigest()[:10]
    compact = f"F{digest}({int(seed)}).spawn_placeholder({int(instance_index)})"
    if len(compact.encode("utf-8")) > int(max_bytes):
        raise ValueError(
            f"placeholder identity exceeds Blender's {max_bytes}-byte limit")
    return compact


def import_path_matches_overlay(module_path: str | Path,
                                manifest_root: str | Path,
                                import_roots) -> bool:
    """Check that a module was imported through the requested overlay tree.

    A continuation run may put a symlink alias of ``manifest_root`` on
    ``PYTHONPATH``.  Resolve roots to identify those aliases, but deliberately
    do *not* resolve ``module_path``: unchanged files inside a generated
    overlay are themselves symlinks back to the runtime tree.  Resolving the
    complete module path would therefore reject a valid overlay mirror.
    """

    manifest = Path(manifest_root).resolve()
    allowed_roots = [Path(manifest_root).absolute()]
    for value in import_roots:
        if not value:
            continue
        candidate = Path(value)
        try:
            if candidate.resolve() == manifest:
                allowed_roots.append(candidate.absolute())
        except (OSError, RuntimeError):
            continue
    module = Path(module_path).absolute()
    for root in allowed_roots:
        try:
            module.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _read_base_pkg_meta(path: Path) -> Path | None:
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(meta, dict):
        return None
    base_pkg = meta.get("base_pkg")
    return Path(base_pkg) if base_pkg else None


def overlay_base_pkg(overlay_root: str | Path) -> Path | None:
    """Return the recorded base package for one staged overlay root, if known."""
    root = Path(overlay_root)
    resolved = root.resolve(strict=False)
    for candidate in (
        root / "overlay_source.json",
        root / "library_source.json",
        resolved / "overlay_source.json",
        resolved / "library_source.json",
        root.parent / "overlay_source.json",
        root.parent / "library_source.json",
        resolved.parent / "overlay_source.json",
        resolved.parent / "library_source.json",
    ):
        base_pkg = _read_base_pkg_meta(candidate)
        if base_pkg is not None:
            return base_pkg

    overlay_name = root.name
    root_str = str(root)
    resolved_str = str(resolved)
    for parent in root.parents:
        table_path = parent / "overlay_table.json"
        if not table_path.is_file():
            continue
        try:
            table = json.loads(table_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(table, dict):
            continue
        entry = table.get(overlay_name)
        if isinstance(entry, dict):
            base_pkg = entry.get("base_pkg")
            if base_pkg:
                return Path(base_pkg)
        for row in table.values():
            if not isinstance(row, dict):
                continue
            if {root_str, resolved_str} & {
                str(row.get("overlay")),
                str(row.get("staged_overlay")),
            }:
                base_pkg = row.get("base_pkg")
                if base_pkg:
                    return Path(base_pkg)
        # The nearest overlay_table.json should describe this run.  If it does
        # not mention the overlay, keep looking only for a direct sidecar.
        break
    return None


class PlacementProjectionFailure(RuntimeError):
    """Composer screen points that cannot create valid placeholders."""

    def __init__(self, failures: list[dict]):
        self.failures = failures
        super().__init__(
            "placement projection failed for "
            + ", ".join(
                f"{row.get('edit_id')}[{row.get('instance_index')}]"
                for row in failures
            )
        )

# Normalized render coordinates: xmin, ymin, xmax, ymax (bottom-left origin).
SCREEN_REGIONS = {
    "auto": (0.15, 0.20, 0.85, 0.65),
    "left": (0.18, 0.22, 0.45, 0.65),
    "center": (0.35, 0.22, 0.65, 0.65),
    "right": (0.55, 0.22, 0.82, 0.65),
    "lower_left": (0.18, 0.20, 0.45, 0.43),
    "lower_center": (0.35, 0.20, 0.65, 0.43),
    "lower_right": (0.55, 0.20, 0.82, 0.43),
    "upper_left": (0.18, 0.50, 0.45, 0.75),
    "upper_center": (0.35, 0.50, 0.65, 0.75),
    "upper_right": (0.55, 0.50, 0.82, 0.75),
    "full_frame": (0.12, 0.15, 0.88, 0.80),
}


def default_semantic_target(anchoring: str | None) -> str:
    if anchoring == "airborne":
        return "open air"
    if anchoring == "submerged":
        return "water volume"
    if anchoring == "afloat":
        return "open water surface"
    return "visible ground"


def _semantic_words(*values) -> set[str]:
    words = set()
    for value in values:
        text = str(value or "").casefold()
        token = []
        for char in text:
            if char.isalnum():
                token.append(char)
            elif token:
                words.add("".join(token))
                token = []
        if token:
            words.add("".join(token))
    return words


def uses_small_insect_proxy(*, factory: str | None = None,
                            code_from: str | None = None,
                            anchoring: str | None = None,
                            semantic_target: str | None = None) -> bool:
    """Return whether missing proxy metadata should use a small insect box."""
    words = _semantic_words(code_from, semantic_target)
    if words & SMALL_INSECT_TOKENS:
        return True
    return anchoring == "airborne" and str(factory or "") in SMALL_INSECT_FACTORIES


def inferred_proxy_dimensions(*, factory: str | None = None,
                              code_from: str | None = None,
                              anchoring: str | None = None,
                              semantic_target: str | None = None
                              ) -> list[float] | None:
    """Infer a narrow fallback only for well-known tiny asset classes."""
    if uses_small_insect_proxy(
            factory=factory, code_from=code_from, anchoring=anchoring,
            semantic_target=semantic_target):
        return list(SMALL_INSECT_PROXY_DIMENSIONS_M)
    return None


def inferred_proxy_dimension_source(*, factory: str | None = None,
                                    code_from: str | None = None,
                                    anchoring: str | None = None,
                                    semantic_target: str | None = None
                                    ) -> str | None:
    if inferred_proxy_dimensions(
            factory=factory, code_from=code_from, anchoring=anchoring,
            semantic_target=semantic_target) is not None:
        return "semantic_small_insect_fallback"
    return None


def inferred_placeholder_proxy_dimensions(*, factory: str | None = None
                                          ) -> list[float] | None:
    """Return a conservative fallback for known placeholder-heavy plants."""
    dimensions = PLACEHOLDER_PROXY_DIMENSIONS_M.get(str(factory or ""))
    if dimensions is None:
        return None
    return list(dimensions)


def inferred_placeholder_proxy_dimension_source(*, factory: str | None = None
                                                ) -> str | None:
    if inferred_placeholder_proxy_dimensions(factory=factory) is not None:
        return "semantic_placeholder_fallback"
    return None


def uses_dense_field_request(*, factory: str | None = None,
                             code_from: str | None = None,
                             semantic_target: str | None = None,
                             arrangement: str | None = None,
                             count: int | None = None) -> bool:
    """Return whether a plan should behave like a compact plant field."""
    if is_creature(factory, code_from):
        return False
    if str(arrangement or "") == "field":
        return True
    identity_words = _semantic_words(factory, code_from)
    target_words = _semantic_words(semantic_target)
    has_botanical_identity = (
        str(factory or "") in DENSE_FIELD_FACTORIES
        or bool(identity_words & DENSE_FIELD_OBJECT_TOKENS)
        or bool(target_words & DENSE_FIELD_TARGET_TOKENS)
    )
    if not has_botanical_identity:
        return False
    if count is None:
        return True
    try:
        return int(count) >= 8
    except (TypeError, ValueError):
        return True


def dense_field_proxy_dimensions(*, declared_dimensions=None,
                                 factory: str | None = None,
                                 code_from: str | None = None,
                                 semantic_target: str | None = None,
                                 arrangement: str | None = None,
                                 count: int | None = None
                                 ) -> list[float] | None:
    """Return a compact footprint cap for botanical field requests."""
    if not uses_dense_field_request(
            factory=factory, code_from=code_from,
            semantic_target=semantic_target, arrangement=arrangement,
            count=count):
        return normalize_proxy_dimensions(declared_dimensions)
    declared = normalize_proxy_dimensions(declared_dimensions)
    if declared is None:
        return list(DENSE_FIELD_PROXY_DIMENSIONS_M)
    return [
        min(float(declared[0]), float(DENSE_FIELD_PROXY_DIMENSIONS_M[0])),
        min(float(declared[1]), float(DENSE_FIELD_PROXY_DIMENSIONS_M[1])),
        float(declared[2]),
    ]


def dense_field_visual_fit_dimensions(*, actual_dimensions,
                                      factory: str | None = None,
                                      code_from: str | None = None,
                                      semantic_target: str | None = None,
                                      arrangement: str | None = None,
                                      count: int | None = None
                                      ) -> list[float] | None:
    """Return a denser visual fit target for botanical field requests."""
    if not uses_dense_field_request(
            factory=factory, code_from=code_from,
            semantic_target=semantic_target, arrangement=arrangement,
            count=count):
        return None
    actual = normalize_proxy_dimensions(actual_dimensions)
    if actual is None:
        return list(DENSE_FIELD_VISUAL_FIT_DIMENSIONS_M)
    return [
        max(float(actual[0]), float(DENSE_FIELD_VISUAL_FIT_DIMENSIONS_M[0])),
        max(float(actual[1]), float(DENSE_FIELD_VISUAL_FIT_DIMENSIONS_M[1])),
        max(float(actual[2]), float(DENSE_FIELD_VISUAL_FIT_DIMENSIONS_M[2])),
    ]


def dense_field_instance_count(*, count: int | None = None,
                               factory: str | None = None,
                               code_from: str | None = None,
                               semantic_target: str | None = None,
                               arrangement: str | None = None) -> int:
    """Return the minimum instance count for botanical dense-field requests."""
    try:
        requested = max(1, int(count or 0))
    except (TypeError, ValueError):
        requested = 1
    if not uses_dense_field_request(
            factory=factory, code_from=code_from,
            semantic_target=semantic_target, arrangement=arrangement,
            count=requested):
        return requested
    if is_creature(factory, code_from):
        return requested
    identity_words = _semantic_words(factory, code_from)
    target_words = _semantic_words(semantic_target)
    botanical = (
        str(factory or "") in DENSE_FIELD_FACTORIES
        or bool(identity_words & DENSE_FIELD_OBJECT_TOKENS)
        or bool(target_words & DENSE_FIELD_TARGET_TOKENS)
    )
    if not botanical:
        return requested
    return max(requested, DENSE_FIELD_INSTANCE_FLOOR)


def dense_field_visual_fit_count(*, population_target_count: int | None = None,
                                 manifest_population_targets: int | None = None,
                                 asset_count: int | None = None) -> int:
    """Return the best available dense-field count for visual fit scaling."""
    for candidate in (
            population_target_count, manifest_population_targets, asset_count):
        try:
            count = int(candidate or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 1


def indoor_proxy_dimensions(factory: str | None, declared_dimensions
                            ) -> list[float] | None:
    """Apply an indoor semantic minimum without shrinking Director metadata."""
    declared = normalize_proxy_dimensions(declared_dimensions)
    minimum = INDOOR_PROXY_DIMENSION_MINIMUMS_M.get(str(factory or ""))
    if minimum is None:
        return declared
    if declared is None:
        return list(minimum)
    return [max(float(value), float(floor))
            for value, floor in zip(declared, minimum)]


def indoor_visibility_scale_factor(
        projected_major_fraction,
        surface_types,
        *,
        target_fraction: float = INDOOR_VISIBILITY_TARGET_MAJOR_FRACTION,
        ) -> float:
    """Return a coarse indoor-only scale that makes a proxy camera-legible.

    ``projected_major_fraction`` is the larger normalized screen dimension of
    the provisional proxy at candidate support depths.  The returned scale is
    selected from a small stable ladder and capped by broad support category;
    this avoids turning a soap dispenser into a room-sized prop while still
    allowing bed/seat objects to become visually prominent.
    """
    selected = {
        str(value) for value in (surface_types or [])
        if str(value) in INDOOR_VISIBILITY_MAX_SCALE_BY_SURFACE
    }
    if not selected:
        return 1.0
    try:
        projected = float(projected_major_fraction)
        target = float(target_fraction)
    except (TypeError, ValueError):
        return 1.0
    if (not math.isfinite(projected) or projected <= 0.0
            or not math.isfinite(target) or target <= 0.0
            or projected >= target):
        return 1.0
    cap = max(INDOOR_VISIBILITY_MAX_SCALE_BY_SURFACE[value]
              for value in selected)
    needed = target / projected
    allowed = [step for step in INDOOR_VISIBILITY_SCALE_STEPS
               if step <= cap + 1e-9]
    return float(next((step for step in allowed if step >= needed), cap))


def indoor_visibility_proxy_dimensions(
        declared_dimensions,
        projected_major_fraction,
        surface_types,
        *,
        target_fraction: float = INDOOR_VISIBILITY_TARGET_MAJOR_FRACTION,
        ) -> tuple[list[float] | None, float]:
    """Uniformly enlarge one indoor proxy according to camera visibility."""
    declared = normalize_proxy_dimensions(declared_dimensions)
    if declared is None:
        return None, 1.0
    scale = indoor_visibility_scale_factor(
        projected_major_fraction,
        surface_types,
        target_fraction=target_fraction,
    )
    if scale <= 1.0:
        return declared, 1.0
    return [float(value) * scale for value in declared], scale


def harden_indoor_surface_policy(value) -> dict:
    """Require a locally planar footprint for every registered indoor support.

    Earlier indoor prompts intentionally allowed center-only support for small
    objects.  That made a sink rim or the front face of a shelf sufficient for
    a mechanical PASS.  This is intentionally a coarse category contract:
    rigid indoor supports allow 20 degrees and soft bed/seat surfaces allow
    45-degree local fabric triangles.  The geometric sampling itself
    lives in ``placement_surface_evaluator.py`` and is activated by these types.
    """
    policy = normalize_surface_policy(value)
    selected = set(policy["required_surface_types"])
    if not selected & INDOOR_PLANAR_SURFACE_TYPES:
        return policy
    policy["require_full_footprint_support"] = True
    slope_cap = (20.0 if selected & INDOOR_RIGID_PLANAR_SURFACE_TYPES
                 else 45.0)
    current = policy.get("max_slope_deg")
    policy["max_slope_deg"] = slope_cap
    return policy


def default_min_sep(*, factory: str | None = None,
                    code_from: str | None = None,
                    anchoring: str | None = None,
                    semantic_target: str | None = None,
                    arrangement: str | None = None,
                    count: int | None = None) -> float:
    if uses_small_insect_proxy(
            factory=factory, code_from=code_from, anchoring=anchoring,
            semantic_target=semantic_target):
        return SMALL_INSECT_MIN_SEP_M
    if uses_dense_field_request(
            factory=factory, code_from=code_from,
            semantic_target=semantic_target, arrangement=arrangement,
            count=count):
        try:
            n = int(count or 0)
        except (TypeError, ValueError):
            n = 0
        if n >= 20:
            return 0.30
        if n >= 8:
            return 0.45
        return 0.55
    return 2.5


def _bounds(value, region: str) -> list[float]:
    if value is None:
        return list(SCREEN_REGIONS[region])
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("screen_bounds must be [xmin,ymin,xmax,ymax]")
    xmin, ymin, xmax, ymax = map(float, value)
    if not (0.05 <= xmin < xmax <= 0.95 and 0.05 <= ymin < ymax <= 0.90):
        raise ValueError(f"unsafe screen_bounds: {[xmin, ymin, xmax, ymax]}")
    if xmax - xmin < 0.08 or ymax - ymin < 0.08:
        raise ValueError("screen_bounds is too small for a reviewable placement")
    return [xmin, ymin, xmax, ymax]


def extended_grid_factor() -> float:
    """BI_PLACEMENT_EXTENDED_GRID=k (batch opt-in): search a k x k frame-sized
    screen area centred on the frame, so crowded scenes can spill objects just
    off-screen instead of failing "no free cell". 1 (default) = the frame only."""
    import os
    try:
        return max(1.0, float(os.environ.get("BI_PLACEMENT_EXTENDED_GRID", "1") or 1))
    except ValueError:
        return 1.0


def extended_search_bounds() -> list[float] | None:
    """Screen bounds of the extended search area, or None when it is off."""
    factor = extended_grid_factor()
    if factor <= 1.0:
        return None
    half = factor / 2.0
    return [0.5 - half, 0.5 - half, 0.5 + half, 0.5 + half]


def _surface_names(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{field} must be a list of at most 20 mesh names")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 120:
            raise ValueError(f"{field} entries must be non-empty strings up to 120 chars")
        name = item.strip()
        if name.casefold() not in {x.casefold() for x in result}:
            result.append(name)
    return result


def normalize_proxy_dimensions(value) -> list[float] | None:
    """Validate one conservative pre-generation full-size proxy estimate.

    The dimensions are authored once by the semantic planner when no generated
    mesh exists yet.  Blender measures an existing placeholder when possible;
    this estimate is the deterministic fallback for a genuinely new factory.
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("proxy_dimensions_m must be [length,width,height]")
    dimensions = [float(item) for item in value]
    if any(not math.isfinite(item) or not 0.05 <= item <= 100.0
           for item in dimensions):
        raise ValueError(
            "proxy_dimensions_m entries must be finite metres within [0.05, 100]")
    return dimensions


def uniform_proxy_fit_scale(actual_dimensions, proxy_dimensions) -> float:
    """Return one isotropic scale that keeps real geometry inside its proxy.

    ``proxy_dimensions_m`` is a conservative collision envelope, not permission
    to stretch generated geometry independently on X/Y/Z. The smallest axis
    ratio preserves the asset's proportions while ensuring that no measured
    dimension outgrows the region-map footprint reviewed before generation.
    """
    actual = [float(item) for item in actual_dimensions]
    proxy = normalize_proxy_dimensions(proxy_dimensions)
    if len(actual) != 3 or any(
            not math.isfinite(item) or item <= 1e-6 for item in actual):
        raise ValueError("actual_dimensions must contain three positive finite values")
    if proxy is None:
        raise ValueError("proxy_dimensions must not be null")
    # ★以資產最長軸對齊 proxy 的同一軸★(2026-09-12 cliff：老鷹翼展 1.48m/腳垂下 1.32m 高，
    #   proxy 2.0×2.2×0.6 被 0.6m 的高度卡住，縮成 ×0.456 翼展只剩 0.67m，放到 9.7m 變一小點)。
    #   其他軸允許超出 proxy —— proxy 是擺放用的粗略包絡，不該把動物縮成三分之一。
    dominant = max(range(3), key=lambda axis: actual[axis])
    return proxy[dominant] / actual[dominant]


def front_axis_to_proxy_heading_quarter_turns(front_axis: str) -> int:
    """Map an asset's horizontal front axis onto placeholder-local ``+X``."""
    turns = {"+X": 0, "+Y": 3, "-X": 2, "-Y": 1}
    axis = str(front_axis).strip().upper()
    if axis not in turns:
        raise ValueError(f"unsupported horizontal front axis: {front_axis!r}")
    return turns[axis]


def best_horizontal_proxy_fit_quarter_turns(
        actual_dimensions, proxy_dimensions, front_axis: str) -> int:
    """Choose the horizontal orientation that maximizes isotropic box fill.

    A generated asset's front axis describes which way it faces; it does not
    prove that this axis is also the asset's widest/longest dimension.  Compare
    the semantic heading alignment with its orthogonal orientation and retain
    the one permitting the larger uniform scale.  Ties prefer the semantic
    heading so existing correctly-oriented assets remain unchanged.
    """
    actual = [float(item) for item in actual_dimensions]
    proxy = normalize_proxy_dimensions(proxy_dimensions)
    if len(actual) != 3 or proxy is None:
        raise ValueError("actual/proxy dimensions must contain three values")
    preferred = front_axis_to_proxy_heading_quarter_turns(front_axis) % 2
    candidates = [preferred, 1 - preferred]
    scored = []
    for turns in candidates:
        aligned = ([actual[1], actual[0], actual[2]]
                   if turns else list(actual))
        scored.append((uniform_proxy_fit_scale(aligned, proxy), turns))
    best_scale = max(score for score, _turns in scored)
    return next(turns for score, turns in scored
                if math.isclose(score, best_scale, rel_tol=1e-9, abs_tol=1e-12))


def uniform_proxy_fit_failures(actual_before, actual_after, proxy_dimensions,
                               factor: float, *, rel_tol: float = 0.02,
                               abs_tol: float = 0.002) -> list[str]:
    """Return violations proving that a proxy fit violates hard bounds.

    This is intentionally independent of Blender.  The placer must remeasure
    evaluated geometry after changing the transform; recording a scale factor is
    not proof that the generated hierarchy actually inherited it.  Rigged and
    library assets can report evaluated bounds that shrink more than the root
    transform predicts; that is acceptable for the placement contract as long as
    the realized geometry stays inside the reviewed proxy envelope.
    """
    before = [float(item) for item in actual_before]
    after = [float(item) for item in actual_after]
    proxy = normalize_proxy_dimensions(proxy_dimensions)
    scale = float(factor)
    if (len(before) != 3 or len(after) != 3 or proxy is None
            or not math.isfinite(scale) or scale <= 0.0):
        return ["proxy fit evidence is malformed"]
    failures = []
    dominant = max(range(3), key=lambda axis: before[axis])
    for axis, (source, measured, limit) in enumerate(zip(before, after, proxy)):
        expected_upper = source * scale
        tolerance = max(abs_tol, rel_tol * max(abs(expected_upper), abs(limit)))
        if measured > expected_upper + tolerance:
            failures.append(
                f"axis {axis}: measured after={measured:.6f}m, "
                f"exceeds expected upper-bound {expected_upper:.6f}m "
                f"from factor={scale:.6f}")
        # 只有主軸(資產最長軸)受 proxy 上限約束；其他軸允許超出(見 uniform_proxy_fit_scale)
        if axis == dominant and measured > limit + tolerance:
            failures.append(
                f"axis {axis}: measured after={measured:.6f}m exceeds "
                f"proxy={limit:.6f}m")
    return failures


def placeholder_template_choice(root_counts) -> dict:
    """Choose a non-empty existing placeholder collection when one exists.

    Scene scans can retain an empty ``placeholders:Factory(seed)`` collection.
    That is not a usable template source, but it is still the correct collection
    identity for Stage C.  Callers should create a neutral proxy in it instead
    of indexing an empty template list.
    """
    counts = [int(value) for value in root_counts]
    for index, count in enumerate(counts):
        if count > 0:
            return {"collection_index": index, "use_neutral_proxy": False,
                    "reason": "existing_templates"}
    return {
        "collection_index": 0 if counts else None,
        "use_neutral_proxy": True,
        "reason": "empty_existing_collection" if counts else "missing_collection",
    }


def normalize_surface_policy(value, *, require_full_default: bool = True) -> dict:
    """Validate an Agent-authored policy without guessing mesh semantics."""
    raw = value if isinstance(value, dict) else {}
    required_types = _surface_names(
        raw.get("required_surface_types"), "required_surface_types")
    forbidden_types = _surface_names(
        raw.get("forbidden_surface_types"), "forbidden_surface_types")
    import surface_registry as sr
    unknown_types = [x for x in required_types + forbidden_types
                     if x not in sr.SURFACE_TYPES]
    if unknown_types:
        raise ValueError(f"unknown semantic surface types: {unknown_types}")
    type_conflict = set(required_types) & set(forbidden_types)
    if type_conflict:
        raise ValueError(
            "required_surface_types and forbidden_surface_types must be disjoint")
    required = _surface_names(raw.get("required_surface_names"), "required_surface_names")
    forbidden = _surface_names(raw.get("forbidden_surface_names"), "forbidden_surface_names")
    conflict = {x.casefold() for x in required} & {x.casefold() for x in forbidden}
    if conflict:
        raise ValueError("required_surface_names and forbidden_surface_names must be disjoint")
    slope = raw.get("max_slope_deg")
    if slope is not None:
        slope = float(slope)
        if not math.isfinite(slope) or not 0.0 <= slope <= 90.0:
            raise ValueError("max_slope_deg must be null or within [0, 90]")
    full_support = raw.get("require_full_footprint_support", require_full_default)
    if not isinstance(full_support, bool):
        raise ValueError("require_full_footprint_support must be boolean")
    return {
        "required_surface_types": required_types,
        "forbidden_surface_types": forbidden_types,
        "required_surface_names": required,
        "forbidden_surface_names": forbidden,
        "max_slope_deg": slope,
        "require_full_footprint_support": full_support,
    }


def legacy_outdoor_ultra_loose_metadata(value) -> dict | None:
    """Return legacy ultra-loose metadata, if an old plan carried it."""
    if not isinstance(value, dict):
        return None
    metadata = value.get("outdoor_ultra_loose_relaxation")
    if not isinstance(metadata, dict):
        spec = value.get("placement_spec")
        if isinstance(spec, dict):
            metadata = spec.get("outdoor_ultra_loose_relaxation")
    if not isinstance(metadata, dict):
        return None
    if (metadata.get("mode") == "ultra_loose"
            or metadata.get("strict_rules_relaxed")
            or metadata.get("planning_proxy_dimensions_m") is not None):
        return metadata
    return None


def recover_legacy_outdoor_ultra_loose_spec(value) -> dict:
    """Restore hard placement fields that legacy outdoor ultra-loose rewrote."""
    raw = deepcopy(value if isinstance(value, dict) else {})
    metadata = legacy_outdoor_ultra_loose_metadata(raw)
    if metadata is None:
        return raw
    restored = []
    original_policy = metadata.get("original_surface_policy")
    if isinstance(original_policy, dict):
        raw["surface_policy"] = deepcopy(original_policy)
        restored.append("surface_policy")
    original_bounds = metadata.get("original_screen_bounds")
    if isinstance(original_bounds, (list, tuple)) and len(original_bounds) == 4:
        raw["screen_bounds"] = _bounds(original_bounds, str(
            raw.get("screen_region") or "auto"))
        restored.append("screen_bounds")
    original_dimensions = metadata.get("original_proxy_dimensions_m")
    if original_dimensions is not None:
        raw["proxy_dimensions_m"] = normalize_proxy_dimensions(original_dimensions)
        restored.append("proxy_dimensions_m")
    raw.pop("outdoor_ultra_loose_relaxation", None)
    raw["legacy_outdoor_ultra_loose_recovery"] = {
        "applied": True,
        "scope": "outdoor_composer_only",
        "reason": (
            "legacy ultra-loose placement restored to strict hard rules; "
            "only later broad-search may widen screen_bounds"),
        "restored_fields": restored,
    }
    return raw


def recover_legacy_outdoor_ultra_loose_edit(edit: dict) -> dict | None:
    """Mutate one add edit back from legacy ultra-loose to strict placement."""
    if not isinstance(edit, dict):
        return None
    metadata = legacy_outdoor_ultra_loose_metadata(edit)
    if metadata is None:
        return None
    edit["placement_spec"] = recover_legacy_outdoor_ultra_loose_spec(
        edit.get("placement_spec"))
    edit.pop("outdoor_ultra_loose_relaxation", None)
    original_min_sep = metadata.get("original_min_sep")
    if original_min_sep is not None:
        original_min_sep = float(original_min_sep)
        if math.isfinite(original_min_sep) and original_min_sep > 0.0:
            edit["min_sep"] = original_min_sep
            edit["placement_spec"].setdefault(
                "legacy_outdoor_ultra_loose_recovery", {}).setdefault(
                    "restored_fields", []).append("min_sep")
    recovery = deepcopy(
        edit["placement_spec"].get("legacy_outdoor_ultra_loose_recovery") or {})
    edit["legacy_outdoor_ultra_loose_recovery"] = recovery
    return recovery


def surface_name_matches(actual: str | None, declared: str) -> bool:
    """Match an explicit Blender name, tolerating only Blender's .001 suffix."""
    if actual is None:
        return False
    actual_key = str(actual).strip().casefold()
    declared_key = str(declared).strip().casefold()
    return actual_key == declared_key or (
        actual_key.startswith(declared_key + ".")
        and actual_key[len(declared_key) + 1:].isdigit())


def validate_surface_policy_names(policy: dict, available_names) -> dict:
    """Reject Agent mesh names that the deterministic scene scan did not expose."""
    policy = normalize_surface_policy(policy)
    available = [str(x) for x in (available_names or [])]
    unknown = [name for field in ("required_surface_names", "forbidden_surface_names")
               for name in policy[field]
               if not any(surface_name_matches(actual, name) for actual in available)]
    if unknown:
        raise ValueError(
            f"surface policy names are absent from raycast_surfaces: {unknown}; "
            f"available={available}")
    return policy


def validate_surface_policy_types(policy: dict, available_types) -> dict:
    """Reject semantic types not proven visible by deterministic scene scan."""
    policy = normalize_surface_policy(policy)
    available = {str(x) for x in (available_types or [])}
    unknown = [x for field in ("required_surface_types", "forbidden_surface_types")
               for x in policy[field] if x not in available]
    if unknown:
        raise ValueError(
            f"surface policy types are absent from current camera inventory: {unknown}; "
            f"available={sorted(available)}")
    return policy


def surface_policy_issue(policy: dict, *, surface_name: str | None,
                         slope_deg: float | None) -> str | None:
    """Return the first deterministic center-ray violation, if any."""
    policy = normalize_surface_policy(policy)
    required = policy["required_surface_names"]
    forbidden = policy["forbidden_surface_names"]
    if surface_name is None:
        return "raycast did not hit a visible surface"
    if required and not any(surface_name_matches(surface_name, x) for x in required):
        return f"surface {surface_name!r} is not in required_surface_names"
    if any(surface_name_matches(surface_name, x) for x in forbidden):
        return f"surface {surface_name!r} is forbidden"
    limit = policy["max_slope_deg"]
    if limit is not None and (slope_deg is None or float(slope_deg) > limit):
        return f"slope {slope_deg!r} exceeds max_slope_deg={limit}"
    return None


def surface_type_policy_issue(policy: dict, surface_types) -> str | None:
    """Return a semantic surface-type violation for one resolved mesh hit.

    ``surface_policy_issue`` intentionally handles only explicit Blender mesh
    names and slope.  Indoor maps additionally resolve each hit through the
    deterministic scene registry and use this helper for both the center ray
    and every footprint sample.  Keeping the comparison pure makes it easy to
    verify without importing Blender.
    """
    policy = normalize_surface_policy(policy)
    actual = {str(value) for value in (surface_types or [])}
    required = set(policy["required_surface_types"])
    forbidden = set(policy["forbidden_surface_types"])
    if required and not actual & required:
        return (
            f"surface types {sorted(actual)!r} do not satisfy "
            f"required_surface_types={sorted(required)!r}")
    blocked = actual & forbidden
    if blocked:
        return f"surface types {sorted(blocked)!r} are forbidden"
    return None


def normalize_placement_spec(value, *, count: int, anchoring: str | None,
                             factory: str | None = None,
                             code_from: str | None = None) -> dict:
    """Validate/default one add-placement contract without scene guessing."""
    count = int(count)
    if count < 1:
        raise ValueError("count must be >= 1")
    raw = recover_legacy_outdoor_ultra_loose_spec(value)
    raw_arrangement = raw.get("arrangement")
    arrangement = str(raw_arrangement or
                      ("single" if int(count) == 1 else "scatter"))
    semantic_target = str(
        raw.get("semantic_target") or default_semantic_target(anchoring)).strip()
    broad_search = raw.get("outdoor_broad_search_relaxation")
    allow_field_full_frame = bool(
        isinstance(broad_search, dict) and broad_search.get("applied"))
    if (arrangement == "scatter"
            and uses_dense_field_request(
                factory=factory, code_from=code_from,
                semantic_target=semantic_target,
                arrangement=arrangement, count=count)):
        arrangement = "field"
    if arrangement not in ARRANGEMENTS:
        raise ValueError(f"invalid arrangement {arrangement!r}; use {sorted(ARRANGEMENTS)}")
    if arrangement == "single" and count != 1:
        raise ValueError("arrangement='single' requires count=1")
    if arrangement in {"cluster", "field", "school"} and count == 1:
        raise ValueError(f"arrangement={arrangement!r} requires count >= 2")
    if arrangement == "school" and anchoring != "submerged":
        raise ValueError("arrangement='school' requires anchoring='submerged'")

    default_region = (
        "upper_center" if anchoring == "airborne"
        else "center" if anchoring == "submerged"
        else "lower_center" if anchoring == "afloat"
        else "lower_center" if arrangement == "field"
        else "auto"
    )

    def field_region(value):
        if (arrangement == "field"
                and value in (None, "", "auto", "full_frame")
                and not allow_field_full_frame):
            return "lower_center"
        return str(value or default_region)

    raw_region = raw.get("screen_region")
    region = field_region(raw_region)
    if region not in SCREEN_REGIONS:
        raise ValueError(f"invalid screen_region {region!r}; use {sorted(SCREEN_REGIONS)}")
    axis = str(raw.get("row_axis") or "horizontal")
    if axis not in ROW_AXES:
        raise ValueError(f"invalid row_axis {axis!r}; use {sorted(ROW_AXES)}")
    orientation = str(raw.get("orientation") or
                      ("parallel" if arrangement == "row" else "random"))
    if orientation not in ORIENTATIONS:
        raise ValueError(
            f"invalid orientation {orientation!r}; use {sorted(ORIENTATIONS)}")
    placement_mode = str(raw.get("placement_mode") or
                         ("row" if arrangement == "row" else "center"))
    if placement_mode not in PLACEMENT_MODES:
        raise ValueError(
            f"invalid placement_mode {placement_mode!r}; use {sorted(PLACEMENT_MODES)}")

    if not semantic_target or len(semantic_target) > 120:
        raise ValueError("semantic_target must be 1..120 characters")

    if placement_mode in {"back_to_wall", "wall_mount"}:
        if "screen_region" not in raw:
            region = "upper_center"
        else:
            region = field_region(raw_region)
    else:
        region = field_region(raw_region)
    if region not in SCREEN_REGIONS:
        raise ValueError(f"invalid screen_region {region!r}; use {sorted(SCREEN_REGIONS)}")

    screen_bounds_value = raw.get("screen_bounds")
    if (arrangement == "field"
            and raw_region in (None, "", "auto", "full_frame")
            and not allow_field_full_frame):
        screen_bounds_value = None

    result = {
        "arrangement": arrangement,
        "semantic_target": semantic_target,
        "screen_region": region,
        "screen_bounds": _bounds(screen_bounds_value, region),
        "row_axis": axis,
        "orientation": orientation,
        "placement_mode": placement_mode,
        "proxy_dimensions_m": dense_field_proxy_dimensions(
            declared_dimensions=raw.get("proxy_dimensions_m"),
            factory=factory, code_from=code_from,
            semantic_target=semantic_target, arrangement=arrangement,
            count=count),
        "surface_policy": normalize_surface_policy(
            raw.get("surface_policy"),
            require_full_default=anchoring not in {"airborne", "submerged"}),
    }
    ultra_loose = raw.get("outdoor_ultra_loose_relaxation")
    if ultra_loose is not None:
        if not isinstance(ultra_loose, dict):
            raise ValueError(
                "outdoor_ultra_loose_relaxation must be an object when present")
        result["outdoor_ultra_loose_relaxation"] = deepcopy(ultra_loose)
    broad_search = raw.get("outdoor_broad_search_relaxation")
    if broad_search is not None:
        if not isinstance(broad_search, dict):
            raise ValueError(
                "outdoor_broad_search_relaxation must be an object when present")
        result["outdoor_broad_search_relaxation"] = deepcopy(broad_search)
    legacy_recovery = raw.get("legacy_outdoor_ultra_loose_recovery")
    if legacy_recovery is not None:
        if not isinstance(legacy_recovery, dict):
            raise ValueError(
                "legacy_outdoor_ultra_loose_recovery must be an object when present")
        result["legacy_outdoor_ultra_loose_recovery"] = deepcopy(
            legacy_recovery)
    return result


def is_outdoor_ultra_loose_placement_spec(value) -> bool:
    """Legacy ultra-loose never relaxes outdoor Composer hard rules."""
    return False


def initial_screen_positions(spec: dict, count: int) -> list[list[float]] | None:
    """Generate a deterministic first draft in screen space.

    This is initialization, not geometric auto-repair.  In particular it does
    not classify ice/water/rock or try to move a rejected object.  The executor
    projects these points onto the first visible surface; the visual critic then
    owns semantic acceptance and discrete instance-level candidate selection.
    """
    count = int(count)
    if count < 1:
        raise ValueError("count must be >= 1")
    arrangement = spec["arrangement"]
    xmin, ymin, xmax, ymax = map(float, spec["screen_bounds"])
    if arrangement == "single":
        return [[(xmin + xmax) / 2.0, (ymin + ymax) / 2.0]]
    if arrangement in {"scatter", "cluster", "field", "school"}:
        def radical_inverse(index: int, base: int) -> float:
            value, denominator = 0.0, 1.0
            while index:
                index, remainder = divmod(index, base)
                denominator *= base
                value += remainder / denominator
            return value

        # Low-discrepancy centers are stable across reruns.  Scatter spans most
        # of the bounds; field/cluster/school starts compactly around the
        # composition centre until the machine feasibility map owns the cells.
        scale = 0.84 if arrangement == "scatter" else 0.28 if arrangement == "field" else 0.32
        offset = (1.0 - scale) / 2.0
        return [
            [
                xmin + (offset + scale * radical_inverse(index + 1, 2)) * (xmax - xmin),
                ymin + (offset + scale * radical_inverse(index + 1, 3)) * (ymax - ymin),
            ]
            for index in range(count)
        ]

    # Leave 10% padding at both ends so a box has room around its center.
    x0, x1 = xmin + 0.10 * (xmax - xmin), xmax - 0.10 * (xmax - xmin)
    y0, y1 = ymin + 0.10 * (ymax - ymin), ymax - 0.10 * (ymax - ymin)
    values = [0.5] if count == 1 else [index / (count - 1) for index in range(count)]
    if spec["row_axis"] == "vertical":
        x = (xmin + xmax) / 2.0
        return [[x, y0 + t * (y1 - y0)] for t in values]
    y = (ymin + ymax) / 2.0
    return [[x0 + t * (x1 - x0), y] for t in values]


def normalize_screen_positions(value, *, spec: dict, count: int) -> list[list[float]]:
    """Validate Agent-proposed centers against this edit's declared search area."""
    if not isinstance(value, list) or len(value) != int(count):
        raise ValueError(
            f"screen_positions must contain exactly {int(count)} points")
    # With the extended grid, region selection may legitimately choose off-frame cells.
    xmin, ymin, xmax, ymax = map(float, extended_search_bounds() or spec["screen_bounds"])
    result = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"screen_positions[{index}] must be [x,y]")
        x, y = map(float, point)
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            # ★夾回範圍內而不是炸掉★(2026-10-20 寬鬆政策：1019 重跑 03/07 整個 case 死在這一行)
            cx, cy = min(max(x, xmin), xmax), min(max(y, ymin), ymax)
            print(f"[contract] ⚠️ screen_positions[{index}]={[round(x, 4), round(y, 4)]} outside "
                  f"screen_bounds={[xmin, ymin, xmax, ymax]} → clamped to {[round(cx, 4), round(cy, 4)]}",
                  flush=True)
            x, y = cx, cy
        result.append([x, y])
    return result


def apply_instance_screen_updates(current, updates, *, spec: dict,
                                  count: int) -> list[list[float]]:
    """Replace only explicitly targeted instances in a stable add batch.

    The visual critic used to replace the entire ``screen_positions`` array even
    when only one instance was wrong.  That made an already-correct object move
    again on every review round.  Instance updates preserve all untouched points
    exactly and still pass through the same bounds/count validation as a full
    replacement.
    """
    points = normalize_screen_positions(current, spec=spec, count=count)
    if not isinstance(updates, list) or not updates:
        raise ValueError("instance_positions must be a non-empty list")
    seen = set()
    for row in updates:
        if not isinstance(row, dict):
            raise ValueError("each instance_positions item must be an object")
        index = row.get("instance_index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("instance_index must be an integer")
        if not (0 <= index < int(count)):
            raise ValueError(
                f"instance_index {index} is outside 0..{int(count) - 1}")
        if index in seen:
            raise ValueError(f"duplicate instance_index {index}")
        seen.add(index)
        candidate = list(points)
        candidate[index] = row.get("screen_position")
        points = normalize_screen_positions(candidate, spec=spec, count=count)
    return points


def minimum_center_distance(points: list[list[float]]) -> float | None:
    """Small pure helper used by validation/tests; not an auto-repair policy."""
    if len(points) < 2:
        return None
    return min(
        math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))
        for index, a in enumerate(points)
        for b in points[index + 1:]
    )


def parallel_row_yaw(points) -> float | None:
    """Yaw that aligns local +X with the first-to-last row direction in world XY."""
    if len(points) < 2:
        return None
    dx = float(points[-1][0]) - float(points[0][0])
    dy = float(points[-1][1]) - float(points[0][1])
    if math.hypot(dx, dy) <= 1e-8:
        return None
    return math.atan2(dy, dx)

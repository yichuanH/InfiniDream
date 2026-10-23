"""Official placeholder scatter for plants and rocks, restricted to the camera's surroundings.

generate_nature places trees / bushes / boulders / cacti / kelp with
``placement.scatter_placeholders_mesh``: Poisson points over the terrain at a per-factory
density and minimum spacing. An ``add`` of such an asset used to go through the hero grid
(one collision-free screen cell per instance), which capped it at 1-3 copies and was the
largest Step-8 "no free cell" failure. This route keeps the official density and spacing and
only adds a near-camera window (distance + slightly widened frustum).

The Director picks the look and a density LEVEL only — never numbers.
"""

from __future__ import annotations

import re

# normal-level parameters, taken from generate_nature.py (single species, no noise mask):
#   density      Poisson "Density Max" per m^2
#   distance_min official minimum spacing (m)
#   altitude     official placeholder altitude offset (m)
#   domain       land | underwater | any
#   max_dist     near-camera window (m); beyond it an instance is a few pixels
#   cap          most placeholders this route may create (populate cost)
#   clearance    minimum distance to any existing placeholder (m)
#   max_slope    steepest ground accepted (deg)
GROUPS = {
    "tree":        dict(density=0.08, distance_min=2.0, altitude=-0.1, domain="land",
                        max_dist=45.0, cap=25, clearance=2.0, max_slope=35.0),
    "bush":        dict(density=0.075, distance_min=0.8, altitude=-0.05, domain="land",
                        max_dist=30.0, cap=40, clearance=1.0, max_slope=40.0),
    "small_plant": dict(density=0.15, distance_min=0.5, altitude=-0.02, domain="land",
                        max_dist=25.0, cap=40, clearance=0.5, max_slope=40.0),
    "cactus":      dict(density=0.04, distance_min=1.0, altitude=-0.05, domain="land",
                        max_dist=35.0, cap=30, clearance=1.0, max_slope=35.0),
    "boulder":     dict(density=0.035, distance_min=1.5, altitude=-0.25, domain="any",
                        max_dist=35.0, cap=30, clearance=1.5, max_slope=60.0),
    "glowing_rock": dict(density=0.05, distance_min=1.0, altitude=0.0, domain="any",
                         max_dist=30.0, cap=20, clearance=1.0, max_slope=90.0),
    "kelp":        dict(density=0.5, distance_min=3.0, altitude=-0.05, domain="underwater",
                        max_dist=30.0, cap=40, clearance=1.0, max_slope=60.0),
}

# ★每個類別的參考寬度(m)★(2026-09-12)：replace / 官方撒點的資產生出來比這個寬 k 倍，
#   placer 只填 1/k² 的目標(desert 12 株 5.6m 寬的巨柱仙人掌擠在 35m 內 = 一面牆)。
REFERENCE_WIDTH_M = {
    "tree": 8.0, "bush": 1.5, "small_plant": 0.5, "cactus": 1.5,
    "boulder": 1.5, "glowing_rock": 1.0, "kelp": 3.0,
}


def reference_width_for(factory: str | None) -> float | None:
    """Typical footprint width of this factory's scatter group (None = no thinning)."""
    group = group_of(factory)
    return REFERENCE_WIDTH_M.get(group) if group else None


# first match wins: Kelp before Monocot, GlowingRocks before Rock, Fallen trees excluded
_PATTERNS = (
    (r"Kelp", "kelp"),
    (r"GlowingRocks", "glowing_rock"),
    (r"Fallen", None),
    (r"BoulderPile", None),            # walks placeholder children; not a scatter asset
    (r"(Tree|Palm)", "tree"),
    (r"Bush", "bush"),
    (r"(Boulder|BlenderRock)", "boulder"),
    (r"Cactus", "cactus"),
    (r"(Succulent|Monocot)", "small_plant"),
)

# three semantic levels only; very_dense (the ground_scatter vocabulary) maps to dense
LEVELS = {"sparse": 0.5, "normal": 1.0, "dense": 2.0}
DEFAULT_LEVEL = "normal"
MIN_IN_FRAME = 3


def group_of(factory: str | None) -> str | None:
    """Factory class name -> parameter group, or None when this route does not apply."""
    name = str(factory or "")
    for pattern, group in _PATTERNS:
        if re.search(pattern, name):
            return group
    return None


def level_of(value) -> str:
    value = str(value or "").strip().lower()
    if value == "very_dense":
        return "dense"
    return value if value in LEVELS else DEFAULT_LEVEL


def params_for(factory: str | None, level=None) -> dict:
    group = group_of(factory)
    if group is None:
        raise ValueError(f"{factory!r} has no official scatter route")
    params = dict(GROUPS[group], group=group, level=level_of(level))
    params["density"] *= LEVELS[params["level"]]
    return params


# fish_school add: fish per school scales with the official volume ("uniform", 3, 10)
FISH_VOLUME = {"sparse": 0.5, "normal": 1.0, "dense": 2.0}

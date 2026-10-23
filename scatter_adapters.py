"""Per-system adapters for Infinigen's generate_nature scatter stages.

The director chooses semantics (system, surface, density level).  This module
owns the low-level call count, mask arguments, target tag, and apply kwargs so a
compound official stage cannot silently collapse into one generic call.
"""
from __future__ import annotations

from copy import deepcopy

import scatter_catalog as sc


def _generic_pass(system: str, domain_tag: str, level: str) -> dict:
    params = sc.official_mask_params(system)
    mask = {"tag": domain_tag}
    if params["mask_scale"] is not None:
        mask["scale"] = params["mask_scale"]
    threshold = sc.select_thresh_for(system, level)
    if threshold is not None:
        mask["select_thresh"] = threshold
    if params["normal_thresh"] is not None:
        mask["normal_thresh"] = params["normal_thresh"]
    if params["return_scalar"]:
        mask["return_scalar"] = True
    kwargs = {}
    density = sc.density_for(system, level)
    if density is not None:
        kwargs["density"] = density
    return {
        "name": "official",
        "call": "class",
        "mask": mask,
        "kwargs": kwargs,
        "source": "generate_nature.py",
    }


def passes_for(system: str, domain_tag: str, level: str) -> list[dict]:
    """Return the deterministic adapter contract for one semantic system."""
    system = str(system or "").strip().lower()
    if system not in sc.SCATTERS:
        raise KeyError(system)
    level = level if level in sc.DENSITY_LEVELS else sc.DEFAULT_DENSITY_LEVEL

    if system == "coral_reef":
        density = sc.density_for(system, level)
        return [
            {
                "name": "vertical",
                "call": "class",
                "mask": {
                    "scale": 0.15,
                    "select_thresh": sc.select_thresh_for(system, level),
                },
                # Official forwards the habitat tag to CoralReef.apply; the
                # upstream implementation accepts it through **kwargs.
                "kwargs": {"tag": domain_tag, "density": density},
                "source": "generate_nature.py:add_corals",
            },
            {
                "name": "horizontal",
                "call": "class",
                "mask": {
                    "scale": 0.15,
                    "normal_thresh": -0.4,
                    "normal_thresh_high": 0.4,
                },
                "kwargs": {
                    "n": 5,
                    "horizontal": True,
                    "tag": domain_tag,
                    "density": density,
                },
                "source": "generate_nature.py:add_corals",
            },
        ]

    if system == "monocots":
        return [
            {
                "name": "grass_form",
                "call": "class",
                "mask": {
                    "normal_dir": (0, 0, 1),
                    "scale": 0.2,
                    "tag": domain_tag,
                },
                "kwargs": {"grass": True},
                "source": "generate_nature.py:add_monocots",
            },
            {
                "name": "broadleaf_form",
                "call": "module",
                "mask": {
                    "normal_dir": (0, 0, 1),
                    "scale": 0.2,
                    "select_thresh": sc.select_thresh_for(system, level),
                    # generate_nature uses params.get("grass_habitats", None).
                    "tag": None,
                },
                "kwargs": {"grass": False},
                "source": "generate_nature.py:add_monocots",
            },
        ]

    result = _generic_pass(system, domain_tag, level)
    if system in {"ground_twigs", "chopped_trees"}:
        result["mask"]["tag"] = sc.domain_tag("nonliving")
    elif system == "seashells":
        result["mask"]["tag"] = "landscape,"
    return [result]


def describe(system: str) -> dict:
    """Static adapter metadata used by tests and reports."""
    entry = sc.get(system)
    if not entry:
        raise KeyError(system)
    compound = system in {"coral_reef", "monocots"}
    return {
        "system": system,
        "adapter": (
            "experimental_module_default"
            if entry.get("official_unused")
            else "official_generate_nature"
        ),
        "compound": compound,
        "pass_count": 2 if compound else 1,
    }


def execute(module, class_name: str, base_obj, pass_specs, build_selection, *,
            season=None):
    """Run adapter passes and return one result record per official call."""
    records = []
    for pass_spec in pass_specs:
        spec = deepcopy(pass_spec)
        selection = build_selection(spec["mask"], spec["name"])
        kwargs = dict(spec.get("kwargs") or {})
        kwargs["selection"] = selection
        if class_name == "GroundLeaves" and season is not None:
            kwargs["season"] = season
        if spec["call"] == "module":
            result = module.apply(base_obj, **kwargs)
        else:
            result = getattr(module, class_name)().apply(base_obj, **kwargs)
        records.append({
            "name": spec["name"],
            "call": spec["call"],
            "mask": spec["mask"],
            "kwargs": {
                key: value for key, value in kwargs.items()
                if key != "selection"
            },
            "source": spec["source"],
            "result": result,
        })
    return records

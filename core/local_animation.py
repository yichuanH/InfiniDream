"""Catalog-gated local Factory animation inheritance.

Local animation deforms an asset in place.  It is intentionally independent
from :mod:`world_motion`, which moves the populated placeholder/root through
the scene.  Agents do not author constructor kwargs: this module derives them
only from modes explicitly approved in ``director/animation_catalog.json``.
"""

from __future__ import annotations

import json
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent.parent / "director" / "animation_catalog.json"
POLICIES = {"none", "inherit", "gn_scene_time"}


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def inherited_request(factory: str | None) -> dict:
    """Return the single catalog-approved local mode, otherwise ``none``.

    Ambiguous factories deliberately do not animate.  This keeps the automatic
    policy deterministic and prevents a newly-added catalog mode from silently
    changing existing renders.
    """
    entry = load_catalog().get("factories", {}).get(str(factory), {})
    selectable = list(entry.get("director_selectable_modes") or [])
    approved = []
    for mode in selectable:
        detail = (entry.get("modes") or {}).get(mode) or {}
        if (detail.get("status") == "user_verified_success"
                and detail.get("director_enabled", True) is not False
                and isinstance(detail.get("constructor_kwargs"), dict)):
            approved.append(str(mode))
    if len(approved) != 1:
        return {"policy": "none", "mode": None}
    return {"policy": "inherit", "mode": approved[0]}


def gn_scene_time_entry(factory: str | None) -> dict | None:
    """The catalog-approved Scene-Time wing/geometry driver for this Factory.

    A second, independent kind of local animation (see ``local_animation_kinds``):
    the Factory has no ``animation_mode`` and no armature, but its geometry nodes
    already animate from a Scene Time node.  Generated variants often ship with the
    amplitude zeroed, so placer normalises the instantiated sockets after populate.
    """
    entry = load_catalog().get("factories", {}).get(str(factory), {})
    detail = entry.get("gn_scene_time")
    if not isinstance(detail, dict):
        return None
    if (detail.get("status") != "user_verified_success"
            or detail.get("director_enabled", True) is False):
        return None
    return dict(detail)


def available_request(factory: str | None) -> dict:
    """The one animation this Factory can do in place, or ``none``."""
    inherited = inherited_request(factory)
    if inherited["policy"] != "none":
        return inherited
    if gn_scene_time_entry(factory) is not None:
        return {"policy": "gn_scene_time", "mode": None}
    return {"policy": "none", "mode": None}


def director_request(factory: str | None, *, enabled: bool) -> dict:
    """Director decides whether this asset animates in place; the catalog decides how."""
    return available_request(factory) if enabled else {"policy": "none", "mode": None}


def gn_scene_time_params(factory: str | None) -> dict:
    """Normalised frequency/amplitude for the executor."""
    detail = gn_scene_time_entry(factory)
    if detail is None:
        raise ValueError(f"{factory!r} has no approved gn_scene_time driver")
    return {"frequency_hz": float(detail["frequency_hz"]),
            "flap_magnitude": float(detail["flap_magnitude"])}


def normalize_request(value, *, factory: str | None) -> dict:
    """Validate a persisted request against the current capability catalog."""
    if value is None:
        return inherited_request(factory)
    if not isinstance(value, dict):
        raise ValueError("local_animation must be an object or null")
    policy = str(value.get("policy") or "none")
    if policy not in POLICIES:
        raise ValueError(f"local_animation policy must be one of {sorted(POLICIES)}")
    if policy == "none":
        if value.get("mode") not in (None, ""):
            raise ValueError("local_animation policy=none requires mode=null")
        return {"policy": "none", "mode": None}
    if policy == "gn_scene_time":
        if value.get("mode") not in (None, ""):
            raise ValueError("local_animation policy=gn_scene_time requires mode=null")
        if gn_scene_time_entry(factory) is None:
            raise ValueError(
                f"{factory!r} has no user-verified Scene-Time animation driver")
        return {"policy": "gn_scene_time", "mode": None}
    inherited = inherited_request(factory)
    if inherited["policy"] != "inherit":
        raise ValueError(f"{factory!r} has no single user-verified inherited animation")
    mode = value.get("mode") or inherited["mode"]
    if mode != inherited["mode"]:
        raise ValueError(
            f"{factory!r} may inherit only {inherited['mode']!r}, received {mode!r}")
    return inherited


def constructor_kwargs(request, *, factory: str | None) -> dict:
    """Resolve trusted Factory kwargs without accepting Agent-authored kwargs."""
    canonical = normalize_request(request, factory=factory)
    if canonical["policy"] in ("none", "gn_scene_time"):
        # gn_scene_time needs no constructor kwarg: placer edits the instantiated
        # node sockets after populate instead.
        return {}
    detail = (load_catalog()["factories"][str(factory)]["modes"]
              [canonical["mode"]])
    return dict(detail["constructor_kwargs"])

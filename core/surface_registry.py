"""Deterministic semantic surface registry; no model calls or visual guessing."""

from __future__ import annotations

import json
import re
from pathlib import Path


PATH = Path(__file__).with_name("surface_registry.json")
DATA = json.loads(PATH.read_text(encoding="utf-8"))
SURFACE_TYPES = tuple(DATA["vocabulary"])

SCENE_ALIASES = {
    "Bedroom": "bedroom",
    "LivingRoom": "living-room",
    "Kitchen": "kitchen",
    "Bathroom": "bathroom",
    "DiningRoom": "dining-room",
    "snow_mountain": "snowy_mountain",
}


def canonical_scene_name(scene_name: str) -> str:
    value = str(scene_name or "")
    return SCENE_ALIASES.get(value, value)


def scene_rules(scene_name: str) -> dict[str, list[str]]:
    return dict(DATA.get("scenes", {}).get(canonical_scene_name(scene_name), {}))


def types_for_mesh(scene_name: str, mesh_name: str) -> list[str]:
    return [surface_type for surface_type, patterns in scene_rules(scene_name).items()
            if any(re.fullmatch(pattern, str(mesh_name), re.IGNORECASE)
                   for pattern in patterns)]


def resolve_available(scene_name: str, visible_mesh_names) -> list[dict]:
    names = sorted({str(name) for name in visible_mesh_names}, key=str.casefold)
    result = []
    for surface_type in SURFACE_TYPES:
        matched = [name for name in names
                   if surface_type in types_for_mesh(scene_name, name)]
        if matched:
            result.append({
                "type": surface_type,
                "mesh_names": matched,
                "evidence": "deterministic_scene_registry+camera_raycast",
            })
    return result


def mesh_type_map(available_surface_types) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in available_surface_types or []:
        surface_type = str(entry.get("type", ""))
        if surface_type not in SURFACE_TYPES:
            continue
        for name in entry.get("mesh_names", []):
            result.setdefault(str(name), []).append(surface_type)
    return result

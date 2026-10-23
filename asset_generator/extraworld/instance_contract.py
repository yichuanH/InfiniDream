"""Pure-data contract between scene planning and one Factory spawn.

Scene-level prompts often mention a total count or an arrangement (for example,
"five seagulls" or "three jellyfish at different heights").  The generator,
however, authors the implementation of *one* ``create_asset`` call.  Counts and
arrangements are materialized later by Composer/placer placeholders.
"""

from __future__ import annotations


CONTRACT_VERSION = 1


def logical_instance_contract(scene_count: int | None) -> dict:
    """Return the machine-readable cardinality contract for one asset type."""
    count = 1 if scene_count is None else int(scene_count)
    if count < 1:
        raise ValueError("scene_count must be >= 1")
    return {
        "version": CONTRACT_VERSION,
        "logical_instances_per_spawn": 1,
        "scene_instance_count": count,
        "scene_count_owner": "composer_placer",
    }


def single_instance_prompt(prompt: str, scene_count: int | None) -> str:
    """Wrap a scene description with the non-multiplication rule.

    The original text is retained verbatim as appearance evidence.  The prefix
    is deliberately consumed by reference generation, coding agents, verifier,
    and selector so none of those stages can reinterpret a scene count as
    geometry that must be duplicated inside one Factory spawn.
    """
    source = str(prompt or "").strip()
    contract = logical_instance_contract(scene_count)
    count = contract["scene_instance_count"]
    return (
        "SINGLE LOGICAL INSTANCE CONTRACT\n"
        "Generate and judge exactly ONE logical object for each create_asset() "
        "call. The scene pipeline will call this Factory once per placeholder; "
        f"it owns the requested total count ({count}) and all group arrangement. "
        "Do not build a flock, school, cluster, row, or other repeated group "
        "inside create_asset(), even if the source description mentions a plural "
        "count or spatial arrangement. Use the source description only for the "
        "appearance of one member. A returned hierarchy may contain component "
        "parts of that one object, but not multiple logical copies.\n\n"
        f"SOURCE SCENE DESCRIPTION:\n{source}"
    )

#!/usr/bin/env python
"""Vision selector for one labeled batch of discrete placement candidates."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from planner.placement_critic import (
        MODEL, client, extract_json, normalize_decision_kind)
except ModuleNotFoundError:
    # Direct ``python planner/this_file.py`` puts planner/ first on sys.path,
    # where planner.py can shadow the namespace package.
    from placement_critic import (
        MODEL, client, extract_json, normalize_decision_kind)


SYSTEM = """You select a location for exactly one requested object instance in a 3D
scene. The image is a candidate contact sheet. Each candidate tile contains two labeled
views of the same exact box position: FINAL CAMERA uses one full-size opaque solid box,
and TOP VIEW uses a wireframe box from a shared orthographic camera. Different candidate
ids use different colors. It may also contain gray objects labeled FIXED. FIXED objects
are context and must never be moved. Use FINAL CAMERA for composition and visible bottom
contact context. Use TOP VIEW for the complete footprint, support-region boundaries,
spacing, and front/back ambiguity. The solid box intentionally does not reveal background
water or reflections through its body; do not infer support from scenery behind it. All
top views share one framing, so their distances and footprints are comparable.
Candidate tiles may summarize an entire support patch instead of a tiny cell.
Use support_patch_id, patch_kind, and placement_mode together: placement_mode
describes how the patch is being used (center, edge, back_to_wall, wall_mount, or row),
while support_patch_id identifies the plane-sized region itself.

Choose the single candidate that best satisfies semantic_target and looks physically
and compositionally reasonable. For example, a polar bear targeted at a visible ice
floe must have its full bottom footprint supported by solid visible ice, not water or an
ice/water boundary. Candidate ids map to exact executor-owned coordinates; never invent
coordinates. The payload may name the exact Blender mesh hit by the camera ray as
selected_visible_surface. Treat mesh names as opaque geometry identifiers, not semantic
classes. In particular, ``CollectiveTransparentTerrain`` is ambiguous and is evidence
of neither ice nor water. An explicit name ``liquid`` is water, but every other support
judgment must come from the visible full-size box bottom/footprint in both views and scene
landmarks.
The payload may also include semantic-grid metadata such as region_id, cell_id, and
support_patch_id, patch_kind, placement_mode, and surface_role. Treat floor_fallback as
the last resort after the earlier semantic choices have already failed or are clearly
worse.
Your reason must describe that visual support (for example, foreground snow shelf or
right-hand ice bridge), then quote selected_visible_surface only as corroborating
identity. Do not select a candidate merely because its mesh name sounds suitable. If no
candidate has visually unambiguous support in both views, or the two views conflict,
return none instead of guessing.
selected_surface_slope_deg is the measured angle from world-up:
0 is horizontal and 90 is a vertical wall. Interpret it for the requested object; the
executor imposes no semantic standability threshold. Do not infer validity merely from
visibility. Avoid candidates with machine_issues.

Return exactly one JSON object:
{"decision":"select","candidate_id":"B0-C3","reason":"..."}
or, if every candidate is unreasonable:
{"decision":"none","candidate_id":null,"reason":"..."}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--apply-selection", action="store_true")
    return parser.parse_args()


def resolve_candidate_id(value, payload: dict) -> str:
    """Match a returned id against the known ids, ignoring case and padding."""
    text = str(value or "").strip()
    known = [row["candidate_id"] for row in payload["candidates"]]
    for candidate_id in known:
        if text.lower() == candidate_id.lower():
            return candidate_id
    return text


def candidate_payload(metadata: dict, review: dict) -> dict:
    placements = review.get("placements") or []
    anchors = placements[0].get("anchors") or [] if placements else []
    centers = placements[0].get("screen_centers") or [] if placements else []
    surface_policy = (
        ((placements[0].get("placement_spec") or {}).get("surface_policy") or {})
        if placements else {})
    require_full_footprint = surface_policy.get(
        "require_full_footprint_support", True)
    by_index = {
        int(row.get("instance_index", index)): row
        for index, row in enumerate(centers)
    }
    audits = {index: audit for index, audit in enumerate(anchors)}
    fixed_temp_indices = set(range(len(metadata.get("fixed_instances") or [])))
    collision_names = set()
    object_by_temp = {
        index: audit.get("object") for index, audit in audits.items()
    }
    fixed_names = {object_by_temp.get(index) for index in fixed_temp_indices}
    for violation in review.get("violations") or []:
        objects = set(violation.get("objects") or [])
        if objects & fixed_names:
            collision_names |= objects - fixed_names

    rows = []
    for candidate in metadata.get("candidates") or []:
        temp_index = int(candidate["temporary_instance_index"])
        audit = audits.get(temp_index) or {}
        center = by_index.get(temp_index) or {}
        issues = []
        warnings = []
        if not audit.get("selected_visible_surface"):
            issues.append("camera ray hit no visible surface")
        if not audit.get("all_frames_visible"):
            issues.append("not visible for every checked frame")
        if not audit.get("all_frames_fully_visible"):
            if metadata.get("semantic_mode"):
                issues.append("complete placeholder box is not fully visible")
            else:
                warnings.append(
                    "raw centered proxy may be clipped; final bottom-anchored "
                    "preview must revalidate it")
        if audit.get("object") in collision_names:
            issues.append("3D world box collides with a fixed requested object")
        if audit.get("surface_policy_issue"):
            issues.append(str(audit["surface_policy_issue"]))
        if (require_full_footprint
                and not (audit.get("footprint_support") or {}).get(
                    "supported", True)):
            issues.append("full footprint is not supported")
        rows.append({
            "candidate_id": candidate["candidate_id"],
            "planned_screen_position": candidate["screen_position"],
            "actual_screen_center": center.get("xy_bottom_origin"),
            "selected_visible_surface": audit.get("selected_visible_surface"),
            "selected_surface_normal": audit.get("selected_surface_normal"),
            "selected_surface_slope_deg": audit.get(
                "selected_surface_slope_deg"),
            "region_id": candidate.get("region_id"),
            "cell_id": candidate.get("cell_id"),
            "semantic_cell_id": candidate.get("semantic_cell_id"),
            "support_patch_id": candidate.get("support_patch_id"),
            "patch_kind": candidate.get("patch_kind"),
            "patch_label": candidate.get("patch_label"),
            "placement_mode": candidate.get("placement_mode"),
            "surface_role": candidate.get("surface_role"),
            "hit_surface": candidate.get("hit_surface"),
            "surface_types": candidate.get("surface_types"),
            "plane_quality_score": candidate.get("plane_quality_score"),
            "clearance_steps": candidate.get("clearance_steps"),
            "machine_issues": issues,
            "machine_warnings": warnings,
        })
    return {
        "edit_id": metadata.get("edit_id"),
        "instance_index": metadata.get("instance_index"),
        "semantic_target": metadata.get("semantic_target"),
        "fixed_instances": metadata.get("fixed_instances"),
        "candidates": rows,
    }


def explicit_support_record(row: dict) -> dict:
    return {
        "surface": row.get("hit_surface"),
        "surface_normal": row.get("surface_normal"),
        "slope_deg": row.get("slope_deg"),
        "semantic_cell_id": row.get("semantic_cell_id"),
        "cell_id": row.get("cell_id"),
        "region_id": row.get("region_id"),
        "support_plane_id": row.get("support_plane_id"),
        "support_patch_id": row.get("support_patch_id"),
        "patch_kind": row.get("patch_kind"),
        "placement_mode": row.get("placement_mode"),
        "surface_types": row.get("surface_types"),
    }


def apply_candidate(plan: dict, metadata: dict, candidate_id: str) -> tuple[dict, dict]:
    from placement_contract import (
        apply_instance_screen_updates,
        normalize_placement_spec,
        normalize_surface_policy,
    )

    choices = {
        row["candidate_id"]: row for row in metadata.get("candidates") or []
    }
    if candidate_id not in choices:
        raise ValueError(f"unknown candidate_id {candidate_id!r}")
    revised = deepcopy(plan)
    edit_id = str(metadata["edit_id"])
    matches = [
        edit for edit in revised.get("hero_edits", [])
        if edit.get("op") == "add" and str(edit.get("code_from")) == edit_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one editable add {edit_id!r}, found {len(matches)}")
    edit = matches[0]
    count = int(edit.get("n", 0))
    spec = normalize_placement_spec(
        edit.get("placement_spec"), count=count, anchoring=edit.get("anchoring"))
    selected = choices[candidate_id]
    before = deepcopy(edit.get("screen_positions"))
    after = apply_instance_screen_updates(
        before,
        [{
            "instance_index": int(metadata["instance_index"]),
            "screen_position": selected["screen_position"],
        }],
        spec=spec,
        count=count,
    )
    edit["screen_positions"] = after
    edit["screen_position_projection"] = "visible_surface"
    if selected.get("placement_mode"):
        edit.setdefault("placement_spec", {})["placement_mode"] = str(
            selected["placement_mode"])
    selected_types = [
        str(value) for value in (selected.get("surface_types") or [])]
    selected_surface = selected.get("hit_surface")
    if selected_types:
        policy_source = (
            ((edit.get("surface_fallback") or {}).get("original_surface_policy"))
            or (edit.get("placement_spec") or {}).get("surface_policy")
            or {})
        policy = normalize_surface_policy(policy_source)
        policy["required_surface_types"] = selected_types
        policy["required_surface_names"] = (
            [str(selected_surface)] if selected_surface else [])
        edit.setdefault("placement_spec", {})["surface_policy"] = policy
    selected_world = (
        selected.get("candidate_world_position")
        or selected.get("world_position"))
    existing_positions = edit.get("positions")
    can_use_explicit_world = (
        selected_world is not None
        and (count == 1 or (
            isinstance(existing_positions, list)
            and len(existing_positions) == count))
    )
    if can_use_explicit_world:
        if isinstance(existing_positions, list) and len(existing_positions) == count:
            positions = deepcopy(existing_positions)
        else:
            positions = [[0.0, 0.0, 0.0] for _index in range(count)]
        positions[int(metadata["instance_index"])] = list(
            map(float, selected_world))
        edit["positions"] = positions
        supports = edit.get("explicit_position_supports")
        if not isinstance(supports, list) or len(supports) != count:
            supports = [{} for _index in range(count)]
        supports[int(metadata["instance_index"])] = explicit_support_record(
            selected)
        edit["explicit_position_supports"] = supports
    else:
        edit.pop("positions", None)
        edit.pop("explicit_position_supports", None)
    edit.pop("placement_preview_labels", None)
    return revised, {
        "edit_id": edit_id,
        "instance_index": int(metadata["instance_index"]),
        "candidate_id": candidate_id,
        "screen_position": selected["screen_position"],
        "world_position": (
            list(map(float, selected_world))
            if selected_world is not None else None),
        "surface_types": selected.get("surface_types"),
        "hit_surface": selected.get("hit_surface"),
        "screen_positions_before": before,
        "screen_positions_after": after,
        "untouched_instances": [
            index for index in range(count)
            if index != int(metadata["instance_index"])
        ],
    }


def sha256_json(value: dict) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    args = parse_args()
    metadata = json.loads(args.candidates.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    payload = candidate_payload(metadata, review)
    before_hash = sha256_json(plan)
    viable = {
        row["candidate_id"] for row in payload["candidates"]
        if not row["machine_issues"]
    }
    if not viable:
        result = {
            "contract_version": 1,
            "decision": "none",
            "candidate_id": None,
            "contract_notes": [
                "no candidate passed deterministic machine review"],
            "reason": "No candidate in this batch passed machine review.",
            "applied_selection": False,
            "applied": None,
            "plan_before_sha256": before_hash,
            "plan_after_sha256": before_hash,
            "candidate_payload": payload,
            "raw_response": "",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print("[candidate-critic] NONE: no candidate passed machine review")
        return 0
    image = base64.standard_b64encode(args.preview.read_bytes()).decode("ascii")
    response = client().messages.create(
        model=args.model,
        max_tokens=900,
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": image}},
            {"type": "text", "text": json.dumps(
                payload, ensure_ascii=False, indent=2)},
        ]}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    decision = extract_json(raw, what="candidate critic")
    kind = normalize_decision_kind(decision.get("decision"), {"select", "none"})
    applied = None
    revised = plan
    candidate_id = None
    contract_notes = []
    if kind == "select":
        candidate_id = resolve_candidate_id(decision.get("candidate_id"), payload)
        if candidate_id not in viable:
            contract_notes.append(
                f"selected candidate {candidate_id!r} has machine issues or is unknown")
            kind = "none"
            candidate_id = None
        else:
            revised, applied = apply_candidate(plan, metadata, candidate_id)
            if args.apply_selection:
                args.plan.write_text(
                    json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    elif decision.get("candidate_id") is not None:
        # A ``none`` verdict that still names its least-bad candidate is a
        # formatting slip, not a placement decision.  The reason text is what
        # the next round needs, so keep the rejection and drop the stray id
        # rather than killing the case.
        contract_notes.append(
            "none decision carried candidate_id "
            f"{decision.get('candidate_id')!r}; ignored per contract")
    result = {
        "contract_version": 1,
        "decision": kind,
        "candidate_id": candidate_id,
        "contract_notes": contract_notes,
        "reason": decision.get("reason"),
        "applied_selection": bool(args.apply_selection and kind == "select"),
        "applied": applied,
        "plan_before_sha256": before_hash,
        "plan_after_sha256": sha256_json(revised),
        "candidate_payload": payload,
        "raw_response": raw,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    for note in contract_notes:
        print(f"[candidate-critic] note: {note}")
    print(
        f"[candidate-critic] {kind.upper()}: "
        f"{candidate_id} — {decision.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Vision critic for the placement and controlled world-motion loop.

The critic never edits Blender data.  It may replace ``screen_positions`` and,
after a Step-9 diagnostic, bounded world-motion params on Composer ``add``
entries.  The pipeline then deterministically rebuilds every derived output
from the original baked coarse blend.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")


def env() -> dict:
    merged = {}
    for path in (ROOT / ".env", ROOT.parent / "ExtraWorld" / ".env",
                 ROOT.parent / "infinigen_o" / ".env"):
        if path.is_file():
            merged.update({key: value for key, value in dotenv_values(path).items() if value})
    for key in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


def client():
    import anthropic

    values = env()
    key = values.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found")
    base = (values.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


SYSTEM = """You are the visual placement proposer/critic for a 3D scene pipeline. You see a
render from the exact final active camera. Colored wireframe boxes are placeholders;
new requested subjects are highlighted. You also receive machine projections that map
each requested subject id and stable zero-based instance_index to its box in the image.

Machine violations (visibility and 3D world-box collision) are non-bypassable hard
constraints, but the machine must never choose the replacement location. Camera-space
projected overlap is not collision. A composition_warnings entry only reports severe
70%+ occlusion for your semantic/composition judgment and is never itself a hard gate. You choose a
new semantically reasonable location by editing Composer screen coordinates, then the
machine will rebuild and revalidate it. Never accept while a hard violation remains.

Judge composition and semantic support as well: each box must actually appear on/near
its declared semantic_target. For example, a polar bear intended for an ice floe must
stand on visible ice rather than merely being in-frame over water; houses targeted at a
river bank should form the requested row along that bank.

Any selected_visible_surface value is an opaque Blender mesh identifier, not a semantic
classifier. In particular, ``CollectiveTransparentTerrain`` is evidence of neither ice
nor water. Use the rendered full-size box bottom/footprint and visible scene landmarks
as the semantic source of truth. An explicit mesh name ``liquid`` may be treated as
water. When explaining support, cite the visible landmark beneath the box rather than
inferring it from an ambiguous mesh name.

When a STEP-9 MOTION CONTACT SHEET and runtime_motion_review are present, also judge the
start/middle/end composition. You may keep the start point and revise only these bounded
world_motion params to prevent the subject leaving its semantic support, leaving frame,
or colliding: heading_deg in [0,360), speed_scale in [0.25,1.5]. Prefer changing heading
or shortening travel before moving an otherwise good start point. Do not change the
preset, object identity, count, anchoring, generator, appearance, rig, or bone animation.

If the layout is already good, return:
{"decision":"accept","reason":"...","placements":[]}

If it needs revision, return screen coordinates in normalized image coordinates, where
x=0 is left, x=1 is right, y=0 is bottom, and y=1 is top:
{"decision":"revise","reason":"...","placements":[
  {"edit_id":"stable id from the input",
   "screen_bounds":[xmin,ymin,xmax,ymax],
   "instance_positions":[
      {"instance_index":0,"screen_position":[x,y]}
   ],
   "world_motion_params":{"heading_deg":0.0,"speed_scale":0.75}}
]}

Revise only the instances that are actually wrong. Never include a correct instance just
to repeat its existing position: omitted instances remain byte-for-byte unchanged. Omit
instance_positions when revising only motion. The legacy full-array screen_positions form
is accepted for compatibility but should not be emitted. Omit world_motion_params when
revising only the starting placement. world_motion_params is allowed only when a runtime
review exists. Every point must remain inside that edit's placement_spec.screen_bounds.
You may include screen_bounds when the current bounds prevent a semantically correct
correction; keep them inside x=[0.05,0.95], y=[0.05,0.90]. Omit screen_bounds otherwise.
Bounds are a permitted search region, not proof of semantic correctness. Return one JSON
object and nothing else."""


def sha256_json(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--top-preview", type=Path)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--runtime-review", type=Path)
    parser.add_argument("--runtime-preview", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--apply-revision", action="store_true")
    parser.add_argument(
        "--request-candidates", action="store_true",
        help="identify wrong instances but let a discrete candidate sheet move them")
    parser.add_argument(
        "--max-candidate-requests", type=int, default=3,
        help="maximum wrong instances routed to candidates in one review round")
    parser.add_argument(
        "--prior-candidate-decision", type=Path, action="append", default=[],
        help="previous NONE/selection result supplied as bounded feedback")
    return parser.parse_args()


CANDIDATE_REQUEST_SYSTEM = SYSTEM + """

CANDIDATE ROUTING MODE: Do not propose or repeat any screen coordinates. If revision is
needed, identify only the stable zero-based instance indices that are actually wrong:
{"decision":"revise","reason":"...","placements":[
  {"edit_id":"stable id","instance_indices":[0],
   "screen_bounds":[0.05,0.05,0.95,0.90]}
]}
Correct instances must be omitted. The next deterministic stage will show labeled
candidate boxes for each listed instance and a separate Agent will select an exact id.
Omit screen_bounds when the current search region contains plausible alternatives. If the
current bounds are visibly too narrow or exclude the required semantic support, you may
revise only screen_bounds; this changes the candidate search area, not an object position.
The new screen_bounds belongs to the whole add edit, so it MUST still contain the current
screen_position of every instance, including FIXED siblings. Expand the old bounds toward
the needed landmark; never shrink them in a way that excludes an existing point.
List wrong instances in descending severity and request no more than the payload's
max_candidate_requests. Lower-priority problems remain for a later review round.

The payload may include prior_candidate_feedback. A previous decision=none means that
the shown candidate batch had no acceptable point. Read its reason. If all candidates
failed because the current search area was crowded, unsupported, or excluded the needed
landmark, change screen_bounds before requesting that instance again. Do not keep asking
for blind resamples inside bounds already proven unsuitable.
"""


def compact_candidate_feedback(paths: list[Path]) -> list[dict]:
    """Expose conclusions from prior candidate sheets without resending images/payloads."""
    rows = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            continue
        applied = value.get("applied") or {}
        payload = value.get("candidate_payload") or {}
        rows.append({
            "edit_id": payload.get("edit_id") or applied.get("edit_id"),
            "instance_index": payload.get("instance_index", applied.get("instance_index")),
            "decision": value.get("decision"),
            "candidate_id": value.get("candidate_id"),
            "reason": value.get("reason"),
        })
    return rows[-9:]


def compact_review(review: dict) -> dict:
    """Keep the critic payload visual and bounded; omit 72-frame raw projections."""
    placements = []
    for row in review.get("placements", []):
        screen_centers = []
        for index, center in enumerate(row.get("screen_centers") or []):
            screen_centers.append({
                "instance_index": center.get("instance_index", index),
                "object": center.get("object"),
                "xy_bottom_origin": center.get("xy_bottom_origin"),
            })
        placements.append({
            "placement": row.get("placement"),
            "edit_id": row.get("code_from"),
            "factory": row.get("factory"),
            "anchoring": row.get("anchoring"),
            "world_motion": row.get("world_motion"),
            "placement_spec": row.get("placement_spec"),
            "screen_centers": screen_centers,
            "audits": [
                {
                    "instance_index": index,
                    "object": audit.get("object"),
                    "visible_frames": audit.get("visible_frames"),
                    "frame_count": audit.get("frame_count"),
                    "all_frames_visible": audit.get("all_frames_visible"),
                    "fully_visible_frames": audit.get("fully_visible_frames"),
                    "all_frames_fully_visible": audit.get("all_frames_fully_visible"),
                    "selected_visible_surface": audit.get("selected_visible_surface"),
                    "selected_surface_normal": audit.get("selected_surface_normal"),
                    "selected_surface_slope_deg": audit.get(
                        "selected_surface_slope_deg"),
                }
                for index, audit in enumerate(row.get("anchors", []))
            ],
        })
    return {
        "status": review.get("status"),
        "frame_range": review.get("frame_range"),
        "failures": review.get("failures"),
        "violations": review.get("violations"),
        "composition_warnings": review.get("composition_warnings"),
        "placements": placements,
    }


def balanced_object_end(text: str, start: int) -> int | None:
    """Return the index just past the object opening at ``start``, or None.

    Braces inside string literals do not count, so a reason field mentioning
    ``{B1-C2}`` cannot unbalance the scan.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def iter_json_objects(text: str):
    """Yield each parsable JSON object in emission order.

    Every ``{`` is a possible start, because a stray unbalanced brace in the
    surrounding prose must not swallow the real decision that follows it.
    """
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return
        end = balanced_object_end(text, start)
        if end is None:
            index = start + 1
            continue
        try:
            value = json.loads(text[start:end])
        except json.JSONDecodeError:
            index = start + 1
            continue
        yield value
        index = end


def extract_json(text: str, *, required_keys: tuple[str, ...] = ("decision",),
                 what: str = "critic") -> dict:
    """Recover the decision object from a free-form model reply.

    A greedy ``\\{.*\\}`` match used to span from the first brace to the last
    one, so a reply carrying prose or a second JSON object became unparsable
    ("Extra data") and killed the whole benchmark case.  Scan for balanced
    objects instead and prefer the last one that actually looks like a
    decision.
    """
    objects = [value for value in iter_json_objects(text) if isinstance(value, dict)]
    if not objects:
        raise ValueError(f"{what} returned no parsable JSON object: {text[:500]}")
    for value in reversed(objects):
        if all(key in value for key in required_keys):
            return value
    return objects[-1]


def normalize_decision_kind(value, allowed: set[str]) -> str:
    """Accept ``"Revise"`` or ``" none "`` as the bare verb they clearly mean."""
    if not isinstance(value, str):
        raise ValueError(f"invalid decision {value!r}")
    kind = value.strip().lower()
    if kind not in allowed:
        raise ValueError(f"invalid decision {value!r}")
    return kind


def compact_runtime_review(value: dict | None) -> dict | None:
    """Expose detect-only Step-9 failures without dumping full 72-frame audits."""
    if not value:
        return None
    collisions = []
    for row in (value.get("animated_collision_review") or {}).get("violations", []):
        collisions.append({
            key: row.get(key) for key in (
                "type", "objects", "edit_ids", "first_frame", "last_frame",
                "collision_frames", "max_overlap_of_smaller",
                "screen_at_first_collision",
            )
        })
    motions = []
    for placement in value.get("world_motion", []):
        motions.append({
            "edit_id": placement.get("code_from"),
            "factory": placement.get("factory"),
            "request": placement.get("request"),
            "objects": [{
                key: obj.get(key) for key in (
                    "object", "preset", "heading_deg", "heading_source",
                    "speed_scale", "max_world_displacement_m",
                    "final_world_displacement_m", "applied",
                )
            } for obj in placement.get("objects", [])],
        })
    return {
        "status": value.get("status"),
        "automatic_placement_repairs": value.get("automatic_placement_repairs"),
        "hard_failures": value.get("hard_failures"),
        "animated_collision_violations": collisions,
        "world_motion": motions,
        "motion_path_failures": value.get("motion_path_failures"),
        "camera_failures": (value.get("camera_visibility") or {}).get("failures"),
    }


def validate_and_apply(decision: dict, plan: dict, *, apply_revision: bool) -> tuple[dict, list]:
    kind = decision.get("decision")
    if kind not in {"accept", "revise"}:
        raise ValueError(f"invalid critic decision: {kind!r}")
    patches = decision.get("placements") or []
    if kind == "accept" and patches:
        raise ValueError("accept decision must not contain placement patches")
    if kind == "revise" and not patches:
        raise ValueError("revise decision contains no placement patches")

    revised = deepcopy(plan)
    adds = {
        str(edit.get("code_from")): edit
        for edit in revised.get("hero_edits", [])
        if edit.get("op") == "add" and edit.get("code_from")
    }
    applied = []
    for patch in patches:
        edit_id = str(patch.get("edit_id") or "")
        if edit_id not in adds:
            raise ValueError(f"critic targeted unknown/non-add edit_id {edit_id!r}")
        edit = adds[edit_id]
        count = int(edit.get("n", 0))
        from placement_contract import (
            apply_instance_screen_updates,
            normalize_placement_spec,
            normalize_screen_positions,
        )
        raw_spec = deepcopy(edit.get("placement_spec") or {})
        if patch.get("screen_bounds") is not None:
            raw_spec["screen_bounds"] = patch["screen_bounds"]
        spec = normalize_placement_spec(
            raw_spec, count=count,
            anchoring=edit.get("anchoring"))
        points = patch.get("screen_positions")
        instance_points = patch.get("instance_positions")
        if points is not None and instance_points is not None:
            raise ValueError(
                f"critic patch for {edit_id!r} mixes full and instance positions")
        if instance_points is not None:
            current_points = edit.get("screen_positions")
            if current_points is None:
                raise ValueError(
                    f"instance patch for {edit_id!r} requires existing screen_positions")
            clean = apply_instance_screen_updates(
                current_points, instance_points, spec=spec, count=count)
        else:
            clean = (normalize_screen_positions(points, spec=spec, count=count)
                     if points is not None else None)
        motion_patch = patch.get("world_motion_params")
        if clean is None and motion_patch is None:
            raise ValueError(
                f"critic patch for {edit_id!r} changes neither placement nor motion")
        motion = None
        if motion_patch is not None:
            import world_motion as wm
            current = deepcopy(edit.get("world_motion") or {})
            current["params"] = motion_patch
            motion = wm.validate_runtime_request(current)
            if motion["preset"] == "none":
                raise ValueError("none world_motion cannot receive path params")
        if apply_revision:
            edit["placement_spec"] = spec
            if clean is not None:
                edit["screen_positions"] = clean
                edit["screen_position_projection"] = "visible_surface"
                edit.pop("positions", None)
            if motion is not None:
                edit["world_motion"] = motion
        applied_row = {
            "edit_id": edit_id,
            "screen_bounds": spec["screen_bounds"],
            "screen_positions": clean,
        }
        if instance_points is not None:
            applied_row["instance_positions"] = instance_points
        if motion is not None:
            applied_row["world_motion"] = motion
        applied.append(applied_row)
    return revised, applied


def validate_candidate_requests(decision: dict, plan: dict, *, max_requests: int = 3) -> list[dict]:
    kind = decision.get("decision")
    if kind not in {"accept", "revise"}:
        raise ValueError(f"invalid critic decision: {kind!r}")
    patches = decision.get("placements") or []
    if kind == "accept" and patches:
        raise ValueError("accept decision must not contain candidate requests")
    if kind == "revise" and not patches:
        raise ValueError("revise decision contains no candidate requests")
    adds = {
        str(edit.get("code_from")): edit
        for edit in plan.get("hero_edits", [])
        if edit.get("op") == "add" and edit.get("code_from")
    }
    requests = []
    seen = set()
    for patch in patches:
        edit_id = str(patch.get("edit_id") or "")
        if edit_id not in adds:
            raise ValueError(f"candidate request targets unknown add {edit_id!r}")
        if any(key in patch for key in (
                "screen_positions", "instance_positions", "world_motion_params")):
            raise ValueError("candidate routing mode cannot include coordinates or motion")
        indices = patch.get("instance_indices")
        if not isinstance(indices, list) or not indices:
            raise ValueError("instance_indices must be a non-empty list")
        count = int(adds[edit_id].get("n", 0))
        new_bounds = patch.get("screen_bounds")
        if new_bounds is not None:
            from placement_contract import (
                normalize_placement_spec,
                normalize_screen_positions,
            )
            raw_spec = deepcopy(adds[edit_id].get("placement_spec") or {})
            raw_spec["screen_bounds"] = new_bounds
            bounded_spec = normalize_placement_spec(
                raw_spec, count=count,
                anchoring=adds[edit_id].get("anchoring"))
            # Bounds belong to the whole add edit even though candidates move
            # one instance.  They may not exclude a sibling that must remain
            # fixed byte-for-byte.
            normalize_screen_positions(
                adds[edit_id].get("screen_positions"),
                spec=bounded_spec, count=count)
            new_bounds = bounded_spec["screen_bounds"]
        for index in indices:
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("candidate instance index must be an integer")
            if not (0 <= index < count):
                raise ValueError(
                    f"{edit_id} instance {index} is outside 0..{count - 1}")
            key = (edit_id, index)
            if key in seen:
                raise ValueError(f"duplicate candidate request {edit_id}#{index}")
            seen.add(key)
            request = {"edit_id": edit_id, "instance_index": index}
            if new_bounds is not None:
                request["screen_bounds"] = new_bounds
            requests.append(request)
    # The Agent orders requests by severity.  Enforce the cost bound
    # deterministically even if it emitted too many; lower-priority instances
    # remain visible and can be requested in a later formal review round.
    return requests[:int(max_requests)]


def apply_candidate_request_bounds(plan: dict, requests: list[dict]) -> tuple[dict, list[dict]]:
    """Apply only Agent-proposed search bounds; never choose an object position."""
    revised = deepcopy(plan)
    adds = {
        str(edit.get("code_from")): edit
        for edit in revised.get("hero_edits", [])
        if edit.get("op") == "add" and edit.get("code_from")
    }
    applied = []
    seen = set()
    for request in requests:
        edit_id = str(request["edit_id"])
        bounds = request.get("screen_bounds")
        if bounds is None or edit_id in seen:
            continue
        seen.add(edit_id)
        edit = adds[edit_id]
        before = deepcopy((edit.get("placement_spec") or {}).get("screen_bounds"))
        edit.setdefault("placement_spec", {})["screen_bounds"] = list(bounds)
        applied.append({
            "edit_id": edit_id,
            "screen_bounds_before": before,
            "screen_bounds_after": list(bounds),
        })
    return revised, applied


def main() -> int:
    args = parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    runtime_review = (json.loads(args.runtime_review.read_text(encoding="utf-8"))
                      if args.runtime_review else None)
    image = base64.standard_b64encode(args.preview.read_bytes()).decode("ascii")
    top_image = (base64.standard_b64encode(args.top_preview.read_bytes()).decode("ascii")
                 if args.top_preview else None)
    runtime_image = (base64.standard_b64encode(args.runtime_preview.read_bytes()).decode("ascii")
                     if args.runtime_preview else None)
    payload = {
        "mechanical_review": compact_review(review),
        "runtime_motion_review": compact_runtime_review(runtime_review),
        "composer_edit_plan": plan,
        "max_candidate_requests": int(args.max_candidate_requests),
        "prior_candidate_feedback": compact_candidate_feedback(
            args.prior_candidate_decision),
    }
    response = client().messages.create(
        model=args.model,
        max_tokens=1800,
        system=CANDIDATE_REQUEST_SYSTEM if args.request_candidates else SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "STEP-8 PLACEHOLDER PREVIEW:"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": image}},
            *([{"type": "text", "text": (
                    "STEP-8 ORTHOGRAPHIC TOP VIEW. Requested instances keep the same "
                    "colors and labels as the final-camera view. Use this diagnostic view "
                    "for footprint, support region, spacing, and front/back ambiguity. "
                    "The final-camera image remains authoritative for composition and "
                    "visibility. Do not infer a 3D terrain intersection from overlap in "
                    "only one view:")},
               {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": top_image}}] if top_image else []),
            *([{"type": "text", "text": (
                    "STEP-9 MOTION CONTACT SHEET. This is diagnostic only; validators "
                    "did not move any object or rewrite any trajectory:")},
               {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": runtime_image}}] if runtime_image else []),
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    decision = extract_json(raw)
    decision["decision"] = normalize_decision_kind(
        decision.get("decision"), {"accept", "revise"})
    if runtime_review is None and any(
            patch.get("world_motion_params") is not None
            for patch in decision.get("placements") or []):
        raise ValueError("critic cannot edit world motion without a runtime review")
    runtime_failing = bool(
        runtime_review and runtime_review.get("status") == "NEEDS_AGENT_REVISION")
    if ((review.get("status") != "PASS" or runtime_failing)
            and decision.get("decision") == "accept"):
        raise ValueError("critic cannot accept a mechanically failing placement review")
    if args.request_candidates:
        if runtime_review is not None:
            raise ValueError("candidate routing mode is Step-8 placement only")
        requests = validate_candidate_requests(
            decision, plan, max_requests=args.max_candidate_requests)
        revised, applied = apply_candidate_request_bounds(plan, requests)
    else:
        requests = []
        revised, applied = validate_and_apply(
            decision, plan, apply_revision=args.apply_revision)
    before_hash = sha256_json(plan)
    after_hash = sha256_json(revised)
    if args.apply_revision and decision["decision"] == "revise":
        args.plan.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    result = {
        "contract_version": 1,
        "decision": decision["decision"],
        "reason": decision.get("reason"),
        "applied_revision": bool(args.apply_revision and decision["decision"] == "revise"),
        "patches": applied,
        "candidate_requests": requests,
        "plan_before_sha256": before_hash,
        "plan_after_sha256": after_hash,
        "raw_response": raw,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"[placement-critic] {result['decision'].upper()}: {result['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

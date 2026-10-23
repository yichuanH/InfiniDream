#!/usr/bin/env python
"""Let Composer rewrite placement JSON from deterministic machine failures."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

import anthropic

try:
    from .composer import _env
    from .placement_critic import extract_json
except ImportError:  # direct script execution keeps planner/ on sys.path
    from composer import _env
    from placement_critic import extract_json
from placement_contract import normalize_screen_positions


MACHINE_OWNED_PLACEMENT_FIELDS = {
    "composer_image_placement_intent",
    "explicit_position_supports",
    "indoor_planar_hardening",
    "indoor_visibility_scaling",
    "large_ground_relaxation",
    "outdoor_ultra_loose_relaxation",
    "placement_yaws",
    "positions",
    "positions_source",
    "surface_fallback",
}


SYSTEM = """You are the placement-revision mode of a 3D scene Composer.
The deterministic machine review has rejected one or more placeholder boxes.
Rewrite only screen_positions for rejected instances. Preserve every other field,
object count, edit, anchoring, surface policy, and valid sibling position exactly.
Coordinates are normalized camera/render [x,y] with (0,0) at the BOTTOM-LEFT;
x increases rightward and y increases upward. They must stay inside
placement_spec.screen_bounds.
Use failure type mechanically: clipped boxes move inward; unsupported/slope failures
move to another well-separated part of the allowed region; collisions separate the
reported pair. The current camera preview and top preview may be attached. Use them
together with each anchor's projected bounds as occupied exclusion rectangles, not just
the center points. A revision history lists rejected coordinates and exact reasons: do
not reuse or make a tiny perturbation of a rejected coordinate unless the reported
constraint has materially changed. When several currently rejected instances interact,
lay out that entire rejected set coherently in one reply while preserving every passing
sibling. Return one JSON object only: {"hero_edits": [...], "extra_edits": [...]}.
Never claim success; the machine review will verify the rewritten plan."""


def build_revision_history(history_root: Path | None) -> list[dict]:
    """Compact saved rounds into rejected coordinates plus deterministic causes."""
    if history_root is None or not history_root.is_dir():
        return []
    history = []
    for round_dir in sorted(history_root.glob("round_*")):
        plan_path = round_dir / "edit_plan_before.json"
        review_path = round_dir / "machine_review.json"
        if not plan_path.is_file() or not review_path.is_file():
            continue
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        review = json.loads(review_path.read_text(encoding="utf-8"))
        allowed = collect_allowed_updates(review)
        rejected = []
        for edit in plan.get("hero_edits", []):
            edit_id = str(edit.get("code_from") or "")
            points = edit.get("screen_positions") or []
            for index in sorted(allowed.get(edit_id, set())):
                if 0 <= index < len(points):
                    rejected.append({
                        "edit_id": edit_id,
                        "instance_index": index,
                        "screen_position": points[index],
                    })
        history.append({
            "round": round_dir.name,
            "rejected_positions": rejected,
            "failures": review.get("failures", []),
            "projection_failures": review.get("failed_instances", []),
            "collision_violations": [{
                "objects": row.get("objects", []),
                "edit_ids": row.get("edit_ids", []),
                "placement_indices": row.get("placement_indices", []),
                "max_overlap_of_smaller": row.get("max_overlap_of_smaller"),
            } for row in review.get("violations", [])],
        })
    return history


def oscillation_evidence(history: list[dict]) -> dict:
    """Flag exact rejected-coordinate reuse and multi-round nonconvergence."""
    seen = {}
    repeated = []
    for row in history:
        for rejected in row.get("rejected_positions", []):
            key = (rejected.get("edit_id"), rejected.get("instance_index"))
            point = tuple(rejected.get("screen_position") or [])
            prior = seen.setdefault(key, set())
            if point and point in prior:
                repeated.append({
                    "edit_id": key[0], "instance_index": key[1],
                    "screen_position": list(point),
                })
            prior.add(point)
    return {
        "round_count": len(history),
        "coordinated_failed_set_relayout_required": len(history) >= 2,
        "exact_rejected_positions_reused": repeated,
    }


def image_block(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def collect_allowed_updates(review: dict) -> dict[str, set[int]]:
    """Return exactly the instances rejected by projection or machine review."""
    allowed_updates: dict[str, set[int]] = {}
    for failure in review.get("failed_instances", []):
        edit_id = str(failure.get("edit_id") or "")
        if edit_id and failure.get("instance_index") is not None:
            allowed_updates.setdefault(edit_id, set()).add(
                int(failure["instance_index"]))
    object_to_instance = {}
    for row in review.get("placements", []):
        edit_id = str(row.get("code_from") or "")
        for anchor in row.get("anchors", []):
            index = int(anchor.get("instance_index", 0))
            object_to_instance[str(anchor.get("object"))] = (edit_id, index)
            hard_failure = (
                not anchor.get("all_frames_fully_visible", False)
                or bool(anchor.get("surface_policy_issue"))
                or not anchor.get("footprint_support", {}).get("supported", False)
            )
            if hard_failure:
                allowed_updates.setdefault(edit_id, set()).add(index)
    for violation in review.get("violations", []):
        for object_name in violation.get("objects", []):
            mapped = object_to_instance.get(str(object_name))
            if mapped:
                allowed_updates.setdefault(mapped[0], set()).add(mapped[1])
    return allowed_updates


def validate_revision(plan: dict, revised: dict,
                      allowed_updates: dict[str, set[int]]) -> dict:
    """Validate a reply without mutating the current accepted plan."""
    before_edits = plan.get("hero_edits", [])
    after_edits = revised.get("hero_edits", [])
    if len(after_edits) != len(before_edits):
        raise ValueError("Composer revision changed hero edit count")
    changed_allowed = set()
    for before, after in zip(before_edits, after_edits):
        for key in MACHINE_OWNED_PLACEMENT_FIELDS:
            if key in before:
                after[key] = before[key]
            else:
                after.pop(key, None)
        protected_before = {k: v for k, v in before.items()
                            if k != "screen_positions"
                            and k not in MACHINE_OWNED_PLACEMENT_FIELDS}
        protected_after = {k: v for k, v in after.items()
                           if k != "screen_positions"
                           and k not in MACHINE_OWNED_PLACEMENT_FIELDS}
        if protected_after != protected_before:
            changed = sorted(set(protected_before) | set(protected_after))
            changed = [key for key in changed
                       if protected_before.get(key) != protected_after.get(key)]
            raise ValueError(
                f"Composer revision changed protected fields: {changed}")
        # Replacement edits reuse an existing reviewed placeholder and therefore
        # legitimately have no placement_spec / n / screen_positions.  They are
        # protected above and are not candidates for screen-position revision;
        # only newly placed edits participate in the per-instance comparison.
        spec = before.get("placement_spec")
        count = before.get("n")
        if not isinstance(spec, dict) or count is None:
            continue
        before_points = normalize_screen_positions(
            before.get("screen_positions"), spec=spec, count=int(count))
        after_points = normalize_screen_positions(
            after.get("screen_positions"), spec=spec, count=int(count))
        edit_id = str(before.get("code_from") or "")
        allowed = allowed_updates.get(edit_id, set())
        changed = {index for index, (left, right) in enumerate(
                   zip(before_points, after_points)) if left != right}
        illegal = changed - allowed
        if illegal:
            raise ValueError(
                f"Composer revision changed passing instances for {edit_id}: "
                f"{sorted(illegal)}; allowed failed instances={sorted(allowed)}")
        changed_allowed.update((edit_id, index) for index in changed & allowed)
    required_changes = {(edit_id, index) for edit_id, indices in allowed_updates.items()
                        for index in indices}
    missing = required_changes - changed_allowed
    if missing:
        raise ValueError(
            "Composer revision did not change failed instances: "
            f"{sorted(missing)}")
    if revised.get("extra_edits", []) != plan.get("extra_edits", []):
        raise ValueError("Composer revision changed extra edits")
    return revised


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--top-preview", type=Path)
    parser.add_argument("--history-root", type=Path)
    parser.add_argument("--model", default=os.environ.get(
        "COMPOSER_MODEL", "claude-opus-4-8"))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    allowed_updates = collect_allowed_updates(review)
    if not allowed_updates:
        raise ValueError("machine review did not identify any failed instances")
    revision_history = build_revision_history(args.history_root)
    payload = {
        "current_edit_plan": plan,
        "machine_status": review.get("status"),
        "failures": review.get("failures", []),
        "collision_violations": review.get("violations", []),
        "placements": review.get("placements", []),
        "allowed_failed_instance_updates": {
            edit_id: sorted(indices) for edit_id, indices in allowed_updates.items()
        },
        "revision_history": revision_history,
        "oscillation_evidence": oscillation_evidence(revision_history),
    }
    values = _env()
    key = values.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found")
    base = (values.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    client = anthropic.Anthropic(api_key=key, base_url=base or None)
    revised = None
    correction = None
    for attempt in range(1, 4):
        request_payload = dict(payload)
        if correction:
            request_payload["previous_reply_contract_error"] = correction
            request_payload["instruction"] = (
                "Return a complete corrected plan. Change every listed failed "
                "instance and no passing instance or protected field.")
        content = [{"type": "text", "text": json.dumps(
            request_payload, ensure_ascii=False)}]
        for label, path in (("CURRENT CAMERA PREVIEW", args.preview),
                            ("CURRENT TOP PREVIEW", args.top_preview)):
            block = image_block(path)
            if block is not None:
                content.append({"type": "text", "text": label})
                content.append(block)
        response = client.messages.create(
            model=args.model, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": content}])
        text = "".join(
            block.text for block in response.content if block.type == "text")
        try:
            candidate = extract_json(
                text, required_keys=("hero_edits",), what="composer revision")
            revised = validate_revision(plan, candidate, allowed_updates)
            if attempt > 1:
                print(f"[composer revision] corrected contract on attempt {attempt}")
            break
        except (TypeError, ValueError) as error:
            correction = str(error)
            print(f"[composer revision] invalid contract attempt {attempt}/3: {error}")
    if revised is None:
        raise ValueError(
            "Composer revision failed deterministic contract after 3 attempts: "
            f"{correction}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    args.plan.write_text(json.dumps(revised, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"COMPOSER_MACHINE_REVISION → {args.plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

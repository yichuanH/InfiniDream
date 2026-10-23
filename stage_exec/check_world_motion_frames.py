"""Fail a full E2E run before asset generation when motion cannot be authored."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import world_motion


def frame_range_failures(plan: dict, frame_start: int, frame_end: int) -> list[str]:
    edits = plan.get("edits")
    if not isinstance(edits, list):
        edits = list(plan.get("hero_edits") or []) + list(plan.get("extra_edits") or [])
    failures = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or not edit.get("feasible", True):
            continue
        request = edit.get("world_motion")
        try:
            world_motion.validate_frame_range(request, frame_start, frame_end)
        except ValueError as error:
            edit_id = edit.get("id") or edit.get("code_from") or edit.get("factory")
            failures.append(f"edit #{index} {edit_id or '<unknown>'}: {error}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--frame-start", type=int, required=True)
    parser.add_argument("--frame-end", type=int, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    failures = frame_range_failures(plan, args.frame_start, args.frame_end)
    if failures:
        print(f"[world-motion-frames] FAIL ({len(failures)} edits)")
        for failure in failures:
            print(f"[world-motion-frames]   {failure}")
        return 1
    print(
        f"[world-motion-frames] PASS: deliverable range "
        f"{args.frame_start}..{args.frame_end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

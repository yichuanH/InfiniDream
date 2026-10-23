#!/usr/bin/env python
"""Copy a placement plan and opt its add edits into diagnostic projection.

The canonical Composer plan is never changed.  This derived plan exists only to
render the final rejected proposal after bounded retries are exhausted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def prepare_diagnostic_plan(plan: dict) -> dict:
    """Return a derived plan that permits rendering rejected projections."""
    plan = json.loads(json.dumps(plan))
    for edit in plan.get("hero_edits", []):
        if edit.get("op") == "add" and edit.get("screen_positions") is not None:
            edit["diagnostic_preview_allow_rejected_projection"] = True
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    plan = prepare_diagnostic_plan(json.loads(
        args.plan.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Refuse rendering unless the exact final blend passed its contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--ok-marker", type=Path, required=True)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--frame-start", type=int)
    parser.add_argument("--frame-end", type=int)
    args = parser.parse_args()

    if not args.ok_marker.is_file():
        raise SystemExit(f"render denied: missing gate {args.ok_marker}")
    with args.contract.open(encoding="utf-8") as handle:
        contract = json.load(handle)
    blend = args.blend.resolve()
    if contract.get("status") != "PASS" or contract.get("render_allowed") is not True:
        raise SystemExit("render denied: final contract did not pass")
    if Path(contract.get("final_scene", "")).resolve() != blend:
        raise SystemExit("render denied: contract belongs to a different blend")
    expected = contract.get("final_scene_stat") or {}
    actual = blend.stat()
    if expected.get("size") != actual.st_size or expected.get("mtime_ns") != actual.st_mtime_ns:
        raise SystemExit("render denied: fine blend changed after validation; rerun contract step")
    frames = contract.get("frames") or []
    if args.frame_start is not None and (not frames or min(frames) != args.frame_start):
        raise SystemExit("render denied: requested start frame differs from validated contract")
    if args.frame_end is not None and (not frames or max(frames) != args.frame_end):
        raise SystemExit("render denied: requested end frame differs from validated contract")
    print(f"[render-gate] PASS exact validated blend: {blend}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

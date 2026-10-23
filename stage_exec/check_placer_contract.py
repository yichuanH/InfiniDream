"""Fail before expensive fine meshing when placer cannot deliver the edit plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from validate_final_scene import (placer_delivery_audit,
                                  requested_camera_visibility_audit,
                                  world_motion_audit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--placer-report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-blend", type=Path)
    parser.add_argument("--output-blend", type=Path)
    parser.add_argument("--frame-start", type=int)
    parser.add_argument("--frame-end", type=int)
    args = parser.parse_args()
    with args.placer_report.open(encoding="utf-8") as handle:
        placer = json.load(handle)
    with args.manifest.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    audit = placer_delivery_audit(placer, manifest)
    motion = world_motion_audit(placer, manifest)
    camera = requested_camera_visibility_audit(placer, manifest)
    failures = (audit["placer_failures"] + audit["delivery_failures"]
                + motion["failures"] + camera["failures"])
    failures.extend(placer.get("hard_failures") or [])
    collisions = (placer.get("animated_collision_review") or {}).get("violations") or []
    failures.extend(
        f"animated collision {item.get('edit_ids') or item.get('objects')} at "
        f"frames {item.get('first_frame')}..{item.get('last_frame')}"
        for item in collisions
    )
    provenance = placer.get("provenance") or {}
    manifest_id = provenance.get("manifest") or {}
    manifest_bytes = args.manifest.resolve().read_bytes()
    if (manifest_id.get("path") != str(args.manifest.resolve())
            or manifest_id.get("sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
        failures.append("placer report does not belong to the current manifest")
    for label, requested, recorded in (
        ("input", args.input_blend, provenance.get("input_blend") or {}),
        ("output", args.output_blend, provenance.get("output_blend") or {}),
    ):
        if requested is None:
            continue
        resolved = requested.resolve()
        if not resolved.is_file():
            failures.append(f"placer {label} blend missing: {resolved}")
            continue
        stat = resolved.stat()
        if (recorded.get("path") != str(resolved)
                or recorded.get("size") != stat.st_size
                or recorded.get("mtime_ns") != stat.st_mtime_ns):
            failures.append(f"placer report does not belong to current {label} blend")
    if args.frame_start is not None or args.frame_end is not None:
        expected = [args.frame_start, args.frame_end]
        if None in expected or placer.get("frame_range") != expected:
            failures.append(
                f"placer frame range={placer.get('frame_range')} expected={expected}; rerun step 9")
    if failures:
        print(f"[placer-contract] FAIL ({len(failures)} problems)")
        for failure in failures:
            print(f"[placer-contract]   {failure}")
        return 1
    print(
        f"[placer-contract] PASS: {audit['result_count']} feasible results, "
        f"{audit['checked_deliveries']} requested deliveries, "
        f"{len(motion['checked'])} world-motion anchors, "
        f"{camera['represented']} placements visible every frame"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

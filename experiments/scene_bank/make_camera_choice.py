"""從 angle 名稱產生 camera_choice.json（純 python，不需要 bpy）。

★ANGLES 只有一份來源★ —— run_baseline/preview_angles.py，也就是實際渲出那 5 張
preview 的那份。不要在這裡抄一份 offset 表，抄了就會漂（pick_camera.py 的註解
已經踩過這個坑）。

用法:
  python make_camera_choice.py <angle> <out.json> --scene forest/seed3 \
         --blend /abs/path/to/coarse/scene.blend
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "run_baseline" / "preview_angles.py"


def load_angles() -> dict:
    src = PREVIEW.read_text(encoding="utf-8")
    start = src.index("ANGLES = {")
    end = src.index("\n}\n", start) + 3
    ns: dict = {}
    exec(src[start:end], {"dict": dict}, ns)  # noqa: S102
    return ns["ANGLES"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("angle")
    ap.add_argument("out_json")
    ap.add_argument("--scene", required=True, help="e.g. forest/seed3, for the 'why' field")
    ap.add_argument("--blend", required=True, help="coarse blend this choice applies to")
    args = ap.parse_args()

    angles = load_angles()
    if args.angle not in angles:
        raise SystemExit(f"unknown angle {args.angle!r}; choose from {list(angles)}")

    out = Path(args.out_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "angle": args.angle,
                "cfg": angles[args.angle],
                "why": f"User-selected {args.angle} pose from scenes/{args.scene}/preview/contact_sheet.png.",
                "blend": args.blend,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[choice] {args.scene} -> {args.angle} -> {out}", flush=True)


if __name__ == "__main__":
    main()

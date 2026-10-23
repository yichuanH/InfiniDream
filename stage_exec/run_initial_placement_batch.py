"""Run initial placement, review, and two previews in one Blender process."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import json


ROOT = Path(__file__).resolve().parent.parent


def run_script(relative_path: str, args: list[str]) -> None:
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(ROOT / relative_path), "--", *args]
        try:
            runpy.run_path(str(ROOT / relative_path), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    finally:
        sys.argv = old_argv


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:]
    if len(argv) < 8:
        raise SystemExit(
            "usage: run_initial_placement_batch.py -- SOURCE OUT_BLEND PLAN REPORT "
            "REVIEW_JSON FINAL_PNG TOP_PNG FRAME")
    source, out_blend, plan, report, review, final_png, top_png, frame = argv[:8]
    review_extra = []
    if "--region-map-index" in argv[8:]:
        index = argv.index("--region-map-index")
        review_extra = ["--region-map-index", argv[index + 1]]
    semantic_grid_overlay = "--semantic-grid-overlay" in argv[8:]
    semantic_grid_data = None
    if review_extra and semantic_grid_overlay:
        region_map_index = Path(review_extra[1])
        semantic_grid_data = Path(review).with_name("semantic_grid_overlay.json")
        run_script("stage_exec/build_semantic_grid_overlay.py", [
            "--map-index", str(region_map_index),
            "--output", str(semantic_grid_data),
        ])
    try:
        run_script("stage_exec/apply_plan.py", [source, out_blend, plan, report])
    except Exception as error:
        # apply_plan and this orchestrator import the same workspace module.
        # Avoid importing Blender-only code here; recognize the JSON-ready
        # projection exception structurally so runpy module identity is harmless.
        structured = getattr(error, "failures", None)
        if not isinstance(structured, list):
            raise
        manifest_path = Path(report).with_name(
            Path(report).stem.replace("report", "manifest") + ".json")
        scene_manifest_path = Path(report).with_name("scene_manifest.json")
        scene_inventory = {}
        if scene_manifest_path.is_file():
            source_manifest = json.loads(scene_manifest_path.read_text(encoding="utf-8"))
            scene_inventory = {
                "available_surface_types": source_manifest.get(
                    "available_surface_types", []),
                "raycast_surfaces": source_manifest.get("raycast_surfaces", []),
            }
        payload = {
            "contract_version": 1,
            "status": "NEEDS_REVISION",
            "failure_stage": "placement_projection",
            "failed_instances": structured,
            "failures": [
                f"{row.get('edit_id')} instance {row.get('instance_index')}: "
                f"{row.get('failure')} ({row.get('reason')})"
                for row in structured
            ],
            "placements": [],
            "violations": [],
            "scene_surface_evidence": scene_inventory,
        }
        Path(review).parent.mkdir(parents=True, exist_ok=True)
        Path(review).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print("INITIAL_PLACEMENT_PROJECTION_NEEDS_REVISION", flush=True)
        return 0
    manifest = str(Path(report).with_name(
        Path(report).stem.replace("report", "manifest") + ".json"))
    run_script("stage_exec/review_placeholders.py", [
        out_blend, manifest, report, plan, review, "--frame", frame,
        "--reuse-open", *review_extra,
    ])
    common = [
        "--reuse-open", "--as-is", "--frame", frame, "--show-placeholders",
        "--preview-anchoring", "--color", "--wire", "--marker-box",
        "--instance-colors", "--label-added", "--mark-added", report,
        "--mark-requested-manifest", manifest,
        "--res", "1280", "720", "--samples", "32", "--view", "AgX",
    ]
    if semantic_grid_data is not None and semantic_grid_data.is_file():
        common.extend([
            "--semantic-grid-overlay",
            "--semantic-grid-data", str(semantic_grid_data),
        ])
    run_script("stage_exec/render_view.py", [out_blend, final_png, *common])
    # ★只有 ground_scatter 的 plan 沒有任何 hero 交付物★(2026-09-08)
    #   俯視預覽是拿「這個 plan 要求擺放的 placeholder」當中心算出來的，
    #   一個都沒有時 render_view 會 raise SystemExit，整個 step 8 跟著 die。
    #   ground_scatter 走的是 GN scatter，render 時才算位置、blend 裡沒有個體，
    #   本來就★不存在可以框的 placeholder★ —— 這不是錯誤，是這類 plan 的常態。
    #   ⇒ 事前判斷，沒有就跳過俯視圖(其餘預覽與 review 照常產出)。
    requested = []
    try:
        with open(manifest, encoding="utf-8") as handle:
            requested = [
                row for row in (json.load(handle).get("placements") or [])
                if isinstance(row, dict) and (
                    row.get("added_this_plan")
                    or row.get("claimed_by_composer")
                    or row.get("code"))
            ]
    except Exception as error:                        # noqa: BLE001
        print(f"[batch] ⚠️ 讀 manifest 失敗，仍嘗試俯視圖: "
              f"{type(error).__name__}: {error}", flush=True)
        requested = [None]
    if top_png != "-" and not requested:
        print("[batch] ↷ 這個 plan 沒有 hero 交付物(只有 ground_scatter) → "
              "跳過俯視預覽", flush=True)
        top_png = "-"
    if top_png != "-":
        run_script("stage_exec/render_view.py", [
            out_blend, top_png, *common, "--top-view-requested",
        ])
    print("INITIAL_PLACEMENT_BATCH_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

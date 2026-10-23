"""Build a human-readable, per-step index for one E2E run.

The index uses symlinks for large blends/directories, so organizing a run does
not duplicate multi-gigabyte artifacts.  It is safe to call repeatedly while a
pipeline is running; missing future outputs are listed as pending.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from paths import ASSET_ROOT, asset_store_dir  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--log-prefix", type=Path, required=True)
    parser.add_argument("--asset-qa-mode", choices=("smoke", "full"),
                        help="configured asset turntable mode; otherwise read/infer it")
    return parser.parse_args()


def artifact(name: str, path: Path, required: bool = True) -> dict:
    return {"name": name, "path": path, "required": required}


def _scene_slug(run_root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", run_root.name).strip("_") or "scene"


def orchestration_path(run_root: Path) -> Path:
    return ASSET_ROOT / "_manifests" / f"{_scene_slug(run_root)}_orchestration.json"


def existing_orchestration_path(run_root: Path) -> Path:
    current = orchestration_path(run_root)
    if current.is_file():
        return current
    return run_root / "3_generator/orchestration.json"


def reused_asset_sources(run_root: Path) -> dict[str, str]:
    """Map requested asset IDs to the on-disk IDs selected by reuse matching."""
    table_path = run_root / "overlay_table.json"
    if not table_path.is_file():
        return {}
    try:
        table = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    result = {}
    for entry in table.values() if isinstance(table, dict) else []:
        if not isinstance(entry, dict):
            continue
        requested = entry.get("id")
        source = entry.get("source_id")
        if requested and source:
            result[str(requested)] = str(source)
    return result


def stage_definitions(run_root: Path, input_dir: Path, log_prefix: Path) -> list[dict]:
    def log(suffix: str) -> Path:
        return Path(f"{log_prefix}_{suffix}.log")

    generation_list = run_root / "2_director/generation_list.json"
    reused_sources = reused_asset_sources(run_root)
    generation_outputs = [
        artifact("generation_list.json", generation_list),
        artifact("orchestration.json", existing_orchestration_path(run_root)),
        artifact("asset_store", ASSET_ROOT, required=False),
    ]
    if generation_list.is_file():
        try:
            requested = json.loads(generation_list.read_text(encoding="utf-8")).get("objects", [])
        except (OSError, ValueError, AttributeError):
            requested = []
        for request in requested:
            asset_id = request.get("id")
            generator = request.get("generator")
            if not asset_id or generator not in (
                "ProceduralGenerator", "BlenderGenerator"
            ):
                continue
            source_id = reused_sources.get(asset_id, asset_id)
            generation_outputs.append(
                artifact(
                    f"{asset_id}_final.png",
                    asset_store_dir(generator, source_id, "run") / "final.png",
                )
            )

    return [
        {
            "number": 1,
            "name": "scene_picker",
            "inputs": [input_dir / "prompt.txt", "scene_picker catalog", "seed preview bank"],
            "outputs": [
                artifact("pick.json", run_root / "pick/pick.json"),
                artifact("baseline_pick.json", run_root / "pick/baseline_pick.json", required=False),
                artifact("scene_picks.json", run_root / "pick/scene_picks.json", required=False),
                artifact("seed_pick.json", run_root / "pick/seed_pick.json", required=False),
            ],
            "logs": [log("pick")],
        },
        {
            "number": 2,
            "name": "prepare",
            "inputs": ["picked scene_dir/coarse + selected baseline blend/render"],
            "outputs": [
                artifact("input_scene.blend", input_dir / "scene.blend"),
                artifact("director_preview.png", input_dir / "render.png"),
            ],
            "logs": [],
        },
        {
            "number": 3,
            "name": "official_camera",
            "inputs": ["picked coarse scene.blend"],
            "outputs": [
                artifact("camera_outputs", run_root / "0_camera"),
                artifact("scene_manifest.json", run_root / "scene_manifest.json"),
            ],
            "logs": [log("official_camera"), log("scan")],
        },
        {
            "number": 4,
            "name": "director",
            "inputs": [input_dir / "prompt.txt", input_dir / "render.png", run_root / "scene_manifest.json"],
            "outputs": [artifact("director_plan", run_root / "1_director")],
            "logs": [log("director")],
        },
        {
            "number": 5,
            "name": "asset_generation",
            "inputs": [run_root / "1_director/edit_plan.json"],
            "outputs": generation_outputs,
            "logs": [log("genassets")],
        },
        {
            "number": 6,
            "name": "overlay_merge",
            "inputs": [run_root / "2_director/generation_list.json", ASSET_ROOT],
            "outputs": [
                artifact("overlay_merged", run_root / "overlay_merged"),
                artifact("overlay_table.json", run_root / "overlay_table.json"),
            ],
            "logs": [],
        },
        {
            "number": 7,
            "name": "composer",
            "inputs": [run_root / "1_director/edit_plan.json", run_root / "scene_manifest.json"],
            "outputs": [artifact("composer_outputs", run_root / "3_composer")],
            "logs": [log("composer")],
        },
        {
            "number": 8,
            "name": "apply_plan",
            "inputs": [run_root / "0_camera/baked_coarse.blend", run_root / "3_composer/edit_plan.json"],
            "outputs": [
                artifact("edited_coarse", run_root / "work"),
                artifact("apply_report.json", run_root / "report.json"),
                artifact("placement_manifest.json", run_root / "manifest.json"),
                artifact("placeholder_preview.png",
                         run_root / "placement_review/placement_preview.png"),
                artifact("placement_review.json",
                         run_root / "placement_review/placement_review.json"),
            ],
            "logs": [log("applyplan"), log("placement_preview"), log("placement_review")],
        },
        {
            "number": 9,
            "name": "placer",
            "inputs": [run_root / "work/scene.blend", run_root / "manifest.json", run_root / "overlay_merged"],
            "outputs": [
                artifact("populated_scene", run_root / "populated"),
                artifact("placer_report.json", run_root / "placer_report.json"),
            ],
            "logs": [log("placer")],
        },
        {
            "number": 10,
            "name": "fine_terrain",
            "inputs": [run_root / "populated/scene.blend", run_root / "populated/MaskTag.json", run_root / "populated/assets"],
            "outputs": [
                artifact("fine_scene", run_root / "fine"),
                artifact("mesher.txt", run_root / "contracts/mesher.txt"),
            ],
            "logs": [log("fine_terrain")],
        },
        {
            "number": 11,
            "name": "final_contract",
            "inputs": [run_root / "populated/scene.blend", run_root / "fine/scene.blend", run_root / "placer_report.json", run_root / "manifest.json"],
            "outputs": [
                artifact("contract_snapshots", run_root / "contracts"),
                artifact("final_contract.json", run_root / "final_contract.json"),
                artifact("final_contract.ok", run_root / "final_contract.ok"),
            ],
            "logs": [log("snapshot_populated"), log("snapshot_fine"), log("final_contract")],
        },
        {
            "number": 12,
            "name": "render",
            "inputs": [run_root / "fine/scene.blend", run_root / "final_contract.json", run_root / "final_contract.ok"],
            "outputs": [
                artifact("validated_render", run_root / "render"),
                artifact("stage_timings.tsv", run_root / "stage_timings.tsv"),
                artifact("stage_timings_complete.tsv",
                         run_root / "stage_timings_complete.tsv", required=False),
                artifact("stage_timing_report.md",
                         run_root / "stage_timing_report.md", required=False),
            ],
            "logs": [log("render"), log("render_animation")],
        },
    ]


def display_path(value) -> str:
    return str(value.resolve()) if isinstance(value, Path) else str(value)


def path_info(path: Path) -> dict:
    exists = path.exists() or path.is_symlink()
    result = {"path": str(path.resolve(strict=False)), "exists": exists}
    if not exists:
        result["kind"] = "missing"
        return result
    stat = path.stat()
    result.update(
        kind="directory" if path.is_dir() else "file",
        size_bytes=None if path.is_dir() else int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
    )
    return result


def asset_qa_summary(run_root: Path, configured_mode: str | None = None) -> dict:
    """Report configured and actual turntable PNG counts for each generated asset."""
    expected_by_mode = {"smoke": 8, "full": 72}
    orchestration = {}
    opath = existing_orchestration_path(run_root)
    if opath.is_file():
        try:
            orchestration = json.loads(opath.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            pass

    mode = configured_mode or orchestration.get("asset_qa_mode")
    expected = orchestration.get("turntable_expected_total_frames_per_asset")
    configured_views = orchestration.get("turntable_views")
    generation_list = run_root / "2_director/generation_list.json"
    try:
        requested = json.loads(generation_list.read_text(encoding="utf-8")).get("objects", [])
    except (OSError, ValueError, AttributeError):
        requested = []

    reused_sources = reused_asset_sources(run_root)
    assets = []
    for item in requested:
        asset_id = item.get("id")
        generator = item.get("generator")
        if not asset_id or generator not in {"ProceduralGenerator", "BlenderGenerator"}:
            continue
        source_id = reused_sources.get(asset_id, asset_id)
        frames_dir = asset_store_dir(generator, source_id, "run") / "final_360_frames"
        level = len(list(frames_dir.glob("level_*.png"))) if frames_dir.is_dir() else 0
        top = len(list(frames_dir.glob("top_*.png"))) if frames_dir.is_dir() else 0
        assets.append({
            "id": asset_id,
            "source_id": source_id,
            "generator": generator,
            "frames_dir": str(frames_dir.resolve(strict=False)),
            "level_frames": level,
            "topdown_frames": top,
            "actual_total_frames": level + top,
        })

    actual_counts = [asset["actual_total_frames"] for asset in assets
                     if asset["actual_total_frames"] > 0]
    source = "configured" if configured_mode else "orchestration"
    if mode not in expected_by_mode:
        source = "inferred"
        if actual_counts and all(count == expected_by_mode["smoke"] for count in actual_counts):
            mode = "smoke"
        elif actual_counts and all(count == expected_by_mode["full"] for count in actual_counts):
            mode = "full"
        else:
            mode = "unknown"
    if expected is None:
        expected = expected_by_mode.get(mode)
    return {
        "mode": mode,
        "mode_source": source,
        "expected_total_frames_per_asset": expected,
        "views": (["level"] if configured_views == "level"
                  else ["level", "topdown"]),
        "assets": assets,
    }


def link_artifact(stage_dir: Path, name: str, target: Path) -> tuple[str | None, str | None]:
    if not (target.exists() or target.is_symlink()):
        return None, None
    suffix = target.suffix if target.is_file() else ""
    link_name = name if name.endswith(suffix) or not suffix else f"{name}{suffix}"
    link = stage_dir / link_name
    relative_target = os.path.relpath(target.resolve(strict=False), stage_dir)
    if link.is_symlink():
        if os.readlink(link) == relative_target:
            return link_name, None
        link.unlink()
    elif link.exists():
        return None, f"not replacing non-symlink {link}"
    link.symlink_to(relative_target, target_is_directory=target.is_dir())
    return link_name, None


def main() -> int:
    args = parse_args()
    run_root = args.run_root.resolve()
    input_dir = args.input_dir.resolve()
    log_prefix = args.log_prefix.resolve()
    organized = run_root / "outputs_by_step"
    organized.mkdir(parents=True, exist_ok=True)

    qa_summary = asset_qa_summary(run_root, args.asset_qa_mode)
    index = {"run_root": str(run_root), "organized_root": str(organized),
             "asset_qa": qa_summary, "stages": []}
    warnings = []
    markdown = [
        "# E2E outputs by step",
        "",
        f"Run root: `{run_root}`",
        "",
        "Large artifacts are symlinked, not copied.",
        "",
        "## Asset QA turntables",
        "",
        f"Mode: `{qa_summary['mode']}` ({qa_summary['mode_source']}); expected per asset: "
        f"`{qa_summary['expected_total_frames_per_asset']}` total frames "
        "(level + top-down).",
        "",
        "| Asset | Generator | Level | Top-down | Actual total |",
        "|---|---|---:|---:|---:|",
        *[
            f"| {asset['id']}"
            f"{' (reused from ' + asset['source_id'] + ')' if asset['source_id'] != asset['id'] else ''} "
            f"| {asset['generator']} | {asset['level_frames']} | "
            f"{asset['topdown_frames']} | {asset['actual_total_frames']} |"
            for asset in qa_summary["assets"]
        ],
        "",
        "## Pipeline stages",
        "",
        "| Step | Status | Inputs | Outputs |",
        "|---:|---|---|---|",
    ]

    for stage in stage_definitions(run_root, input_dir, log_prefix):
        label = f"{stage['number']:02d}_{stage['name']}"
        stage_dir = organized / label
        stage_dir.mkdir(exist_ok=True)
        output_records = []
        required_states = []
        output_links = []
        for output in stage["outputs"]:
            info = path_info(output["path"])
            info.update(name=output["name"], required=output["required"])
            output_records.append(info)
            if output["required"]:
                required_states.append(info["exists"])
            link_name, warning = link_artifact(stage_dir, output["name"], output["path"])
            if warning:
                warnings.append(warning)
            if link_name:
                output_links.append(f"[{output['name']}]({label}/{link_name})")

        log_links = []
        for log_path in stage["logs"]:
            link_name, warning = link_artifact(stage_dir, log_path.name, log_path)
            if warning:
                warnings.append(warning)
            if link_name:
                log_links.append(f"[log:{log_path.name}]({label}/{link_name})")

        if required_states and all(required_states):
            status = "complete"
        elif any(record["exists"] for record in output_records):
            status = "partial"
        else:
            status = "pending"
        inputs = [display_path(value) for value in stage["inputs"]]
        stage_record = {
            "step": stage["number"],
            "name": stage["name"],
            "status": status,
            "inputs": inputs,
            "outputs": output_records,
            "logs": [path_info(path) for path in stage["logs"]],
        }
        index["stages"].append(stage_record)
        markdown.append(
            f"| {stage['number']} {stage['name']} | {status} | "
            f"{'<br>'.join(f'`{value}`' for value in inputs)} | "
            f"{'<br>'.join(output_links + log_links) or '—'} |"
        )

        stage_readme = [
            f"# Step {stage['number']}: {stage['name']}",
            "",
            f"Status: **{status}**",
            "",
            "## Inputs",
            "",
            *[f"- `{value}`" for value in inputs],
            "",
            "## Outputs",
            "",
            *[
                f"- {record['name']}: `{record['path']}` "
                f"({'ready' if record['exists'] else 'pending'})"
                for record in output_records
            ],
            "",
        ]
        if stage["number"] == 5:
            stage_readme += [
                "## Asset QA",
                "",
                f"- Mode: `{qa_summary['mode']}` ({qa_summary['mode_source']})",
                f"- Expected total frames per asset: "
                f"`{qa_summary['expected_total_frames_per_asset']}`",
                *[
                    f"- {asset['id']}"
                    f"{' (reused from ' + asset['source_id'] + ')' if asset['source_id'] != asset['id'] else ''}: "
                    f"{asset['actual_total_frames']} actual "
                    f"({asset['level_frames']} level + {asset['topdown_frames']} top-down)"
                    for asset in qa_summary["assets"]
                ],
                "",
            ]
        (stage_dir / "README.md").write_text("\n".join(stage_readme), encoding="utf-8")

    index["warnings"] = warnings
    (organized / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (organized / "INDEX.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    complete = sum(stage["status"] == "complete" for stage in index["stages"])
    print(f"[organize] {complete}/{len(index['stages'])} stages complete -> {organized}")
    for warning in warnings:
        print(f"[organize] warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

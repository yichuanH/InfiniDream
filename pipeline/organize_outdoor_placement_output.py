#!/usr/bin/env python3
"""Organize an outdoor Composer placement run into a compact review folder.

The folder mirrors the indoor placement review layout:

  01_initial_render.png
  02_box_preview.png
  03_populated_render.png
  objects/<asset_id>.png
  objects_by_factory/<Factory>/<asset_id>.png
  manifest.json
  README.md

Heavy files such as populated/fine .blend are referenced from manifest.json
instead of copied into the review folder.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from paths import asset_store_dir  # noqa: E402

LIBRARY_ROOT = ROOT / "asset_generator" / "library" / "variants"
MAX_OBJECT_PREVIEWS = 3


def _existing_file(path: Path | None) -> Path | None:
    if path is not None and path.is_file():
        return path
    return None


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def _copy_file(src: Path, dst: Path) -> dict:
    if not src.is_file():
        raise FileNotFoundError(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {
        "source": str(src.resolve()),
        "path": str(dst.resolve()),
        "bytes": dst.stat().st_size,
    }


def _clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("_") or "object"


def _starting_factory(entry: dict) -> str:
    factory = entry.get("route_factory") or entry.get("factory")
    return str(factory or "UnknownFactory")


def _add_factory_group_item(
    manifest: dict,
    entry: dict,
    status: str,
    copied: dict | None = None,
    factory_filename: str | None = None,
    render_kind: str | None = None,
) -> None:
    factory = _starting_factory(entry)
    group = manifest["factory_groups"].setdefault(factory, {
        "starting_factory": factory,
        "items": [],
    })
    asset_id = _slug(str(entry.get("id") or entry.get("name") or "object"))
    item = {
        "id": asset_id,
        "name": entry.get("name"),
        "action": entry.get("action"),
        "factory": entry.get("factory"),
        "route_factory": entry.get("route_factory"),
        "library_verdict": entry.get("library_verdict"),
        "library_variant": entry.get("library_variant"),
        "status": status,
        "render_kind": render_kind,
        "factory_filename": factory_filename,
    }
    if copied is not None:
        item["source"] = copied.get("source")
        item["path"] = copied.get("path")
        item["bytes"] = copied.get("bytes")
    group["items"].append(item)


def _case_root(run_root: Path) -> Path:
    return run_root.parent if run_root.name == "run" else run_root


def _camera_angle(run_root: Path) -> str | None:
    choice = _load_json(run_root / "0_camera" / "camera_choice.json") or {}
    angle = choice.get("angle")
    return str(angle) if angle else None


def _baseline_scene_render(run_root: Path) -> Path | None:
    pick = _load_json(run_root / "pick" / "pick.json") or {}
    scene_dir_value = pick.get("scene_dir")
    candidates: list[Path] = []
    if scene_dir_value:
        scene_dir = Path(str(scene_dir_value))
        angle = _camera_angle(run_root)
        if angle:
            candidates.extend([
                scene_dir / f"confirm_{angle}.png",
                scene_dir / "preview" / f"angle_{angle}.png",
            ])
        preview = pick.get("preview_image")
        if preview:
            candidates.append(Path(str(preview)))
        candidates.append(scene_dir / "frames" / "frame_0001.png")
    return _first_existing(candidates)


def _initial_render(run_root: Path, explicit: Path | None) -> Path | None:
    case_root = _case_root(run_root)
    candidates = []
    baseline = _baseline_scene_render(run_root)
    if baseline is not None:
        candidates.append(baseline)
    candidates.extend([
        case_root / "input" / "render.png",
        run_root / "input" / "render.png",
        run_root / "0_camera" / "selected.png",
        run_root / "0_camera" / "official.png",
    ])
    return _existing_file(explicit) or _first_existing(candidates)


def _box_preview(run_root: Path, explicit: Path | None) -> Path | None:
    return _existing_file(explicit) or _first_existing([
        run_root / "placement_review" / "final" / "placement_preview.png",
        run_root / "placement_review" / "placement_preview.png",
        run_root / "3_composer" / "final_placeholder_placement.png",
    ])


def _populated_render(run_root: Path, explicit: Path | None) -> Path | None:
    return _existing_file(explicit) or _first_existing([
        run_root / "render" / "view.png",
        run_root / "render" / "connectivity_view.png",
        run_root / "populated" / "populated_preview.png",
        run_root / "placement_review" / "final" / "placement_preview.png",
        run_root / "placement_review" / "placement_preview.png",
    ])


def _generation_entries(run_root: Path) -> list[dict]:
    genlist = _load_json(run_root / "2_director" / "generation_list.json") or {}
    entries = []
    for section in ("objects", "reused"):
        for row in genlist.get(section, []) or []:
            if isinstance(row, dict):
                copied = dict(row)
                copied["source_section"] = section
                entries.append(copied)
    return entries


def _library_preview(entry: dict) -> Path | None:
    variant = entry.get("library_variant")
    if not variant:
        return None
    factories = [
        entry.get("factory"),
        entry.get("route_factory"),
    ]
    for factory in factories:
        if not factory:
            continue
        preview = LIBRARY_ROOT / str(factory) / str(variant) / "preview" / "final.png"
        if preview.is_file():
            return preview
    return None


def _object_render(run_root: Path, entry: dict) -> tuple[Path | None, str]:
    asset_id = str(entry.get("id") or entry.get("name") or "")
    candidates = []
    if asset_id:
        gen = str(entry.get("generator") or "procedural")
        candidates.extend([
            asset_store_dir(gen, asset_id, "catalog_asset") / "render.png",
            asset_store_dir(gen, asset_id, "catalog_asset") / "final.png",
            asset_store_dir(gen, asset_id, "run") / "final.png",
            run_root / "3_asset" / asset_id / "render.png",
            run_root / "3_asset" / asset_id / "final.png",
            run_root / "3_procedural" / asset_id / "final.png",
            run_root / "3_blender" / asset_id / "final.png",
        ])
    path = _first_existing(candidates)
    if path is not None:
        return path, "run_asset_render"
    path = _library_preview(entry)
    if path is not None:
        return path, "library_preview"
    return None, "missing"


def _placement_tests(run_root: Path) -> list[dict]:
    plan = _load_json(run_root / "3_composer" / "edit_plan.json") or {}
    application = (
        _load_json(
            run_root / "placement_review" / "region_map_initial"
            / "region_application.json")
        or _load_json(
            run_root / "placement_review" / "final"
            / "region_application.json")
        or {}
    )
    applied_by_id: dict[str, list[dict]] = {}
    for row in application.get("applied", []) or []:
        edit_id = str(row.get("edit_id") or "")
        if not edit_id:
            continue
        applied_by_id.setdefault(edit_id, []).append({
            "instance_index": row.get("instance_index"),
            "region_id": row.get("region_id"),
            "cell_id": row.get("cell_id"),
            "surface_type": row.get("surface_type"),
            "surface_name": row.get("surface_name"),
            "screen_position": row.get("screen_position_after"),
            "world_position": row.get("world_position"),
            "placement_yaw": row.get("placement_yaw"),
        })

    tests = []
    for edit in plan.get("hero_edits", []) or []:
        if edit.get("op") not in {"add", "replace"}:
            continue
        edit_id = str(edit.get("code_from") or edit.get("id") or "")
        if not edit_id:
            continue
        spec = edit.get("placement_spec") or {}
        policy = spec.get("surface_policy") or {}
        tests.append({
            "id": edit_id,
            "op": edit.get("op"),
            "factory": edit.get("factory"),
            "count": edit.get("n"),
            "arrangement": spec.get("arrangement"),
            "anchoring": edit.get("anchoring") or spec.get("anchoring"),
            "semantic_target": spec.get("semantic_target"),
            "required_surface_types": (
                policy.get("required_surface_types") or []),
            "forbidden_surface_types": (
                policy.get("forbidden_surface_types") or []),
            "placements": applied_by_id.get(edit_id, []),
        })
    return tests


def _request_placeholder_groups(run_root: Path) -> list[dict]:
    placement_manifest = (
        _load_json(run_root / "manifest.json")
        or _load_json(
            run_root / "placement_review" / "region_map_initial"
            / "selected_manifest.json")
        or {}
    )
    generation_by_id = {
        str(entry.get("id")): entry
        for entry in _generation_entries(run_root)
        if entry.get("id")
    }
    grouped: dict[str, dict] = {}
    for placement in placement_manifest.get("placements", []) or []:
        code_from = str(placement.get("code_from") or "")
        if not code_from:
            continue
        group = grouped.setdefault(code_from, {
            "id": code_from,
            "mode": "replace_existing_placeholders",
            "factory": placement.get("factory"),
            "route_factory": placement.get("factory"),
            "target_factory": None,
            "boxes": 0,
            "added_this_plan": 0,
            "collections": [],
        })
        count = int(placement.get("count") or 0)
        added = int(placement.get("added_this_plan") or 0)
        group["boxes"] += count
        group["added_this_plan"] += added
        if added > 0:
            group["mode"] = "add"
        collection = placement.get("collection")
        if collection and collection not in group["collections"]:
            group["collections"].append(collection)
        entry = generation_by_id.get(code_from) or {}
        group["target_factory"] = entry.get("factory") or group["target_factory"]
        group["route_factory"] = entry.get("route_factory") or group["route_factory"]
        group["library_variant"] = entry.get("library_variant")
    for group in grouped.values():
        if group["added_this_plan"] <= 0:
            group["mode"] = "replace_existing_placeholders"
    return [grouped[key] for key in sorted(grouped)]


def _write_readme(dest: Path, manifest: dict) -> None:
    object_lines = "\n".join(
        f"- objects/{item['filename']} ({item['id']}, {item['render_kind']})"
        for item in manifest.get("objects", [])
    )
    missing_lines = "\n".join(
        f"- {item['id']} ({item.get('factory')}, {item.get('library_variant')})"
        for item in manifest.get("missing_object_renders", [])
    )
    omitted_lines = "\n".join(
        f"- {item['id']} ({item.get('factory')}, {item.get('library_variant')})"
        for item in manifest.get("omitted_object_renders", [])
    )
    test_lines = "\n".join(
        "- {id}: {op}; {surfaces}; {target}; placements={count}".format(
            id=item["id"],
            op=item.get("op") or "edit",
            surfaces="+".join(item.get("required_surface_types") or ["any"]),
            target=item.get("semantic_target") or "unspecified target",
            count=len(item.get("placements") or []),
        )
        for item in manifest.get("placement_tests", [])
    )
    factory_lines = "\n".join(
        f"- objects_by_factory/{_slug(factory)}/ ({len(group.get('items') or [])} items)"
        for factory, group in sorted(manifest.get("factory_groups", {}).items())
    )
    request_lines = "\n".join(
        (
            f"- {item['id']}: {item['mode']}; boxes={item['boxes']}; "
            f"factory={item.get('factory')}; target={item.get('target_factory')}; "
            f"collections={len(item.get('collections') or [])}"
        )
        for item in manifest.get("request_placeholder_groups", [])
    )
    text = f"""# Outdoor Composer Placement Review

Source run:
{manifest['source_run']}

Images:
- 01_initial_render.png
- 02_box_preview.png
- 03_populated_render.png

Object renders:
{object_lines or "- none"}

Object renders by starting factory:
{factory_lines or "- none"}

Missing object renders:
{missing_lines or "- none"}

Omitted object renders:
{omitted_lines or "- none"}

Composer request placeholder groups:
{request_lines or "- none"}

Placement tests:
{test_lines or "- none"}

Heavy files are not copied. See manifest.json for populated/fine blend paths and
provenance.
"""
    (dest / "README.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--input-render", type=Path)
    parser.add_argument("--box-preview", type=Path)
    parser.add_argument("--populated-render", type=Path)
    parser.add_argument("--max-object-previews", type=int, default=MAX_OBJECT_PREVIEWS)
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    dest = args.dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    objects_dir = dest / "objects"
    factory_objects_dir = dest / "objects_by_factory"
    _clear_dir(objects_dir)
    _clear_dir(factory_objects_dir)

    initial = _initial_render(run_root, args.input_render)
    box = _box_preview(run_root, args.box_preview)
    populated = _populated_render(run_root, args.populated_render)

    missing = []
    if initial is None:
        missing.append("initial render")
    if box is None:
        missing.append("box preview")
    if populated is None:
        missing.append("populated render")
    if missing:
        raise SystemExit("missing required artifact(s): " + ", ".join(missing))

    manifest = {
        "format": "outdoor_composer_placement_review_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(run_root),
        "populated_blend": (
            str((run_root / "populated" / "scene.blend").resolve())
            if (run_root / "populated" / "scene.blend").is_file()
            else None
        ),
        "fine_blend": (
            str((run_root / "fine" / "scene.blend").resolve())
            if (run_root / "fine" / "scene.blend").is_file()
            else None
        ),
        "final_contract": (
            str((run_root / "final_contract.json").resolve())
            if (run_root / "final_contract.json").is_file()
            else None
        ),
        "final_contract_ok": (
            str((run_root / "final_contract.ok").resolve())
            if (run_root / "final_contract.ok").is_file()
            else None
        ),
        "images": {
            "initial": _copy_file(initial, dest / "01_initial_render.png"),
            "box_preview": _copy_file(box, dest / "02_box_preview.png"),
            "populated": _copy_file(populated, dest / "03_populated_render.png"),
        },
        "objects": [],
        "factory_groups": {},
        "omitted_object_renders": [],
        "missing_object_renders": [],
        "request_placeholder_groups": _request_placeholder_groups(run_root),
        "placement_tests": _placement_tests(run_root),
    }

    for entry in _generation_entries(run_root):
        asset_id = _slug(str(entry.get("id") or entry.get("name") or "object"))
        factory = _starting_factory(entry)
        factory_slug = _slug(factory)
        if len(manifest["objects"]) >= max(0, args.max_object_previews):
            manifest["omitted_object_renders"].append(entry)
            _add_factory_group_item(manifest, entry, "omitted_max_preview_limit")
            continue
        render, kind = _object_render(run_root, entry)
        if render is None:
            manifest["missing_object_renders"].append(entry)
            _add_factory_group_item(manifest, entry, "missing_render")
            continue
        copied = _copy_file(render, objects_dir / f"{asset_id}.png")
        factory_filename = f"{asset_id}.png"
        factory_copied = _copy_file(
            render,
            factory_objects_dir / factory_slug / factory_filename,
        )
        copied.update({
            "id": asset_id,
            "name": entry.get("name"),
            "factory": entry.get("factory"),
            "route_factory": entry.get("route_factory"),
            "starting_factory": factory,
            "library_verdict": entry.get("library_verdict"),
            "library_variant": entry.get("library_variant"),
            "render_kind": kind,
            "filename": f"{asset_id}.png",
            "factory_path": factory_copied["path"],
        })
        manifest["objects"].append(copied)
        _add_factory_group_item(
            manifest,
            entry,
            "copied",
            factory_copied,
            f"{factory_slug}/{factory_filename}",
            kind,
        )

    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_readme(dest, manifest)

    print(f"[outdoor-organize] wrote {dest}")
    print(f"[outdoor-organize] objects={len(manifest['objects'])}")
    if manifest["missing_object_renders"]:
        print(
            "[outdoor-organize] missing object renders="
            f"{len(manifest['missing_object_renders'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

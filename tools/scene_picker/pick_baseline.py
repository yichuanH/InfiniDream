#!/usr/bin/env python
"""Text prompt -> two scene types -> best existing seed preview.

This is the light-weight front half of text-to-scene:
  1. choose indoor/outdoor and the two closest scene groups from the fixed catalog;
  2. show their ten already-rendered seed previews to a vision model;
  3. write a baseline_pick.json plus an input/ folder that later stages can reuse.

It deliberately does not run Infinigen or the edit pipeline.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent   # repo root (this file lives in tools/<x>/)
SCENES = ROOT / "scenes"
SCENES_INSIDE = ROOT / "scenes_inside"
CAMERA_MANIFEST = SCENES / "CAMERA_MANIFEST.tsv"

sys.path.insert(0, str(HERE))
from catalog import INDOOR, OUTDOOR, as_prompt_block  # noqa: E402
from pick_scene import OPUS_MODEL, _claude  # noqa: E402

sys.path.insert(0, str(ROOT))
from project_namer import name_project  # noqa: E402


SCENE_TOP_K = 2
PICKER_OUTPUT_ROOT = ROOT / "output" / "_picker"

SCENE_SYSTEM = """You are the SCENE GROUP PICKER for a text-to-scene editing pipeline.

Given a user prompt, first decide whether it is an outdoor natural environment or an indoor
room. Then rank the TWO closest existing Infinigen scene groups within that location. The
next stage will visually compare the five random seeds for each selected scene group, so
your second choice should be a genuinely plausible alternative, especially when biomes can
overlap visually (for example forest/mountain/plain/river/cliff/coast).

Do not create a new scene name. Only use exact names from the supplied catalog. Prefer
outdoor if the prompt is ambiguous. Pick underwater entries only for genuinely submerged
scenes. For indoor prompts, prefer supported room types unless an unsupported room type is
unmistakably requested.

Return ONE JSON object and nothing else:
{
  "location": "outdoor" | "indoor",
  "ranked_scenes": [
    {
      "scene": "<exact catalog name>",
      "rank": 1,
      "reason": "<one short sentence: why this is a strong baseline group>",
      "match": <0-100 integer>,
      "gap": "<what this native scene group will still lack before editing, or 'none'>",
      "caveat": "<capability limit that applies, or 'none'>"
    },
    {
      "scene": "<exact catalog name>",
      "rank": 2,
      "reason": "<one short sentence: why this alternative might be visually useful>",
      "match": <0-100 integer>,
      "gap": "<what this native scene group will still lack before editing, or 'none'>",
      "caveat": "<capability limit that applies, or 'none'>"
    }
  ]
}
"""

SEED_SYSTEM = """You are the BASELINE SEED PICKER for a text-to-scene editing pipeline.

Two scene groups have already been selected from the fixed Infinigen catalog. You now choose
which of their already-generated random seeds is the best starting point for editing. You
may choose from either scene group; do not assume rank 1 is always visually better.

Judge by visual fit to the user's requested final scene, but remember this is only a
baseline: objects that the later edit pipeline can add, delete, or replace do not need to
already exist. Prefer the seed whose terrain/room layout, camera framing, object density,
open space, support surfaces, and overall composition would require the least downstream
editing. Penalize views that are too occluded, too empty for the request, badly framed, or
missing the kind of usable region the edits need.

Return ONE JSON object and nothing else:
{
  "candidate_id": "<exact candidate_id from the list>",
  "scene": "<exact scene group name for the selected candidate>",
  "seed": <integer>,
  "score": <0-100 integer>,
  "reason": "<one short sentence explaining why this seed is the best baseline>",
  "visual_gap": "<what this baseline still lacks before editing, or 'none'>",
  "ranked": [
    {"candidate_id":"...", "scene":"...", "seed":0, "score":0-100, "reason":"short visual reason"}
  ]
}
"""


def pick_scene_candidates(prompt: str, *, k: int = SCENE_TOP_K,
                          model: str = OPUS_MODEL) -> dict:
    user = (
        f"{as_prompt_block()}\n\n"
        f"=== PROMPT ===\n{prompt}\n\n"
        f"Rank the closest {k} scene groups now. Return the JSON only."
    )
    response = _claude().messages.create(
        model=model,
        max_tokens=1400,
        system=SCENE_SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text, label="scene group picker")


def _abs(p: Path | None) -> str | None:
    return str(p.resolve()) if p is not None else None


def _read_camera_manifest(path: Path = CAMERA_MANIFEST) -> dict[tuple[str, int], dict]:
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return {}
    header = lines[0].split("\t")
    rows = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        row = dict(zip(header, values))
        try:
            key = (row["biome"], int(row["seed"]))
        except (KeyError, ValueError):
            continue
        rows[key] = row
    return rows


def _first_existing(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def _preview_for(seed_dir: Path, *, angle: str | None, indoor: bool) -> tuple[Path | None, str]:
    preview_dir = seed_dir / "preview"
    preferred: list[Path] = []
    if angle:
        preferred.append(preview_dir / f"angle_{angle}.png")
    if indoor:
        preferred.extend([
            preview_dir / "angle_corner.png",
            preview_dir / "angle_wide.png",
            preview_dir / "angle_ground.png",
            preview_dir / "contact_sheet.png",
        ])
    else:
        preferred.extend([
            preview_dir / "contact_sheet.png",
            seed_dir / "frames" / "frame_0001.png",
        ])
    p = _first_existing(preferred)
    role = "selected_camera" if p and angle and p.name == f"angle_{angle}.png" else "representative"
    return p, role


def outdoor_seed_candidates(
    scene: str,
    *,
    scenes_dir: Path = SCENES,
    manifest_path: Path = CAMERA_MANIFEST,
) -> list[dict]:
    """Return the five available outdoor seed candidates for one biome."""
    manifest = _read_camera_manifest(manifest_path)
    out = []
    for seed in range(5):
        seed_name = f"seed{seed}"
        seed_dir = scenes_dir / scene / seed_name
        row = manifest.get((scene, seed), {})
        angle = row.get("angle") or "ground"
        rel_blend = row.get("blend")
        blend = scenes_dir / rel_blend if rel_blend else seed_dir / "fine" / "scene.blend"
        coarse_blend = seed_dir / "coarse" / "scene.blend"
        camera_choice = seed_dir / "camera_choice.json"
        preview, preview_role = _preview_for(seed_dir, angle=angle, indoor=False)
        available = (
            seed_dir.is_dir()
            and coarse_blend.is_file()
            and blend.is_file()
            and preview is not None
            and (not row or row.get("status") == "OK")
        )
        if not available:
            continue
        out.append({
            "candidate_id": f"{scene}_seed{seed}",
            "location": "outdoor",
            "scene": scene,
            "seed": seed,
            "angle": angle,
            "scene_dir": _abs(seed_dir),
            "coarse_blend": _abs(coarse_blend),
            "baseline_blend": _abs(blend),
            "camera_choice": _abs(camera_choice) if camera_choice.is_file() else None,
            "preview_image": _abs(preview),
            "preview_role": preview_role,
            "size_gb": row.get("size_gb"),
            "cam_match": row.get("cam_match"),
        })
    return out


def indoor_seed_candidates(
    scene: str,
    *,
    scenes_inside_dir: Path = SCENES_INSIDE,
) -> list[dict]:
    """Return the available indoor seed candidates for one room type."""
    info = INDOOR.get(scene)
    if not info:
        return []
    room_dir_name = info.get("semantics") or scene
    out = []
    for seed in range(5):
        seed_dir = scenes_inside_dir / room_dir_name / f"seed{seed}"
        blend = seed_dir / "coarse" / "scene.blend"
        preview, preview_role = _preview_for(seed_dir, angle=None, indoor=True)
        if not (seed_dir.is_dir() and blend.is_file() and preview is not None):
            continue
        out.append({
            "candidate_id": f"{scene}_seed{seed}",
            "location": "indoor",
            "scene": scene,
            "room_type": room_dir_name,
            "seed": seed,
            "angle": None,
            "scene_dir": _abs(seed_dir),
            "coarse_blend": _abs(blend),
            "baseline_blend": _abs(blend),
            "camera_choice": None,
            "preview_image": _abs(preview),
            "preview_role": preview_role,
            "supported_room_type": bool(info.get("supported", True)),
        })
    return out


def seed_candidates(scene_pick: dict) -> list[dict]:
    if scene_pick["location"] == "outdoor":
        return outdoor_seed_candidates(scene_pick["scene"])
    return indoor_seed_candidates(scene_pick["scene"])


def _encode_image(path: str) -> dict:
    p = Path(path)
    media_type = mimetypes.guess_type(p.name)[0] or "image/png"
    return {
        "type": "base64",
        "media_type": media_type,
        "data": base64.standard_b64encode(p.read_bytes()).decode("ascii"),
    }


def _extract_json(text: str, *, label: str = "seed picker") -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise SystemExit(f"{label} did not return JSON:\n{text}")
    return json.loads(match.group(0))


def _catalog_for(location: str) -> dict:
    return OUTDOOR if location == "outdoor" else INDOOR


def _default_scene_order(location: str) -> list[str]:
    if location == "indoor":
        return ["living-room", "kitchen", "bedroom", "dining-room", "bathroom"]
    return [
        "forest",
        "mountain",
        "plain",
        "coast",
        "river",
        "cliff",
        "canyon",
        "desert",
        "arctic",
        "snowy_mountain",
        "kelp_forest",
        "coral_reef",
        "under_water",
        "cave",
    ]


def _valid_location_for_items(raw_location, items: list[dict]) -> str:
    if raw_location in ("outdoor", "indoor"):
        preferred = str(raw_location)
        if any(item.get("scene") in _catalog_for(preferred) for item in items):
            return preferred
        other = "indoor" if preferred == "outdoor" else "outdoor"
        if any(item.get("scene") in _catalog_for(other) for item in items):
            print(f"  [warn] location {raw_location!r} did not match ranked scenes -> {other!r}")
            return other
        return preferred

    indoor_count = sum(1 for item in items if item.get("scene") in INDOOR)
    outdoor_count = sum(1 for item in items if item.get("scene") in OUTDOOR)
    return "indoor" if indoor_count > outdoor_count else "outdoor"


def _scene_pick_from_item(location: str, scene: str, item: dict, rank: int) -> dict:
    entry = _catalog_for(location)[scene]
    out = {
        "rank": rank,
        "location": location,
        "scene": scene,
        "reason": str(item.get("reason", "")),
        "match": _clamped_score(item.get("match")),
        "gap": str(item.get("gap", "")),
        "caveat": str(item.get("caveat", "none") or "none"),
        "supported": bool(entry.get("supported", True)),
        "cue": entry["cue"],
    }
    if entry.get("caveat") and out["caveat"] == "none":
        out["caveat"] = entry["caveat"]
    if entry.get("underwater"):
        out["underwater"] = True
    return out


def validate_scene_candidates(raw: dict, *, k: int = SCENE_TOP_K) -> list[dict]:
    """Validate the model-ranked scene groups against the fixed catalog."""
    raw_items = raw.get("ranked_scenes")
    if not isinstance(raw_items, list):
        raw_items = []
    if raw.get("scene"):
        raw_items.insert(0, {
            "scene": raw.get("scene"),
            "reason": raw.get("reason", ""),
            "match": raw.get("match", 0),
            "gap": raw.get("gap", ""),
            "caveat": raw.get("caveat", "none"),
        })
    if raw.get("runner_up"):
        raw_items.append({
            "scene": raw.get("runner_up"),
            "reason": "runner-up from legacy picker output",
            "match": 0,
            "gap": "",
            "caveat": "none",
        })

    location = _valid_location_for_items(raw.get("location"), raw_items)
    table = _catalog_for(location)
    picks = []
    seen = set()
    for item in raw_items:
        scene = item.get("scene")
        if scene not in table or scene in seen:
            continue
        seen.add(scene)
        picks.append(_scene_pick_from_item(location, scene, item, len(picks) + 1))
        if len(picks) >= k:
            break

    for scene in _default_scene_order(location):
        if len(picks) >= k:
            break
        if scene in table and scene not in seen:
            seen.add(scene)
            picks.append(_scene_pick_from_item(
                location,
                scene,
                {
                    "reason": "fallback catalog candidate added after validation",
                    "match": 0,
                    "gap": "",
                    "caveat": "none",
                },
                len(picks) + 1,
            ))

    return picks


def seed_candidates_for_scene_picks(
    scene_picks: list[dict],
    *,
    k: int = SCENE_TOP_K,
) -> tuple[list[dict], list[dict]]:
    """Collect seed candidates, filling skipped scene groups until k groups are usable."""
    all_candidates = []
    usable_scene_picks = []
    seen = set()

    def add_scene_pick(scene_pick: dict) -> bool:
        if scene_pick["scene"] in seen:
            return False
        seen.add(scene_pick["scene"])
        candidates = seed_candidates(scene_pick)
        if not candidates:
            print(
                f"  [warn] no rendered seeds for "
                f"{scene_pick['location']}/{scene_pick['scene']} -> skipped")
            return False
        usable = dict(scene_pick)
        usable["rank"] = len(usable_scene_picks) + 1
        usable_scene_picks.append(usable)
        for c in candidates:
            row = dict(c)
            row["scene_rank"] = usable["rank"]
            row["scene_match"] = usable["match"]
            all_candidates.append(row)
        return True

    for scene_pick in scene_picks:
        add_scene_pick(scene_pick)
        if len(usable_scene_picks) >= k:
            return usable_scene_picks, all_candidates

    location = scene_picks[0]["location"] if scene_picks else "outdoor"
    for scene in _default_scene_order(location):
        if len(usable_scene_picks) >= k:
            break
        if scene in seen:
            continue
        add_scene_pick(_scene_pick_from_item(
            location,
            scene,
            {
                "reason": "fallback rendered scene group added to keep two visual groups",
                "match": 0,
                "gap": "",
                "caveat": "none",
            },
            len(usable_scene_picks) + 1,
        ))
    return usable_scene_picks, all_candidates


def pick_seed(
    prompt: str,
    scene_picks: list[dict],
    candidates: list[dict],
    *,
    model: str = OPUS_MODEL,
) -> dict:
    if not candidates:
        scenes = ", ".join(f"{p['location']}/{p['scene']}" for p in scene_picks)
        raise SystemExit(f"no rendered seed candidates for selected scene groups: {scenes}")

    table = [{
        "candidate_id": c["candidate_id"],
        "location": c["location"],
        "scene": c["scene"],
        "scene_rank": c.get("scene_rank"),
        "scene_match": c.get("scene_match"),
        "seed": c["seed"],
        "angle": c.get("angle"),
        "preview_role": c.get("preview_role"),
    } for c in candidates]
    content = [{
        "type": "text",
        "text": (
            f"USER REQUEST:\n{prompt}\n\n"
            "SELECTED SCENE GROUPS:\n"
            + json.dumps(scene_picks, ensure_ascii=False, indent=2)
            + "\n\nCANDIDATES:\n"
            + json.dumps(table, ensure_ascii=False, indent=2)
            + "\n\nImages follow. Each image is labelled by exact candidate_id."
        ),
    }]
    for c in candidates:
        content.append({
            "type": "text",
            "text": (
                f"CANDIDATE {c['candidate_id']} | scene={c['scene']} | "
                f"scene_rank={c.get('scene_rank')} | seed={c['seed']} | "
                f"angle={c.get('angle') or 'representative'} | "
                f"preview_role={c.get('preview_role')}"
            ),
        })
        content.append({"type": "image", "source": _encode_image(c["preview_image"])})

    response = _claude().messages.create(
        model=model,
        max_tokens=1800,
        system=SEED_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)


def _clamped_score(value) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def validate_seed_pick(raw: dict, candidates: list[dict]) -> dict:
    """Deterministically map the model's reply back to one existing candidate."""
    if not candidates:
        raise ValueError("cannot validate a seed pick without candidates")

    by_id = {c["candidate_id"]: c for c in candidates}
    by_scene_seed = {(c.get("scene"), int(c["seed"])): c for c in candidates}
    seed_counts: dict[int, int] = {}
    for c in candidates:
        seed_counts[int(c["seed"])] = seed_counts.get(int(c["seed"]), 0) + 1

    def resolve_candidate(item: dict) -> dict | None:
        selected = by_id.get(item.get("candidate_id"))
        if selected is not None:
            return selected
        scene = item.get("scene")
        try:
            seed = int(item.get("seed"))
        except (TypeError, ValueError):
            return None
        if scene:
            selected = by_scene_seed.get((scene, seed))
            if selected is not None:
                return selected
        if seed_counts.get(seed) == 1:
            return next(c for c in candidates if int(c["seed"]) == seed)
        return None

    selected = resolve_candidate(raw)
    fallback_reason = None
    if selected is None:
        selected = candidates[0]
        fallback_reason = (
            "model returned no valid candidate_id or unambiguous scene/seed; "
            "fell back to first available seed"
        )

    ranked = []
    seen = set()
    for item in raw.get("ranked", []) if isinstance(raw.get("ranked"), list) else []:
        c = resolve_candidate(item)
        if c is None or c["candidate_id"] in seen:
            continue
        seen.add(c["candidate_id"])
        ranked.append({
            "candidate_id": c["candidate_id"],
            "scene": c.get("scene"),
            "seed": c["seed"],
            "score": _clamped_score(item.get("score")),
            "reason": str(item.get("reason", "")),
        })
    if selected["candidate_id"] not in seen:
        ranked.insert(0, {
            "candidate_id": selected["candidate_id"],
            "scene": selected.get("scene"),
            "seed": selected["seed"],
            "score": _clamped_score(raw.get("score")),
            "reason": str(raw.get("reason", "")),
        })

    result = {
        "candidate_id": selected["candidate_id"],
        "scene": selected.get("scene"),
        "seed": selected["seed"],
        "score": _clamped_score(raw.get("score")),
        "reason": str(raw.get("reason", "")),
        "visual_gap": str(raw.get("visual_gap", "")) or "none",
        "ranked": ranked,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def write_prepared_input(prompt: str, selected: dict, out_dir: Path) -> Path:
    """Write an input folder that records the selected baseline for later stages."""
    input_dir = out_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "prompt.txt").write_text(prompt.strip() + "\n", encoding="utf-8")
    links = {
        "scene.blend": selected["baseline_blend"],
        "render.png": selected["preview_image"],
    }
    if selected.get("camera_choice"):
        links["camera_choice.json"] = selected["camera_choice"]
    for name, target in links.items():
        link = input_dir / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(Path(target))
    return input_dir


def init_picker_project(project: str, source_input: str) -> Path:
    project_root = PICKER_OUTPUT_ROOT / project
    project_root.mkdir(parents=True, exist_ok=True)
    meta = project_root / "project.json"
    if not meta.exists():
        meta.write_text(json.dumps(
            {
                "project": project,
                "input": source_input,
                "created": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n", encoding="utf-8")
    return project_root


def choose_baseline(prompt: str, *, model: str = OPUS_MODEL) -> dict:
    raw_scene_pick = pick_scene_candidates(prompt, k=SCENE_TOP_K, model=model)
    validated_scene_picks = validate_scene_candidates(raw_scene_pick, k=SCENE_TOP_K)
    scene_picks, candidates = seed_candidates_for_scene_picks(
        validated_scene_picks, k=SCENE_TOP_K)
    raw_seed_pick = pick_seed(prompt, scene_picks, candidates, model=model)
    seed_pick = validate_seed_pick(raw_seed_pick, candidates)
    selected = next(c for c in candidates if c["candidate_id"] == seed_pick["candidate_id"])
    return {
        "version": 2,
        "created": datetime.now().isoformat(timespec="seconds"),
        "prompt": prompt,
        "scene_pick": scene_picks[0],
        "scene_picks": scene_picks,
        "validated_scene_picks": validated_scene_picks,
        "usable_scene_picks": scene_picks,
        "seed_pick": seed_pick,
        "selected": selected,
        "candidates": candidates,
        "raw_scene_pick": raw_scene_pick,
        "raw_seed_pick": raw_seed_pick,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pick the best existing Infinigen baseline seed for an input text prompt.")
    ap.add_argument("prompt", nargs="+", help="the desired final scene text")
    ap.add_argument("--model", default=OPUS_MODEL)
    ap.add_argument("--out-dir", help="output dir (default: output/_picker/<slug>/2_scene_picker)")
    ap.add_argument("--project", help="project slug; default uses project_namer")
    ap.add_argument("--no-prepared-input", action="store_true",
                    help="only write JSON; do not create input/ symlinks")
    args = ap.parse_args()

    prompt = " ".join(args.prompt).strip()
    project = args.project or name_project(prompt)
    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        project_root = init_picker_project(project, prompt)
        out_dir = project_root / "2_scene_picker"
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[baseline] prompt: {prompt}")
    print(f"[baseline] output: {out_dir}")
    result = choose_baseline(prompt, model=args.model)

    scene_pick_path = out_dir / "scene_pick.json"
    scene_picks_path = out_dir / "scene_picks.json"
    seed_pick_path = out_dir / "seed_pick.json"
    baseline_path = out_dir / "baseline_pick.json"
    scene_pick_path.write_text(
        json.dumps(result["scene_pick"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    scene_picks_path.write_text(
        json.dumps(result["scene_picks"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    seed_pick_path.write_text(
        json.dumps(result["seed_pick"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    input_dir = None
    if not args.no_prepared_input:
        input_dir = write_prepared_input(prompt, result["selected"], out_dir)
        result["prepared_input_dir"] = _abs(input_dir)
    baseline_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    selected = result["selected"]
    print("\n=== BASELINE PICK ===")
    print(f"  location : {result['scene_pick']['location']}")
    scenes = ", ".join(
        f"{p['rank']}:{p['scene']}({p['match']}%)" for p in result["scene_picks"])
    print(f"  scenes   : {scenes}")
    print(f"  images   : {len(result['candidates'])} seed previews")
    print(f"  seed     : {selected['seed']}")
    print(f"  candidate: {selected['candidate_id']}")
    print(f"  preview  : {selected['preview_image']}")
    print(f"  blend    : {selected['baseline_blend']}")
    if selected.get("camera_choice"):
        print(f"  camera   : {selected['camera_choice']}")
    print(f"  reason   : {result['seed_pick']['reason']}")
    print(f"  gap      : {result['seed_pick']['visual_gap']}")
    if input_dir:
        print(f"  input dir: {input_dir}")
    print(f"\n[baseline] baseline_pick.json -> {baseline_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Scene Picker: text prompt -> the closest native Infinigen scene (biome or room type).

★ This is a SINGLE API call, not an agent ★ — the candidate set is a fixed catalog of 14
biomes + 11 room types, fully known up front, so there is nothing for a tool-using agent to
go and discover. The LLM only judges; every decision it makes is then validated against the
catalog deterministically (same pattern as director.py's finalize()).

Usage:
  python pick_scene.py "a snowy mountain pass at dawn"
  python pick_scene.py "a cozy kitchen with morning light" --run
  python pick_scene.py "..." --seed 7 --gpu 4 --out-dir output/mykitchen/2_scene_picker
  python pick_scene.py "..." --dry-run          # print the command, don't execute
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # BeyondInfinigen/
sys.path.insert(0, str(HERE))
from catalog import INDOOR, OUTDOOR, as_prompt_block  # noqa: E402

sys.path.insert(0, str(ROOT))
from paths import init_project, project_dir, stage_dir  # noqa: E402
from project_namer import name_project  # noqa: E402

OUTPUT_ROOT = ROOT / "output"                        # 專案優先: OUTPUT_ROOT/<slug>/2_scene_picker/
OPUS_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# Infinigen (0726 bench 用的同一組路徑)
INF_ROOT = Path(os.environ.get("INFINIGEN_ROOT", ROOT / "third_party" / "infinigen"))
INFINIGEN_PY = os.environ.get("INFINIGEN_PY", os.environ.get("BI_PYTHON", sys.executable))

# ★ 輕量渲染設定 —— 與 0726 bench B 版一致，讓單場景 ~1h 而不是 ~14h ★
LIGHTWEIGHT = [
    "compose_nature.inview_distance=40",
    "infinigen.terrain.elements.landtiles.load_assets.on_the_fly_instances=1",
    "infinigen.terrain.elements.caves.load_assets.on_the_fly_instances=1",
]


# ------------------------------------------------------------------------------- env
def _env() -> dict:
    merged: dict = {}
    for f in (ROOT / ".env", ROOT.parent / "ExtraWorld" / ".env",
              ROOT.parent / "infinigen_o" / ".env", ROOT.parent / "OtherTest" / ".env"):
        if f.is_file():
            merged.update({k: v for k, v in dotenv_values(f).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


ENV = _env()


def _claude():
    import anthropic
    key = ENV.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found (checked BeyondInfinigen/.env, "
                         "ExtraWorld/.env, infinigen_o/.env, OtherTest/.env, env)")
    base = (ENV.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):              # the anthropic SDK appends /v1 itself
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


# ------------------------------------------------------------------------------ pick
SYSTEM = """You are the SCENE PICKER for a native-Infinigen 3D world pipeline. Given a text
prompt describing a scene, choose the ONE existing Infinigen scene that is CLOSEST to it.

You are given the full catalog: OUTDOOR biomes (each a scene_type gin) and INDOOR room types.
You do not create anything — you only pick the closest existing starting point, which the
pipeline then renders as-is.

Decide in this order:
  1. LOCATION — is the prompt describing an outdoor natural environment, or the inside of a
     built room? Judge from what the prompt actually says. If it is ambiguous, prefer outdoor
     (the outdoor path is the better-supported one).
  2. SCENE — within that location, the single closest entry by TERRAIN / SETTING, matching on
     what the place fundamentally IS, not on incidental words. Never invent a name.

Use the resolved asset lists as FACTS about each biome:
  - A biome's NEVER list is a hard fact — that content will not appear. Do not pick a biome
    and then hope for something on its NEVER list; either pick a biome that has it, or pick
    the closest one and record the shortfall honestly in `gap`.
  - ALWAYS / usually / sometimes are spawn chances, not guarantees of framing. Something on
    the "sometimes" list may well be absent in any given seed.
  - Never rule a biome out merely because its asset list is short. A sparse biome is exactly
    right for a sparse prompt. Judge terrain/setting fit FIRST, then check the asset lists to
    see what the prompt asks for that the biome will not deliver.
  - Pick an [UNDERWATER] biome ONLY if the prompt is genuinely submerged.
  - An entry marked [NO FURNITURE CONSTRAINTS] will render a nearly empty room — pick it only
    if it is unmistakably what was asked for, and say so in `caveat`.

Also report, honestly, how far the prompt is from the chosen scene. This gap is the INPUT to
a later adaptation stage, so do not flatter the match.

Return ONE JSON object and nothing else:
{
  "location": "outdoor" | "indoor",
  "scene": "<exact catalog name>",
  "reason": "<one short sentence: why this is the closest>",
  "match": <0-100 int: how well the native scene alone realizes the prompt>,
  "gap": "<what the prompt asks for that this native scene will NOT deliver, or 'none'>",
  "caveat": "<any capability limit that applies to this pick, or 'none'>",
  "runner_up": "<second-closest catalog name, or null>"
}"""


def pick(prompt: str) -> dict:
    user = (f"{as_prompt_block()}\n\n"
            f"=== PROMPT ===\n{prompt}\n\n"
            f"Pick the closest scene now. Return the JSON only.")
    resp = _claude().messages.create(
        model=OPUS_MODEL, max_tokens=800, system=SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise SystemExit(f"picker did not return JSON:\n{text}")
    return json.loads(m.group(0))


def validate(d: dict) -> dict:
    """★ Deterministic validation — the LLM judges, the catalog decides. ★
    Mirrors director.py's finalize(): never let an invented/mismatched name through."""
    loc = d.get("location")
    scene = d.get("scene")

    if loc not in ("outdoor", "indoor"):
        loc = "indoor" if scene in INDOOR else "outdoor"
        print(f"  [warn] location {d.get('location')!r} invalid -> inferred {loc!r}")

    table = OUTDOOR if loc == "outdoor" else INDOOR
    if scene not in table:
        # the name may simply be in the other table -> trust the name, fix the location
        other = INDOOR if loc == "outdoor" else OUTDOOR
        if scene in other:
            loc = "indoor" if loc == "outdoor" else "outdoor"
            table = other
            print(f"  [warn] {scene!r} belongs to the {loc} table -> location corrected")
        else:
            print(f"  [warn] {scene!r} is not in the catalog -> falling back to 'forest'")
            loc, scene, table = "outdoor", "forest", OUTDOOR
            d["caveat"] = f"picker returned an unknown scene; fell back to forest"

    entry = table[scene]
    match = d.get("match")
    match = int(match) if isinstance(match, (int, float)) or str(match).isdigit() else 0

    out = {
        "location": loc,
        "scene": scene,
        "reason": d.get("reason", ""),
        "match": match,
        "gap": d.get("gap", ""),
        "caveat": d.get("caveat", "none"),
        "runner_up": d.get("runner_up"),
        "supported": bool(entry.get("supported", True)),
        "cue": entry["cue"],
    }
    if not out["supported"]:
        print(f"  [warn] {scene!r} has no furniture constraints — expect a near-empty room")
    if entry.get("caveat"):
        print(f"  [warn] {scene!r}: {entry['caveat']}")
    if entry.get("underwater"):
        out["underwater"] = True
    return out


# ----------------------------------------------------------------------------- run
def build_commands(pick: dict, out_dir: Path, seed: int, lightweight: bool) -> list[list[str]]:
    """The native Infinigen commands that realize the pick. Outdoor and indoor are two
    DIFFERENT entrypoints — generate_nature.py vs generate_indoors.py."""
    coarse, fine = out_dir / "coarse", out_dir / "fine"
    extra = ["-p"] + LIGHTWEIGHT if lightweight else []

    if pick["location"] == "outdoor":
        base = [INFINIGEN_PY, "-m", "infinigen_examples.generate_nature",
                "--seed", str(seed)]
        return [
            base + ["--task", "coarse", "--output_folder", str(coarse),
                    "-g", pick["scene"]] + extra,
            base + ["--task", "populate", "--input_folder", str(coarse),
                    "--output_folder", str(fine), "-g", pick["scene"]] + extra,
            base + ["--task", "fine_terrain", "--input_folder", str(fine),
                    "--output_folder", str(fine), "-g", pick["scene"]] + extra,
        ]

    # ---- indoor ----
    # ★ Room type cannot be specified natively ★: generate_indoors.py:167-176 hardcodes
    # np.random.choice over 5 room types. We pass the choice through a gin override that
    # room_overlay.gin consumes; see scene_picker/README.md for the one-line patch it needs.
    sem = INDOOR[pick["scene"]]["semantics"]
    base = [INFINIGEN_PY, "-m", "infinigen_examples.generate_indoors",
            "--seed", str(seed)]
    overrides = ["-p", "compose_indoors.restrict_single_supported_roomtype=True",
                 f"compose_indoors.force_roomtype='{sem}'"]
    return [
        base + ["--task", "coarse", "--output_folder", str(coarse),
                "-g", "singleroom"] + overrides,
        base + ["--task", "populate", "--input_folder", str(coarse),
                "--output_folder", str(fine), "-g", "singleroom"] + overrides,
    ]


def run(cmds: list[list[str]], out_dir: Path, gpu: str) -> bool:
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    env["PYTHONPATH"] = f"{INF_ROOT}:{env.get('PYTHONPATH', '')}"
    for cmd in cmds:
        task = cmd[cmd.index("--task") + 1]
        log = out_dir / f"{task}.log"
        print(f"[picker] {task} ... (GPU {gpu}, log -> {log.name})")
        t0 = time.time()
        with open(log, "w") as lf:
            rc = subprocess.run(cmd, cwd=str(INF_ROOT), env=env,
                                stdout=lf, stderr=subprocess.STDOUT).returncode
        dt = time.time() - t0
        print(f"[picker] {task} rc={rc}  ({dt / 60:.1f}m)")
        if rc != 0:
            print(f"[picker] FAILED at {task} — see {log}")
            return False
    return True


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Scene Picker: text prompt -> closest native Infinigen scene (+ optionally run it)")
    ap.add_argument("prompt", nargs="+", help="the text prompt describing the scene")
    ap.add_argument("--run", action="store_true", help="actually run Infinigen (long!)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, do not run")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--gpu", default=os.environ.get("GPU", "0"))
    ap.add_argument("--out-dir", help="output dir (default: output/<slug>/2_scene_picker)")
    ap.add_argument("--project", help="專案名(預設由 project_namer 從 prompt 摘要)")
    ap.add_argument("--full-quality", action="store_true",
                    help="drop the lightweight overrides (inview=40, on_the_fly=1)")
    args = ap.parse_args()

    prompt = " ".join(args.prompt).strip()

    print(f"[picker] picking the closest Infinigen scene with {OPUS_MODEL} ...")
    result = validate(pick(prompt))

    # ★專案名在這裡定案一次★，之後每個 stage 都寫進 output/<slug>/ 底下。
    #   `--project` 讓呼叫端沿用既有專案(第二次跑同一個場景不要另開一個目錄)。
    slug = args.project or name_project(prompt)
    if args.out_dir:
        # ★呼叫端自己管輸出位置時，不要另開一個 output/<slug>/★(2026-08-20 修)
        #   director.py 早就修過同一個 bug，但這裡漏了：run_e2e 每跑一次就會用
        #   LLM 摘要出的 slug 建一個★只有 project.json 的空資料夾★，真正的結果
        #   全在 $W 底下，output/ 於是被這種孤兒目錄塞滿(累積了 12 個)。
        #   而且它連「記下原句」都不必要 —— pick.json 本來就存了完整 prompt。
        out_dir = Path(args.out_dir)
    else:
        init_project(slug, prompt)      # 記下原句 —— slug 摘要過，反推不回來
        out_dir = stage_dir(slug, 2)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[picker] project: {slug}   ->  {out_dir}")

    print(f"\n=== PICK ===")
    print(f"  prompt   : {prompt}")
    print(f"  location : {result['location']}")
    print(f"  scene    : {result['scene']}   ({result['cue']})")
    print(f"  reason   : {result['reason']}")
    print(f"  match    : {result['match']}%")
    print(f"  gap      : {result['gap']}")
    print(f"  caveat   : {result['caveat']}")
    print(f"  runner-up: {result['runner_up']}")

    cmds = build_commands(result, out_dir, args.seed, lightweight=not args.full_quality)
    record = {"prompt": prompt, "seed": args.seed, **result,
              "commands": [" ".join(c) for c in cmds]}
    (out_dir / "pick.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n[picker] pick.json -> {out_dir}")

    print("\n=== COMMANDS ===")
    for c in cmds:
        print("  " + " ".join(c))

    if args.dry_run or not args.run:
        print("\n[picker] not running (add --run to execute; this takes ~1h outdoor).")
        return
    if result["location"] == "indoor":
        print("\n[picker] ⚠ indoor needs the one-line force_roomtype patch — see "
              "scene_picker/README.md. Without it the room type will be RANDOM.")
    ok = run(cmds, out_dir, args.gpu)
    print(f"\n[picker] {'done' if ok else 'FAILED'} -> {out_dir}")


if __name__ == "__main__":
    main()

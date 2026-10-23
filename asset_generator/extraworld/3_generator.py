"""Generator Orchestrator — the Agent-3 driver of the ExtraWorld pipeline.

Reads Agent 2's generation_list.json and, for every object TYPE, dispatches it to the
generator Agent 2 assigned, running them in parallel across GPUs.

Each row in the manifest is already a DISTINCT type (Agent 1/2 deduplicated identical
objects into one type with a count), so every type is "new object code": its generator
runs ONCE to produce that type's asset/code. WITHIN a type, the instance grouping says how
its copies relate:
  - seed_mode "same"      -> identical copies: one code, one seed, reused `count` times.
  - seed_mode "different" -> seed-varied copies: the SAME generated code re-run at each of
                             `count` distinct seeds (no re-generation — just change seed).
So the generator is never re-run per instance; seed variation is a cheap reuse of one code.

Formal routing:
  Every object is normalized to ProceduralGenerator and dispatched to
  3_procedural_generator.py. The manifest's original `generator` field is kept only as
  `requested_generator` in orchestration records for audit. BlenderGenerator remains as
  a standalone script/research branch, not a formal pipeline route.

Each generator runs as its own subprocess (isolated, GPU-pinned). A worker pool sized to
the GPU count runs them concurrently; each task borrows one GPU for its render(s).

    python 3_generator.py glowing_tree_plaza
    python 3_generator.py glowing_tree_plaza --gpus 0,1
    python 3_generator.py glowing_tree_plaza --only glowing_tree      # one object
    python 3_generator.py glowing_tree_plaza --dry-run                # just print the plan
"""
import argparse
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BI_ROOT = HERE.parents[1]
if str(BI_ROOT) not in sys.path:
    sys.path.insert(0, str(BI_ROOT))
from paths import ASSET_ROOT, asset_store_dir  # noqa: E402

FORMAL_ASSET_GENERATOR = "ProceduralGenerator"
GEN_SCRIPT = {
    FORMAL_ASSET_GENERATOR: HERE / "3_procedural_generator.py",
}
# Human-facing asset QA defaults. Connectivity runs can override the frame count
# and select a level-only orbit while preserving these production defaults.
ASSET_QA_FRAMES_PER_VIEW = {"smoke": 4, "full": 36}

_REF_HELPERS = None
_REF_HELPERS_LOCK = threading.Lock()


def _reference_helpers():
    """Load the already-tested Gemini image helper without duplicating API code."""
    global _REF_HELPERS
    with _REF_HELPERS_LOCK:
        if _REF_HELPERS is None:
            source = HERE / "3_procedural_generator.py"
            spec = importlib.util.spec_from_file_location("bi_procedural_reference_helpers", source)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load Gemini reference helpers from {source}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _REF_HELPERS = module
    return _REF_HELPERS


def _resolve_existing_reference(obj: dict, scene_root: Path) -> Path | None:
    refs = obj.get("refs") or {}
    candidates = []
    if isinstance(refs, dict):
        candidates.extend([refs.get("single"), refs.get("multi")])
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = scene_root / path
        if path.is_file():
            return path.resolve()
    return None


def ensure_blender_reference(obj: dict, scene_root: Path) -> Path:
    """Return a valid visual target; BlenderGenerator is forbidden to run text-only."""
    existing = _resolve_existing_reference(obj, scene_root)
    helpers = _reference_helpers()
    if existing is not None:
        if helpers.sniff_media_type(existing.read_bytes()) is None:
            raise RuntimeError(f"existing BlenderGenerator reference is not an image: {existing}")
        print(f"[{obj['id']}] REF reuse -> {existing}", flush=True)
        return existing

    ref_dir = scene_root / "2_director" / "references" / obj["id"]
    canonical = ref_dir / "target_single.png"
    # A previous formal run may have produced this image before generation was
    # interrupted.  Validate and reuse it even when an old generation_list did
    # not yet contain a refs field.
    if canonical.is_file():
        if helpers.sniff_media_type(canonical.read_bytes()) is None:
            raise RuntimeError(f"cached Gemini reference is not a valid image: {canonical}")
        print(f"[{obj['id']}] REF reuse -> {canonical.resolve()}", flush=True)
        return canonical.resolve()
    prompt = (obj.get("instance_prompt") or obj.get("prompt")
              or obj.get("name") or obj["id"])
    generated = helpers.auto_reference(prompt, ref_dir)
    if generated is None or not Path(generated).is_file():
        raise RuntimeError("Gemini did not produce the required BlenderGenerator reference")
    generated = Path(generated).resolve()
    if helpers.sniff_media_type(generated.read_bytes()) is None:
        raise RuntimeError(f"Gemini reference has an unsupported image format: {generated}")
    print(f"[{obj['id']}] REF generated -> {generated}", flush=True)
    return generated


def gen_dir(scene_root, gen: str, obj_id: str):
    """Central run directory for generated object code, renders, and diagnostics."""
    return asset_store_dir(gen, obj_id, "run")


def catalog_dir(gen: str, obj_id: str) -> Path:
    return asset_store_dir(gen, obj_id, "catalog_asset")


def asset_log_path(scene_root: Path, gen: str, obj_id: str) -> Path:
    scene_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", scene_root.name).strip("_") or "scene"
    return asset_store_dir(gen, obj_id, "logs") / f"{scene_slug}.log"


def orchestration_path(scene_root: Path) -> Path:
    scene_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", scene_root.name).strip("_") or "scene"
    return ASSET_ROOT / "_manifests" / f"{scene_slug}_orchestration.json"

# for the .blend export (a Blender/bpy subprocess)
BLENDER_PYTHON = os.environ.get("BLENDER_PYTHON", sys.executable)
EXPORT_BLEND = HERE / "export_blend.py"
REPO_ROOT = HERE.parents[1]
INFINIGEN_ROOT = Path(os.environ.get(
    "INFINIGEN_ROOT", REPO_ROOT / "third_party" / "infinigen")).resolve()


def resolve_scene(arg: str) -> tuple[Path, Path]:
    """Accept a scene name, a scene dir, or a generation_list.json path.
    Return (scene_root, manifest_path)."""
    p = Path(arg)
    if p.is_file() and p.name == "generation_list.json":
        return p.parent.parent, p
    for cand in (p / "2_director" / "generation_list.json",
                 BI_ROOT / "output" / arg / "2_director" / "generation_list.json"):
        if cand.is_file():
            return cand.parent.parent, cand
    raise SystemExit(f"could not find generation_list.json for '{arg}'")


def classify(obj: dict) -> str:
    """One-line instance-grouping classification for the plan printout."""
    g = obj.get("instance_grouping", {}) or {}
    count = g.get("count", 1)
    mode = g.get("seed_mode", "same")
    if count <= 1:
        return "single instance (new code)"
    if mode == "different":
        return f"{count} copies — SAME code, {count} different seeds (seed-varied)"
    return f"{count} copies — SAME code+seed, duplicated (identical)"


def normalize_object_generator(obj: dict) -> dict:
    """Return a manifest row routed through the formal procedural-only path."""
    out = dict(obj)
    requested = out.get("generator")
    if requested != FORMAL_ASSET_GENERATOR:
        out["requested_generator"] = requested
        out["generator"] = FORMAL_ASSET_GENERATOR
    return out


def normalize_objects(objects: list[dict]) -> list[dict]:
    return [normalize_object_generator(obj) for obj in objects]


def build_plan(objects: list[dict]) -> list[dict]:
    plan = []
    for obj in objects:
        gen = obj.get("generator")
        plan.append({
            "id": obj["id"],
            "generator": gen,
            "requested_generator": obj.get("requested_generator"),
            "factory": obj.get("factory"),
            "grouping": classify(obj),
            "runnable": gen in GEN_SCRIPT,
        })
    return plan


def print_plan(scene_root: Path, plan: list[dict]) -> None:
    print(f"\n=== GENERATION PLAN  ({scene_root.name}) ===")
    for e in plan:
        flag = "" if e["runnable"] else "  [!! unknown generator — will skip]"
        fac = f"factory={e['factory']}" if e["factory"] else ""
        requested = e.get("requested_generator")
        routed = (f" normalized_from={requested}" if requested is not None else "")
        print(f"  {e['id']:20} -> {str(e['generator']):19} {fac:28} "
              f"{e['grouping']}{routed}{flag}")
    print()


def _extract_result_json(stdout: str) -> dict | None:
    """Each generator prints its result dict as JSON at the end; grab the last {...}."""
    matches = re.findall(r"\{.*\}", stdout, re.DOTALL)
    for m in reversed(matches):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None


def run_one(obj: dict, scene_arg: str, scene_root: Path, gpu_q: queue.Queue,
            log_dir: Path, asset_qa_mode: str = "full",
            turntable: bool = True, seed_check: bool = True,
            turntable_frames_per_view: int | None = None,
            turntable_views: str = "both") -> dict:
    """Run one object's generator as a GPU-pinned subprocess. Returns a result record."""
    obj_id = obj["id"]
    gen = obj.get("generator")
    frames_per_view = (turntable_frames_per_view
                       if turntable_frames_per_view is not None
                       else ASSET_QA_FRAMES_PER_VIEW[asset_qa_mode])
    view_count = 1 if turntable_views == "level" else 2
    rec = {"id": obj_id, "generator": gen, "status": "skipped", "final": None,
           "log": None, "seconds": 0.0, "asset_qa_mode": asset_qa_mode,
           "turntable_views": turntable_views if turntable else "none",
           "turntable_frames_per_view": frames_per_view if turntable else 0,
           "turntable_expected_total_frames": (
               frames_per_view * view_count if turntable else 0),
           "seed_check": seed_check}
    if obj.get("requested_generator") is not None:
        rec["requested_generator"] = obj.get("requested_generator")
    script = GEN_SCRIPT.get(gen)
    if script is None:
        rec["error"] = f"unknown generator '{gen}'"
        print(f"[{obj_id}] SKIP — unknown generator '{gen}'")
        return rec

    run_dir = gen_dir(scene_root, gen, obj_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    final = run_dir / "final.png"
    final_before_ns = final.stat().st_mtime_ns if final.exists() else None
    log_path = asset_log_path(scene_root, gen, obj_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rec["asset_run_dir"] = str(run_dir)
    rec["log"] = str(log_path)
    rec["log_dir"] = str(log_path.parent)

    reference = None
    if gen == "BlenderGenerator":
        try:
            reference = ensure_blender_reference(obj, scene_root)
            rec["reference"] = str(reference)
            rec["reference_required"] = True
        except Exception as error:
            rec["status"] = "failed"
            rec["reference_required"] = True
            rec["error"] = f"required Gemini reference failed: {type(error).__name__}: {error}"
            log_path.write_text(rec["error"] + "\n", encoding="utf-8")
            print(f"[{obj_id}] FAILED BlenderGenerator reference — {error}", flush=True)
            return rec

    gpu = gpu_q.get()
    t0 = time.time()
    try:
        env = dict(**_os_environ(), CUDA_VISIBLE_DEVICES=str(gpu))
        cmd = [sys.executable, str(script), "--scene", scene_arg, "--id", obj_id,
               "--out-dir", str(run_dir),
               "--turntable-frames-per-view", str(frames_per_view),
               "--turntable-views", turntable_views]
        if gen == "BlenderGenerator":
            cmd += ["--detail"]
        if reference is not None:
            cmd += ["--single", str(reference), "--require-reference"]
        elif gen == "ProceduralGenerator" and os.environ.get("BI_REQUIRE_REFERENCE", "1") == "1":
            # ★2026-10-19 起預設要參考圖★：Gemini key 失效時以前會默默降成純文字模式，
            #   1019 整批 33 個資產沒有參考圖卻都標成 ok。BI_REQUIRE_REFERENCE=0 可放行。
            cmd += ["--require-reference"]
        if not turntable:
            cmd += ["--no-turntable"]
        # Both generators expose --no-seed-check.  Keeping this restricted to
        # ProceduralGenerator made ASSET_SEED_CHECK=0 lie for BlenderGenerator:
        # every parameterized Blender asset still rendered five extra seeds.
        if not seed_check:
            cmd += ["--no-seed-check"]
        # ★輪數由 manifest 逐筆決定★(2026-08-06 加) —— 以前這裡不傳 --rounds，
        #   兩支 generator 只能吃各自的 DEFAULT_ROUNDS(procedural 3 / blender 5)。
        #   複雜的硬表面(小木屋、漁船)前幾輪都在搭骨架，5 輪常常還沒收斂。
        if obj.get("rounds"):
            cmd += ["--rounds", str(int(obj["rounds"]))]
        # ★ADAPT★：genlist 標成從庫存變體改，glue prepare-adapt 已把那棵 overlay
        #   materialize 好並把路徑寫進 manifest。傳下去 generator 才知道要從
        #   既有變體出發，而不是從官方 infinigen 重做。
        if gen == "ProceduralGenerator" and obj.get("base_overlay"):
            cmd += ["--base-overlay", str(obj["base_overlay"])]
        # ★★BlenderGenerator 一律走 --param★★(2026-08-07)
        #   --param 本來是拿來跟 baseline 比品質的 opt-in 旗標，但★對 pipeline 不是可選的★：
        #   param_adapter 的 FACTORY_TMPL 呼叫 build(sample_params(rng))，
        #   平鋪腳本沒有這兩個函式 ⇒ 包出來的 factory 一 populate 就 NameError。
        #   0806 的一條龍與 0806 那 10 個 Blender 資產全是 param_mode=false，
        #   ⇒ ★Blender 這條線從沒真的接到 placer 過★。要做純腳本的對照組就在
        #     manifest 明寫 param=false。
        if gen == "BlenderGenerator" and obj.get("param", True):
            cmd += ["--param"]
        turntable_desc = (f"{frames_per_view * view_count} frames "
                          f"({turntable_views})" if turntable else "off")
        print(f"[{obj_id}] START {gen} on GPU {gpu} "
              f"(asset QA={asset_qa_mode}, turntable={turntable_desc}, "
              f"seed-check={'on' if seed_check else 'off'})")
        proc = subprocess.run(cmd, cwd=str(HERE), env=env, capture_output=True, text=True)
        log_path.write_text(proc.stdout + "\n===STDERR===\n" + proc.stderr)
        rec["seconds"] = round(time.time() - t0, 1)
        result = _extract_result_json(proc.stdout)
        final_exists = final.exists()
        final_after_ns = final.stat().st_mtime_ns if final_exists else None
        final_is_current = final_exists and (
            final_before_ns is None or final_after_ns != final_before_ns)
        rec["final"] = str(final) if final_exists else None
        if result:
            rec["result"] = result
        if proc.returncode == 0 and final_is_current:
            rec["status"] = "ok"
            print(f"[{obj_id}] OK {gen} ({rec['seconds']}s) -> {final.name}")
        else:
            rec["status"] = "failed"
            final_state = (
                "updated" if final_is_current
                else "stale" if final_exists else "missing")
            rec["error"] = f"exit={proc.returncode}, final.png={final_state}"
            print(f"[{obj_id}] FAILED {gen} ({rec['seconds']}s) — see {log_path}")
    finally:
        gpu_q.put(gpu)
    return rec


def _os_environ() -> dict:
    import os
    return os.environ.copy()


# ------------------------------------------------------------------- consolidation
def _read_kv(path: Path, key: str) -> str | None:
    """Read a 'key: value' line from a small text file (factory.txt / best.md)."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*{re.escape(key)}\s*:\s*(.+)", line)
        if m:
            return m.group(1).strip()
    return None


def _best_blender_script(gdir: Path) -> Path | None:
    """The blender generator's best round script: parse summary.md's '**Best ...: round_XX**'."""
    summ = gdir / "summary.md"
    if summ.is_file():
        m = re.search(
            r"^\s*\*\*Best(?:\s+\([^\n)]*\))?\s*:\s*"
            r"(round_\d+(?:_fix\d+)?)\*\*",
            summ.read_text(encoding="utf-8"),
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if m and (gdir / f"{m.group(1)}.py").is_file():
            return gdir / f"{m.group(1)}.py"
    scripts = sorted(gdir.glob("round_*.py"))            # fallback: last round
    return scripts[-1] if scripts else None


def _export_blend(mode_args: list[str], out_blend: Path, gpu: str = "0") -> bool:
    cmd = [BLENDER_PYTHON, str(EXPORT_BLEND)] + mode_args + [str(out_blend)]
    env = dict(_os_environ(), CUDA_VISIBLE_DEVICES=gpu)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    ok = out_blend.exists() and "BLEND_SAVED" in r.stdout
    if not ok:
        print(f"  [blend] export failed: {(r.stdout + r.stderr).strip().splitlines()[-1:]}")
    return ok


def consolidate_object(scene_root: Path, obj: dict, assets_dir: Path,
                       gpu: str = "0", export_blend: bool = False) -> dict:
    """Gather one object's render + GIFs + lightweight SPAWN RECIPE into catalog_asset/.
    The recipe (build.py / rewritten infinigen code / fqn+seeds) is what Step-4 assembly
    re-spawns and places — it is tiny and handles seed variation, so the heavy baked
    .blend is NOT stored unless export_blend=True (opt-in)."""
    gen, oid = obj.get("generator"), obj["id"]
    gdir = gen_dir(scene_root, gen, oid)
    rec = {"id": oid, "generator": gen, "dir": None, "blend": None, "files": []}
    if not gdir.is_dir():
        return rec
    adir = catalog_dir(gen, oid)
    adir.mkdir(parents=True, exist_ok=True)
    rec["dir"] = str(adir)
    rec["run_dir"] = str(gdir)

    # renders + turntable gifs (same names for every generator) — the human-facing results
    for src, dst in [("final.png", "render.png"),
                     ("final_360.gif", "turntable.gif"),
                     ("final_360_topdown.gif", "turntable_topdown.gif")]:
        if (gdir / src).is_file():
            shutil.copy2(gdir / src, adir / dst)
            rec["files"].append(dst)

    grouping = obj.get("instance_grouping", {})
    seeds = grouping.get("seeds", [])
    fqn = _read_kv(gdir / "factory.txt", "fqn")
    spawn: dict = {}                                       # the Step-4 re-spawn recipe

    if gen == "BlenderGenerator":
        script = _best_blender_script(gdir)
        if script:
            shutil.copy2(script, adir / "build.py")        # tiny bpy build script
            rec["files"].append("build.py")
            spawn = {"method": "script", "script": "build.py"}
    elif gen == "ProceduralGenerator":
        best = _read_kv(gdir / "best.md", "best_round")    # e.g. "r3"
        if best and (gdir / best / "code").is_dir():        # the rewritten infinigen src (small)
            shutil.copytree(gdir / best / "code", adir / "code", dirs_exist_ok=True)
            rec["files"].append("code/")
        # Step-4 rebuilds an overlay from code/ then spawns the rewritten factory; seeds
        # give minor variation of the same appearance.
        spawn = {"method": "factory_overlay", "fqn": fqn, "src": "code",
                 "seeds": seeds or [0]}

    # opt-in heavy artifact: a standalone baked .blend (large for foliage-heavy assets)
    if export_blend:
        blend = adir / "object.blend"
        if gen == "BlenderGenerator" and (adir / "build.py").is_file():
            if _export_blend(["script", str(adir / "build.py")], blend, gpu):
                rec["blend"] = str(blend)
        elif gen == "ProceduralGenerator" and fqn and (gdir / "code" / "overlay").is_dir():
            if _export_blend(["factory", str(gdir / "code" / "overlay"), fqn, "0"], blend, gpu):
                rec["blend"] = str(blend)

    (adir / "asset.json").write_text(json.dumps({
        "id": oid, "generator": gen, "factory": fqn, "prompt": obj.get("prompt"),
        "source_scene": str(scene_root),
        "source_run": str(gdir),
        "instance_grouping": grouping, "spawn": spawn,
        "render": "render.png", "turntable": "turntable.gif",
        "blend": Path(rec["blend"]).name if rec["blend"] else None,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return rec


def consolidate_scene(scene_root: Path, objects: list[dict], gpus: list[str],
                      export_blend: bool = False) -> dict:
    """Post-process existing generator outputs into output/asset/.../catalog_asset."""
    assets_dir = ASSET_ROOT
    manifest_dir = ASSET_ROOT / "_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== CONSOLIDATE -> {assets_dir}  (blend={'on' if export_blend else 'off'}) ===")
    gpu = (gpus or ["0"])[0]
    records = []
    for obj in objects:
        rec = consolidate_object(scene_root, obj, assets_dir, gpu=gpu, export_blend=export_blend)
        mark = "✓" if rec["dir"] else "-"
        blend = "  +blend" if rec["blend"] else ""
        print(f"  {mark} {obj['id']:20} files={len(rec['files'])}{blend}")
        records.append(rec)
    scene_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", scene_root.name).strip("_") or "scene"
    manifest = {"assets_dir": str(assets_dir), "source_scene": str(scene_root), "objects": records}
    manifest_path = manifest_dir / f"{scene_slug}_assets.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[consolidate] assets.json -> {manifest_path}")
    return manifest


def main():
    ap = argparse.ArgumentParser(description="Run each object's assigned generator (parallel across GPUs)")
    ap.add_argument("scene", help="scene name, scene dir, or generation_list.json path")
    ap.add_argument("--gpus", default="0,1", help="comma-separated GPU ids to spread work over (default 0,1)")
    ap.add_argument("--only", help="run only this object id")
    ap.add_argument("--generators", help="comma-separated generator names to include (default all)")
    ap.add_argument("--serial", action="store_true", help="run one at a time (still GPU-pinned)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--consolidate-only", action="store_true",
                    help="skip generation; just gather existing outputs into assets/ + export .blend")
    ap.add_argument("--no-consolidate", action="store_true",
                    help="skip the 3_asset/ consolidation step")
    ap.add_argument("--blend", action="store_true",
                    help="also export a standalone object.blend per asset (large for foliage; "
                         "not needed for Step-4 assembly, which re-spawns from the recipe)")
    ap.add_argument("--asset-qa-mode", choices=sorted(ASSET_QA_FRAMES_PER_VIEW), default="full",
                    help="turntable QA: smoke=8 total frames, full=72 (default: full)")
    ap.add_argument("--turntable-frames-per-view", type=int,
                    help="override frames per turntable view")
    ap.add_argument("--turntable-views", choices=("level", "both"), default="both",
                    help="level=one horizontal orbit; both=level + top-down")
    ap.add_argument("--no-turntable", action="store_true",
                    help="skip asset turntable renders while retaining generator iteration renders")
    ap.add_argument("--no-seed-check", action="store_true",
                    help="skip ProceduralGenerator's extra five-seed diversity renders")
    args = ap.parse_args()
    if args.turntable_frames_per_view is not None and args.turntable_frames_per_view < 1:
        ap.error("--turntable-frames-per-view must be >= 1")

    scene_root, manifest_path = resolve_scene(args.scene)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    objects = manifest.get("objects", [])
    if args.only:
        objects = [o for o in objects if o["id"] == args.only]
    objects = normalize_objects(objects)
    normalized = [o for o in objects if o.get("requested_generator") is not None]
    if normalized:
        ids = ", ".join(str(o.get("id")) for o in normalized[:8])
        more = "" if len(normalized) <= 8 else f", ... +{len(normalized) - 8}"
        print(f"[orchestrator] procedural-only routing normalized "
              f"{len(normalized)} object(s): {ids}{more}")
    if args.generators:
        keep = {g.strip() for g in args.generators.split(",")}
        objects = [o for o in objects if o.get("generator") in keep]
    if not objects:
        raise SystemExit("no matching objects to run")

    gpus = [g.strip() for g in args.gpus.split(",") if g.strip()] or ["0"]

    if args.consolidate_only:
        consolidate_scene(scene_root, objects, gpus, export_blend=args.blend)
        return

    plan = build_plan(objects)
    print_plan(scene_root, plan)
    if args.dry_run:
        return
    gpu_q: queue.Queue = queue.Queue()
    for g in gpus:
        gpu_q.put(g)

    out_dir = orchestration_path(scene_root).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = out_dir

    runnable = [o for o in objects if o.get("generator") in GEN_SCRIPT]
    max_workers = 1 if args.serial else max(1, min(len(gpus), len(runnable)) or 1)
    qa_frames = (args.turntable_frames_per_view
                 if args.turntable_frames_per_view is not None
                 else ASSET_QA_FRAMES_PER_VIEW[args.asset_qa_mode])
    qa_view_count = 1 if args.turntable_views == "level" else 2
    print(f"[orchestrator] running {len(runnable)} object(s) on GPUs {gpus} "
          f"with {max_workers} worker(s); asset QA={args.asset_qa_mode} "
          f"({'no turntable' if args.no_turntable else str(qa_frames * qa_view_count) + ' turntable frames/object (' + args.turntable_views + ')'}); "
          f"seed-check={'off' if args.no_seed_check else 'on'}\n")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(
            lambda o: run_one(o, args.scene, scene_root, gpu_q, log_dir,
                              args.asset_qa_mode, turntable=not args.no_turntable,
                              seed_check=not args.no_seed_check,
                              turntable_frames_per_view=qa_frames,
                              turntable_views=args.turntable_views), objects))

    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]

    orchestration = {
        "scene": manifest.get("scene_name", scene_root.name),
        "asset_qa_mode": args.asset_qa_mode,
        "turntable_enabled": not args.no_turntable,
        "turntable_views": "none" if args.no_turntable else args.turntable_views,
        "turntable_frames_per_view": 0 if args.no_turntable else qa_frames,
        "turntable_expected_total_frames_per_asset": (
            0 if args.no_turntable else qa_frames * qa_view_count),
        "seed_check_enabled": not args.no_seed_check,
        "asset_root": str(ASSET_ROOT),
        "orchestration_path": str(orchestration_path(scene_root)),
        "total": len(results), "ok": len(ok), "failed": len(failed), "skipped": len(skipped),
        "seconds": round(time.time() - t0, 1),
        "objects": results,
    }
    orchestration_path(scene_root).write_text(
        json.dumps(orchestration, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n=== ORCHESTRATION DONE ({orchestration['seconds']}s) ===")
    print(f"  ok={len(ok)}  failed={len(failed)}  skipped={len(skipped)}")
    for r in results:
        mark = {"ok": "✓", "failed": "✗", "skipped": "-"}.get(r["status"], "?")
        print(f"  {mark} {r['id']:20} {str(r['generator']):19} {r.get('final') or r.get('error','')}")
    print(f"\n[orchestrator] orchestration.json -> {orchestration_path(scene_root)}")

    if not args.no_consolidate:
        consolidate_scene(scene_root, objects, gpus, export_blend=args.blend)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Collect one e2e run into the standard deliverable layout (★每個 case 跑完都要有★ 2026-10-20):

    <final_root>/<case>/
        output/before.png        baseline scene, same camera as after (Step 3 的 selected.png)
        output/after.png         final validated still (run/render/view.png)
        output/before.mp4/.gif   baseline rendered with the after camera + same move (--before-video)
        output/after.mp4/.gif    final video (run/render/final.mp4)
        output/obj/<asset>/      每個★實際生成/放進去★的資產：final.png, turntable gif, 參考圖,
                                 request.txt, score.json, code/<改寫過的檔案>  ← 看得到它到底加了什麼
        output/info.txt          case / status / stopped step / error / prompt / Director 的每個 edit /
                                 Composer 的擺放方式 / 資產清單
        case_spec.json  SOURCE.txt  result.json  e2e.log  logs/  run -> <heavy run dir> (symlink)

Pass or fail: failures still get info.txt + whatever artifacts exist (before.png, placement
preview).  Heavy data stays where the run was (e.g. /project3); only small deliverables are
copied here, plus a `run` symlink.

    finalize_case.py --run-dir RUN --case ID --scene plain --seed 2 [--status PASS|FAIL]
                     [--prompt-file F] [--final-root DIR] [--before-video --gpu 0]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
INF = os.environ.get("INFINIGEN_ROOT", str(ROOT / "third_party" / "infinigen"))


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def copy_if(src: Path, dst: Path) -> bool:
    if src and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def last_step(e2e_log: Path) -> str:
    step = ""
    for line in e2e_log.read_text(encoding="utf-8", errors="replace").splitlines() if e2e_log.is_file() else []:
        m = re.search(r"════════ \[(\d+)\] (.+?)\s+\d\d:\d\d:\d\d", line)
        if m:
            step = f"{m.group(1)} {m.group(2).strip()}"
    return step


def last_error(e2e_log: Path) -> str:
    err = ""
    for line in e2e_log.read_text(encoding="utf-8", errors="replace").splitlines() if e2e_log.is_file() else []:
        if "⛔" in line:
            err = line.strip()
    return err


def gif_from_mp4(mp4: Path, gif: Path) -> None:
    if not mp4.is_file() or shutil.which("ffmpeg") is None:
        return
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vf",
                    "fps=12,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", str(gif)], check=False)


def director_edits(run: Path) -> list[dict]:
    plan = read_json(run / "1_director" / "edit_plan.json", {}) or {}
    out = []
    for e in plan.get("edits", []):
        out.append({k: e.get(k) for k in ("id", "name", "action", "target_factory", "host_factory",
                                            "placement", "count", "anchoring", "layer")})
    return out


def composer_methods(run: Path) -> dict[str, str]:
    plan = read_json(run / "3_composer" / "edit_plan.json", {}) or {}
    methods: dict[str, str] = {}
    for e in plan.get("hero_edits", []):
        key = e.get("code_from") or e.get("director_edit_id") or e.get("factory")
        spec = e.get("placement_spec") or {}
        methods[str(key)] = (f"hero:{e.get('anchoring')}/{spec.get('arrangement', '?')} x{e.get('n', '?')}"
                             f" motion={(e.get('world_motion') or {}).get('preset', 'none')}")
    for e in plan.get("extra_edits", []):
        key = e.get("code_from") or e.get("director_edit_id") or e.get("target")
        methods[str(key)] = f"ground_scatter:{e.get('target')} density={e.get('density_level', e.get('density'))}"
    for e in plan.get("official_scatter_edits", []) + plan.get("swarm_edits", []):
        key = e.get("code_from") or e.get("director_edit_id") or e.get("factory")
        methods[str(key)] = f"{e.get('op', 'scatter')}:{e.get('factory')}"
    return methods


def collect_assets(run: Path, obj_dir: Path) -> list[str]:
    """overlay_table.json → 每個資產的成品/參考圖/改寫過的 code 複製到 obj/<id>/。"""
    table = read_json(run / "overlay_table.json", {}) or {}
    ids = []
    for aid, info in table.items():
        ov = Path(info.get("source_overlay") or info.get("staged_overlay") or "")
        arun = ov.parent.parent if ov.name == "overlay" else ov  # <id>/run/code/overlay → <id>/run
        if arun.name == "code":
            arun = arun.parent
        dst = obj_dir / aid
        dst.mkdir(parents=True, exist_ok=True)
        got = False
        for name in ("final.png", "final_360.gif", "final_360_topdown.gif", "turntable.gif",
                     "target_single.png", "target_single.source.txt", "request.txt", "score.json",
                     "plan.md", "best.md", "factory.txt"):
            got |= copy_if(arun / name, dst / name)
        if not got:  # library reuse: catalog_asset next to the overlay
            cat = arun.parent / "catalog_asset"
            for name in ("render.png", "turntable.gif", "asset.json"):
                copy_if(cat / name, dst / name)
        for rel in info.get("rewrote") or []:
            src = ov / "infinigen" / rel
            if src.is_file() and not rel.endswith("__init__.py"):
                copy_if(src, dst / "code" / rel)
        meta = {"factory": info.get("factory"), "source_id": info.get("source_id"),
                "source_overlay": str(ov), "rewrote": info.get("rewrote")}
        (dst / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        ids.append(aid)
    return ids


def before_video(run: Path, scene: str, seed: str, out_mp4: Path, gpu: str, log: Path) -> bool:
    sd = ROOT / "scenes" / scene / f"seed{seed}"
    before = sd / "fine_cam" / "scene.blend"
    if not before.is_file():
        before = sd / "fine" / "scene.blend"
    after = run / "fine" / "scene.blend"
    if not before.is_file() or not after.is_file():
        print(f"[finalize] before 影片跳過：缺 {'baseline' if not before.is_file() else 'after'} fine blend")
        return False
    move = "auto"
    for lg in glob.glob(str(run / "outputs_by_step" / "*" / "*render_animation.log")):
        m = re.search(r"\[animation\] camera move: (\S+)", Path(lg).read_text(errors="replace"))
        if m:
            move = m.group(1)
            break
    n_frames = len(list((run / "render" / "frames").glob("frame_*.png"))) or 72
    work = out_mp4.parent.parent / "before_video"
    frames = work / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    cmd = [PY, str(ROOT / "stage_exec" / "render_before_animation.py"), "--", str(before.resolve()), str(frames),
           "--after-blend", str(after.resolve()), "--frame-start", "1", "--frame-end", str(n_frames),
           "--fps", os.environ.get("VIDEO_FPS", "24"), "--samples", os.environ.get("VIDEO_SAMPLES", "48"),
           "--res", os.environ.get("VIDEO_RES", "960x540"), "--view", "AgX",
           "--camera-move", move, "--camera-move-m", os.environ.get("VIDEO_CAMERA_MOVE_M", "1.6"),
           "--scene", scene]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu), PYTHONPATH=INF)
    print(f"[finalize] before 影片：{before.name} + after 相機 move={move} frames={n_frames} gpu={gpu}", flush=True)
    # 共用卡 OOM 就等再試(跟 run_e2e.sh 的 render 一樣)
    retries = int(os.environ.get("RENDER_OOM_RETRIES", "3")); wait = int(os.environ.get("RENDER_OOM_WAIT", "600"))
    for attempt in range(retries + 1):
        with open(log, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
        if rc == 0:
            break
        if "out of GPU memory" in log.read_text(errors="replace") and attempt < retries:
            print(f"[finalize] ⚠️ before 影片 GPU OOM，等 {wait}s 再試 ({attempt + 1}/{retries})", flush=True)
            time.sleep(wait); shutil.rmtree(frames, ignore_errors=True); continue
        print(f"[finalize] ⛔ before 影片渲染失敗 rc={rc}（見 {log}）")
        return False
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", os.environ.get("VIDEO_FPS", "24"),
                        "-start_number", "1", "-i", str(frames / "frame_%04d.png"), "-frames:v", str(n_frames),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out_mp4)])
    return r.returncode == 0 and out_mp4.is_file()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--scene", default="")
    ap.add_argument("--seed", default="")
    ap.add_argument("--status", default="auto", help="PASS | FAIL | auto(from render/final.mp4)")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--final-root", default=os.environ.get("FINAL_ROOT", str(ROOT / "output" / "final")))
    ap.add_argument("--before-video", action="store_true")
    ap.add_argument("--gpu", default=os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",")[0])
    a = ap.parse_args()

    run = Path(a.run_dir).resolve()
    attempt = run.parent
    final = Path(a.final_root) / a.case
    out = final / "output"
    out.mkdir(parents=True, exist_ok=True)
    e2e_log = attempt / "e2e.log"
    status = a.status
    if status == "auto":
        status = "PASS" if (run / "render" / "final.mp4").is_file() else "FAIL"

    # scene / seed / prompt
    pick = read_json(run / "pick" / "pick.json", {}) or {}
    scene = a.scene or pick.get("scene") or ""
    seed = a.seed or str((read_json(run / "project.json", {}) or {}).get("seed", "")) or ""
    prompt = ""
    for cand in ([Path(a.prompt_file)] if a.prompt_file else []) + [attempt / "input" / "prompt.txt"]:
        if cand.is_file():
            prompt = cand.read_text(encoding="utf-8").strip()
            break

    # stills / videos
    copy_if(run / "render" / "view.png", out / "after.png")
    if copy_if(run / "render" / "final.mp4", out / "after.mp4"):
        gif_from_mp4(out / "after.mp4", out / "after.gif")
    if not (out / "after.png").is_file():
        copy_if(run / "placement_review" / "final" / "placement_preview.png", out / "after_placeholder_preview.png") or \
            copy_if(run / "placement_review" / "placement_preview.png", out / "after_placeholder_preview.png")
    copy_if(run / "0_camera" / "selected.png", out / "before.png") or copy_if(run / "0_camera" / "official.png", out / "before.png")
    if a.before_video and status == "PASS" and scene and seed != "":
        if before_video(run, scene, seed, out / "before.mp4", a.gpu, final / "before_video.log"):
            gif_from_mp4(out / "before.mp4", out / "before.gif")

    # assets
    assets = collect_assets(run, out / "obj")

    # info.txt
    edits = director_edits(run)
    methods = composer_methods(run)
    timings = (run / "stage_timings.tsv").read_text(encoding="utf-8").strip().splitlines() if (run / "stage_timings.tsv").is_file() else []
    total = next((l.split("\t")[4] for l in timings if l.startswith("total\t")), "?")
    lines = [f"case: {a.case}", f"status: {status}", f"scene: {scene} seed{seed}",
             f"stopped at: {last_step(e2e_log) or '?'}", f"total seconds: {total}",
             f"error: {last_error(e2e_log) or '-'}", f"run: {run}", "", "prompt:", f"  {prompt}", "",
             "director edits (what was asked to change):"]
    for e in edits:
        tgt = e.get("target_factory") or e.get("host_factory")
        lines.append(f"  {e.get('id')}: {e.get('action')} {tgt or ''} placement={e.get('placement')} "
                     f"count={e.get('count')} anchoring={e.get('anchoring')}  — {e.get('name')}")
    lines += ["", "composer methods (how each one was placed):"]
    lines += [f"  {k}: {v}" for k, v in methods.items()] or ["  (no composer plan)"]
    lines += ["", f"generated / reused assets ({len(assets)}) → output/obj/<id>/:"]
    for aid in assets:
        meta = read_json(out / "obj" / aid / "meta.json", {}) or {}
        src = read_json(out / "obj" / aid / "target_single.source.txt", None)
        lines.append(f"  {aid}: factory={meta.get('factory')} source={meta.get('source_id')} "
                     f"rewrote={len(meta.get('rewrote') or [])} files")
    (out / "info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # case_spec / SOURCE / result / logs / run link
    (final / "case_spec.json").write_text(json.dumps(
        {"case_id": a.case, "scene": scene, "seed": seed, "prompt": prompt, "asset_ids": assets,
         "director_edits": edits, "methods": methods}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (final / "SOURCE.txt").write_text(f"{run}  (finalized {time.strftime('%Y-%m-%d %H:%M:%S')}, status {status})\n",
                                      encoding="utf-8")
    (final / "result.json").write_text(json.dumps(
        {"case_id": a.case, "status": status, "run_dir": str(run), "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "total_seconds": total, "has_after_video": (out / "after.mp4").is_file(),
         "has_before_video": (out / "before.mp4").is_file()}, indent=2) + "\n", encoding="utf-8")
    copy_if(e2e_log, final / "e2e.log")
    logs = final / "logs"
    for lg in glob.glob(str(run / "outputs_by_step" / "*" / "*.log")):
        copy_if(Path(lg), logs / Path(lg).name)
    link = final / "run"
    if link.is_symlink() or link.exists():
        link.unlink() if link.is_symlink() else None
    if not link.exists():
        link.symlink_to(run)
    print(f"[finalize] {status} {a.case} → {final}  (assets: {', '.join(assets) or '-'})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

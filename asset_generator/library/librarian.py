#!/usr/bin/env python
"""Expanded per-Factory asset library —— 讓生成過的資產可以被查、被重用、被改。

★這支補上 run_e2e.sh Step 5 註解裡那個公開的待辦★：
  「SKIP_GEN=1 ⇒ 這★不是★正式的重用機制，只是把手動注入變成一個旗標(待辦仍在)」

════════════════════════════════════════════════════════════════════════
★為什麼存 diff 而不是存整棵 overlay★
════════════════════════════════════════════════════════════════════════
  實測一份 procedural overlay = 93 MB / 96 個實體檔(polar_bear)。存 N 個變體不可行。
  更致命的是★會 stale★：placer 的 verify_overlay_not_stale 要求「未改寫的鏡射檔
  必須與 runtime 基底逐位元組相同」，infinigen_o 一更新，整庫的舊 overlay 全部失效。

  ⇒ 庫只存★真正被改寫的那幾個檔★(changed_files()，通常 3~5 個)，用的時候才
    對★當下的★基底重建 overlay。基底更新後，庫仍然有效。

════════════════════════════════════════════════════════════════════════
★lineage 是 metadata，不是重建路徑★
════════════════════════════════════════════════════════════════════════
  黑白孔雀從彩色孔雀改出來，`base` 記著彩色孔雀，但 `changed/` 存的仍是
  ★對官方基底★的完整 diff。⇒ 任何變體都能獨立 materialize，不需要遞迴重放
  整條血緣鏈。這是「乾淨 rebase」的具體意思：diff 不疊 diff。

════════════════════════════════════════════════════════════════════════
★good_list.jsonl 是正式查庫入口，index.json 是兼容視圖★
════════════════════════════════════════════════════════════════════════
  這個 repo 已經踩過兩次「同一張表散在多處、改一處忘另一處」(placement_modes.py
  開頭記著 0730 3-A 與 0731 3-B)。正式路由現在看 good_list；index / Expand_Factory.txt
  只保留給舊工具相容。

用法:
  python -m asset_generator.library.librarian add <run_dir> [--keywords a,b] [--min-score N]
  python -m asset_generator.library.librarian lookup "<request>" [--factory FishFactory]
  python -m asset_generator.library.librarian materialize <variant_id> --out <dir>
  python -m asset_generator.library.librarian list [--factory X]
  python -m asset_generator.library.librarian reindex
"""
import argparse
import contextlib
import difflib
import json
import re
import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ★單一來源★：「哪些檔真的被改寫過」與「基底在哪」都用 glue.py 那一份定義，
#   不要在這裡另抄一份。glue.py 的 import 是無副作用的(main 被 __main__ 保護)。
from pipeline import glue as _glue  # noqa: E402
from pipeline.glue import changed_files  # noqa: E402
from paths import ASSET_ROOT  # noqa: E402


def base_pkg() -> Path:
    """當下的 overlay 鏡射基底。

    ★一律問 glue，不要自己存一份★ —— changed_files() 判斷「誰被改寫了」用的是
    glue.BASE_PKG。這裡若另存一份符號，兩邊在基底搬家時會安靜地分岔：
    入庫存的是對 A 算的 diff，materialize 卻疊到 B 上。
    """
    return _glue.BASE_PKG

LIB = Path(__file__).resolve().parent
INDEX = LIB / "index.json"
VARIANTS = LIB / "variants"
EXPAND_TXT = LIB / "Expand_Factory.txt"
ASSET_MANIFESTS = ASSET_ROOT / "_manifests"
GOOD_LIST_JSONL = ASSET_MANIFESTS / "good_list.jsonl"
GOOD_LIST_TXT = ASSET_MANIFESTS / "good_list.txt"

# ★門檻 45 是有數據的選擇，不是拍腦袋★(2026-08-20)
#   掃過 output/ 底下 89 個歷史 run 的最佳輪分數：中位數 42、最高 82，
#   >=80 只有 2 個、>=60 有 11 個、>=45 約一半。procedural 的 VERIFIER_SYSTEM
#   本來就寫「缺一個關鍵特徵要壓到 80 以下」，設計上就嚴 —— 使用者手挑的 16 個
#   「可以用」的資產★全部落在 20~55★，一個都不到 60。
#   ⇒ 60 會讓自動入庫幾乎收不到東西；45 大致對應「主體特徵有做出來」。
#   ⚠️ 這仍是★過渡值★：逐輪 verifier 與跨輪 selector 會互相打架(北極熊 selector
#     選 r2=45，但 r1 其實 55)。統一 scorer 累積夠多樣本後要重新校一次，
#     並用新分數重新篩全庫。
#   BI_LIBRARY_MIN_SCORE 可以逐次覆寫。
DEFAULT_MIN_SCORE = 45
SCORE_FILE = "score.json"
LEGACY_SCORE_FILE = "library_score.json"

# lookup 的三態。REUSE 直接用，ADAPT 拿它當生成基底，FRESH 走現行流程。
REUSE, ADAPT, FRESH = "REUSE", "ADAPT", "FRESH"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def index_lock():
    """★index.json 的 read-modify-write 必須互斥★(2026-08-20)。

    自動入庫讓 N 個生成器行程各自在結束時寫庫。沒有鎖的話兩個行程會同時讀到
    舊的 index、各自加上自己那一筆、後寫的把先寫的蓋掉 —— ★入庫看起來成功、
    log 也顯示 ✅，但庫裡少了一筆★。跑 84 個資產時這幾乎必然發生。
    """
    LIB.mkdir(parents=True, exist_ok=True)
    lock_path = LIB / ".index.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_index() -> dict:
    if not INDEX.is_file():
        return {"version": 1, "variants": {}}
    return json.loads(INDEX.read_text(encoding="utf-8"))


def save_index(idx: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    reindex(idx)


def read_meta_file(path: Path) -> dict:
    """讀 factory.txt 這種 `key: value` 檔(與 placer.read_factory_meta 同格式)。"""
    meta = {}
    if not Path(path).is_file():
        return meta
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return s or "variant"


# ─────────────────────────────────────────────────────── harvest(讀 generator 產物)
def detect_run(run_dir: Path, *, round_tag=None) -> dict | None:
    """認出一個 generator run 是 procedural 還是 blender，並找出它的 overlay。

    procedural : <run>/code/overlay/factory.txt          (3_procedural_generator.py)
    blender    : <run>/adapter/adapter.json → overlay    (param_adapter.py 包過)

    ⚠️ blender 那條★不接受沒過 verify 的 adapter★ —— 0819 的 red_canoe 就是把
       已知會 crash 的 round 包進去，一路滑到 placer 才炸(glue.py 的 _best_script 修正)。
    """
    run_dir = Path(run_dir).resolve()

    # ★指定輪次★：consolidated overlay 是 selector 挑的那一輪，但人眼常常
    #   不同意(luna_moth 的 best.md 是 r5，實際 r3 比較好)。每一輪的 code 都完整
    #   保留著，所以指定輪次不是 hack，是拿另一份同樣完整的產物。
    #   ⚠️ r<N>/code ★不是★ overlay(沒有 symlink 鏡像、沒有 factory.txt)，它相當於
    #     overlay 底下的 infinigen/ 那一層 ⇒ 用 pkg_root 指出來，factory.txt 仍取
    #     run 根目錄那份(entry/fqn 不隨輪次改變)。
    if round_tag:
        rdir = run_dir / round_tag / "code"
        ftxt = run_dir / "factory.txt"
        if not rdir.is_dir():
            return {"generator": None, "overlay": None, "run_dir": run_dir,
                    "reject": f"找不到指定輪次的 code: {rdir}"}
        if not ftxt.is_file():
            return {"generator": None, "overlay": None, "run_dir": run_dir,
                    "reject": f"找不到 {ftxt}"}
        return {"generator": "ProceduralGenerator", "overlay": run_dir / "code" / "overlay",
                "pkg_root": rdir, "factory_txt": ftxt, "round": round_tag,
                "run_dir": run_dir}

    ov = run_dir / "code" / "overlay"
    if (ov / "factory.txt").is_file():
        return {"generator": "ProceduralGenerator", "overlay": ov,
                "pkg_root": ov / "infinigen", "factory_txt": ov / "factory.txt",
                "run_dir": run_dir}

    aj = run_dir / "adapter" / "adapter.json"
    if aj.is_file():
        info = json.loads(aj.read_text(encoding="utf-8"))
        # ⚠️ ★「沒有 verify 欄位」與「verify 失敗」是兩件事★
        #    --verify 是 0819 才變成強制的(glue.py 的 _best_script 修正)，在那之前的
        #    adapter.json 根本沒有這個欄位。把兩者混為一談，會用一個假理由擋掉所有
        #    舊的 Blender 資產，然後看起來像「Blender 這條線壞了」。
        verify = info.get("verify")
        if verify is not None and not verify.get("ok"):
            return {"generator": "BlenderGenerator", "overlay": None,
                    "run_dir": run_dir,
                    "reject": f"adapter verify 失敗: {verify.get('error') or 'unknown'}"}
        ov = Path(info["overlay"])
        if not (ov / "factory.txt").is_file():
            return {"generator": "BlenderGenerator", "overlay": None,
                    "run_dir": run_dir, "reject": f"找不到 {ov}/factory.txt"}
        return {"generator": "BlenderGenerator", "overlay": ov,
                "pkg_root": ov / "infinigen", "factory_txt": ov / "factory.txt",
                "run_dir": run_dir}

    return None


def harvest_quality(run_dir: Path, *, round_tag=None) -> dict:
    """把 generator 已經算過的品質訊號讀出來(不呼叫任何 API)。

    procedural 每輪寫 r<N>/verify.md 的 match_pct，best.md 記哪一輪最好；
    blender 只有 judge 挑最佳輪，★沒有留下數值分數★ ⇒ 回 match_pct=None，
    由入庫端的統一 scorer 補。
    """
    run_dir = Path(run_dir)
    out = {"match_pct": None, "best_round": None, "stage_c_gate": None,
           "seed_check": None, "source": None}

    # ★統一分數優先★ —— 它是兩種 generator 可比的那一個。generator 自己的
    #   逐輪分數只在還沒評過時當後備。
    unified = (run_dir / round_tag / SCORE_FILE) if round_tag else (run_dir / SCORE_FILE)
    if unified.is_file():
        try:
            u = json.loads(unified.read_text(encoding="utf-8"))
            if u.get("ok") and u.get("match_pct") is not None:
                out["match_pct"] = int(u["match_pct"])
                out["source"] = u.get("scorer", "library_scorer_v1")
        except json.JSONDecodeError:
            pass

    gate = run_dir / "stage_c_gate.json"
    if gate.is_file():
        try:
            g = json.loads(gate.read_text(encoding="utf-8"))
            out["stage_c_gate"] = bool(g.get("ok"))
        except json.JSONDecodeError:
            out["stage_c_gate"] = None

    best = run_dir / "best.md"
    if best.is_file():
        m = re.search(r"best_round:\s*(r\d+)", best.read_text(encoding="utf-8"))
        if m:
            out["best_round"] = m.group(1)

    # BlenderGenerator：summary.md 的★權威 Best 標記★。只認這一個格式 ——
    # 0819 的 red_canoe 就是因為抓 summary 裡第一個出現的 round_XX，包進了一個
    # 已知會 crash 的 round(glue.py::_best_script 的同一個教訓)。
    summary = run_dir / "summary.md"
    if summary.is_file():
        text = summary.read_text(encoding="utf-8")
        m = re.search(r"\*\*Best[^:]*:\s*(round_\d+(?:_fix\d+)?)\*\*", text)
        if m:
            out["best_round"] = out["best_round"] or m.group(1)
        ms = re.search(r"seed sensitivity:\s*(✅|❌)", text)
        if ms:
            out["seed_check"] = ms.group(1) == "✅"

    # 最佳輪的 match_pct 優先；沒有 best.md 就取所有輪的最大值。
    pcts = {}
    for rdir in sorted(run_dir.glob("r[0-9]*")):
        vm = rdir / "verify.md"
        if not vm.is_file():
            continue
        m = re.search(r"match_pct:\s*(\d+)", vm.read_text(encoding="utf-8"))
        if m:
            pcts[rdir.name] = int(m.group(1))
    if pcts and out["match_pct"] is None:
        if out["best_round"] in pcts:
            out["match_pct"] = pcts[out["best_round"]]
        else:
            out["best_round"] = max(pcts, key=pcts.get)
            out["match_pct"] = pcts[out["best_round"]]
        out["source"] = "generator_verifier"
    elif pcts and not out["best_round"]:
        out["best_round"] = max(pcts, key=pcts.get)
    return out


def harvest_preview(run_dir: Path) -> list[Path]:
    """入庫要留的圖：final.png + 水平 360 的那幾張(有就留)。"""
    run_dir = Path(run_dir)
    shots = []
    final = run_dir / "final.png"
    if final.is_file():
        shots.append(final)
    frames = run_dir / "final_360_frames"
    if frames.is_dir():
        shots += sorted(frames.glob("*.png"))
    return shots


# ─────────────────────────────────────────────────────────────────── add(入庫)
def add(run_dir: Path, *, keywords=None, min_score=DEFAULT_MIN_SCORE,
        variant_id=None, base="infinigen", lineage=None, request=None,
        force=False, round_tag=None, hand_picked=False, log=print) -> dict:
    """把一個 generator run 的產物依 Factory 分類存進庫。

    回傳 {"ok": bool, "variant_id": str|None, "reason": str}。
    ★不過門檻就不入庫★，但也不刪任何東西 —— 失敗的產物留在原地當 evidence。
    """
    run_dir = Path(run_dir).resolve()
    det = detect_run(run_dir, round_tag=round_tag)
    if det is None:
        return {"ok": False, "variant_id": None,
                "reason": f"認不出這是哪種 generator 產物: {run_dir}"}
    if det.get("reject"):
        return {"ok": False, "variant_id": None, "reason": det["reject"]}
    if det.get("generator") != "ProceduralGenerator":
        return {"ok": False, "variant_id": None,
                "reason": "BlenderGenerator 不再入庫；只保留 score.json"}

    fmeta = read_meta_file(det["factory_txt"])
    factory = fmeta.get("factory")
    fqn = fmeta.get("fqn")
    if not factory or not fqn:
        return {"ok": False, "variant_id": None,
                "reason": f"{overlay}/factory.txt 缺 factory / fqn"}

    quality = harvest_quality(run_dir, round_tag=round_tag)
    # ★結構閘門與品質分數要分開★：前者問「這份 code 跑得起來嗎」，後者問
    #   「長出來的東西像不像」。混在一起，Blender 那條線(沒有數值分數)會被用
    #   結構性的理由擋掉，看起來像 generator 壞了。
    if quality["stage_c_gate"] is False and not force:
        return {"ok": False, "variant_id": None,
                "reason": "stage_c_gate 未通過(--force 可覆寫)"}
    if quality["seed_check"] is False and not force:
        return {"ok": False, "variant_id": None,
                "reason": "seed sensitivity 未通過(--force 可覆寫)"}
    if det["generator"] == "BlenderGenerator" and not quality["best_round"] and not force:
        return {"ok": False, "variant_id": None,
                "reason": "summary.md 沒有權威的 Best (LLM judge) 標記"}
    score = quality["match_pct"]
    if score is None and not force:
        return {"ok": False, "variant_id": None,
                "reason": "沒有品質分數 —— 用 --score N 指定，或先跑統一 scorer"}
    if score is not None and score < min_score and not force:
        return {"ok": False, "variant_id": None,
                "reason": f"match_pct={score} < 門檻 {min_score}"}

    # ★只存真正被改寫的檔★ —— 與 glue.py 的合併衝突偵測同一把尺。
    changed = changed_files(det["pkg_root"])
    if not changed:
        return {"ok": False, "variant_id": None,
                "reason": "這份 overlay 與基底沒有任何差異 —— 沒有東西值得入庫"}

    if request is None:
        req_file = run_dir / "request.txt"
        request = (req_file.read_text(encoding="utf-8").strip()
                   if req_file.is_file() else run_dir.name)

    vid = variant_id or f"{slugify(run_dir.name)}_{slugify(factory).replace('factory', '')}"
    vid = vid.strip("_")
    dest = VARIANTS / factory / vid
    if dest.exists():
        if not force:
            return {"ok": False, "variant_id": vid,
                    "reason": f"{vid} 已存在(--force 可覆寫)"}
        shutil.rmtree(dest)

    (dest / "changed").mkdir(parents=True, exist_ok=True)
    for rel, src in sorted(changed.items()):
        dst = dest / "changed" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(det["factory_txt"], dest / "factory.txt")

    shots = harvest_preview(run_dir)
    if shots:
        (dest / "preview").mkdir(exist_ok=True)
        for s in shots[:8]:
            shutil.copy2(s, dest / "preview" / s.name)

    entry_files = sorted(changed.keys())
    meta = {
        "variant_id": vid,
        "factory": factory,
        "class_name": fqn.rsplit(".", 1)[1],
        "fqn": fqn,
        "generator": det["generator"],
        "base": base,
        "lineage": list(lineage or []),
        "request": request,
        "keywords": sorted(set(keywords or auto_keywords(request, factory))),
        "quality": {"match_pct": score, "scored_at": _now(),
                    "scorer": quality["source"] or "manual"},
        "hand_picked": bool(hand_picked),
        "round": det.get("round"),
        "provenance": {"run_dir": str(run_dir),
                       "best_round": quality["best_round"],
                       "stage_c_gate": quality["stage_c_gate"],
                       "seed_check": quality["seed_check"]},
        "entry_files": entry_files,
        # 入庫★當下★基底沒有的檔(例 bauble.py)。之後基底改版時，stale 檢查要靠
        # 這份紀錄才能分辨「本來就是新增的」與「基底把它拿掉了」。
        "new_files": [rel for rel in entry_files
                      if not (base_pkg() / rel).is_file()],
        # ★跨檔改寫★ = Phase 3 的變體隔離做不到的那一類(import 要重寫)。
        "cross_file": len(entry_files) > 1,
        "placement": {k: fmeta[k] for k in ("anchoring", "kind", "role")
                      if k in fmeta},
        "added_at": _now(),
    }
    (dest / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with index_lock():
        idx = load_index()
        idx["variants"][vid] = {k: meta[k] for k in
                                ("factory", "generator", "request", "keywords",
                                 "quality", "entry_files", "cross_file", "base",
                                 "lineage", "added_at")}
        idx["variants"][vid]["path"] = str(dest.relative_to(LIB))
        save_index(idx)

    if run_dir.resolve().is_relative_to(ASSET_ROOT.resolve()):
        entry = _build_good_list_entry(
            run_dir,
            {"match_pct": score, "scored_at": meta["added_at"],
             "note": quality["source"] or "manual"},
            Path(score_paths(run_dir, round_tag=round_tag)[0]),
        )
        if entry:
            _upsert_good_list_entry(entry, min_score=min_score)

    log(f"[library] ✅ 入庫 {factory}/{vid}  score={score}  "
        f"改寫 {len(entry_files)} 個檔  cross_file={meta['cross_file']}")
    return {"ok": True, "variant_id": vid, "reason": "", "meta": meta}


def auto_keywords(request: str, factory: str) -> list[str]:
    """沒給 keywords 時的確定性後備 —— 不呼叫 API，只切詞。

    ⚠️ 這★不是★語意檢索，只是讓 index 有東西可以先過濾。真正的判斷在 lookup()
       的語意層(或人自己補 --keywords)。
    """
    stop = {"the", "and", "with", "for", "a", "an", "of", "in", "on"}
    kws = [w for w in tokenize(request) if w not in stop]
    return sorted(set(kws + [factory.replace("Factory", "").lower()]))[:12]


def _score_holder(run_dir: Path, round_tag: str | None = None) -> Path:
    run_dir = Path(run_dir).resolve()
    return run_dir / round_tag if round_tag else run_dir


def score_paths(run_dir: Path, round_tag: str | None = None) -> tuple[Path, Path]:
    holder = _score_holder(run_dir, round_tag=round_tag)
    return holder / SCORE_FILE, holder / LEGACY_SCORE_FILE


def _read_json(path: Path) -> dict | None:
    if not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_good_list() -> list[dict]:
    if not GOOD_LIST_JSONL.is_file():
        return []
    rows = []
    for line in GOOD_LIST_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_good_list(rows: list[dict]) -> Path:
    GOOD_LIST_JSONL.parent.mkdir(parents=True, exist_ok=True)
    GOOD_LIST_JSONL.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# good_list —— 只有 procedural 的高分資產",
        "#",
        f"# 更新於 {_now()}",
        f"# 門檻 >= {DEFAULT_MIN_SCORE}",
        "# score\tasset_id\tfactory\trequest",
        "",
    ]
    for row in rows:
        request = " ".join(str(row.get("request") or "").split())
        lines.append(
            f"{row.get('match_pct', '')}\t{row.get('asset_id', '')}\t"
            f"{row.get('factory', '')}\t{request[:160]}"
        )
    GOOD_LIST_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return GOOD_LIST_JSONL


def _good_list_index() -> dict[str, dict]:
    return {row.get("variant_id"): row for row in load_good_list() if row.get("variant_id")}


def _good_list_sort_key(row: dict) -> tuple[int, str, str]:
    return (
        -int(row.get("match_pct") or 0),
        str(row.get("factory") or ""),
        str(row.get("variant_id") or ""),
    )


def _upsert_good_list_entry(entry: dict, *, min_score: int = DEFAULT_MIN_SCORE) -> Path:
    rows = [
        row for row in load_good_list()
        if row.get("variant_id") != entry.get("variant_id")
    ]
    match_pct = entry.get("match_pct")
    if match_pct is not None and int(match_pct) >= min_score:
        rows.append(entry)
    rows.sort(key=_good_list_sort_key)
    return _write_good_list(rows)


def _asset_store_run_dir(asset_id: str, generator: str = "procedural") -> Path:
    return ASSET_ROOT / generator / asset_id / "run"


def _score_record_path(run_dir: Path) -> Path | None:
    run_dir = Path(run_dir)
    for path in (run_dir / SCORE_FILE, run_dir / LEGACY_SCORE_FILE):
        if path.is_file():
            return path
    return None


def _score_record(run_dir: Path, round_tag: str | None = None) -> tuple[dict | None, Path | None]:
    run_dir = Path(run_dir)
    holder = _score_holder(run_dir, round_tag=round_tag)
    for path in (holder / SCORE_FILE, holder / LEGACY_SCORE_FILE):
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8")), path
        except json.JSONDecodeError:
            continue
    return None, None


def _build_good_list_entry(run_dir: Path, score: dict, score_path: Path) -> dict | None:
    run_dir = Path(run_dir).resolve()
    det = detect_run(run_dir)
    if not det or det.get("reject") or det.get("generator") != "ProceduralGenerator":
        return None

    factory_meta = read_meta_file(det["factory_txt"])
    factory = factory_meta.get("factory")
    request = read_request(run_dir) or ""
    overlay_root = Path(det["overlay"]).resolve()
    overlay_pkg = overlay_root / "infinigen"
    if not overlay_pkg.is_dir():
        return None

    entry_files = sorted(changed_files(overlay_pkg))
    new_files = [rel for rel in entry_files if not (base_pkg() / rel).is_file()]
    match_pct = score.get("match_pct")
    if match_pct is None:
        return None

    return {
        "variant_id": run_dir.parent.name,
        "asset_id": run_dir.parent.name,
        "kind": "procedural",
        "generator": "ProceduralGenerator",
        "factory": factory,
        "fqn": factory_meta.get("fqn"),
        "request": request,
        "keywords": auto_keywords(request, factory or "ProceduralFactory"),
        "match_pct": int(match_pct),
        "score": int(match_pct),
        "scored_at": score.get("scored_at"),
        "note": score.get("note"),
        "run_dir": str(run_dir),
        "score_path": str(score_path),
        "overlay_root": str(overlay_root),
        "overlay_pkg": str(overlay_pkg),
        "entry_files": entry_files,
        "new_files": new_files,
        "path": str(overlay_root),
    }


def refresh_good_list(root: Path | None = None, *, min_score: int = DEFAULT_MIN_SCORE,
                      log=print) -> Path:
    root = Path(root or ASSET_ROOT)
    proc_root = root / "procedural"
    rows: list[dict] = []
    if proc_root.is_dir():
        for asset_dir in sorted(p for p in proc_root.iterdir() if p.is_dir()):
            run_dir = asset_dir / "run"
            if not run_dir.is_dir():
                continue
            score, score_path = _score_record(run_dir)
            if score is None:
                legacy = run_dir / LEGACY_SCORE_FILE
                if legacy.is_file():
                    try:
                        score = json.loads(legacy.read_text(encoding="utf-8"))
                        score_path = legacy
                        if not (run_dir / SCORE_FILE).is_file():
                            (run_dir / SCORE_FILE).write_text(
                                legacy.read_text(encoding="utf-8"), encoding="utf-8"
                            )
                            score_path = run_dir / SCORE_FILE
                    except json.JSONDecodeError:
                        score = None
            if not score or score.get("ok") is not True:
                continue
            match_pct = score.get("match_pct")
            if match_pct is None or int(match_pct) < min_score:
                continue
            entry = _build_good_list_entry(run_dir, score, score_path or (run_dir / SCORE_FILE))
            if entry:
                rows.append(entry)

    rows.sort(key=lambda row: (
        -int(row.get("match_pct") or 0),
        str(row.get("factory") or ""),
        str(row.get("variant_id") or ""),
    ))
    path = _write_good_list(rows)
    log(f"[library] good_list → {path} ({len(rows)} entries)")
    return path


def refresh_score_files(root: Path | None = None, *, log=print) -> dict:
    """把舊的 library_score.json 逐個補成 score.json。

    這只是檔名整理，不重算分數；有 legacy 就搬，沒有就略過。
    """
    root = Path(root or ASSET_ROOT)
    touched: list[Path] = []
    for generator in ("procedural", "blender"):
        gen_root = root / generator
        if not gen_root.is_dir():
            continue
        for asset_dir in sorted(p for p in gen_root.iterdir() if p.is_dir()):
            run_dir = asset_dir / "run"
            legacy = run_dir / LEGACY_SCORE_FILE
            dest = run_dir / SCORE_FILE
            if not legacy.is_file() or dest.is_file():
                continue
            try:
                text = legacy.read_text(encoding="utf-8")
                json.loads(text)
            except json.JSONDecodeError:
                continue
            dest.write_text(text, encoding="utf-8")
            touched.append(dest)
    log(f"[library] score.json → {len(touched)} files")
    return {"count": len(touched), "paths": [str(path) for path in touched]}


def _normalize_lookup_row(row: dict, *, source: str) -> dict | None:
    variant_id = row.get("variant_id") or row.get("asset_id")
    factory = row.get("factory")
    generator = row.get("generator") or ("ProceduralGenerator"
                                         if row.get("kind") == "procedural" else None)
    if generator != "ProceduralGenerator" or not variant_id or not factory:
        return None

    match_pct = row.get("match_pct")
    if match_pct is None:
        match_pct = row.get("score")
    if match_pct is None and isinstance(row.get("quality"), dict):
        match_pct = row["quality"].get("match_pct")
    try:
        match_pct = int(match_pct) if match_pct is not None else None
    except (TypeError, ValueError):
        match_pct = None

    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    entry_files = row.get("entry_files") or []
    if not isinstance(entry_files, list):
        entry_files = []
    path = row.get("run_dir") or row.get("path") or row.get("overlay_root")
    return {
        "variant_id": str(variant_id),
        "factory": str(factory),
        "request": str(row.get("request") or ""),
        "keywords": keywords,
        "match_pct": match_pct,
        "cross_file": bool(row.get("cross_file") or len(entry_files) > 1),
        "path": str(path) if path else "",
        "generator": generator,
        "score": match_pct,
        "_source": source,
    }


def _lookup_rows() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()

    for row in load_good_list():
        normalized = _normalize_lookup_row(row, source="good_list")
        if not normalized:
            continue
        seen.add(normalized["variant_id"])
        rows.append(normalized)

    idx = load_index()
    for vid, info in idx.get("variants", {}).items():
        if vid in seen:
            continue
        normalized = _normalize_lookup_row(
            {
                "variant_id": vid,
                "factory": info.get("factory"),
                "request": info.get("request"),
                "keywords": info.get("keywords"),
                "match_pct": (info.get("quality") or {}).get("match_pct"),
                "cross_file": info.get("cross_file"),
                "path": info.get("path"),
                "generator": info.get("generator"),
            },
            source="index",
        )
        if normalized:
            rows.append(normalized)
    return rows


def _materialize_source(variant_id: str) -> tuple[dict, Path, Path]:
    idx = load_index()
    info = idx["variants"].get(variant_id)
    if info is not None:
        src = LIB / info["path"]
        meta_path = src / "meta.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta, src / "changed", src / "factory.txt"

    entry = _good_list_index().get(variant_id)
    if entry:
        overlay_root = Path(entry.get("overlay_root") or entry.get("path") or
                            entry.get("run_dir") or "").resolve()
        overlay_pkg = Path(entry.get("overlay_pkg") or overlay_root / "infinigen")
        factory_txt = overlay_root / "factory.txt"
        if overlay_pkg.is_dir() and factory_txt.is_file():
            entry_files = list(entry.get("entry_files") or [])
            if not entry_files:
                entry_files = [str(rel) for rel in sorted(changed_files(overlay_pkg))]
            meta = {
                "variant_id": variant_id,
                "factory": entry.get("factory"),
                "fqn": entry.get("fqn"),
                "entry_files": entry_files,
                "new_files": list(entry.get("new_files") or []),
            }
            return meta, overlay_pkg, factory_txt

    raise KeyError(f"庫裡沒有這個變體: {variant_id}")


# ──────────────────────────────────────────────────────── materialize(重建 overlay)
def materialize(variant_id: str, out_dir: Path, *, log=print) -> dict:
    """把庫裡的變體重建成一棵★合法的 overlay★(對當下的基底)。

    做法與 param_adapter.build_overlay / 3_procedural_generator.build_overlay
    完全一致：`cp -rs` 出整棵 symlink 鏡像，再把改寫檔實體覆蓋上去。

    ⚠️ ★不能只放改寫的那幾個檔★ —— infinigen 是一般套件(有 __init__.py)，
       overlay/infinigen 會整包遮蔽掉真的那個，infinigen.core.* 會全部 import 不到。
       (param_adapter.py:219 記著同一個坑)
    """
    meta, src_root, factory_txt = _materialize_source(variant_id)

    # ★stale 檢查★：基底更新後，被改寫的檔可能已經不在原路徑。
    #   這時候硬疊上去會產生一棵「看起來對、import 卻走官方版」的 overlay，
    #   正是 placer docstring 記的 0728 11-E 那個坑。⇒ 明確報錯，不要猜。
    known_new = set(meta.get("new_files", []))
    stale = [rel for rel in meta["entry_files"]
             if not (base_pkg() / rel).is_file() and rel not in known_new]
    if stale:
        raise RuntimeError(
            f"★變體 {variant_id} 對當下基底 stale★：{len(stale)} 個改寫檔在基底已不存在"
            f"(例 {stale[0]}) —— 需要重新生成，不要硬疊")

    out_dir = Path(out_dir).resolve()
    overlay = out_dir / "overlay"
    subprocess.run(["rm", "-rf", str(overlay)], check=True)
    overlay.mkdir(parents=True)
    subprocess.run(["cp", "-rs", str(base_pkg().resolve()), str(overlay / "infinigen")],
                   check=True)

    for rel in meta["entry_files"]:
        dst = overlay / "infinigen" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            dst.unlink()                      # ★先斷 symlink 再寫★，否則會寫穿到基底
        shutil.copy2(src_root / rel, dst)

    shutil.copy2(factory_txt, overlay / "factory.txt")
    (out_dir / "library_source.json").write_text(
        json.dumps({"variant_id": variant_id, "factory": meta["factory"],
                    "fqn": meta["fqn"], "materialized_at": _now(),
                    "base_pkg": str(base_pkg())}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    log(f"[library] ♻️ materialize {variant_id} → {overlay} "
        f"({len(meta['entry_files'])} 個改寫檔)")
    return {"overlay": overlay, "meta": meta}


# ─────────────────────────────────────────────────────────────── lookup(查庫)
def tokenize(text: str) -> set:
    """切出用來比對的詞。

    ⚠️ ★中文不能只抓連續漢字串★(2026-08-20 實測抓到)：`[一-鿿]{2,}` 是貪婪的，
       「一隻孔雀」會變成★一個★ token，永遠對不上入庫時存的 keyword「孔雀」。
       ⇒ 連續漢字再切成字元 bigram(一隻/隻孔/孔雀)，孔雀才配得上。
    """
    text = str(text or "").lower()
    out = set(re.findall(r"[a-zA-Z]{3,}", text))
    for run in re.findall(r"[一-鿿]+", text):
        out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


def lookup(request: str, *, factory=None, top=5) -> list[dict]:
    """回傳候選變體，依關鍵字/字面相似度排序。

    ★這一層是確定性的 prefilter，不是最終判斷。★語意判斷(彩色孔雀＝孔雀、
    黑白孔雀＝要改)由呼叫端的 LLM 做，本函式只負責把候選集縮小到可以餵進 prompt。
    """
    want = str(request or "").lower()
    want_tokens = tokenize(want)

    out = []
    for info in _lookup_rows():
        if factory and info["factory"] != factory:
            continue
        # keyword 可能是片語("polar bear")，也要拆開來比對，否則
        # 「a polar bear on ice」對不上 keyword「polar bear」。
        kws = set(k.lower() for k in info.get("keywords", []))
        kw_tokens = set()
        for k in kws:
            kw_tokens |= tokenize(k)
        overlap = len(want_tokens & (kws | kw_tokens))
        ratio = difflib.SequenceMatcher(None, want, str(info.get("request", "")).lower()).ratio()
        out.append({
            "variant_id": info["variant_id"], "factory": info["factory"],
            "request": info.get("request", ""), "keywords": sorted(kws),
            "match_pct": info.get("match_pct"),
            "cross_file": info.get("cross_file"),
            "keyword_overlap": overlap, "text_ratio": round(ratio, 3),
            "path": info.get("path", ""),
            "generator": info.get("generator"),
        })
    out.sort(key=lambda c: (c["keyword_overlap"], c["text_ratio"],
                            c.get("match_pct") if c.get("match_pct") is not None else -1),
             reverse=True)
    return out[:top]


# ────────────────────────────────────────────────── reindex(產生人可讀的視圖)
def reindex(idx: dict | None = None) -> Path:
    """由 index.json 產生 Expand_Factory.txt。

    ★這個檔是唯讀視圖★ —— director / composer 的 prompt 會讀它，但任何手改都會
    被下一次 reindex 覆蓋。要改內容請改 index.json(或重新 add)。
    """
    idx = idx if idx is not None else load_index()
    by_fac = {}
    for vid, info in sorted(idx["variants"].items()):
        by_fac.setdefault(info["factory"], []).append((vid, info))

    lines = [
        "# Expand_Factory —— 擴充資產庫現有內容",
        "#",
        "# ★這份是 index.json 產生的唯讀視圖，手改會被 reindex 覆蓋★",
        f"# 更新於 {_now()}",
        f"# 共 {len(idx['variants'])} 個變體，分屬 {len(by_fac)} 個 Factory",
        "",
    ]
    for fac in sorted(by_fac):
        lines.append(f"## {fac}")
        for vid, info in by_fac[fac]:
            q = (info.get("quality") or {}).get("match_pct")
            kws = ", ".join(info.get("keywords", [])[:8])
            flag = " [cross_file]" if info.get("cross_file") else ""
            base = info.get("base", "infinigen")
            lines.append(f"  {vid}")
            lines.append(f"      request : {info.get('request','')}")
            lines.append(f"      keywords: {kws}")
            lines.append(f"      score   : {q}   generator: {info.get('generator')}"
                         f"   base: {base}{flag}")
        lines.append("")
    EXPAND_TXT.write_text("\n".join(lines), encoding="utf-8")
    return EXPAND_TXT


# ──────────────────────────────────────────────────── route(語意判斷：用/改/生)
ROUTER_SYSTEM = """\
You decide whether a requested 3D asset can reuse an existing generated asset.

For each request you are given candidates already in the library. Every candidate
is the SAME Infinigen Factory as the request, so only appearance matters.

Reply with one verdict per request:
  REUSE  - a candidate already IS this object. Only cosmetic wording differs
           (\"a colorful peacock\" vs \"a peacock with its train fanned out\").
           Reusing it must not lose any explicitly requested feature.
  ADAPT  - a candidate is clearly the same KIND of object but a stated feature
           differs (\"a black and white peacock\" against a colorful peacock).
           The candidate becomes the starting point for a new edit.
  FRESH  - no candidate is close enough. Prefer FRESH when unsure.

Be strict. A wrong REUSE silently ships the wrong object; a wrong FRESH only
costs generation time. Any explicitly requested colour, material, count, or
body part that a candidate lacks means ADAPT or FRESH, never REUSE.

Output ONLY JSON:
{"routes": [{"id": "<request id>", "verdict": "REUSE|ADAPT|FRESH",
             "variant_id": "<candidate id, or null for FRESH>",
             "why": "<one short sentence>"}]}"""


def route_requests(items, *, top=5, log=print) -> dict:
    """對一批請求做語意判斷，回 {id: {verdict, variant_id, why}}。

    items = [{"id":..., "request":..., "factory":...}, ...]

    ★一次呼叫判斷整批★ —— 每個物體各叫一次 API 沒有比較準，只是比較貴。
    ★沒有候選的請求連問都不問★，直接 FRESH：庫裡沒有同 Factory 的東西時，
      再聰明的語意判斷也只能回 FRESH。
    """
    out, asked = {}, []
    for it in items:
        cands = lookup(it["request"], factory=it.get("factory"), top=top)
        if not cands:
            out[it["id"]] = {"verdict": FRESH, "variant_id": None,
                             "why": "庫裡沒有同 Factory 的候選"}
            continue
        asked.append((it, cands))

    if not asked:
        return out

    lines = []
    for it, cands in asked:
        lines.append(f'REQUEST id={it["id"]} factory={it.get("factory")}\n'
                     f'  wanted: {it["request"]}')
        for c in cands:
            lines.append(f'  candidate {c["variant_id"]}: {c["request"]}')
        lines.append("")

    client, model = _router_client()
    resp = client.messages.create(
        model=model, max_tokens=1500, system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    routes = json.loads(m.group(0))["routes"] if m else []

    known = {c["variant_id"] for _it, cands in asked for c in cands}
    for r in routes:
        vid = r.get("variant_id")
        verdict = str(r.get("verdict", FRESH)).upper()
        # ★agent 只能挑我們給過的候選★ —— 幻覺出一個不存在的 variant_id 會讓
        #   下游 materialize 直接 KeyError，而且看起來像庫壞了。
        if verdict in (REUSE, ADAPT) and vid not in known:
            log(f"[library] ⚠️ {r.get('id')}: 回了不存在的 variant_id={vid!r} ⇒ FRESH")
            verdict, vid = FRESH, None
        if verdict not in (REUSE, ADAPT, FRESH):
            verdict, vid = FRESH, None
        out[r.get("id")] = {"verdict": verdict,
                            "variant_id": vid if verdict != FRESH else None,
                            "why": r.get("why", "")}

    for it, _c in asked:                      # agent 漏答的一律 FRESH，不要猜
        out.setdefault(it["id"], {"verdict": FRESH, "variant_id": None,
                                  "why": "router 沒有回覆這一項"})
    return out


def _router_client():
    """與 generator 共用同一組 API 設定(.env)，不要另外一套。"""
    import importlib.util
    ew = ROOT / "asset_generator" / "extraworld"
    if str(ew) not in sys.path:
        sys.path.insert(0, str(ew))
    spec = importlib.util.spec_from_file_location(
        "_ew_procedural_generator", ew / "3_procedural_generator.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._client(), mod.OPUS_MODEL


# ────────────────────────────────────────────────── score(統一評分／★留分數★)


def _generator_verifier():
    """借用 3_procedural_generator.py 的 run_verifier。

    ★不要在這裡另抄一份 VERIFIER_SYSTEM★ —— 兩把尺一旦分岔，庫裡的分數就不可比，
    而且是安靜的(舊的逐輪分數與新的統一分數會混在同一個欄位裡)。
    """
    import importlib.util
    ew = ROOT / "asset_generator" / "extraworld"
    if str(ew) not in sys.path:
        sys.path.insert(0, str(ew))
    spec = importlib.util.spec_from_file_location(
        "_ew_procedural_generator", ew / "3_procedural_generator.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_verifier


def read_request(run_dir) -> str | None:
    """兩種 generator 的 request 存在不同地方。

    procedural : request.txt
    blender    : summary.md 的 `SOURCE SCENE DESCRIPTION:` 段
    """
    run_dir = Path(run_dir)
    rf = run_dir / "request.txt"
    if rf.is_file():
        return rf.read_text(encoding="utf-8").strip()

    sm = run_dir / "summary.md"
    if sm.is_file():
        m = re.search(r"SOURCE SCENE DESCRIPTION:\s*\n(.+?)(?:\n\s*\n|\nmodel:)",
                      sm.read_text(encoding="utf-8"), re.S)
        if m:
            return m.group(1).strip()
    return None


def find_target_image(run_dir):
    """agent 當時看的 ref 圖(有就一起餵給 verifier，判斷會準得多)。"""
    run_dir = Path(run_dir)
    for name in ("target_single.png", "target_image.png", "target_ref.png"):
        q = run_dir / name
        if q.is_file():
            return q
    sm = run_dir / "summary.md"
    if sm.is_file():
        m = re.search(r"^reference:\s*(\S+)", sm.read_text(encoding="utf-8"), re.M)
        if m and Path(m.group(1)).is_file():
            return Path(m.group(1))
    return None


def score_run(run_dir, *, force=False, round_tag=None, log=print) -> dict:
    """對一個 run 的 final.png 跑一次統一評分，把分數★留在 run 目錄裡★。

    ★為什麼在庫的邊界評，而不是改兩個 generator★
      ・BlenderGenerator 只有 judge 挑最佳輪，★不留數值分數★；procedural 每輪留
        match_pct，但那是「逐輪」的，與 selector 的選擇會打架(北極熊 selector 選
        r2=45，r1 其實 55)。
      ・在邊界評 = 兩種 generator 同一把尺、同一個輸入(最終 final.png)，而且
        ★你自己手動跑的東西也適用★。
      ・成本也低：一個資產一次呼叫，不是每輪一次。
    """
    run_dir = Path(run_dir).resolve()
    # 指定輪次時要評★那一輪的圖★。評 final.png 再宣稱是 r3 的分數，是把分數
    # 掛到另一份產物上 —— 庫裡的數字就不再對應庫裡的 code。
    holder = _score_holder(run_dir, round_tag=round_tag)
    dest, legacy_dest = score_paths(run_dir, round_tag=round_tag)
    if dest.is_file() and not force:
        return json.loads(dest.read_text(encoding="utf-8"))
    if legacy_dest.is_file() and not dest.is_file() and not force:
        text = legacy_dest.read_text(encoding="utf-8")
        dest.write_text(text, encoding="utf-8")
        return json.loads(text)

    final = (holder / "render.png") if round_tag else (run_dir / "final.png")
    if not final.is_file():
        return {"ok": False, "reason": f"沒有可評分的 render: {final}"}
    request = read_request(run_dir)
    if not request:
        return {"ok": False, "reason": f"找不到 request 文字: {run_dir}"}

    det = detect_run(run_dir, round_tag=round_tag)
    factory_meta = read_meta_file(det["factory_txt"]) if det and det.get("factory_txt") else {}
    target = find_target_image(run_dir)
    pct, note = _generator_verifier()(final, request, target)
    rec = {"ok": True, "match_pct": pct, "note": note, "scored_at": _now(),
           "scorer": "library_scorer_v1", "render": str(final),
           "round": round_tag, "target": str(target or ""),
           "generator": det.get("generator") if det else None,
           "factory": factory_meta.get("factory") if factory_meta else None,
           "fqn": factory_meta.get("fqn") if factory_meta else None,
           "asset_id": run_dir.name,
           "run_dir": str(run_dir),
           "score_path": str(dest)}
    dest.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log(f"[library] 🎯 {run_dir.name}{'/' + round_tag if round_tag else ''}: "
        f"match_pct={pct}  ({note[:60]})")
    return rec


# ────────────────────────────────────────────── auto_ingest(生成完自動入庫)
AUTOADD_ENV = "BI_LIBRARY_AUTOADD"
MIN_SCORE_ENV = "BI_LIBRARY_MIN_SCORE"


def autoadd_enabled() -> bool:
    return os.environ.get(AUTOADD_ENV, "1") != "0"


def env_min_score() -> int:
    try:
        return int(os.environ.get(MIN_SCORE_ENV, ""))
    except ValueError:
        return DEFAULT_MIN_SCORE


def unique_variant_id(stem: str) -> str:
    """同名再跑一次不要覆蓋掉舊的 —— 舊的可能才是好的那一個。"""
    idx = load_index()["variants"]
    vid = slugify(stem)
    if vid not in idx:
        return vid
    n = 2
    while f"{vid}_{n}" in idx:
        n += 1
    return f"{vid}_{n}"


def auto_ingest(run_dir, *, round_tag=None, min_score=None, log=print) -> dict:
    """生成結束後自動評分並入庫。★永遠不可以讓生成失敗★。

    同時服務兩條路徑：
      ・run_e2e.sh Step 5(經 3_generator.py)
      ・單獨跑 generator —— experiments/assets/run_procedural_code_edits_0818.py
        是★直接 subprocess 叫 3_procedural_generator.py★，繞過 3_generator.py
    ⇒ 所以 hook 放在★生成器內部★而不是 orchestrator，兩條路才都涵蓋得到。

    ⚠️ 這裡的例外全部吞掉只記 log：資產已經生完了，入不了庫是庫的問題，
       不該讓一個花了半小時的生成回報成失敗。
    """
    run_dir = Path(run_dir)
    try:
        if not autoadd_enabled():
            log(f"[library] 自動入庫關閉({AUTOADD_ENV}=0)")
            return {"ok": False, "reason": "disabled"}
        threshold = env_min_score() if min_score is None else min_score

        sc = score_run(run_dir, round_tag=round_tag, log=log)
        if not sc.get("ok"):
            log(f"[library] 自動入庫略過：{sc.get('reason')}")
            return {"ok": False, "reason": sc.get("reason")}

        det = detect_run(run_dir, round_tag=round_tag)
        if det is None or det.get("reject"):
            reason = det.get("reject") if det else "認不出 generator 產物"
            log(f"[library] 自動入庫略過：{reason}")
            return {"ok": False, "reason": reason}

        if run_dir.resolve().is_relative_to(ASSET_ROOT.resolve()):
            if det.get("generator") != "ProceduralGenerator":
                log("[library] BlenderGenerator 只保留 score.json，不再入庫")
                return {"ok": False, "reason": "blender_legacy_skipped", "score": sc}
            entry = _build_good_list_entry(run_dir, sc, Path(sc.get("score_path") or score_paths(run_dir, round_tag=round_tag)[0]))
            if entry:
                path = _upsert_good_list_entry(entry, min_score=threshold)
                log(f"[library] good_list updated → {path}")
                return {"ok": True, "reason": "good_list_updated", "meta": entry}
            log("[library] 自動入庫略過：無法建立 good_list entry")
            return {"ok": False, "reason": "good_list_entry_failed"}

        r = add(run_dir, round_tag=round_tag, min_score=threshold,
                variant_id=unique_variant_id(run_dir.name), log=log)
        if not r["ok"]:
            log(f"[library] 自動入庫略過：{r['reason']}")
        return r
    except Exception as ex:                     # noqa: BLE001
        log(f"[library] ⚠️ 自動入庫失敗(不影響生成結果)：{type(ex).__name__}: {ex}")
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}


# ─────────────────────────────────────────────────────────────── bulk(批次入庫)
def find_runs(root: Path) -> list[Path]:
    """在一棵 e2e/output 樹底下找出所有 generator run 目錄。

    ⚠️ ★不要用 os.walk 掃整個 output/★ —— 那棵樹有數十 GB 的 blend 與 frame，
       實測走完超過兩分鐘。generator 產物的位置是固定的，直接對著它 glob。
    """
    root = Path(root)
    runs = []
    for pat in ("3_procedural/*", "3_blender/*",
                "*/3_procedural/*", "*/3_blender/*",
                "*/*/3_procedural/*", "*/*/3_blender/*"):
        runs += [d for d in root.glob(pat) if d.is_dir()]
    return sorted(set(runs))


def bulk_add(root: Path, *, min_score=DEFAULT_MIN_SCORE, force=False,
             log=print) -> dict:
    """把一棵樹底下所有夠格的 run 入庫。跳過的一律說明理由，不要安靜略過。"""
    runs = find_runs(root)
    added, skipped = [], []
    for run in runs:
        vid = f"{slugify(run.name)}_{slugify(run.parent.parent.name)}"
        r = add(run, min_score=min_score, variant_id=vid, force=force,
                log=lambda *_: None)
        if r["ok"]:
            added.append((r["variant_id"], r["meta"]["factory"],
                          r["meta"]["quality"]["match_pct"]))
        else:
            skipped.append((run.name, r["reason"]))
    log(f"[library] 掃到 {len(runs)} 個 run：入庫 {len(added)}，跳過 {len(skipped)}")
    for vid, fac, sc in added:
        log(f"    ✅ {fac:24s} {vid:40s} score={sc}")
    for name, why in skipped:
        log(f"    ⏭️  {name:40s} {why}")
    return {"added": added, "skipped": skipped, "scanned": len(runs)}


# ─────────────────────────────────────────────────────────────────────── CLI
def _cmd_add(a):
    kws = [k.strip() for k in a.keywords.split(",")] if a.keywords else None
    r = add(Path(a.run_dir), keywords=kws, min_score=a.min_score,
            variant_id=a.variant_id, base=a.base,
            lineage=a.lineage.split(",") if a.lineage else None,
            request=a.request, force=a.force, round_tag=a.round,
            hand_picked=a.hand_picked)
    if not r["ok"]:
        print(f"[library] ⛔ 沒有入庫: {r['reason']}")
        return 1
    return 0


def _cmd_score(a):
    r = score_run(Path(a.run_dir), force=a.force, round_tag=a.round)
    if not r.get("ok"):
        print(f"[library] ⛔ 評分失敗: {r.get('reason')}")
        return 1
    print(f"[library] match_pct={r['match_pct']}  {r.get('note','')}")
    return 0


def _cmd_bulk(a):
    if a.score:
        for run in find_runs(Path(a.root)):
            if (run / "final.png").is_file():
                score_run(run, force=False)
    r = bulk_add(Path(a.root), min_score=a.min_score, force=a.force)
    return 0 if r["added"] or not r["scanned"] else 0


def _cmd_route(a):
    items = [{"id": a.id or "q", "request": a.request, "factory": a.factory}]
    r = route_requests(items)
    for k, v in r.items():
        print(f"  {k}: {v['verdict']}  variant={v['variant_id']}  — {v['why']}")
    return 0


def _cmd_lookup(a):
    cands = lookup(a.request, factory=a.factory, top=a.top)
    if not cands:
        print("[library] 庫裡沒有候選 ⇒ FRESH")
        return 0
    for c in cands:
        print(f"  {c['variant_id']:34s} {c['factory']:22s} "
              f"overlap={c['keyword_overlap']} ratio={c['text_ratio']} "
              f"score={c['match_pct']}")
        print(f"      request: {c['request']}")
    return 0


def _cmd_materialize(a):
    materialize(a.variant_id, Path(a.out))
    return 0


def _cmd_list(a):
    rows = sorted(
        _lookup_rows(),
        key=lambda row: (-(row.get("match_pct") or -1), row.get("factory") or "",
                         row.get("variant_id") or ""),
    )
    for row in rows:
        if a.factory and row["factory"] != a.factory:
            continue
        print(f"  {row['factory']:22s} {row['variant_id']:34s} "
              f"score={row.get('match_pct')} generator={row.get('generator')}")
    print(f"[library] 共 {len(rows)} 個變體")
    return 0


def _cmd_reindex(a):
    p = reindex()
    print(f"[library] → {p}")
    return 0


def _cmd_refresh_good_list(a):
    refresh_good_list(log=print)
    return 0


def _cmd_refresh_scores(a):
    refresh_score_files(log=print)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Expanded per-Factory asset library")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="把一個 generator run 存進庫")
    p.add_argument("run_dir")
    p.add_argument("--keywords", default=None, help="逗號分隔；不給就自動切詞")
    p.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    p.add_argument("--variant-id", default=None)
    p.add_argument("--base", default="infinigen", help="ADAPT 時填來源 variant_id")
    p.add_argument("--lineage", default=None)
    p.add_argument("--request", default=None)
    p.add_argument("--force", action="store_true", help="繞過門檻/覆寫同名變體")
    p.add_argument("--round", default=None,
                   help="用指定輪次的 code(例 r3)，而不是 selector 挑的那一輪")
    p.add_argument("--hand-picked", action="store_true",
                   help="標記成人工挑選 —— 分數以外的品質依據")
    p.set_defaults(fn=_cmd_add)

    p = sub.add_parser("score", help="對一個 run 跑統一評分並把分數留在 run 目錄")
    p.add_argument("run_dir")
    p.add_argument("--force", action="store_true", help="重新評分並覆蓋既有分數")
    p.add_argument("--round", default=None, help="評指定輪次的 render.png")
    p.set_defaults(fn=_cmd_score)

    p = sub.add_parser("bulk", help="把一棵 e2e/output 樹底下夠格的 run 全部入庫")
    p.add_argument("root")
    p.add_argument("--score", action="store_true",
                   help="★會呼叫 API★：先幫沒有分數的 run 評分再入庫")
    p.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_bulk)

    p = sub.add_parser("lookup", help="查庫(確定性 prefilter)")
    p.add_argument("request")
    p.add_argument("--factory", default=None)
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(fn=_cmd_lookup)

    p = sub.add_parser("route", help="★會呼叫 API★：語意判斷用庫/改庫/重生")
    p.add_argument("request")
    p.add_argument("--factory", default=None)
    p.add_argument("--id", default=None)
    p.set_defaults(fn=_cmd_route)

    p = sub.add_parser("materialize", help="把變體重建成 overlay")
    p.add_argument("variant_id")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=_cmd_materialize)

    p = sub.add_parser("list")
    p.add_argument("--factory", default=None)
    p.set_defaults(fn=_cmd_list)

    p = sub.add_parser("reindex")
    p.set_defaults(fn=_cmd_reindex)

    p = sub.add_parser("refresh-good-list")
    p.set_defaults(fn=_cmd_refresh_good_list)

    p = sub.add_parser("refresh-scores")
    p.set_defaults(fn=_cmd_refresh_scores)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())

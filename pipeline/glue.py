"""★一條龍缺的三個接點★(2026-08-06)

在此之前 pipeline 是「每一段都能跑，但沒有接起來」：
  ・沒有任何東西讀 director 的 edit_plan.json 去叫資產生成器
  ・manifest 的 `code` 欄★全部是手填的★(scratchpad 底下每支測試腳本都自己塞)
  ・一次只掛得動一個 overlay(PYTHONPATH 先掛的會蓋掉後者的共用模組)

這支補上這三個接點，三個子命令：

  genlist   edit_plan.json ──▶ generation_list.json
            (BeyondInfinigen director 的格式 → ExtraWorld 3_generator.py 吃的格式)
            ・正式 pipeline 會把所有非 delete 資產正規化成 ProceduralGenerator

  overlays  資產生成產物 ──▶ ★分流成 per-asset overlay roots★ + code→overlay 對照表
            ・BlenderGenerator 保留為歷史/研究分支，不再由正式 pipeline 選用
            ・正式 pipeline 拒絕 ProceduralSampler；舊 director plan 會在 genlist 升級
            ・★staging★：每個資產保留自己的 overlay root，table 只記路徑。
              同名同檔的不同內容不再互撞，placer 直接按 code 路由各自載入。

  fillcode  對照表 ──▶ 填進 manifest 的 code 欄(按 code_from 路由)
"""

import argparse
import difflib
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paths import ASSET_ROOT, asset_store_dir  # noqa: E402
from asset_generator.extraworld.instance_contract import (  # noqa: E402
    logical_instance_contract,
    single_instance_prompt,
)
from placement_contract import overlay_base_pkg  # noqa: E402
import scatter_catalog as sc  # noqa: E402

EW = ROOT / "asset_generator" / "extraworld"
PARAM_ADAPTER = EW / "param_adapter.py"
PY = sys.executable

# 所有 overlay 的鏡射基底(0806 五節統一到 infinigen_o)。與 param_adapter.CODEBASE
# 同一個位置 —— 「哪些檔真的被改寫過」要拿它當參照，見 changed_files()。
BASE_PKG = Path(os.environ.get("INFINIGEN_ROOT", ROOT / "third_party" / "infinigen")).resolve() / "infinigen"


# ─────────────────────────────────────────────────────────── genlist
def _factory_class_name(value):
    if value is None:
        return None
    return str(value).split("(", 1)[0]


def _route_factory_class_name(value):
    if value is None:
        return None
    parts = re.findall(r"[A-Za-z0-9]+", str(value))
    if not parts:
        return None
    stem = "".join(part[:1].upper() + part[1:] for part in parts)
    return stem if stem.endswith("Factory") else f"{stem}Factory"


def _factory_class_name_from_meta(meta: dict) -> str | None:
    factory = _factory_class_name(meta.get("factory"))
    if factory:
        return factory
    fqn = str(meta.get("fqn") or "")
    if fqn:
        return _factory_class_name(fqn.rsplit(".", 1)[-1])
    return None


def _load_force_reuse_map():
    """Optional batch hook: force selected asset ids to library REUSE."""
    map_path = os.environ.get("BI_FORCE_LIBRARY_REUSE_MAP")
    if not map_path:
        return {}
    p = Path(map_path)
    if not p.is_file():
        raise SystemExit(f"BI_FORCE_LIBRARY_REUSE_MAP not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("variants"), dict):
        raw = raw["variants"]
    if not isinstance(raw, dict):
        raise SystemExit("BI_FORCE_LIBRARY_REUSE_MAP must be a JSON object")
    out = {}
    for asset_id, spec in raw.items():
        if isinstance(spec, str):
            spec = {"library_variant": spec}
        if not isinstance(spec, dict) or not spec.get("library_variant"):
            raise SystemExit(
                f"invalid force-reuse entry for {asset_id!r}: {spec!r}")
        out[str(asset_id)] = spec
    return out


def cmd_genlist(a):
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    force_reuse = _load_force_reuse_map()
    force_used = set()
    objs, skipped, reused = [], [], []
    for e in plan.get("edits", []):
        if not e.get("feasible", True):
            skipped.append((e.get("id"), e.get("blocked_reason", "")[:60]))
            continue
        action = e.get("action") or e.get("op")
        if action == "delete":
            skipped.append((e.get("id"), "delete action has no asset generation"))
            continue
        gen = e.get("generator")
        # Formal BeyondInfinigen asset generation is procedural-only. Stale plans
        # may still contain BlenderGenerator / ProceduralSampler routes; normalize
        # them here so resume/recovery cannot bypass the current policy.
        if gen != "ProceduralGenerator":
            print(f"[genlist] {e.get('id')}: generator={gen!r} "
                  "→ ProceduralGenerator")
            gen = "ProceduralGenerator"
        # ★factory 為 null 時用 host_factory★ —— 憑空造的東西(雪橇)在 catalog 裡沒有
        #   對應 factory，director 會合成一個宿主名。add 自己建 box，任何名字都行。
        fac = _factory_class_name(e.get("factory") or e.get("host_factory"))
        route_fac = fac
        if action == "add" and not route_fac:
            route_fac = _route_factory_class_name(e.get("name"))
        if e.get("placement") == "fish_school":
            # 魚群換的是粒子母本，不配對 box ⇒ route 就是魚本身的類名，
            # 不能套 hero replace 的 target_factory(FishSchoolFactory)。
            route_fac = fac
        elif action == "replace":
            # Replace plans may request a new appearance factory (for example
            # PalmTreeFactory) while the existing placeholders remain TreeFactory.
            # Generation/library lookup should use the requested asset factory;
            # placer routing must use the placeholder class name.
            route_fac = _factory_class_name(
                e.get("target_factory") or e.get("target") or fac)
        source_prompt = e.get("spec") or e.get("name")
        scene_count = e.get("count") or 1
        verdict = e.get("library_verdict") or "FRESH"
        entry = {
            "id": e.get("id") or _slug(e.get("name")),
            "name": e.get("name"),
            "action": action,
            "generator": gen,
            "factory": fac,
            "route_factory": route_fac,
            # ★下游要能分辨 hero 與 ground_scatter★ —— cmd_overlays 的
            #   「庫存類名必須等於需求」那道檢查只對 hero 成立(見該處註解)。
            "placement": e.get("placement"),
            "scatter_system": e.get("scatter_system"),
            "prompt": source_prompt,
            "instance_prompt": single_instance_prompt(source_prompt, scene_count),
            "logical_instance_contract": logical_instance_contract(scene_count),
            "count": e.get("count"),
            "anchoring": e.get("anchoring"),
            "world_motion": e.get("world_motion"),
            "local_animation": e.get("local_animation"),
            "n_masters": e.get("n_masters"),
            "refs": e.get("refs"),
            "seed_mode": "different" if (e.get("count") or 1) > 1 else "same",
            "library_verdict": verdict,
            "library_variant": e.get("library_variant"),
        }
        forced = force_reuse.get(entry["id"])
        if forced:
            expected_factory = forced.get("factory")
            if expected_factory and expected_factory != entry["factory"]:
                raise SystemExit(
                    f"force-reuse factory mismatch for {entry['id']}: "
                    f"plan has {entry['factory']}, map expects {expected_factory}")
            entry["library_verdict"] = "REUSE"
            entry["library_variant"] = forced["library_variant"]
            if forced.get("route_factory"):
                entry["route_factory"] = forced["route_factory"]
            force_used.add(entry["id"])
        # ★REUSE 的項目要移出 objects★ —— 3_generator.py 只讀 objects，所以搬走
        #   就是「不生成」，★不需要教生成器認識資產庫★。overlays 再從 reused
        #   把它們接回來。
        if entry["library_verdict"] == "REUSE" and entry.get("library_variant"):
            reused.append(entry)
        else:
            objs.append(entry)
    unused_force = sorted(set(force_reuse) - force_used)
    if unused_force:
        raise SystemExit(
            "force-reuse map id(s) were not present in the edit plan: "
            + ", ".join(unused_force))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"objects": objs, "reused": reused},
                              indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"[genlist] {len(objs)} 個要生成 → {out}")
    for o in reused:
        print(f"    ♻️ {o['id']:26s} 直接用庫存 {o['library_variant']}")
    for o in objs:
        if o.get("library_verdict") == "ADAPT" and o.get("library_variant"):
            print(f"    ✎ {o['id']:26s} 以庫存 {o['library_variant']} 為改寫基底"
                  f"(需先跑 glue.py prepare-adapt)")
    for o in objs:
        route = o.get("route_factory")
        route_note = f" route={route}" if route and route != o["factory"] else ""
        print(f"    {o['id']:26s} {o['generator']:20s} factory={o['factory']} "
              f"n={o['count']} anchoring={o['anchoring']}{route_note}")
    for i, why in skipped:
        print(f"    ⏭️  {str(i):26s} 跳過: {why}")


def _slug(name):
    return re.sub(r"[^0-9a-z]+", "_", str(name or "asset").lower()).strip("_")


# ─────────────────────────────────────────────────────────── overlays
def _read_meta(p):
    meta = {}
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _assert_python_identifier(name: str, *, label: str):
    if not re.fullmatch(r"[A-Za-z_]\w*", str(name or "")):
        raise ValueError(f"{label} must be a Python identifier, got {name!r}")


def _factory_meta_for_route(ov: Path, *, requested_id: str,
                            source_factory: str, route_factory: str) -> str:
    """Return factory.txt text for the class name placer must route to.

    A replace request can use a more specific source factory while the existing
    placeholder collections keep their original factory class name.  In that
    case create a tiny class-name shim in the overlay and point factory.txt at
    it, so placement.populate_all receives a class whose __name__ matches the
    placeholder collection.
    """
    ov = Path(ov)
    meta_path = ov / "factory.txt"
    meta = _read_meta(meta_path)
    source_fqn = meta.get("fqn")
    if not source_fqn:
        raise ValueError(f"{meta_path} missing fqn")
    source_factory = _factory_class_name(source_factory)
    route_factory = _factory_class_name(route_factory)
    if not source_factory or not route_factory:
        raise ValueError("source_factory and route_factory are required")
    if source_factory == route_factory:
        return meta_path.read_text(encoding="utf-8")

    _assert_python_identifier(source_factory, label="source_factory")
    _assert_python_identifier(route_factory, label="route_factory")
    source_module, source_class = source_fqn.rsplit(".", 1)
    _assert_python_identifier(source_class, label="source class")

    package = ov / "infinigen" / "assets" / "objects" / "generated"
    package.mkdir(parents=True, exist_ok=True)
    init_py = package / "__init__.py"
    if not init_py.exists():
        init_py.write_text("", encoding="utf-8")

    module_name = f"_bi_{_slug(requested_id)}_{_slug(route_factory)}_shim"
    _assert_python_identifier(module_name, label="shim module")
    shim = package / f"{module_name}.py"
    shim.write_text(
        f"from {source_module} import {source_class} as _SourceFactory\n\n\n"
        f"class {route_factory}(_SourceFactory):\n"
        f"    pass\n",
        encoding="utf-8")

    routed = dict(meta)
    routed.update({
        "fqn": f"infinigen.assets.objects.generated.{module_name}.{route_factory}",
        "factory": route_factory,
        "entry": f"assets/objects/generated/{module_name}.py",
        "source_factory": source_factory,
        "source_fqn": source_fqn,
        "note": f"route shim for {requested_id}: {route_factory} -> {source_factory}",
    })
    order = ("fqn", "factory", "entry", "role", "kind", "anchoring",
             "source_factory", "source_fqn", "note")
    seen = set()
    lines = []
    for key in order:
        if key in routed:
            lines.append(f"{key}: {routed[key]}")
            seen.add(key)
    for key in sorted(k for k in routed if k not in seen):
        lines.append(f"{key}: {routed[key]}")
    return "\n".join(lines) + "\n"


def _best_script(gdir):
    """Return the exact BlenderGenerator round named by its authoritative Best tag.

    Do not accept the first ``round_XX`` appearing in ``summary.md``.  Earlier
    lines are a per-round log and can mention a failed round before the final
    ``**Best ...**`` decision.  That used to wrap the failed script even when
    the judge had selected a later successful round.
    """
    patterns = (
        # Current generator output, including ``Best (LLM judge)`` and repair tags.
        r"^\s*\*\*Best(?:\s+\([^\n)]*\))?\s*:\s*"
        r"(round_\d+(?:_fix\d+)?)\*\*",
        # Small machine-readable sidecars used by some older runs.
        r"^\s*best_(?:tag|round|script)\s*:\s*"
        r"(round_\d+(?:_fix\d+)?)(?:\.py)?\s*$",
    )
    for filename in ("summary.md", "best.md"):
        path = gdir / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                candidate = gdir / f"{match.group(1)}.py"
                if candidate.is_file():
                    return candidate
    scripts = sorted(gdir.glob("round_*.py"))
    return scripts[-1] if scripts else None


def changed_files(ov_pkg: Path) -> dict:
    """overlay 裡★真正被改寫★的檔：{relpath: 絕對路徑}。

    ★★2026-08-07：這裡原本用「實體檔 = 改寫、symlink = 沒改寫」★★
      那個約定(0806 九節)★是錯的★。generator 為了給 agent 一塊可寫的工作區，
      會把 entry 的★整個子樹★攤成實體檔，改沒改都一樣：

        christmas_trees    實體檔 27 → 真的與基底不同的只有 ★5★
        red_nose_reindeer  實體檔 18 → ★3★

      ⇒ 衝突偵測拿灌水的集合去比，會報一堆假衝突並且 return 1 停掉整條 pipeline。
        實測 0806 批次：peacock ∩ panda = ★14 個共用實體檔，真正被改的 0 個★
        —— 孔雀動 bird.py、熊貓動 carnivore.py，八竿子打不著卻放不進同一個場景。
        而 creature 全系列都會攤開 creatures/util/* ⇒ ★任意兩隻動物都會撞★。

    ⇒ 改成★跟基底樹逐位元組比對★。這跟 placer 的 verify_overlay_not_stale 是
      同一把尺(symlink/未改寫的檔必須與基底逐字一致)，只是那邊拿來擋過期鏡射，
      這邊拿來算「誰改了什麼」。

    ⚠️ 新增的檔(基底沒有，例 bauble.py / red_nose.py)一律算改寫。
    """
    ov_pkg = Path(ov_pkg)
    out = {}
    if not ov_pkg.is_dir():
        return out
    for f in sorted(ov_pkg.rglob("*.py")):
        if f.is_symlink() or "__pycache__" in f.parts:
            continue                                     # 鏡射檔，鐵定沒改
        rel = f.relative_to(ov_pkg)
        base = BASE_PKG / rel
        if base.is_file() and filecmp.cmp(f, base, shallow=False):
            continue                                     # 實體檔但內容與基底相同 ⇒ 只是工作區副本
        out[str(rel)] = f
    return out


def _reuse_procedural(scene_root: Path, requested: dict):
    """Find one safe renamed artifact for an explicit SKIP_GEN reuse run."""
    fac = requested.get("factory")
    want = str(requested.get("prompt") or requested.get("name") or "").lower()
    scored = []
    roots = [ASSET_ROOT / "procedural", scene_root / "3_procedural"]
    run_dirs: list[tuple[str, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if (d / "run").is_dir():
                run_dirs.append((d.name, d / "run"))
            elif d.is_dir():
                run_dirs.append((d.name, d))
    for source_id, d in run_dirs:
        ov = d / "code" / "overlay"
        meta = ov / "factory.txt"
        req = d / "request.txt"
        if not meta.is_file() or _read_meta(meta).get("factory") != fac:
            continue
        have = req.read_text(encoding="utf-8").strip().lower() if req.is_file() else ""
        score = difflib.SequenceMatcher(None, want, have).ratio() if have else 0.0
        scored.append((score, source_id, ov))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < 0.35:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
        return None
    return scored[0]


def cmd_overlays(a):
    gl = json.loads(Path(a.genlist).read_text(encoding="utf-8"))
    scene_root = Path(a.scene_root)
    merged = Path(a.out)
    sources = []                       # (route_factory, overlay_dir, requested_id, source_id, factory_txt)
    missing = []

    # ★庫存的 REUSE 項目在這裡接回來★：genlist 把它們移出 objects 讓生成器跳過，
    #   overlays 則對當下的基底重建一棵合法 overlay root，與新生成的產物分開 staging。
    for o in gl.get("reused", []):
        oid, fac, vid = o["id"], o["factory"], o.get("library_variant")
        route_fac = o.get("route_factory") or fac or _route_factory_class_name(o.get("name"))
        try:
            from asset_generator.library import librarian as lb
            out = lb.materialize(vid, asset_store_dir("procedural", oid, "library_reuse"),
                                 log=lambda m: print(f"[overlays] {m}"))
        except Exception as ex:
            print(f"[overlays] ⛔ {oid}: 庫存 {vid} 取用失敗 "
                  f"({type(ex).__name__}: {ex}) —— 停在 overlay 階段")
            missing.append(oid)
            continue
        declared = _factory_class_name_from_meta(_read_meta(out["overlay"] / "factory.txt"))
        if not declared:
            declared = fac
        # official_scatter / fish_school 由 apply_plan 直接用 overlay 自己的類別撒，
        # 不靠類名配對既有 box ⇒ 跟 ground_scatter 一樣不要求同名(2026-09-11)
        if declared != fac and o.get("placement") not in (
                "ground_scatter", "official_scatter", "fish_school"):
            # placement.py 用★類名★配對 box，名字不符就會完全找不到 box 而靜靜長不出來。
            print(f"[overlays] ⛔ {oid}: 庫存類名 {declared!r} 與需求 {fac!r} 不符")
            missing.append(oid)
            continue
        if declared != fac and o.get("placement") == "ground_scatter":
            # ★scatter 沒有這個約束★(2026-09-08)：上面那道檢查是為了 populate_all
            #   用 __name__ 配對 placeholder，那只發生在 hero。ground_scatter 是由
            #   apply_plan ★setattr 到 scatter 模組的全域符號★上，類名叫什麼都行
            #   (0907 smoke：FlowerFactory 的 overlay 塞進 FlowerPlantFactory 符號)。
            #   照 hero 的規則擋下來，等於庫裡有現成的向日葵也永遠用不到。
            #   下面的 route shim 會產生一個名為 route_fac 的子類，讓 factory.txt
            #   與 overlay_table 對外仍以 scatter 母本符號呈現(fillextracode 要用)。
            print(f"[overlays] ♻️ {oid}: ground_scatter 外觀替換，"
                  f"類名 {declared!r} → {route_fac!r}(scatter 不要求同名)")
        try:
            factory_txt = _factory_meta_for_route(
                out["overlay"], requested_id=oid,
                source_factory=declared, route_factory=route_fac)
        except Exception as ex:
            print(f"[overlays] ⛔ {oid}: route shim failed "
                  f"({type(ex).__name__}: {ex})")
            missing.append(oid)
            continue
        if route_fac != declared:
            print(f"[overlays] route shim {oid}: {route_fac} ← {declared}")
        sources.append((route_fac, out["overlay"], oid, f"library:{vid}", factory_txt))

    for o in gl["objects"]:
        oid, gen, fac = o["id"], o["generator"], o["factory"]
        route_fac = o.get("route_factory") or fac or _route_factory_class_name(o.get("name"))
        if gen == "ProceduralSampler":
            print(f"[overlays] ⛔ {oid}: formal pipeline 禁用 ProceduralSampler；"
                  "請重跑 glue.py genlist 升級為 ProceduralGenerator")
            missing.append(oid)
        elif gen == "ProceduralGenerator":
            gdir = asset_store_dir("ProceduralGenerator", oid, "run")
            ov = gdir / "code" / "overlay"
            if not (ov / "factory.txt").is_file():
                reused = _reuse_procedural(scene_root, o) if a.reuse_existing else None
                if reused is None:
                    print(f"[overlays] ⛔ {oid}: 找不到 {ov}/factory.txt —— 生成失敗?")
                    missing.append(oid)
                    continue
                score, source_id, ov = reused
                print(f"[overlays] ♻️ {oid} 重用 {source_id} "
                      f"(factory={fac}, request similarity={score:.3f})")
            else:
                source_id = oid
            declared = _factory_class_name_from_meta(_read_meta(ov / "factory.txt"))
            if not declared:
                declared = fac
            try:
                factory_txt = _factory_meta_for_route(
                    ov, requested_id=oid,
                    source_factory=declared, route_factory=route_fac)
            except Exception as ex:
                print(f"[overlays] ⛔ {oid}: route shim failed "
                      f"({type(ex).__name__}: {ex})")
                missing.append(oid)
                continue
            if route_fac != declared:
                print(f"[overlays] route shim {oid}: {route_fac} ← {declared}")
            sources.append((route_fac, ov, oid, source_id, factory_txt))
        elif gen == "BlenderGenerator":
            # BlenderGenerator：先把參數腳本包成 overlay
            gdir = asset_store_dir("BlenderGenerator", oid, "run")
            script = _best_script(gdir)
            if script is None:
                print(f"[overlays] ⛔ {oid}: {gdir} 裡沒有 round_*.py —— 生成失敗?")
                missing.append(oid)
                continue
            # ⚠️ ★--out-dir 不是 overlay 本身★ —— param_adapter 會在 out_dir 底下
            #    再建一層 overlay/(param_adapter.py:198)。第一版在這裡自己猜路徑，
            #    結果拿到空的 <...>/overlay/，實體檔 0 個、也沒有 factory.txt，
            #    ⇒ placer 退回基底的 factory.txt ⇒「overlay 類名不符」。
            #    ★真實路徑要讀 adapter.json★，不要猜。
            work = asset_store_dir("BlenderGenerator", oid, "adapter")
            cmd = [PY, str(PARAM_ADAPTER), "--script", str(script), "--name", oid,
                   "--out-dir", str(work), "--class-name", fac,
                   "--n-masters", str(o.get("n_masters") or 5), "--verify"]
            if o.get("anchoring"):
                cmd += ["--anchoring", o["anchoring"]]
            print(f"[overlays] param_adapter {oid} → class {fac}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[overlays] ⛔ {oid} param_adapter rc={r.returncode}\n{r.stdout[-600:]}"
                      f"\n{r.stderr[-600:]}")
                missing.append(oid)
                continue
            aj = work / "adapter.json"
            if not aj.is_file():
                print(f"[overlays] ⛔ {oid}: param_adapter rc=0 卻沒有 {aj}")
                missing.append(oid)
                continue
            adapter_info = json.loads(aj.read_text(encoding="utf-8"))
            verification = adapter_info.get("verify") or {}
            if not verification.get("ok"):
                print(f"[overlays] ⛔ {oid}: adapter spawn verification failed: "
                      f"{verification.get('error') or 'missing verification result'}")
                missing.append(oid)
                continue
            ov = Path(adapter_info["overlay"])
            # ★跟 Procedural 分支問同一個問題★ —— 先前只有那邊檢查 factory.txt，
            #   這邊無條件放行，空 overlay 才會一路滑到 placer 才被抓到。
            if not (ov / "factory.txt").is_file():
                print(f"[overlays] ⛔ {oid}: 找不到 {ov}/factory.txt")
                missing.append(oid)
                continue
            # ★Blender 的自動入庫在這裡★，不在 3_blender_generator 裡：
            #   生成當下還沒有 overlay，要等 param_adapter 把參數腳本包成
            #   AssetFactory 之後才有東西可以入庫。
            #   ⚠️ 因此★單獨跑 blender generator 不會自動入庫★(沒有 adapter 這一步)；
            #     那條路要自己跑 librarian add。procedural 沒有這個限制。
            _auto_ingest_library(gdir)
            declared = _factory_class_name_from_meta(_read_meta(ov / "factory.txt"))
            if not declared:
                declared = fac
            try:
                factory_txt = _factory_meta_for_route(
                    ov, requested_id=oid,
                    source_factory=declared, route_factory=route_fac)
            except Exception as ex:
                print(f"[overlays] ⛔ {oid}: route shim failed "
                      f"({type(ex).__name__}: {ex})")
                missing.append(oid)
                continue
            if route_fac != declared:
                print(f"[overlays] route shim {oid}: {route_fac} ← {declared}")
            sources.append((route_fac, ov, oid, oid, factory_txt))
        else:
            print(f"[overlays] ⛔ {oid}: 不支援的 generator={gen!r}")
            missing.append(oid)

    if missing and os.environ.get("BI_SOFT_GATES") == "1" and sources:
        # ★軟關卡★(2026-10-20)：缺的資產不擋整條 pipeline，apply_plan 會把它降級成官方外觀
        print(f"[overlays] ⚠️ (soft gate) 缺少 {len(missing)} 個資產: {', '.join(missing)} —— 改用官方外觀繼續")
        Path(a.table).with_name("overlay_missing.json").write_text(json.dumps(missing, ensure_ascii=False) + "\n", encoding="utf-8")
        missing = []
    if missing:
        print(f"[overlays] ⛔ 缺少 {len(missing)} 個資產: {', '.join(missing)} "
              "—— 停在 overlay 階段")
        return 1

    if not sources:
        print("[overlays] ⛔ 一個可用資產都沒有 —— 停在這裡")
        return 1

    # ── 分流 staging ──────────────────────────────────────────────────────
    def _remove_path(path: Path):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    _remove_path(merged)
    merged.mkdir(parents=True, exist_ok=True)

    table = {}
    unresolved = []
    for fac, ov, oid, source_id, factory_txt in sources:
        link = merged / oid
        _remove_path(link)
        link.symlink_to(Path(ov).resolve(), target_is_directory=True)
        _write_route_factory_meta(Path(ov).resolve(), fac, factory_txt)
        problem = _route_resolution_problem(Path(ov).resolve(), fac)
        if problem:
            unresolved.append(f"{oid}: {problem}")
        base_pkg = overlay_base_pkg(ov)
        table[oid] = {
            "overlay": str(link),
            "staged_overlay": str(link.resolve()),
            "id": oid,
            "factory": fac,
            "route_factory": fac,
            "source_id": source_id,
            "source_factory": _read_meta(Path(ov) / "factory.txt").get("factory", fac),
            "source_overlay": str(Path(ov).resolve()),
            "base_pkg": str(base_pkg) if base_pkg else str(BASE_PKG),
            "rewrote": sorted(
                k for k in changed_files(Path(ov) / "infinigen")
            ),
        }

    Path(a.table).write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    if unresolved:
        # ★在 Step 6 就擋★：以前要等 Step 9 placer 的類名 assert 才爆，前面的生成白跑兩小時。
        for line in unresolved:
            print(f"[overlays] ⛔ 類名接不上 box: {line}")
        return 1
    print(f"[overlays] ✅ staged {len(sources)} 個 overlay root → {merged}")
    for oid, info in table.items():
        print(f"    {oid:28s} ← {info['source_id']:22s} route={info['factory']} "
              f"改寫 {len(info['rewrote'])} 個檔")
    print(f"[overlays] 對照表 → {a.table}")
    return 0


def _write_route_factory_meta(ov: Path, route_factory: str, factory_txt: str) -> None:
    """把 route shim 的 factory.txt 寫成 factories/<route>.txt(2026-10-19)。

    _factory_meta_for_route 算出指向 shim 子類的內容，以前沒有寫出去 ⇒ 根 factory.txt
    仍是母本類名，hero 的 placer 用 box 名載入就 assert(1019 #13 WoodenDockFactory ←
    PalletFactory)。根 factory.txt 不動(overlay 常是資產庫目錄的 symlink，別的 scene
    / scatter 路徑還要讀母本類名)；overlay_imports.load_overlay_factory 在根檔名字
    對不上時本來就會改讀 factories/<expect_name>.txt。
    """
    if not factory_txt:
        return
    root_meta = _read_meta(ov / "factory.txt")
    root_cls = (root_meta.get("fqn") or "").rsplit(".", 1)[-1]
    if route_factory in (root_meta.get("factory"), root_cls):
        return
    target = ov / "factories" / f"{route_factory}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.read_text(encoding="utf-8") != factory_txt:
        target.write_text(factory_txt, encoding="utf-8")


def _route_resolution_problem(ov: Path, route_factory: str) -> str | None:
    """照 overlay_imports.load_overlay_factory 的規則，確認用 box 名能找到同名類別。"""
    meta_path = ov / "factory.txt"
    meta = _read_meta(meta_path)
    root_cls = (meta.get("fqn") or "").rsplit(".", 1)[-1]
    if route_factory not in (meta.get("factory"), root_cls):
        meta_path = ov / "factories" / f"{route_factory}.txt"
        if not meta_path.is_file():
            return (f"factory.txt 宣告 {root_cls or meta.get('factory')!r}，"
                    f"也沒有 factories/{route_factory}.txt")
        meta = _read_meta(meta_path)
    fqn = meta.get("fqn") or ""
    if fqn.rsplit(".", 1)[-1] != route_factory:
        return f"{meta_path.name} 的 fqn {fqn!r} 類名不是 {route_factory!r}"
    entry = meta.get("entry")
    if entry and not (ov / "infinigen" / entry).is_file():
        return f"{meta_path.name} 指向的 {entry} 不在 overlay 裡"
    return None


def _auto_ingest_library(run_dir: Path) -> dict:
    """把產物交給資產庫。★任何失敗都只記 log★，不影響 overlay staging。"""
    try:
        from asset_generator.library import librarian as lb
        return lb.auto_ingest(run_dir, log=lambda m: print(f"[overlays] {m}"))
    except Exception as ex:                                 # noqa: BLE001
        print(f"[overlays] [library] ⚠️ 自動入庫無法啟動(不影響本次執行)："
              f"{type(ex).__name__}: {ex}")
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}


# ──────────────────────────────────────────────────────── prepare-adapt
def cmd_prepare_adapt(a):
    """把 ADAPT 項目的庫存基底 materialize 出來，路徑寫回 generation_list。

    ★為什麼是獨立一步★：materialize 需要庫，而 3_generator.py 刻意不認識庫
    (REUSE 是靠「不出現在 objects 裡」來跳過的)。這一步把「庫 → 檔案路徑」解決掉，
    generator 只看到一個普通的 overlay 路徑。
    """
    from asset_generator.library import librarian as lb
    glp = Path(a.genlist)
    gl = json.loads(glp.read_text(encoding="utf-8"))
    scene_root = Path(a.scene_root)
    n = 0
    for o in gl.get("objects", []):
        if o.get("library_verdict") != "ADAPT" or not o.get("library_variant"):
            continue
        if o.get("generator") != "ProceduralGenerator":
            # BlenderGenerator 從零寫腳本，沒有「以既有 factory 原始碼為基底」這回事。
            print(f"[adapt] {o['id']}: generator={o.get('generator')} 不支援基底改寫 ⇒ 照常重生")
            continue
        vid = o["library_variant"]
        try:
            out = lb.materialize(vid, asset_store_dir("procedural", o["id"], "library_base"),
                                 log=lambda m: print(f"[adapt] {m}"))
        except Exception as ex:
            print(f"[adapt] ⛔ {o['id']}: 庫存 {vid} 取用失敗 "
                  f"({type(ex).__name__}: {ex}) ⇒ 照常重生")
            continue
        o["base_overlay"] = str(out["overlay"])
        n += 1
        print(f"[adapt] {o['id']:26s} 以庫存 {vid} 為基底 → {out['overlay']}")
    glp.write_text(json.dumps(gl, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[adapt] {n} 個項目改為從庫存基底出發")
    return 0


# ─────────────────────────────────────────────────────────── fillcode
def cmd_fillcode(a):
    mf = Path(a.manifest)
    m = json.loads(mf.read_text(encoding="utf-8"))
    table = json.loads(Path(a.table).read_text(encoding="utf-8"))
    hit = 0
    explicit = bool(m.get("explicit_code_routing"))
    # code that is explicitly routed to some row must not ALSO be matched by factory
    # name onto unclaimed rows: an official_scatter palm overlay declared as
    # TreeFactory would otherwise repaint every official TreeFactory collection.
    claimed = {str(p.get("code_from")) for p in m.get("placements", []) if p.get("code_from")}
    for p in m.get("placements", []):
        requested_id = str(p.get("code_from") or "")
        info = table.get(requested_id) if requested_id else None
        if not info and p.get("factory"):
            matches = [
                row for row in table.values()
                if isinstance(row, dict) and row.get("factory") == p.get("factory")
                and str(row.get("id")) not in claimed
            ]
            if len(matches) == 1:
                info = matches[0]
        if not info:
            continue
        if explicit and requested_id and requested_id != info.get("id"):
            # Generated code is routed only to the operation that requested it.
            # Factory-wide replacement deliberately stamps every collection of that
            # factory with the same code_from; add stamps only its target collection.
            continue
        # Sampler means "use official runtime Factory". Preserve code=None;
        # otherwise Placer would treat an official sample as generated code.
        p["code"] = info.get("overlay")
        p["code_from"] = info["id"]
        hit += 1
        print(f"[fillcode] {p['factory']:26s} ← {info['id']}")
    mf.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[fillcode] 填了 {hit} 筆")
    if hit == 0:
        # ★只有 ground_scatter 的 plan 一筆都不該對上★(2026-09-08)
        #   fillcode 走的是 HERO 那條線(manifest 的 placements)。ground_scatter
        #   由 GN scatter 產生，blend 裡沒有 placeholder，manifest 自然不會有
        #   任何一筆等著回填 —— 這是這類 plan 的常態，不是 factory 名對不上。
        #   apply_plan 會在需要 code 的 placement 上蓋 code_from，用它判斷。
        expecting = [row for row in m.get("placements", [])
                     if isinstance(row, dict) and row.get("code_from")]
        if not expecting:
            print("[fillcode] ↷ 這個 plan 沒有 hero 交付物(只有 ground_scatter) → "
                  "沒有 code 需要回填")
            return 0
        print("[fillcode] ⛔ 一筆都沒對上 —— manifest 的 factory 名與生成的 class 名不一致?")
        print(f"[fillcode]    等著回填的 placement: "
              f"{[row.get('factory') for row in expecting]}")
        print(f"[fillcode]    overlay_table 提供的 factory: "
              f"{[row.get('factory') for row in table.values() if isinstance(row, dict)]}")
        return 1
    return 0


# ─────────────────────────────────────────────── fillextracode
def cmd_fillextracode(a):
    """把 composer 寫在 extra_edits 裡的 code slug 換成★真實 overlay 目錄★。

    ★為什麼需要這一支★(2026-09-07)：
      fillcode 只走 manifest 的 `placements`(HERO 那條線)，extra_edits 從來沒有
      人改寫。而且 fillcode 在 step 8 裡是★跑在 apply_plan 之後★的，就算擴充它
      也來不及。於是 composer 寫的 "generated/<slug>" 會原封不動送進 apply_plan，
      load_overlay_factory 找不到 factory.txt ⇒ ground_scatter 的 create/replace
      拿不到我們生成的外觀。

      這支在 apply_plan ★之前★跑，用 overlay_table 把 slug 解析成絕對路徑。
      對不上就★把 code 拿掉★而不是留一個壞路徑 —— scatter 會用官方外觀長出來，
      這是可接受的降級；留著壞路徑則會讓整個 step 8 掛掉。
    """
    plan_path = Path(a.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    table = json.loads(Path(a.table).read_text(encoding="utf-8"))
    edits = plan.get("extra_edits") or []
    # ★密度語意等級由 director 決定，這裡★搬★過去★(2026-09-08)
    #   刻意★不★改 composer 的 schema：這次的失敗正是「叫 composer 填一個欄位，
    #   它沒填」(0907 寫 133.312、0908 直接省略)。少一個 LLM 環節就少一個失敗點。
    #   對照鍵用 scatter_system(= composer 的 target)，那是 director 逐字帶下來的。
    director_by_target = {}
    # getattr：既有測試(與舊呼叫端)建的 args 物件沒有這個屬性，缺了就是「不搬」。
    director_plan = getattr(a, "director_plan", None)
    if director_plan and Path(director_plan).is_file():
        dplan = json.loads(Path(director_plan).read_text(encoding="utf-8"))
        for de in dplan.get("edits", []):
            sys_name = de.get("scatter_system")
            if not sys_name:
                continue
            route = director_by_target.setdefault(
                str(sys_name).strip().lower(), {}
            )
            if de.get("scatter_density"):
                route["density_level"] = de["scatter_density"]
            if de.get("placement_spec") is not None:
                route["placement_spec"] = deepcopy(de["placement_spec"])
            if de.get("scatter_region") is not None:
                route["scatter_region"] = deepcopy(de["scatter_region"])
            if de.get("id") is not None:
                route["director_edit_id"] = de["id"]
    hit = dropped = 0
    for e in edits:
        route = director_by_target.get(
            str(e.get("target") or "").strip().lower(), {}
        )
        if route.get("density_level") and not e.get("density_level"):
            e["density_level"] = route["density_level"]
            print(f"[fillextra]   density_level={e['density_level']} ← director")
        for field in ("placement_spec", "scatter_region", "director_edit_id"):
            if route.get(field) is not None and e.get(field) is None:
                e[field] = deepcopy(route[field])
                print(f"[fillextra]   {field} ← director")
        code = e.get("code")
        if code and Path(code).is_absolute() and (Path(code) / "factory.txt").is_file():
            continue                      # 已經是可用的絕對路徑
        if not code:
            # ⚠️ ★這一支必須可重入★(2026-09-07 實測)：第一版對不上時直接
            #    e.pop("code")，於是 plan 裡的 slug ★永久消失★，從 step 8 續跑
            #    時這裡就 continue 掉，外觀再也接不回來 —— 修好比對邏輯也沒用。
            #    現在改成：沒有 code 時仍嘗試用 target→factory 解析(pipeline 對每個
            #    可行的 ground_scatter edit 都會生資產)，並保留上次的 slug 供診斷。
            if e.get("op") not in ("create", "replace"):
                continue
            code = e.get("code_unresolved") or ""
        slug = str(code).split("/")[-1]
        info = None
        # ★先用 director_edit_id 直接查表★(2026-09-12 3_simple coral_reef 實測)：
        #   composer 把 anemone_purple_bulb 寫成 generated/purple_bulb_anemone(字序不同)，
        #   slug 與尾碼都對不上 ⇒ 珊瑚又默默變官方外觀。extra_edit 本來就帶著 director 的
        #   資產 id，它就是 overlay_table 的 key，不需要猜。
        director_id = str(e.get("director_edit_id") or "")
        if director_id and isinstance(table.get(director_id), dict):
            info = table[director_id]
            print(f"[fillextra]   director_edit_id={director_id} 直接命中")
        for key, row in ([] if info is not None else table.items()):
            if not isinstance(row, dict):
                continue
            if key == slug or row.get("id") == slug or str(row.get("overlay", "")).endswith("/" + slug):
                info = row
                break
        if info is None:
            # ★改用 factory 比對★(2026-09-07 e2e 實測)：composer 自己造 slug，
            #   它★不保證等於 director 的 edit id★ —— 實測 director 的
            #   `sunflower_field` 被 composer 寫成 `generated/yellow_sunflower_field`，
            #   於是名字比對全部落空、外觀被降級成官方版。
            #   scatter 的母本 factory 是確定的(catalog 查得到)，用它比對才穩，
            #   這也正是 fillcode 對 HERO placements 的做法。
            want = None
            spec = sc.get(e.get("target"))
            if spec:
                want = spec.get("factory_symbol")
            if not want:
                want = str(e.get("target") or "")     # replace：target 本身就是 factory 名
            matches = [row for row in table.values()
                       if isinstance(row, dict) and row.get("factory") == want]
            # ★同一個 factory 有兩個資產時，用 slug 的尾碼決勝★(2026-09-12)
            #   0912 coral_reef：orange_sea_anemone(hero) 與 coral_blossom_bush(scatter)
            #   ★都是 CoralFactory★ ⇒ 兩筆命中 ⇒ len==1 不成立 ⇒ 默默改用官方珊瑚，
            #   使用者看到的珊瑚「一直沒被換」就是這樣來的。composer 造的 slug 是
            #   `pink_blue_coral_blossom_bush`，尾碼正好就是資產 id，拿它決勝。
            if len(matches) > 1 and slug:
                by_suffix = [row for row in matches
                             if slug.endswith(str(row.get("id") or ""))
                             and row.get("id")]
                if len(by_suffix) == 1:
                    matches = by_suffix
                    print(f"[fillextra]   factory={want} 命中 {len(matches)} 筆以上，"
                          f"用 slug 尾碼決勝 → {matches[0].get('id')}")
            if len(matches) == 1:
                info = matches[0]
                print(f"[fillextra]   slug 對不上，改用 factory={want} 比對")
        overlay = (info or {}).get("overlay")
        if overlay and (Path(overlay) / "factory.txt").is_file():
            e["code"] = overlay
            e["code_from"] = (info or {}).get("id")
            e.pop("code_unresolved", None)
            hit += 1
            print(f"[fillextra] {e.get('op')} {e.get('target')} ← {overlay}")
        else:
            # ★不要把資訊丟掉★ —— 留著 slug，下次修好比對還能重試。
            if code:
                e["code_unresolved"] = code
            e.pop("code", None)
            dropped += 1
            print(f"[fillextra] ⚠️ {e.get('op')} {e.get('target')}: "
                  f"對不上 overlay(slug={slug!r}) → 改用官方外觀")
    # ★魚群 / 官方撒點★(2026-09-11)：code_from 就是 director 的 edit id，直接查表。
    #   對不上不降級 —— 換不成就等於這個資產沒進場景，留壞路徑讓 apply_plan 報錯。
    for kind in ("swarm_edits", "official_scatter_edits"):
        for e in plan.get(kind) or []:
            info = table.get(str(e.get("code_from") or ""))
            overlay = (info or {}).get("overlay") if isinstance(info, dict) else None
            if overlay and (Path(overlay) / "factory.txt").is_file():
                e["code"] = overlay
                hit += 1
                print(f"[fillextra] {kind} {e.get('code_from')} ← {overlay}")
            else:
                print(f"[fillextra] ⛔ {kind} {e.get('code_from')}: overlay_table 沒有它")
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    print(f"[fillextra] {hit} 筆接上 overlay，{dropped} 筆降級成官方外觀")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genlist"); g.add_argument("--plan", required=True); g.add_argument("--out", required=True)
    o = sub.add_parser("overlays")
    o.add_argument("--genlist", required=True); o.add_argument("--scene-root", required=True)
    o.add_argument("--out", required=True); o.add_argument("--table", required=True)
    o.add_argument("--reuse-existing", action="store_true")
    pa = sub.add_parser("prepare-adapt")
    pa.add_argument("--genlist", required=True); pa.add_argument("--scene-root", required=True)
    pa.set_defaults()

    f = sub.add_parser("fillcode"); f.add_argument("--manifest", required=True); f.add_argument("--table", required=True)
    fe = sub.add_parser("fillextracode")
    fe.add_argument("--plan", required=True); fe.add_argument("--table", required=True)
    fe.add_argument("--director-plan", default=None,
                    help="director 的 edit_plan.json；用來把 scatter_density 搬過來")
    a = ap.parse_args()
    return {"genlist": cmd_genlist, "overlays": cmd_overlays,
            "prepare-adapt": cmd_prepare_adapt, "fillcode": cmd_fillcode,
            "fillextracode": cmd_fillextracode}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())

"""
placer —— ★階段 C★：把 asset generator 產出的 Factory code 塞進「已經定好的 placeholder box」。

════════════════════════════════════════════════════════════════════════
★三階段 pipeline(老師的順序，2026-07-28 定案)★
════════════════════════════════════════════════════════════════════════
  ★階段 A(編輯)★  apply_plan.py
      coarse blend + edit_plan.json
        → ★新的 coarse blend★(含新增/刪除/移動後的 placeholder box)
        → ★placement_manifest.json★(每個 box 需要哪個 Factory + 什麼資料)
      ⇒ ★停在這裡。之後再也不用動 coarse blend。★

  ★階段 B(生成)★  asset generator(另外的 agent)
      讀 manifest 的需求 → 產出 Factory code(overlay 目錄 + factory.txt)

  ★階段 C(縫合)★  ★本支★
      新 coarse blend + manifest + 各自的 code
        → populate → ★populated blend★
      ⇒ ★box 的位置/數量/尺寸完全不變，只是把東西長進去★
      ⇒ 這還不是 final；正式 pipeline 之後必須另跑 fine_terrain + contract。

  ★為什麼要拆★：舊做法(scatter_new_hero.py --populate)把 A 和 C 綁在一起，
    ⇒ ★populate 的那一刻就決定了長出什麼★，
      generator 還沒產出 code 時只能長官方版。
    拆開後，★同一份 coarse blend 可以反覆換不同的 code 重跑 C★。

════════════════════════════════════════════════════════════════════════
★關鍵機制(讀 code 確認)★
════════════════════════════════════════════════════════════════════════
  placement.py:288  re.fullmatch("placeholders:((.*)\\((\\d*)\\))", col.name)
  placement.py:292  ★if classname != factory_class.__name__: continue★
  placement.py:304  fac_inst = factory_class(int(fac_seed), **kwargs)

  ⇒ populate_all ★用「類名」配對 collection，用「傳進去的 class」生成★
  ⇒ ★所以傳 overlay 的 FacClass 進去，就會用你的 code 長進官方命名的 box 裡★
  ⚠️ ★overlay 的 class 名必須與 box 的名字一致★
     (例：box 叫 placeholders:TreeFactory(123) → overlay 也要叫 TreeFactory)
     這正是 generator 產物「介面不可改」的原因。

用法:
  python placer.py -- <coarse.blend> <out.blend> <manifest.json> <report.json>
  python placer.py -- <coarse.blend> --dry-run <manifest.json> <report.json>

manifest 格式(階段 A 產出，可手動編輯把 code 填進去):
{
  "placements": [
    {"factory": "TreeFactory", "collection": "placeholders:TreeFactory(3040046)",
     "count": 231, "in_view": 44,
     "code": "generated/blue_tree",        ← ★階段 B 填這欄★，不填就用官方版
     "populate_kwargs": {"vis_cull": 4}},
    {"factory": "BushFactory", "collection": "placeholders:BushFactory(36234574)",
     "count": 271, "in_view": 87, "code": null}
  ]
}
"""
import pathlib
import hashlib
import bpy, sys, json, importlib, importlib.util, re, gin, logging, os, math
from pathlib import Path

argv = sys.argv[sys.argv.index("--") + 1:]
IN_BLEND, OUT_BLEND, MANIFEST_JSON, REPORT_JSON = argv[0], argv[1], argv[2], argv[3]
DRY_RUN = (OUT_BLEND == "--dry-run")
ALLOW_UNQUALIFIED_OUTPUT = os.environ.get("BI_ALLOW_UNQUALIFIED_OUTPUT") == "1"
# ★ablation 專用的窄旗標★(2026-09-21)
#   只繞過「擺放品質不合格就 raise」這一件事，★其餘行為完全不變★。
#   不能改用 BI_ALLOW_UNQUALIFIED_OUTPUT —— 它同時會讓 rigged 資產跳過 proxy fit
#   (見下面 1236 附近的 preserve_generator_scale)，那是另一個變數，會污染 ablation。
#   用途：SA1(Agent 直接給 3D 座標)這類條件的前提就是「原樣採用、不檢查」——
#   讓契約去擋或讓 review 去修，等於把我們的機制加回去，就不是 ablation 了。
#   不合格的事實仍然完整寫進 report(status / hard_failures)，供離線量測。
ABLATION_NO_PLACEMENT_GATE = (os.environ.get("BI_ABLATION_NO_PLACEMENT_GATE") == "1"
                              or os.environ.get("BI_SOFT_GATES") == "1")   # soft gate(2026-10-20)
FORCE_POPULATE_ALL = os.environ.get("BI_FORCE_POPULATE_ALL") == "1"

PH_COLL_RE = re.compile(r"placeholders:((.*)\((\d*)\))")

manifest = json.loads(Path(MANIFEST_JSON).read_text(encoding="utf-8"))
placements = manifest.get("placements", [])
print(f"[placer] {len(placements)} 筆 placement", flush=True)

bpy.ops.wm.open_mainfile(filepath=IN_BLEND)
print(f"[placer] opened {IN_BLEND}", flush=True)

# Establish the requested deliverable range before authoring world-motion
# actions. Fine terrain receives this range through Gin, but placer previously
# inherited the input blend's default (usually 1..72), so a 1..48 E2E validated
# the middle of a 72-frame loop as though it were the closing frame.
frame_start_env = os.environ.get("BI_FRAME_START")
frame_end_env = os.environ.get("BI_FRAME_END")
if (frame_start_env is None) != (frame_end_env is None):
    raise ValueError("BI_FRAME_START and BI_FRAME_END must be provided together")
if frame_start_env is not None:
    frame_start = int(frame_start_env)
    frame_end = int(frame_end_env)
    if frame_start < 1 or frame_end < frame_start:
        raise ValueError(f"invalid requested frame range: {frame_start}..{frame_end}")
    bpy.context.scene.frame_start = frame_start
    bpy.context.scene.frame_end = frame_end
    bpy.context.scene.frame_set(frame_start)
    print(f"[placer] frame range → {frame_start}..{frame_end}", flush=True)
cams = [o for o in bpy.data.objects if o.type == "CAMERA"]

from infinigen.core.placement import placement as _pl
from infinigen.core.tagging import tag_system
from infinigen.core.util import blender as butil
from infinigen.core.util import logging as _logging_util


class _SafeSuppress:
    """Process-local replacement for Infinigen's stdout suppressor.

    The upstream implementation closes fd 1 before opening ``/dev/null``.  When
    placer stdout is a log on NFS, that close can surface a delayed NFS writeback
    error (EIO) and abort asset construction.  ``dup2`` performs the redirection
    without ever leaving fd 1 closed and does not expose that close error.

    This class is installed only in this Blender process; the shared Infinigen
    checkout remains untouched.
    """

    def __enter__(self, logfile=os.devnull):
        sys.stdout.flush()
        self.old_stdout = os.dup(1)
        target = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            os.dup2(target, 1)
        finally:
            os.close(target)
        self.logging_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            sys.stdout.flush()
            os.dup2(self.old_stdout, 1)
        finally:
            try:
                os.close(self.old_stdout)
            except OSError:
                # A delayed NFS writeback error must not turn output suppression
                # into a modeling failure. fd 1 has already been restored above.
                pass
            logging.disable(self.logging_level)


# blender.py imports Suppress by value, as do a few placement/asset helpers that
# may already be loaded by the time placer starts. Replace those process-local
# aliases as well as the source module for helpers imported later.
_OriginalSuppress = _logging_util.Suppress
_logging_util.Suppress = _SafeSuppress
for _module in tuple(sys.modules.values()):
    if (_module is not None
            and getattr(_module, "Suppress", None) is _OriginalSuppress
            and getattr(_module, "__name__", "").startswith("infinigen.")):
        _module.Suppress = _SafeSuppress
print("[placer] safe stdout suppression enabled (dup2; process-local)", flush=True)

# Factory 建模可能配置新的 semantic tags。一定要先續接 coarse 的 registry，完成後
# 再把更新版寫到 populated stage；否則 fine/GT 讀到舊 MaskTag.json，tag id 會失配。
_input_tag_path = Path(IN_BLEND).parent / "MaskTag.json"
if _input_tag_path.is_file():
    tag_system.load_tag(path=str(_input_tag_path))
    print(f"[placer] loaded tags ← {_input_tag_path}", flush=True)
elif DRY_RUN:
    # 舊的手工 probe 有些只留 blend；dry-run 不建模、不輸出 final metadata，容許它
    # 繼續做結構診斷。正式 populate 缺 registry 仍然 hard fail。
    print(f"[placer] ⚠️ dry-run input 缺 MaskTag.json: {_input_tag_path}", flush=True)
else:
    raise FileNotFoundError(f"placer input 缺 MaskTag.json: {_input_tag_path}")

# ⚠️ 這裡用 sys.path.insert 是安全的 —— placement_modes 是我們自己的頂層模組，
#    不是 infinigen package 的子模組(那種才會踩到 __path__ 已固定的坑)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # BeyondInfinigen/
import placement_modes as pm                                       # noqa: E402
import master_pool                                                 # noqa: E402
import world_motion as wm                                          # noqa: E402
import local_animation as la                                       # noqa: E402

# 會往前跑/走的骨架模式(catalog 的 modes 名字)。沒有 world_motion 時這些一律不套。
LOCOMOTION_MODES = {"run", "walk", "walk_cycle", "swim", "roam", "trot", "gallop"}
# 會飛的 factory 不應該在原地上下浮(2026-09-13 使用者：老鷹/蜂鳥原地抖很怪) ⇒ air_hover 換成往前飛。
#   鳥走 air_soar(快速滑過去、不爬升)，昆蟲走 air_glide(慢、會往上飄，蝴蝶要的就是這個)。
AIRBORNE_SOAR_FACTORIES = {"FlyingBirdFactory", "BirdFactory"}
AIRBORNE_GLIDE_FACTORIES = AIRBORNE_SOAR_FACTORIES | {"DragonflyFactory"}
import gn_scene_time                                               # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))           # stage_exec/
from overlay_imports import load_overlay_factory                   # noqa: E402
import master_swap                                                 # noqa: E402
import camera_visibility as camera_vis                             # noqa: E402
import placement_contract as plc                                   # noqa: E402
import official_scatter as osc                                     # noqa: E402
import placement_region_contract as prc                            # noqa: E402
import proxy_fit_runtime as proxy_fit                              # noqa: E402

# 母本池預設值 —— 只用在 manifest 沒帶 n_masters、而且是 generated 資產的時候。
# 5 是「幾隻動物」的量級；樹木那種需要多樣性的，由 director 給大一點的值。
DEFAULT_N_MASTERS = 5


def _target_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _target_int(value, default=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def _surface_policy_targets(obj) -> tuple[list[str], list[str]]:
    raw = obj.get("bi_surface_policy")
    if not raw:
        return [], []
    if isinstance(raw, str):
        try:
            policy = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], []
    elif isinstance(raw, dict):
        policy = raw
    else:
        return [], []
    required_names = [
        str(value) for value in (policy.get("required_surface_names") or [])
        if str(value)
    ]
    required_types = [
        str(value) for value in (policy.get("required_surface_types") or [])
        if str(value)
    ]
    return required_names, required_types


# Factories whose create_asset SHAPES the placeholder it receives (boulders
# deep_clone it, piles walk its children, trees/bushes read a skeleton child).
# A new factory added by apply_plan gets a neutral proxy box, so for these the
# asset's own create_placeholder must run first — otherwise an igloo whose dome
# lives in create_placeholder renders as a white box (0910 arctic igloo_2).
OWN_PLACEHOLDER_FACTORIES = {
    "BoulderFactory", "BoulderPileFactory", "TreeFactory", "BushFactory"}


def _needs_own_placeholder(factory, placeholder) -> bool:
    if placeholder.get("bi_proxy_dimension_source") is None:   # not apply_plan's neutral box
        return False
    return any(cls.__name__ in OWN_PLACEHOLDER_FACTORIES
               for cls in type(factory).__mro__)


def _adopt_factory_placeholder(factory, i, placeholder) -> None:
    """Swap the neutral box's mesh/children for the factory's real placeholder,
    keeping the box object itself (name, transform, manifest bookkeeping)."""
    own = factory.spawn_placeholder(
        i, placeholder.location.copy(), placeholder.rotation_euler.copy())
    for child in list(own.children):
        world = child.matrix_world.copy()
        child.parent = placeholder
        child.matrix_world = world
    if own.type == "MESH" and placeholder.type == "MESH":
        old = placeholder.data
        placeholder.data = own.data
        if old.users == 0:
            bpy.data.meshes.remove(old)
    bpy.data.objects.remove(own, do_unlink=True)
    placeholder["bi_placeholder_source"] = "factory_create_placeholder"
    print(f"[placer]   🧱 {placeholder.name}: neutral box → "
          f"{type(factory).__name__}.create_placeholder()", flush=True)


def _tree_width_m(obj) -> float | None:
    """世界座標下整棵資產樹的 XY 最長邊。"""
    from mathutils import Vector
    xs, ys = [], []
    for node in butil.iter_object_tree(obj):
        if node.type != "MESH":
            continue
        for corner in node.bound_box:
            w = node.matrix_world @ Vector(corner)
            xs.append(w.x); ys.append(w.y)
    if not xs:
        return None
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _populate_collection_with_manifest_targets(
        factory, *, placeholder_col, target_rows, asset_col_target,
        thin_reference_width_m=None, **asset_kwargs):
    """Populate exactly the manifest-selected placeholders with cached distances.

    thin_reference_width_m：★大東西減量★(2026-09-12)。replace / 官方撒點的目標數是照原生
    小東西算的；第一株生出來若比這個參考寬度寬 k 倍(k>1.3)，只填最近的 1/k² 個，其餘佔位隱藏。
    """
    from infinigen.core.placement import detail as _detail

    if asset_col_target is None:
        asset_col_target = butil.get_collection(f"unique_assets:{repr(factory)}")
    target_by_name = {
        str(row.get("object")): row
        for row in target_rows
        if (isinstance(row, dict)
            and row.get("object")
            and row.get("will_populate", True))
    }
    placeholders = [
        obj for obj in placeholder_col.objects
        if obj.parent is None
        and (
            obj.name in target_by_name
            or str(obj.get("bi_full_placeholder_name") or "") in target_by_name
        )
    ]
    targets = []
    for placeholder in placeholders:
        row = (target_by_name.get(placeholder.name)
               or target_by_name.get(
                   str(placeholder.get("bi_full_placeholder_name") or "")))
        targets.append((placeholder, row))
    root_count = len([o for o in placeholder_col.objects if o.parent is None])
    print(
        f"Populating {len(targets)} manifest-selected placeholders for "
        f"factory={factory} out of {root_count} total "
        f"({len(target_rows)} manifest row(s))",
        flush=True,
    )

    all_objs = []
    updated_pholders = []
    if asset_col_target is not None:
        asset_col_target.hide_viewport = False
    if thin_reference_width_m:
        # ★dist_m 是負的代表 placeholder 在相機★後面★★(2026-09-15)。
        #   純粹「由近到遠」排序會先挑走它們 —— river seed3 實測那三個 target 是
        #     spawn_placeholder(32) dist_m=-0.874   ← 相機後面
        #     spawn_placeholder(53) dist_m=+19.938  ← 畫面正中那棵大樹
        #     spawn_placeholder(52) dist_m=-1.098   ← 相機後面
        #   keep_n=2 於是留下兩個在相機後面的、把 19.9m 那棵真正看得到的丟掉 ⇒
        #   成品看起來就是「原本的大樹不見了、也沒有換成新的」(連跑四次都一樣)。
        #   改成：相機後面(dist_m <= 0)的一律排到最後，其餘維持原本的由近到遠。
        #   正常場景不會有負距離 ⇒ 行為完全不變。
        def _keep_rank(t):
            d = _target_float((t[1] or {}).get("dist_m"), 1e9)
            return (d <= 0.0, d)
        targets.sort(key=_keep_rank)
    keep_n = len(targets)
    for i, (placeholder, row) in enumerate(targets):
        if i >= keep_n:
            placeholder.hide_render = True
            for child in placeholder.children_recursive:
                child.hide_render = True
            continue
        classname, inst_seed, *_ = _pl.parse_asset_name(placeholder.name)
        if classname is None:
            raise ValueError(
                f"Could not parse p.name={placeholder.name!r}, got classname=None")
        dist = _target_float(row.get("dist_m"), None)
        vis_dist = _target_float(row.get("vis_dist_m"), 0.0)
        recorded_dist = (
            dist if dist is not None else float(_detail.scatter_res_distance()))
        if _needs_own_placeholder(factory, placeholder):
            _adopt_factory_placeholder(factory, i, placeholder)
        obj = factory.spawn_asset(
            i,
            placeholder=placeholder,
            distance=dist,
            vis_distance=vis_dist,
            **asset_kwargs,
        )
        if placeholder is not obj:
            placeholder.hide_render = True
        if i == 0 and thin_reference_width_m:
            width = _tree_width_m(obj)
            if width and width > 1.3 * float(thin_reference_width_m):
                ratio = width / float(thin_reference_width_m)
                # ★下限 1/4★(2026-09-13)：golden_kelp 量到 8m 寬、參考 1m ⇒ 248 株只剩 4 株，
                #   海帶床整個消失。減量是為了避免「一面牆」，不是為了把系統清空。
                keep_n = max(1, min(len(targets),
                                    max(int(-(-len(targets) // (ratio * ratio))),
                                        -(-len(targets) // 4))))
                print(f"[placer]   📉 {factory}: 資產寬 {width:.1f}m 是參考 {thin_reference_width_m:.1f}m 的 "
                      f"{ratio:.1f} 倍 ⇒ 只填最近的 {keep_n}/{len(targets)} 個(面積換算)", flush=True)
        for child in butil.iter_object_tree(obj):
            butil.put_in_collection(child, asset_col_target)
        obj["dist"] = recorded_dist
        obj["vis_dist"] = vis_dist
        updated_pholders.append((inst_seed, placeholder))
        all_objs.append((inst_seed, obj))
    asset_col_target.hide_viewport = True
    factory.finalize_assets([obj for _seed, obj in all_objs])
    return all_objs, updated_pholders

def verify_overlay_coverage(code_dir):
    """★把 overlay 裡★所有★改寫檔都檢查一遍，確認每一個都真的被 import 到。★

    只驗入口檔是不夠的(2026-07-29 的失敗就是這樣漏掉的)。這裡掃 overlay 底下
    每一個 .py，凡是已經在 sys.modules 裡的，就確認它的 __file__ 指向 overlay。
    回傳 (已載入且正確的清單, 有問題的清單)。
    """
    code_dir = Path(code_dir).resolve()
    ok, bad = [], []
    for f in sorted(code_dir.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(code_dir)
        if rel.name == "__init__.py":
            mod_fqn = ".".join(rel.parts[:-1])
        else:
            mod_fqn = ".".join(rel.with_suffix("").parts)
        m = sys.modules.get(mod_fqn)
        if m is None:
            continue                      # 這輪沒用到，不算問題
        src = str(getattr(m, "__file__", "") or "")
        (ok if plc.import_path_matches_overlay(src, code_dir, sys.path) else bad).append(
            {"module": mod_fqn, "file": src})
    return ok, bad


def read_factory_meta(code_dir):
    """讀 overlay 的 factory.txt -> dict。"""
    meta = {}
    for line in (Path(code_dir) / "factory.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def verify_overlay_not_stale(code_dir):
    """★反方向的檢查：overlay 有沒有把 runtime 的修補「還原」掉。★(2026-08-06)

    verify_overlay_coverage 只驗一個方向 ——「我的改寫有沒有真的被 import 到」。
    它對今天這個 bug ★完全無效★：crustacean.py 確實來自 overlay，所以被判成 ok，
    而「來自 overlay」正是問題本身。

    背景：overlay 是整棵 infinigen 的鏡射(必須是整棵 —— infinigen.assets 是 regular
    package，__path__ 不會合併，只放部分模組會 ModuleNotFoundError)。generator 的
    慣例是★改寫過的檔 = 實體檔，沒改的 = symlink 回原始樹★。
    2026-07-09 建的鏡射來源指向 PC_Blen，而 2026-07-22 的 4 個 creature 修補
    (crustacean/fish/jellyfish/reptile 的 TypeError/IndexError/Shapekey)做在
    infinigen_o ⇒ 掛任何 overlay 都等於把那 4 個修補拆掉。
    森林用不到那些檔所以一路沒事，coral_reef 一碰 CrustaceanFactory 就 TypeError。

    ⇒ 規則：★symlink 代表「這個檔我沒改」，那它就必須與 runtime 樹逐字一致。★
      不一致 = 有人在別棵樹上修了東西而鏡射沒跟上，當場擋下來。
    """
    code_dir = Path(code_dir).resolve()
    ov_pkg = code_dir / "infinigen"
    if not ov_pkg.is_dir():
        return []
    runtime = plc.overlay_base_pkg(code_dir)
    if runtime is not None:
        runtime = Path(runtime)
        if not runtime.is_dir() or runtime.resolve() == ov_pkg.resolve():
            runtime = None
    if runtime is None:
        for entry in sys.path:
            try:
                entry_root = Path(entry).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            # Overlay roots carry their own factory.txt and should never serve
            # as the "runtime" baseline for stale-mirror comparison.
            if (entry_root / "factory.txt").is_file():
                continue
            cand = entry_root / "infinigen"
            if cand.is_dir() and cand.resolve() != ov_pkg.resolve():
                runtime = cand
                break
    if runtime is None:
        return []
    stale = []
    for f in sorted(ov_pkg.rglob("*.py")):
        if "__pycache__" in f.parts or not f.is_symlink():
            continue                      # 實體檔 = 刻意改寫，不在此檢查範圍
        other = runtime / f.relative_to(ov_pkg)
        try:
            if other.is_file() and other.read_bytes() != f.read_bytes():
                stale.append(str(f.relative_to(ov_pkg)))
        except OSError:
            continue
    return stale


def official_factory(name):
    """找官方的 Factory class(manifest 沒給 code 時用)。"""
    aliases = {
        # Indoor smoke prompts can name semantic objects more specifically than
        # the stock Infinigen factory surface. Keep these substitutions visible
        # in the placer report via the returned FQN.
        "PlushToyFactory": (
            "infinigen.assets.objects.seating.pillow", "PillowFactory"),
        "ToyBoxFactory": (
            "infinigen.assets.sim_objects.box", "BoxFactory"),
        "DecorativeVaseFactory": (
            "infinigen.assets.objects.table_decorations.vase", "VaseFactory"),
        "ElectricKettleFactory": (
            "infinigen.assets.sim_objects.toaster", "ToasterFactory"),
        "CushionFactory": (
            "infinigen.assets.objects.seating.pillow", "PillowFactory"),
    }
    alias = aliases.get(name)
    if alias is not None:
        mod_name, cls_name = alias
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        if cls.__name__ != name:
            cls = type(name, (cls,), {"__module__": cls.__module__})
        print(
            f"[placer]   ℹ️ 官方 substitute {name} → {mod_name}.{cls_name}",
            flush=True,
        )
        return cls, f"{mod_name}.{cls_name} (substitute for {name})"

    candidates = [
        "infinigen.assets.objects.trees.generate",
        "infinigen.assets.objects.rocks.boulder",
        "infinigen.assets.objects.creatures.carnivore",
        "infinigen.assets.objects.creatures.herbivore",
        "infinigen.assets.objects.cactus.generate",
        "infinigen.assets.objects.monocot.kelp",
        "infinigen.assets.objects.cloud.generate",
        "infinigen.assets.objects.creatures.insects.dragonfly",
        "infinigen.assets.sim_objects.soap_dispenser",
        "infinigen.assets.sim_objects.toaster",
        "infinigen.assets.sim_objects.box",
        "infinigen.assets.objects.table_decorations.book",
        "infinigen.assets.objects.table_decorations.vase",
        "infinigen.assets.objects.seating.pillow",
    ]
    for m in candidates:
        try:
            mod = importlib.import_module(m)
        except Exception:
            continue
        if hasattr(mod, name):
            return getattr(mod, name), f"{m}.{name}"

    # ★退路：整包掃★(2026-08-06)
    #   ⚠️ 上面那份是★手寫的清單，而且只涵蓋森林會用到的★ —— 換地形第一次就炸：
    #      snowy_mountain 的 FlyingBirdFactory、coral_reef 的 CrustaceanFactory
    #      都不在表上，placer 直接 SystemExit。清單會隨場景增加而永遠追不完，
    #      所以查不到就掃 infinigen.assets.objects 底下所有模組。
    #   掃描只在 miss 時發生(命中清單的常見情形不受影響)。
    import pkgutil
    packages_to_scan = []
    for package_name in (
            "infinigen.assets.objects",
            "infinigen.assets.sim_objects",
    ):
        try:
            packages_to_scan.append(importlib.import_module(package_name))
        except Exception:
            continue
    for _pkg in packages_to_scan:
        for _mi in pkgutil.walk_packages(_pkg.__path__, prefix=f"{_pkg.__name__}."):
            if _mi.ispkg:
                continue
            try:
                mod = importlib.import_module(_mi.name)
            except Exception:
                continue
            if hasattr(mod, name):
                print(f"[placer]   ℹ️ 官方 {name} 由整包掃描找到: {_mi.name}", flush=True)
                return getattr(mod, name), f"{_mi.name}.{name}"
    # ★不要整輪死掉★(2026-08-09)：這一筆既沒有生成的 code、也沒有官方實作
    #   (director 合成了宿主名如 WoodenSleighFactory，但那一筆資產沒生成成功)。
    #   ⇒ 讓呼叫端把這筆標成 feasible=False，其餘照走完。整輪 SystemExit 會讓
    #     「一個資產缺席」變成「什麼都沒有」。
    raise LookupError(f"找不到官方 {name}(手寫清單與整包掃描都沒有)"
                      f" —— 這一筆既無生成 code 也無官方實作")


def ensure_skeleton_placeholders(FacClass, seed, colls, fac_kwargs=None):
    """★給「自己覆寫 create_placeholder」的 factory 補上骨架。★(2026-08-07)

    ★問題★：apply_plan 的 add 對場景沒有的 factory 會建★中性立方體★當 box
    (apply_plan.py:389)。那個決定對「憑空造的東西」是對的 —— Infinigen 的預設
    create_placeholder 本來就只是 spawn_cube，create_asset 自己長幾何。

    ★但樹/灌木不是這樣★：它們覆寫 create_placeholder 去生 tree_skeleton 並掛成
    子物件(trees/generate.py:86-102)，create_asset 第一行就讀 placeholder.children[0]
    (generate.py:143)。中性立方體沒有子物件 ⇒ ★IndexError，整個 placer 掛掉★。
    2026-08-06 的聖誕場景就是死在這裡(add 4 棵 TreeFactory)。

    ⇒ 判準不是「哪些 factory 特別」，而是★它有沒有覆寫 create_placeholder★ ——
      有覆寫就代表「placeholder 不只是位置，內容也有意義」，那就得讓它自己生。
      沒覆寫的(絕大多數、以及所有全新資產)完全不受影響。

    做法：在原地(loc 0,0,0)請 factory 生一個真 placeholder，把它的子物件搬到既有的
    box 底下，再刪掉那個臨時 pholder。★box 本身不動★ —— 它的名字/位置/collection
    是 populate_all 與 manifest 的配對依據，換掉會牽動一堆東西。
    """
    from infinigen.core.placement.factory import AssetFactory
    if getattr(FacClass, "create_placeholder", None) is AssetFactory.create_placeholder:
        return 0                                   # 預設立方體就夠，不用管
    fixed = 0
    for c in colls:
        m = PH_COLL_RE.fullmatch(c.name)
        cseed = m.group(3) if m else seed
        for i, box in enumerate([o for o in c.objects if o.parent is None]):
            if box.children:
                continue                           # coarse 烤好的 box，已經有骨架
            fac = FacClass(int(cseed), **(fac_kwargs or {}))
            tmp = fac.create_placeholder(i=i, loc=(0, 0, 0), rot=(0, 0, 0))
            kids = list(tmp.children)
            for ch in kids:
                butil.parent_to(ch, box, no_inverse=True, keep_transform=False)
            butil.delete([tmp])
            fixed += len(kids)
    return fixed


def purge_appearance_masters(factory_name, seed):
    """★刪掉 coarse 階段烤好的「外觀母本」，逼 Factory 用 overlay 的 code 重建。★

    ★★2026-07-29 的根本發現★★ —— 這是「換 code 卻長出官方外觀」的真正原因，
    比 import 那個 bug 重要得多：

      trees/generate.py:462-501 (TreeFactory.__init__)
          colname = f"assets:{self}.twigs"
          if colname not in bpy.data.collections:
              twig_col = make_twig_collection(..., leaf_type, season=season)  # 用你的 code
              ...
          else:
              self.child_col = bpy.data.collections[colname]   ← ★直接沿用 blend 裡的★

    ⇒ coarse blend 裡已經有 assets:TreeFactory(seed).twigs
      ⇒ 走 else 分支 ⇒ ★get_leaf_type() / make_twig_collection() 整段被跳過★
      ⇒ 換 FacClass 只換到「怎麼組裝」，★換不到「用什麼零件組裝」★。

    ★Infinigen 自己的 warning 說明了設計意圖★(generate.py:465)：
      "twigs are typically generated only in coarse"
      ⇒ 外觀(葉/花/果)是 ★coarse 階段烤好的快取★，populate 只負責組裝。
      ⇒ ★這不是 bug，是刻意的設計★ —— 所以要換外觀就必須顯式清掉快取。

    ⚠️ 這也修正了 0728 13-B 的主張：「傳 overlay 的 FacClass 進去就會用你的 code」
       ★對「形狀/擺放」成立，對「外觀」不成立★，除非先清掉母本。

    ★做法：改名成 OLD_ 而不是刪除★(照抄 composer.py:135，0723 就驗證過的解法)
      改名比刪除安全 —— ★舊母本還在，原本的樹不會壞掉★，
      但 colname 找不到了 ⇒ __init__ 走 if 分支，強制用你的 code 重建。
    """
    renamed = []
    for c in list(bpy.data.collections):
        # assets:TreeFactory(3040046).twigs / assets:GenericTreeFactory(3040046)
        if not c.name.startswith("assets:"):
            continue
        if str(seed) not in c.name:
            continue
        if factory_name not in c.name and "Generic" not in c.name:
            continue
        renamed.append((c.name, len(c.objects)))
        c.name = "OLD_" + c.name
    return renamed


def _file_identity(path, *, digest=False):
    resolved = Path(path).resolve()
    stat = resolved.stat()
    result = {"path": str(resolved), "size": int(stat.st_size),
              "mtime_ns": int(stat.st_mtime_ns)}
    if digest:
        result["sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


report = {"in_blend": IN_BLEND, "out_blend": None if DRY_RUN else OUT_BLEND,
          "manifest": MANIFEST_JSON, "dry_run": DRY_RUN, "results": [],
          "frame_range": [int(bpy.context.scene.frame_start),
                          int(bpy.context.scene.frame_end)],
          "provenance": {
              "input_blend": _file_identity(IN_BLEND),
              "manifest": _file_identity(MANIFEST_JSON, digest=True),
          }}
# ★這輪真正被 populate 到的 placeholder 名字★ —— 補正段落只能動這些。
#   ⚠️ 樹/灌木的 box 在 coarse 階段就帶骨架 child(generate.py:86 create_placeholder)，
#      跟有沒有被 populate 無關 ⇒ 用「o.children 存不存在」判斷「長出資產了嗎」是錯的，
#      231 個 box 全部一開始就有 children，會把★從未處理過的空骨架★也拉進沉降補正
#      (2026-08-04 實測：45 個真正生成的資產，混進 417 個空骨架，變成 462)。
POPULATED_NAMES = set()

for idx, p in enumerate(placements):
    name = p["factory"]
    code = p.get("code")
    population_intent = prc.placement_population_intent(p)
    entry = {
        "index": idx,
        "factory": name,
        "code": code,
        "feasible": True,
        "population_intent": population_intent,
        "population_target_count": p.get("population_target_count"),
    }

    # ★realized_at != "populate" 的筆，placer 沒事可做★(2026-08-06，T4 rc=1 逼出來的)
    #   0805 十二節給 manifest 加了「coarse 階段就實現」的筆(swarm，edit_via=particle_col)，
    #   但下游沒跟著改 ⇒ placer 照樣把它們當 populate 目標跑：
    #     ・它們的 placeholder 在 coarse 就被消耗成 0 個，本來就沒有 box 可長
    #     ・populate_kwargs 是 ★null 不是缺鍵★ ⇒ dict(p.get(..., {})) 拿到 None 直接炸
    #   要換這種資產得走 master_swap(換粒子系統的 instance_collection)，不是 populate。
    #   ⚠️ 這不是失敗 ⇒ 用 skipped 而不是 feasible=False，別讓 run 腳本的關卡誤判。
    realized_at = p.get("realized_at") or "populate"
    if realized_at != "populate":
        # ★swarm 有 code 就走 master_swap★(2026-08-06) —— 換粒子系統的
        #   instance_collection，跟金色蘑菇換 GN socket 同構。沒給 code 才是純跳過。
        if p.get("edit_via") == "particle_col" and code:
            try:
                # ⚠️ ★expect_name 不能用 manifest 的 factory★ —— 那是
                #    FishSchoolFactory(整群)，而母本是 FishFactory 生的。
                #    load_overlay_factory 的類名 assert 是為了「populate_all 用類名
                #    配對 box」，這條路★沒有 box★，所以改用 overlay 自己宣告的名字。
                declared = read_factory_meta(code).get("factory", name)
                FacClass, fqn = load_overlay_factory(code, declared)
                stale = verify_overlay_not_stale(code)
                if stale:
                    raise AssertionError(
                        f"overlay 鏡射過期：{len(stale)} 個未改寫檔與 runtime 不一致"
                        f"(例 {stale[0]})")
                rep = master_swap.swap_masters(
                    p["collection"], p.get("master_collection"), FacClass,
                    n_masters=p.get("n_masters"), dry_run=DRY_RUN,
                    log=lambda m: print(f"[placer]   {m}", flush=True))
                entry.update(master_swap=rep, source="generated", fqn=fqn,
                             realized_at=realized_at, edit_via="particle_col")
                if rep.get("feasible"):
                    print(f"[placer] 🔄 #{idx} {name}: 母本 "
                          f"{rep.get('old_n_masters')} → {rep.get('new_n_masters')} 個"
                          f"({declared})  粒子 {rep.get('particle_count')} 不變  "
                          f"only_instance_changed={rep.get('only_instance_changed')} "
                          f"cache_ok={rep.get('cache_ok')}", flush=True)
                else:
                    print(f"[placer] ⛔ #{idx} {name} master_swap: {rep.get('err')}",
                          flush=True)
                    entry["feasible"] = False
            except Exception as e:
                entry.update(feasible=False,
                             blocked_reason=f"master_swap: {type(e).__name__}: {e}")
                print(f"[placer] ⛔ #{idx} {name} master_swap 失敗: {str(e)[:110]}",
                      flush=True)
            report["results"].append(entry)
            continue
        entry.update(skipped=True, realized_at=realized_at,
                     edit_via=p.get("edit_via"),
                     skip_reason=f"realized_at={realized_at} —— 不在 populate 這條線上")
        print(f"[placer] ⏭️  #{idx} {name}: realized_at={realized_at} "
              f"edit_via={p.get('edit_via')} —— 跳過(要換這個得走 master_swap)",
              flush=True)
        report["results"].append(entry)
        continue

    if not prc.placement_should_materialize(p):
        entry.update(
            skipped=True,
            realized_at=realized_at,
            edit_via=p.get("edit_via"),
            skip_reason=(
                "latent unmodified placeholder: not requested, not replaced, "
                "and not predicted to populate from the selected camera"),
        )
        print(
            f"[placer] ⏭️  #{idx} {name}: population_intent={population_intent} "
            "—— latent box 不 populate",
            flush=True,
        )
        report["results"].append(entry)
        continue

    # 確認 box 還在(階段 A 之後不該有人動過)
    colls = [c for c in bpy.data.collections
             if (m := PH_COLL_RE.fullmatch(c.name)) and m.group(2) == name]
    if not colls:
        entry.update(feasible=False,
                     blocked_reason=f"blend 裡沒有 placeholders:{name}(*) —— "
                                    f"manifest 與 blend 不符，階段 A 的產物被動過?")
        print(f"[placer] ⛔ #{idx} {name} —— 找不到 box", flush=True)
        report["results"].append(entry)
        continue
    entry["collections"] = [c.name for c in colls]
    entry["boxes"] = sum(len([o for o in c.objects if o.parent is None]) for c in colls)

    try:
        if code:
            FacClass, fqn = load_overlay_factory(code, name)
            entry["source"] = "generated"
        else:
            FacClass, fqn = official_factory(name)
            entry["source"] = "official"
        entry["fqn"] = fqn
    except Exception as e:
        entry.update(feasible=False, blocked_reason=f"{type(e).__name__}: {e}")
        print(f"[placer] ⛔ #{idx} {name} —— {str(e)[:80]}", flush=True)
        report["results"].append(entry)
        continue

    # ★母本池★：先建 n_masters 個，其餘 linked duplicate(見 master_pool.py)。
    #   ⚠️ ★預設只對 code(我們自己生成的 overlay 資產)開★ —— 官方 factory 維持逐個生成，
    #      否則一片森林 231 棵樹只剩 n 種，會毀掉 infinigen 本來的多樣性。而慢的本來就
    #      只有我們生的那些(長頸鹿 92 秒/隻、摩托車 92→294 秒/台，官方灌木 0.24 秒/個)。
    #   n_masters 由 director 逐筆決定(森林要多樣給 20、幾隻動物給 5)，
    #   manifest 沒帶就用 DEFAULT_N_MASTERS。0 或 None = 關掉。
    requested_local_request = la.normalize_request(
        p.get("local_animation"), factory=name)
    suppress_local_animation = bool(
        ALLOW_UNQUALIFIED_OUTPUT
        and requested_local_request["policy"] == "inherit")
    # ★沒有位移動畫就不要跑步★(2026-09-13 使用者)：「動物現在會沒有座標動畫卻配上跑步
    #   動畫，很奇怪；原地不動時應該配呼吸動畫」。catalog 裡 Carnivore/Herbivore 唯一
    #   通過的模式就是 run ⇒ 站著的動物一律拿掉 rig，交給 creature_idle 打原地呼吸。
    stationary_locomotion = bool(
        str((p.get("world_motion") or {}).get("preset") or "none") == "none"
        and str(requested_local_request.get("mode") or "") in LOCOMOTION_MODES)
    local_request = (
        {"policy": "none", "mode": None}
        if (suppress_local_animation or stationary_locomotion)
        else requested_local_request)
    # ★官方 factory 也開池★(2026-09-09) —— 見 placement_modes.official_pool_size。
    #   舊行為是 `DEFAULT_N_MASTERS if code else 0`，官方一律逐個生成。實測水下的
    #   KelpMonocotFactory 248 株要 143 分鐘，而那全是場景本來就有的東西。
    #   n=20 保留足夠變化(248 株 20 種)，同時 12x 加速。逐 factory 可覆寫，
    #   TreeFactory / BushFactory 維持 0(樹冠形狀太好認)。
    requested_n_masters = p.get(
        "n_masters",
        DEFAULT_N_MASTERS if code else pm.official_pool_size(name))
    n_masters = master_pool.effective_pool_size(
        requested_n_masters, local_request)
    if n_masters:
        # 母本之間錯開 factory_seed(顏色/比例才會不同)：只對「我們生成的 overlay、而且不是生物類」開。
        #   官方 factory 錯開會混物種；生物類錯開會改整個 genome(2026-09-13 與 87 討論後收窄)。
        _vary_seed = bool(code) and ".creatures." not in str(fqn)
        FacClass = master_pool.pooled(FacClass, int(n_masters), vary_seed=_vary_seed)
        entry["master_pool_vary_seed"] = _vary_seed
    entry["n_masters_requested"] = int(requested_n_masters or 0)
    entry["n_masters"] = int(n_masters or 0)
    if requested_n_masters and not n_masters:
        entry["master_pool_disabled_reason"] = (
            "animated rig must be built directly; Object.copy does not preserve "
            "a safe armature bind/pose state")

    # ★`or {}` 不是 default 參數★ —— manifest 寫的是顯式 null，鍵存在 ⇒ default 不會生效
    kwargs = dict(p.get("populate_kwargs") or {})
    local_kwargs = la.constructor_kwargs(local_request, factory=name)
    conflicts = sorted(set(kwargs) & set(local_kwargs))
    if conflicts:
        raise ValueError(
            f"placement #{idx}: populate_kwargs may not override inherited "
            f"local animation keys {conflicts}")
    kwargs.update(local_kwargs)
    entry["local_animation"] = {
        **requested_local_request,
        "effective_policy": local_request["policy"],
        "effective_mode": local_request["mode"],
        "constructor_kwargs": local_kwargs,
        "status": ("suppressed_for_connectivity"
                   if suppress_local_animation
                   else "stationary_breathing_instead_of_locomotion"
                   if stationary_locomotion
                   else "applied" if local_request["policy"] == "inherit"
                   else "pending" if local_request["policy"] == "gn_scene_time"
                   else "none"),
    }
    print(f"[placer] #{idx} {name}: {entry['boxes']} 個 box ← "
          f"★{entry['source']}★ {fqn}"
          f"{f'  母本池={n_masters}' if n_masters else ''}"
          f"{'  connectivity 停用 local rig' if suppress_local_animation else ''}"
          f"{'  無位移 ⇒ 拿掉 ' + str(requested_local_request.get('mode')) + ' 骨架，改原地呼吸' if stationary_locomotion else ''}"
          f"{'  動態 rig 直建（停用母本池）' if requested_n_masters and not n_masters else ''}",
          flush=True)

    # ★不能只驗入口檔★ —— 2026-07-29 的失敗正是「入口對、它 import 的檔全是官方版」。
    if code:
        ok, bad = verify_overlay_coverage(code)
        entry["overlay_modules_ok"] = [m["module"] for m in ok]
        entry["overlay_modules_wrong"] = bad
        print(f"[placer]   overlay 覆蓋檢查: {len(ok)} 個模組來自 overlay, "
              f"{len(bad)} 個是官方版", flush=True)
        if bad:
            for b in bad:
                print(f"[placer]   ⛔ {b['module']} → {b['file']}", flush=True)
            entry.update(feasible=False,
                         blocked_reason=f"overlay 只有部分生效：{len(bad)} 個改寫模組"
                                        f"實際 import 到官方版 —— PYTHONPATH 沒設對")
            report["results"].append(entry)
            continue

        # ★另一個方向★：overlay 有沒有把 runtime 的修補還原掉(見上面的函式說明)
        stale = verify_overlay_not_stale(code)
        entry["overlay_stale_mirrors"] = stale
        if stale:
            print(f"[placer]   ⛔ ★overlay 的鏡射比 runtime 舊★: {len(stale)} 個沒改寫的"
                  f"檔與 runtime 不一致 ⇒ 會靜默還原修補", flush=True)
            for s in stale[:6]:
                print(f"[placer]      {s}", flush=True)
            entry.update(feasible=False,
                         blocked_reason=f"overlay 鏡射過期：{len(stale)} 個未改寫檔與 "
                                        f"runtime 不一致(例 {stale[0]}) —— 鏡射來源指到"
                                        f"另一棵樹，會把該樹沒有的修補還原掉")
            report["results"].append(entry)
            continue

    # ★★換外觀必須先讓母本快取失效★★(2026-07-29，見 purge_appearance_masters)
    #   否則 TreeFactory.__init__ 走 else 分支沿用官方母本 ⇒ ★長出官方外觀★。
    #   ⚠️ 只改 assets: 開頭的母本 collection，★絕不碰 placeholder 底下的 child 骨架★
    #      (create_asset 第一行就要用骨架，動了會 IndexError —— 0728 6-D)
    if code and not DRY_RUN:
        victims = purge_appearance_masters(name, p.get("seed"))
        entry["renamed_masters"] = victims
        if victims:
            for vn, vc in victims:
                print(f"[placer]   ♻️  母本改名 {vn} → OLD_… ({vc} objs) 逼 overlay 重建",
                      flush=True)
        elif p.get("added_this_plan", 0) >= (entry.get("boxes") or 0):
            # ★不是警告★ —— 這一筆的 box 全是這輪 add 進來的，seed 也是新抽的，
            #   coarse 從來沒為它烤過 assets:<Factory>(<seed>) 母本 ⇒ 沒東西要清，
            #   也就★不存在官方外觀洩漏★。
            #   ⚠️ 舊版對 add 與 replace 吐同一句話，分不出「沒得清(安全)」和
            #      「該清的沒清到(危險)」—— 2026-08-06 的 e2e 就被這句誤導過。
            print(f"[placer]   ℹ️ 全部是本輪新增的 box，coarse 沒有母本可清(正常)",
                  flush=True)
        else:
            print(f"[placer]   ⚠️ 沒找到可改名的母本 —— 外觀可能仍是官方版 "
                  f"(這筆有 {entry.get('boxes')} 個 box，其中 "
                  f"{p.get('added_this_plan', 0)} 個是本輪新增)", flush=True)

    if DRY_RUN:
        report["results"].append(entry)
        continue

    # ★★中性立方體補骨架★★(2026-08-07，見 ensure_skeleton_placeholders)
    #   add 進來的 box 是 2m 立方體，但樹/灌木的 create_asset 要讀 children[0]。
    #   ⚠️ 必須★在 purge_appearance_masters 之後★ —— 骨架是用 FacClass 生的，
    #      要先讓母本快取失效，長出來的骨架才是 overlay 的版本。
    try:
        pk = {k: v for k, v in kwargs.items()
              if k not in ("dist_cull", "vis_cull", "cache_system")}
        n_fixed = ensure_skeleton_placeholders(FacClass, p.get("seed"), colls, pk)
        entry["skeletons_added"] = n_fixed
        if n_fixed:
            print(f"[placer]   🦴 {name} 覆寫了 create_placeholder ⇒ "
                  f"為中性 box 補上 {n_fixed} 個骨架", flush=True)
    except Exception as e:
        # ★不要讓補骨架把整輪拖垮★ —— 這一筆擋掉，其餘照走完。
        entry.update(feasible=False,
                     blocked_reason=f"補骨架失敗 {type(e).__name__}: {e}")
        print(f"[placer]   ⛔ #{idx} {name} 補骨架失敗: {type(e).__name__}: "
              f"{str(e)[:120]}", flush=True)
        report["results"].append(entry)
        continue

    # ★★per-collection 而不是 populate_all★★(2026-07-29 修正，0729.txt 第四節)
    #   populate_all(FacClass) 是★用類名過濾★(placement.py:292)，
    #   ⇒ 同名的所有 collection ★都會被同一個 class 處理★
    #   ⇒ 「seed A 換櫻花、seed B 保持原樣」★做不到★。
    #   底下的 populate_collection(placement.py:191) ★吃單一 placeholder_col★，正是需要的粒度。
    target_colls = [c for c in colls
                    if not p.get("collection") or c.name == p["collection"]]
    built = 0
    processed = []
    for c in target_colls:
        # ⚠️ ★placement.py:297-301 的靜默 skip★：unique_assets:<repr> 非空就整個跳過，
        #    只在 log 寫一行不報錯。逐 collection 呼叫時前一筆可能已經填掉後一筆，
        #    ⇒ ★自己檢查並明講★，不要靠它靜默處理。
        m = PH_COLL_RE.fullmatch(c.name)
        target_col = bpy.data.collections.get(f"unique_assets:{m.group(1)}")
        if target_col is not None and len(target_col.objects) > 0:
            print(f"[placer]   ⚠️ {c.name} 的 unique_assets 已有 "
                  f"{len(target_col.objects)} 個物件 → ★會被 Infinigen 靜默跳過★，"
                  f"這輪不處理它", flush=True)
            processed.append({"collection": c.name, "skipped": True,
                              "reason": "unique_assets 非空"})
            continue
        # ⚠️ dist_cull/vis_cull 是 populate_all 的參數，★不能傳進 Factory 建構子★。
        #    預設值必須與 populate_all 一致(placement.py:271-272)，否則 view cull
        #    行為會與昨天的實驗不可比。
        fac_kwargs = {k: v for k, v in kwargs.items()
                      if k not in ("dist_cull", "vis_cull", "cache_system")}
        fac_inst = FacClass(int(m.group(3)), **fac_kwargs)
        populate_cameras = None if FORCE_POPULATE_ALL else cams
        if FORCE_POPULATE_ALL:
            print("[placer]   ⚠️ BI_FORCE_POPULATE_ALL=1 -> skip camera view cull",
                  flush=True)
        # Blender truncates object IDs at 63 bytes.  A long generated class
        # name can lose the trailing ``spawn_placeholder(index)`` and crash
        # Infinigen's name parser before population.  Repair legacy/resumed
        # Step-8 scenes to the same compact identity now authored by apply_plan.
        repaired_names = []
        roots = sorted(
            (obj for obj in c.objects if obj.parent is None),
            key=lambda obj: obj.name,
        )
        for ordinal, placeholder in enumerate(roots):
            if _pl.parse_asset_name(placeholder.name)[0] is not None:
                continue
            instance_index = int(placeholder.get("bi_instance_index", ordinal))
            full_name = (
                f"{name}({int(m.group(3))}).spawn_placeholder({instance_index})")
            compact_name = plc.blender_safe_placeholder_name(
                name, int(m.group(3)), instance_index)
            old_name = placeholder.name
            placeholder["bi_full_placeholder_name"] = full_name
            placeholder.name = compact_name
            if _pl.parse_asset_name(placeholder.name)[0] is None:
                raise ValueError(
                    f"placeholder name repair is still unparsable: {placeholder.name!r}")
            repaired_names.append((old_name, placeholder.name))
        if repaired_names:
            print(
                f"[placer]   🔧 repaired {len(repaired_names)} Blender-truncated "
                f"placeholder name(s): {repaired_names[0][0]} → {repaired_names[0][1]}",
                flush=True,
            )
        manifest_targets = p.get("population_targets")
        requested_population_target_count = _target_int(
            p.get("population_target_count"), None)
        if isinstance(manifest_targets, list):
            entry["population_source"] = "manifest_population_targets"
            entry["manifest_population_targets"] = len(manifest_targets)
            new_assets, pholders = _populate_collection_with_manifest_targets(
                fac_inst,
                placeholder_col=c,
                target_rows=manifest_targets,
                asset_col_target=target_col,
                thin_reference_width_m=(
                    osc.reference_width_for(str(p.get("factory") or ""))
                    if p.get("population_intent") == "requested_replace" else None),
            )
        else:
            entry["population_source"] = "legacy_filter_populate_targets"
            new_assets, pholders = _pl.populate_collection(
                fac_inst, placeholder_col=c, cameras=populate_cameras,
                dist_cull=kwargs.get("dist_cull", 200),
                vis_cull=kwargs.get("vis_cull", 0))
        # ★逐資產尺寸釘選也要管「placeholder 生成」的撒點★(2026-09-13 desert：giant_saguaro_cactus /
        #   flowering_globular_cactus 釘 ×0.76 完全沒生效 —— 它們走 official_scatter → 這裡 populate，
        #   asset_scale_pins 原本只處理 hero proxy 與 restyle 母本兩條路)。
        #   hero add 不在這裡縮：它的 proxy 已經乘過倍率，後面的 proxy fit 會以 proxy 為準。
        #   原點在底部 ⇒ 等比縮放不會浮起來；後面的沉降補正照常貼地。
        if int(p.get("added_this_plan") or 0) <= 0 and p.get("code_from"):
            import asset_scale_pins as _asp
            _pin = _asp.factor_for(_asp.pins(), p.get("code_from"))
            if _pin != 1.0:
                _scaled = 0
                for _seed, _root in new_assets:
                    _root.scale = tuple(float(v) * _pin for v in _root.scale)
                    _scaled += 1
                entry["asset_scale_pin"] = {"factor": _pin, "scaled_roots": _scaled}
                print(f"[placer]   📏 {name}({p.get('code_from')}): 釘選 ×{_pin} 套到 "
                      f"{_scaled} 個 placeholder 生成的資產", flush=True)
        if requested_population_target_count is None or requested_population_target_count <= 0:
            requested_population_target_count = (
                len(manifest_targets)
                if isinstance(manifest_targets, list) and manifest_targets
                else len(pholders)
            )
        # Generated adapters hand spawn_asset() an identity root that is meant
        # to inherit the reviewed placeholder transform.  Blender's
        # parent_no_inverse_set can instead serialize the child's local matrix
        # as placeholder^-1 while the live depsgraph still looks correct.  The
        # saved/reopened scene then evaluates at world origin.  Re-establish the
        # adapter contract explicitly before any fit/settle work or save.
        #
        # Keep this scoped to generated overlays: official factories are not
        # required to return identity roots and may need their local basis.
        # Test ``code`` rather than the FQN because generated rewrites of an
        # official family (polar bear -> CarnivoreFactory, anglerfish ->
        # FishFactory) intentionally retain that official module name.
        parenting_repairs = []
        if code:
            parenting_repairs = (
                proxy_fit.restore_identity_root_placeholder_inheritance(
                    new_assets))
            proxy_fit.force_evaluation(root for _seed, root in new_assets)
            if parenting_repairs:
                print(
                    f"[placer]   🔧 restored placeholder inheritance for "
                    f"{len(parenting_repairs)} generated root(s)",
                    flush=True,
                )
                entry.setdefault("identity_root_parenting_repairs", []).extend(
                    parenting_repairs)
        # ★Scene-Time 原地動畫★(2026-09-11)：DragonflyFactory(蝴蝶/蜜蜂/蜻蜓)沒有
        #   armature，翅膀是 GN 用 Scene Time 驅動的，但生成的變體常常振幅=0 ⇒ 靜止。
        #   populate 完才有實體節點群可改；單格出圖時不做(看不出來，也沒必要動場景)。
        if local_request["policy"] == "gn_scene_time":
            scene_frames = int(bpy.context.scene.frame_end) - int(
                bpy.context.scene.frame_start) + 1
            if scene_frames <= 1:
                entry["local_animation"]["gn_scene_time"] = {
                    "status": "SKIPPED", "reason": "single-frame render"}
            else:
                report_gn = gn_scene_time.apply(
                    [root for _seed, root in new_assets],
                    **la.gn_scene_time_params(name))
                entry["local_animation"]["gn_scene_time"] = report_gn
                entry["local_animation"]["status"] = (
                    "applied" if report_gn.get("applied") else "gn_scene_time_not_found")
                mark = "🦋" if report_gn.get("applied") else "⛔"
                print(f"[placer]   {mark} {name}: Scene-Time 動畫 "
                      f"{report_gn.get('applied', 0)} 條鏈 "
                      f"({report_gn.get('reason', 'normalised')})", flush=True)
        POPULATED_NAMES.update(ph.name for _seed, ph in pholders)
        for _seed, ph in pholders:
            # Blender-generator prompts guarantee front=-Y.  Persist that
            # provenance on the anchor so proxy fitting can align it with the
            # placeholder heading (+X) without guessing for rigged procedural
            # assets, whose evaluated helper bounds are not reliable enough.
            ph["bi_populated_factory_fqn"] = str(fqn)
            ph["bi_population_target_count"] = int(requested_population_target_count)
            ph["bi_manifest_population_targets"] = int(
                len(manifest_targets) if isinstance(manifest_targets, list)
                else requested_population_target_count)
            if ".assets.objects.generated." in str(fqn):
                ph["bi_asset_front_axis"] = "-Y"
            if suppress_local_animation:
                ph["bi_stable_evaluated_bounds"] = True
            if local_request["policy"] == "inherit" and not n_masters:
                # Directly built animated rigs keep one coherent bind/pose
                # hierarchy, so evaluated visible bounds are safe to fit.
                ph["bi_direct_animated_rig"] = True
        if int(p.get("added_this_plan") or 0) > 0:
            for _seed, ph in pholders:
                # Persist an exact placement identity into populated/fine scenes.
                # Snapshot validation can then audit the final evaluated geometry
                # without guessing from names or collection membership.
                requested_id = str(p.get("code_from") or "")
                if requested_id and str(ph.get("bi_code_from") or "") == requested_id:
                    ph["bi_requested_placement_index"] = int(idx)
                    ph["bi_requested_code_from"] = requested_id
        built += len(new_assets)
        processed.append({"collection": c.name, "boxes": len(pholders),
                          "built": len(new_assets)})
        print(f"[placer]   → {c.name}: 生成 {len(new_assets)} 個", flush=True)
    entry.update(collections_processed=processed, assets_built=built)
    print(f"[placer]   ✅ 生成 {built} 個 asset", flush=True)
    report["results"].append(entry)


# ``populate_collection`` hides its generated-asset collection in the viewport
# after building.  Viewport depsgraph bounds can then remain stale when a parent
# scale changes: the 0818 Coral run recorded applied=true while every before/after
# dimension was byte-for-byte identical.  Keep populated geometry evaluable for
# all fit/settle/motion audits *and in the saved populated blend*.  Restoring
# hide_viewport before save serializes Blender's stale hidden-hierarchy world
# matrix (observed as Identity), so requested roots jump back to world origin on
# reopen even though the live visible depsgraph had them at their placeholders.
_validation_collection_visibility = {}
if not DRY_RUN:
    _validation_collection_visibility = proxy_fit.unhide_object_tree_collections(
        obj for obj in bpy.context.scene.objects
        if obj.parent is None and obj.name in POPULATED_NAMES)


def _restore_validation_collection_visibility():
    if DRY_RUN:
        return
    proxy_fit.restore_collection_visibility(_validation_collection_visibility)


# A new asset's proxy is a reviewed collision envelope, not a deformation
# cage. apply_plan bakes the rectangular proxy into mesh vertices and leaves
# the anchor transform at unit scale. Once real geometry exists, fit it with
# one uniform factor so its proportions are preserved and it cannot exceed the
# footprint/height that the region-map review approved.
#
# Blender-generated assets have a front=-Y contract.  Align that direction to
# the proxy heading (+X), then isotropically scale until the first proxy edge is
# full.  This avoids the 0819 submarine failure where its long Y axis was
# compared to the proxy's narrow Y side and limited to 67% of the intended
# length. Pooled rig clones do not expose trustworthy visible bounds (the bad
# copies measured the bear/fish at tens of metres). Animated rigs now bypass
# pooling; their direct hierarchy has stable evaluated bounds and can use the
# same uniform maximum-inscribed fit without deforming the mesh.
if not DRY_RUN:
    report["proxy_fit_policy"] = {
        "applied": True,
        "mode": ("connectivity_box_fill" if ALLOW_UNQUALIFIED_OUTPUT
                 else "strict"),
        "heading_contract": (
            "choose the horizontal quarter-turn with maximum uniform box fill; "
            "front-axis alignment breaks ties"),
    }
    proxy_fit_rows = []
    proxy_fit_failures = []
    bpy.context.view_layer.update()
    for anchor in sorted(
            (obj for obj in bpy.context.scene.objects
             if obj.parent is None
             and obj.name in POPULATED_NAMES
             and obj.get("bi_proxy_dimensions_m")),
            key=lambda obj: obj.name):
        assets = [child for child in anchor.children
                  if ".spawn_asset(" in child.name]
        target = plc.normalize_proxy_dimensions(json.loads(
            str(anchor["bi_proxy_dimensions_m"])))
        actual_before = proxy_fit.dimensions_in_anchor_space(anchor, assets)
        factory_fqn = str(anchor.get("bi_populated_factory_fqn") or "")
        arrangement = str(anchor.get("bi_placement_arrangement") or "")
        semantic_target = str(anchor.get("bi_placement_semantic_target") or "")
        code_from = str(anchor.get("bi_code_from") or "")
        visual_count = plc.dense_field_visual_fit_count(
            population_target_count=anchor.get("bi_population_target_count"),
            manifest_population_targets=anchor.get(
                "bi_manifest_population_targets"),
            asset_count=len(assets),
        )
        visual_target = plc.dense_field_visual_fit_dimensions(
            actual_dimensions=actual_before,
            factory=factory_fqn.rsplit(".", 1)[-1] if factory_fqn else None,
            code_from=code_from,
            semantic_target=semantic_target,
            arrangement=arrangement,
            count=visual_count,
        )
        row = {
            "placeholder": anchor.name,
            "assets": [obj.name for obj in assets],
            "proxy_dimensions_m": target,
            "visual_fit_dimensions_m": visual_target,
            "actual_dimensions_before_m": actual_before,
            "factory_fqn": factory_fqn,
        }
        if not assets or actual_before is None:
            failure = f"{anchor.name}: generated asset geometry is unavailable"
            row.update(applied=False, reason="generated asset geometry is unavailable",
                       failures=[failure])
            proxy_fit_failures.append(failure)
            proxy_fit_rows.append(row)
            print(f"[placer]   ⛔ {anchor.name}: 找不到可量測資產，proxy fit 失敗",
                  flush=True)
            continue
        has_heading_contract = str(anchor.get("bi_asset_front_axis") or "") == "-Y"
        direct_animated_rig = bool(anchor.get("bi_direct_animated_rig", False))
        stable_evaluated_bounds = bool(
            anchor.get("bi_stable_evaluated_bounds", False))
        row["direct_animated_rig"] = direct_animated_rig
        row["stable_evaluated_bounds"] = stable_evaluated_bounds
        fit_target = visual_target if visual_target is not None else target
        if (ALLOW_UNQUALIFIED_OUTPUT and not has_heading_contract
                and not stable_evaluated_bounds):
            row.update(
                applied=False,
                method="preserve_generator_scale",
                reason=("rigged/procedural asset has no reliable visible-bounds "
                        "and forward-axis contract"),
                actual_dimensions_after_m=actual_before,
                failures=[],
            )
            proxy_fit_rows.append(row)
            print(
                f"[placer]   ⏭️ {anchor.name}: rigged/procedural bounds 不可靠，"
                "connectivity 保留 generator 尺寸",
                flush=True,
            )
            continue
        quarter_turns = (
            plc.best_horizontal_proxy_fit_quarter_turns(
                actual_before, fit_target,
                str(anchor.get("bi_asset_front_axis")))
            if has_heading_contract else 0)
        if quarter_turns:
            proxy_fit.rotate_roots_about_parent_z(assets, quarter_turns)
        actual_aligned = proxy_fit.dimensions_in_anchor_space(anchor, assets)
        if actual_aligned is None:
            failure = f"{anchor.name}: geometry unavailable after heading alignment"
            row.update(
                applied=False,
                method="heading_align_then_uniform_fill",
                heading_quarter_turns=int(quarter_turns),
                failures=[failure],
            )
            proxy_fit_failures.append(failure)
            proxy_fit_rows.append(row)
            continue
        fit_target = visual_target if visual_target is not None else target
        factor = plc.uniform_proxy_fit_scale(actual_aligned, fit_target)
        for asset in assets:
            asset.scale = tuple(float(value) * factor for value in asset.scale)
            asset["bi_proxy_uniform_scale"] = float(factor)
        anchor["bi_proxy_uniform_scale"] = float(factor)
        anchor["bi_proxy_heading_quarter_turns"] = int(quarter_turns)
        proxy_fit.force_evaluation(assets)
        actual_after = proxy_fit.dimensions_in_anchor_space(anchor, assets)
        fit_failures = (
            ["generated asset geometry disappeared after proxy fit"]
            if actual_after is None else
            plc.uniform_proxy_fit_failures(
                actual_aligned, actual_after, fit_target, factor)
        )
        row.update(
            applied=not fit_failures,
            method=("connectivity_static_uniform_fill"
                    if stable_evaluated_bounds
                    else "direct_rig_uniform_fill" if direct_animated_rig
                    else "heading_align_then_uniform_fill" if has_heading_contract
                    else "uniform_fit_inside_proxy"),
            heading_front_axis_before=("-Y" if has_heading_contract else None),
            heading_front_axis_after=("+X" if quarter_turns else
                                      "-Y" if has_heading_contract else None),
            heading_quarter_turns=int(quarter_turns),
            actual_dimensions_aligned_m=actual_aligned,
            visual_fit_dimensions_m=fit_target,
            uniform_scale_factor=float(factor),
            actual_dimensions_after_m=actual_after,
            failures=fit_failures,
        )
        if fit_failures:
            proxy_fit_failures.extend(
                f"{anchor.name}: {failure}" for failure in fit_failures)
        proxy_fit_rows.append(row)
        after_display = ([round(v, 3) for v in actual_after]
                         if actual_after is not None else None)
        print(
            f"[placer]   📏 {anchor.name}: 主軸 +{quarter_turns * 90}°，"
            f"真資產等比 ×{factor:.4f} "
            f"{[round(v, 3) for v in actual_aligned]}m → "
            f"{after_display}m "
            f"(proxy {target}m, visual {fit_target}m)",
            flush=True,
        )
    report["proxy_uniform_fit"] = proxy_fit_rows
    report["proxy_fit_failures"] = proxy_fit_failures
    if proxy_fit_failures:
        report["status"] = "PROXY_FIT_FAILED"
        report["hard_failures"] = list(proxy_fit_failures)
        if ALLOW_UNQUALIFIED_OUTPUT or ABLATION_NO_PLACEMENT_GATE:
            if ABLATION_NO_PLACEMENT_GATE:
                report["ablation_gate_bypassed"] = "proxy_fit"
            print(
                "[placer] ⚠️ proxy-fit quality FAIL retained（不擋，原樣輸出）",
                flush=True,
            )
        else:
            _restore_validation_collection_visibility()
            Path(REPORT_JSON).parent.mkdir(parents=True, exist_ok=True)
            Path(REPORT_JSON).write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            raise RuntimeError(
                "generated asset proxy fit did not affect evaluated bounds: "
                + "; ".join(proxy_fit_failures[:5]))


# ════════════════════════════════════════════════════════════════════════
# ★★populate 後補沉降 —— 用★真幾何★重新落地★★
# ════════════════════════════════════════════════════════════════════════
#   ★為什麼階段 A 做不完★：coarse 那層只有 placeholder box(全新 factory 是 2m 立方體、
#     replace 是被取代物的 box)，box 的 bbox ★不等於★資產的 bbox。用 box 沉降，
#     樹的 box 換成摩托車就會浮在半空。
#   ★旋轉不用重做★：它已經烤進 box 的 rotation_euler，populate 的
#     parent_to(obj, placeholder, no_inverse=True) 已經讓資產繼承了。
#     這裡重新呼叫 place() 只是為了讓第③步(量最低點沉降)在真幾何上重算一次
#     —— world_bbox() 是走 children 的，而資產現在正是 box 的 child。
#   ★airborne / submerged 也要在這裡做★：階段 A 現在★完全不做貼地★(2026-08-04 定的
#     分工，它只把屬性帶著走)，所以高度偏移也沒人做了 —— 這裡不收的話會飛的東西
#     就永遠貼在地上。place() 對這兩種只做高度偏移、不碰 bbox，很便宜。
#   ★兩種來源都要收★
#     ① box 自己有 bi_anchoring —— 階段 A 的 add 路徑摆过，這裡只補沉降
#     ② manifest 的 placement 有 anchoring —— ★replace 路徑★。HERO 的 replace 不是
#        一個 op，是靠 manifest 填 code 做的，所以階段 A ★從來沒碰過這些 box★，
#        它們還是「原本那棵樹」的直立擺法 ⇒ 旋轉與沉降都要在這裡做。
#        (樹→摩托車 upright→conform，正是 0803 4-B 講的那個情形)
if not DRY_RUN:
    coll_anchor = {}
    for p in placements:
        a = p.get("anchoring")
        if isinstance(a, str) and a in pm.ANCHORINGS and p.get("collection"):
            coll_anchor[p["collection"]] = a
    settle_targets = {}                          # obj -> (anchoring, yaw)
    for c in bpy.data.collections:
        a = coll_anchor.get(c.name)
        if not a:
            continue
        for o in c.objects:
            # ★用 POPULATED_NAMES 白名單★，不能用「有沒有 children」判斷 ——
            # 樹/灌木的骨架 child 從 coarse 階段就在，跟這輪有沒有被 populate 無關。
            if o.parent is None and o.name in POPULATED_NAMES:
                # 階段 A 只給 box 繞 Z 的 yaw ⇒ rotation_euler.z 就是原本的 yaw
                settle_targets[o] = (a, o.rotation_euler.z)
    for o in bpy.data.objects:
        a = o.get("bi_anchoring")
        if a in pm.ANCHORINGS and o.parent is None and o.name in POPULATED_NAMES:
            settle_targets[o] = (a, float(o.get("bi_yaw", o.rotation_euler.z)))
    if settle_targets:
        terrain = pm.terrain_candidates()
        terrain_cache = {}

        def terrain_for_object(obj):
            support_name = str(obj.get("bi_support_surface") or "")
            if support_name:
                support_obj = bpy.data.objects.get(support_name)
                if support_obj is not None:
                    return [support_obj]
            required_names, required_types = _surface_policy_targets(obj)
            cache_key = (tuple(required_names), tuple(required_types))
            if cache_key not in terrain_cache:
                terrain_candidates = pm.terrain_candidates(
                    surface_names=required_names,
                    surface_types=required_types,
                )
                if not terrain_candidates:
                    terrain_candidates = pm.terrain_candidates()
                terrain_cache[cache_key] = (
                    terrain_candidates if terrain_candidates else None)
            return terrain_cache[cache_key]

        if terrain is None:
            print("[placer] ⚠️ 找不到可 ray_cast 的地形 ⇒ 跳過沉降補正", flush=True)
        else:
            print(f"[placer] 沉降補正: {len(settle_targets)} 個 box, "
                  f"地形={[t.name for t in terrain][:2]}", flush=True)
            bpy.context.view_layer.update()
            # ⚠️ ★支撐面要先一次算完★ —— 每擺一個就 ray_cast 的話，place() 裡的
            #    view_layer.update() 會讓 depsgraph 變髒，大地形(百萬頂點)的 BVH
            #    被反覆重建，30 個物體就要跑十幾分鐘(2026-08-04 實測)。
            jobs = []
            n_nobox = 0
            for o, (a, yaw) in settle_targets.items():
                # ★只量資產、不量 placeholder box★ —— box 是給樹用的(4m 高、原點在
                #   正中心)，把它算進去的話最低點永遠是 box 底，結果是「盒底貼地、
                #   原點停在半空」，掛在原點的摩托車就飛起來(2026-08-04 實測 +2.61m)。
                #   populate 出來的資產名字固定是 `<Factory>(seed).spawn_asset(i)`。
                assets = [c for c in o.children if ".spawn_asset(" in c.name]
                if not assets:
                    assets = None                # 沒 populate 到就退回量整棵(原行為)
                    n_nobox += 1
                lo, hi = pm.world_bbox(assets or o)
                fr = float(max(hi[0] - lo[0], hi[1] - lo[1]) / 2) if lo is not None else 0.0
                support_name = o.get("bi_support_surface")
                support_obj = (bpy.data.objects.get(str(support_name))
                               if support_name else None)
                object_terrain = terrain_for_object(o)
                contact_offset = (
                    float(o["bi_support_contact_offset_m"])
                    if o.get("bi_support_contact_offset_m") is not None
                    else None)
                jobs.append((o, o.location.copy(), a, yaw, fr, assets,
                             object_terrain, contact_offset))
            if n_nobox:
                print(f"[placer]   ⚠️ {n_nobox} 個 box 找不到 spawn_asset child ⇒ 退回量整棵",
                      flush=True)
            supports = [pm.support_normal(object_terrain, loc, fr, verbose=True)
                        for _o, loc, _a, _y, fr, _as, object_terrain, _offset
                        in jobs]
            n_miss = sum(1 for _n, gz in supports if gz is None)
            moved = []
            multi_support_embed = []
            for (o, loc, a, yaw, fr, assets, object_terrain,
                 contact_offset), sup in zip(jobs, supports):
                z0 = o.location.z
                pm.place(o, object_terrain, loc, a, yaw, footprint_radius=fr, support=sup,
                         bbox_objs=assets, altitude_override=contact_offset)
                # A wide rigid upright cannot put every support on a steep slope
                # without tilting.  Keep its vertical pose and Composer XY, then
                # lower the hierarchy until even the uphill support is embedded.
                if a == "upright" and assets is not None and fr >= 0.75:
                    prepared = pm.prepare_terrains(object_terrain)
                    evidence = pm.embed_multi_support_upright(
                        o, assets, prepared, k=4, embed_depth=0.10)
                    if evidence is not None:
                        evidence["object"] = o.name
                        evidence["footprint_radius"] = fr
                        multi_support_embed.append(evidence)
                        print(f"[placer]   wide-upright embed {o.name}: "
                              f"dz={evidence['applied_dz']:+.2f}m, "
                              f"gaps {evidence['gaps_before']} -> "
                              f"{evidence['gaps_after']}", flush=True)
                moved.append(abs(o.location.z - z0))
            moved.sort()
            print(f"[placer]   ✅ 補正完成 (打空 {n_miss} 個, 高度修正中位數 "
                  f"{moved[len(moved) // 2]:.2f}m, 最大 {moved[-1]:.2f}m)", flush=True)
            if multi_support_embed:
                report["multi_support_upright_embed"] = multi_support_embed

            # ★★資產層驗收★★ —— 上面那些數字講的都是 box 的狀態，
            #   ★資產有沒有跟著到位它看不到★。2026-08-04 就是這樣：ray_miss=0、
            #   傾角 14.4°、高度修正 2.71m 全部漂亮，而 20 台摩托車浮在離地 2.6m
            #   的空中，靠近拍圖才發現。所以這裡直接量★資產自己的接地點到地面★。
            #   ⚠️ ★基準必須是真實地形 ray_cast，不能是 support 的擬合平面★ ——
            #     沉降本來就是照那個平面擺的，拿它當基準是★自己驗自己，永遠會過★
            #     (2026-08-04 報 0/20，實際有 8/20 陷入地裡 0.3~0.64m，是獨立寫的
            #      check_gap4.py 用真地形 ray_cast 才抓到的)。
            bpy.context.view_layer.update()
            prepared_cache = {}

            def prepared_for(object_terrain):
                key = tuple(obj.name for obj in (object_terrain or []))
                if key not in prepared_cache:
                    prepared_cache[key] = pm.prepare_terrains(object_terrain)
                return prepared_cache[key]

            gaps = []
            gap_rows = []
            spreads, legs = [], []
            spread_rows = []
            for (o, loc, a, yaw, fr, assets, object_terrain,
                 _contact_offset), sup in zip(jobs, supports):
                if a in pm.AIRBORNE_MODES or assets is None:
                    continue
                prepared = prepared_for(object_terrain)
                g, n_hit, _n = pm.contact_gap(assets, prepared)
                gap_rows.append({
                    "object": o.name,
                    "assets": [asset.name for asset in assets],
                    "support_surfaces": [
                        terrain_obj.name for terrain_obj in object_terrain],
                    "gap_m": float(g) if n_hit else None,
                    "ray_hits": int(n_hit),
                    "sample_count": int(_n),
                })
                if n_hit:
                    gaps.append(g)
                # ★接地間隙只證明「沒穿地」，不證明「站得穩」★ —— 見 contact_spread
                #   的說明。單看 contact_gap 會把「一隻腳貼死、其餘三隻懸空」報成完美。
                sp, gl, _k = pm.contact_spread(assets, prepared)
                spread_rows.append({
                    "object": o.name,
                    "spread_m": None if sp is None else float(sp),
                    "contact_gaps_m": [float(value) for value in gl],
                    "sample_count": int(_k),
                })
                if sp is not None:
                    spreads.append(sp)
                    legs += gl
            report["asset_contact_gaps"] = gap_rows
            report["asset_contact_spreads"] = spread_rows
            if gaps:
                gaps.sort()
                bad = sum(1 for g in gaps if abs(g) > 0.3)
                mark = "✅" if bad == 0 else "⛔"
                print(f"[placer]   {mark} 資產接地間隙(★對真實地形★): 中位數 "
                      f"{gaps[len(gaps)//2]:+.2f}m "
                      f"(最小 {gaps[0]:+.2f} 最大 {gaps[-1]:+.2f})  "
                      f"★偏離>0.3m: {bad}/{len(gaps)}★", flush=True)
                report["asset_gap"] = {
                    "n": len(gaps), "median": gaps[len(gaps) // 2],
                    "min": gaps[0], "max": gaps[-1], "n_bad": bad}
            if spreads:
                spreads.sort()
                nbad_leg = sum(1 for g in legs if g > 0.3)
                mark = "✅" if nbad_leg <= 0.05 * len(legs) else "⛔"
                print(f"[placer]   {mark} ★接地點高度差(站得穩不穩)★: 中位數 "
                      f"{spreads[len(spreads)//2]:.2f}m (最大 {spreads[-1]:.2f})  "
                      f"★懸空>0.3m 的接地點: {nbad_leg}/{len(legs)}★", flush=True)
                report["contact_spread"] = {
                    "n": len(spreads), "median": spreads[len(spreads) // 2],
                    "max": spreads[-1], "n_points": len(legs), "n_bad_points": nbad_leg}
            # 逐 anchoring 模式拆開報 —— 接地的看傾角，airborne/submerged 看離地高度
            import math as _m
            per_mode = {}
            for (o, loc, a, yaw, fr, _as, _terrain,
                 _contact_offset), sup in zip(jobs, supports):
                d = per_mode.setdefault(a, {"n": 0, "tilt": [], "dz": []})
                d["n"] += 1
                if a in pm.AIRBORNE_MODES:
                    d["dz"].append(o.location.z - loc.z)
                else:
                    d["tilt"].append(_m.degrees(_m.acos(max(-1.0, min(1.0, sup[0].z)))))
            for a, d in sorted(per_mode.items()):
                if d["tilt"]:
                    t = sorted(d["tilt"])
                    print(f"[placer]     {a:10s} {d['n']:4d} 個  傾角中位數 "
                          f"{t[len(t) // 2]:.1f}° (最小 {t[0]:.1f}° 最大 {t[-1]:.1f}°)", flush=True)
                else:
                    z = sorted(d["dz"])
                    print(f"[placer]     {a:10s} {d['n']:4d} 個  離地 "
                          f"{z[len(z) // 2]:+.2f}m (最小 {z[0]:+.2f} 最大 {z[-1]:+.2f})", flush=True)
            report["settle_pass"] = {
                "boxes": len(jobs), "ray_miss": n_miss,
                "dz_median": moved[len(moved) // 2], "dz_max": moved[-1],
                "per_mode": {a: {"n": d["n"],
                                 "tilt_median": (sorted(d["tilt"])[len(d["tilt"]) // 2]
                                                 if d["tilt"] else None),
                                 "dz_median": (sorted(d["dz"])[len(d["dz"]) // 2]
                                               if d["dz"] else None)}
                             for a, d in per_mode.items()}}

# ════════════════════════════════════════════════════════════════════════
# ★★world motion —— populate/settle 後才對新增 anchor 套世界座標動畫★★
# ════════════════════════════════════════════════════════════════════════
# 動的是 parentless placeholder anchor；資產及其骨架是 child，所以局部 deformation
# 完全保留。只有 apply_plan 本輪明確 stamp bi_world_motion_* 的 box 會動；同一個
# Factory collection 裡既存的其他物體不會因 collection-level request 一起漂浮。
if not DRY_RUN:
    # Policy: validators never choose a replacement location or trajectory.
    # The Step-8 visual Agent owns Composer ``screen_positions``.  Step 9 only
    # materializes the requested path once, audits it, and reports hard failures.
    report["automatic_placement_repairs"] = {
        "enabled": False,
        "policy": "detect_only; Agent revises Composer screen_positions",
    }

    report["world_motion"] = []
    motion_path_failures = []
    for idx, p in enumerate(placements):
        # ★會飛的 preset 正規化已移到第 8 步★(asset_scale_pins.normalize_flying_motions，打 stamp 之前)。
        #   這裡不能再改 preset/intensity：anchor 的 stamp 是 apply_plan 打的，改了就對不上
        #   (2026-09-13 luna moth / bee 整段不動就是這個)。只補 air_soar 缺的 heading —— anchor 比對只看
        #   preset+intensity，不看 params，所以補 heading 是安全的。
        _requested = dict(p.get("world_motion") or {})
        if str(_requested.get("preset") or "") == "air_soar":
            _params = dict(_requested.get("params") or {})
            if _params.get("heading_deg") is None:
                _cam = bpy.context.scene.camera
                if _cam is not None:
                    _right = _cam.matrix_world.col[0].to_3d()
                    # 使用者：滑翔要「快速從畫面左邊切到右邊」⇒ 固定沿相機右軸正方向(不再依 seed 決定左右)
                    _sign = 1.0
                    _params["heading_deg"] = math.degrees(
                        math.atan2(_sign * _right.y, _sign * _right.x)) % 360.0
                    _requested["params"] = _params
                    p = {**p, "world_motion": _requested}
                    placements[idx] = p
                    print(f"[placer] 🦅 #{idx} {p.get('factory')}: air_soar 橫越畫面 "
                          f"heading={_params['heading_deg']:.0f}°", flush=True)
        request = wm.validate_runtime_request(p.get("world_motion"))
        motion_report = {
            "placement": idx,
            "code_from": p.get("code_from"),
            "factory": p.get("factory"),
            "collection": p.get("collection"),
            "request": request,
            "applied_count": 0,
            "objects": [],
        }
        if request["preset"] == "none":
            report["world_motion"].append(motion_report)
            continue
        if int(p.get("added_this_plan") or 0) <= 0:
            raise ValueError(f"placement #{idx}: non-none world_motion 只允許本輪 add")
        try:
            wm.validate_execution_context(
                request, action="add", anchoring=p.get("anchoring"))
        except ValueError as error:
            raise ValueError(f"placement #{idx}: world_motion context: {error}") from error
        if p.get("realized_at", "populate") != "populate":
            raise ValueError(
                f"placement #{idx}: world_motion 不支援 realized_at={p.get('realized_at')}"
            )
        coll = bpy.data.collections.get(p.get("collection", ""))
        if coll is None:
            raise ValueError(f"placement #{idx}: world_motion 找不到 collection")
        anchors = sorted(
            (
                obj for obj in coll.objects
                if obj.parent is None
                and obj.name in POPULATED_NAMES
                and obj.get("bi_world_motion_preset") == request["preset"]
                and obj.get("bi_world_motion_intensity") == request["intensity"]
            ),
            key=lambda obj: obj.name,
        )
        if not anchors:
            warning = (
                f"placement #{idx}: requested {request['preset']} but no populated "
                "anchor carried the matching world-motion stamp; keeping placement static"
            )
            motion_report.update(
                skipped=True,
                skip_reason="missing_populated_motion_anchor",
                warning=warning,
            )
            report.setdefault("world_motion_warnings", []).append({
                "placement": idx,
                "edit_id": p.get("code_from"),
                "factory": p.get("factory"),
                "collection": p.get("collection"),
                "preset": request["preset"],
                "reason": "missing_populated_motion_anchor",
            })
            report["world_motion"].append(motion_report)
            if idx < len(report.get("results", [])):
                report["results"][idx]["world_motion"] = motion_report
            print(f"[placer] ⚠️ {warning}", flush=True)
            continue
        # ★同一群滑翔的要平行飛★(2026-09-13 cliff：6 隻蝴蝶各自隨機方向 ⇒ 擺放必須替每隻
        #   預留整段掃過範圍，結果被推到 0.8~8.4m，五隻投影 ≤0.6% 等於看不到)。
        #   同一筆 edit 用同一個 seed ⇒ 方向與速度變化完全一樣 ⇒ 路徑是平移、相對位置不變、
        #   永遠不會互撞，擺放就可以把牠們排得很近。
        _parallel_group = request["preset"] in wm.PARALLEL_MOTION_PRESETS
        for order, anchor in enumerate(anchors):
            base_seed = (int(p.get("seed") or 0) if _parallel_group
                         else int(p.get("seed") or 0) + order * 1009)
            motion_terrain = None
            if request["preset"] in wm.GROUND_PRESETS:
                support_name = anchor.get("bi_support_surface")
                support_obj = (bpy.data.objects.get(str(support_name))
                               if support_name else None)
                motion_terrain = ([support_obj] if support_obj is not None
                                  else pm.terrain_candidates() or None)
            try:
                result = wm.apply_world_motion(
                    anchor,
                    request,
                    scene=bpy.context.scene,
                    phase_seed=base_seed,
                    terrain=motion_terrain,
                    measure=([child for child in anchor.children
                              if ".spawn_asset(" in child.name]
                             if request["preset"] in wm.GROUND_PRESETS else None),
                )
            except Exception as error:
                failure = {
                    "type": "motion_path_invalid",
                    "placement": idx,
                    "edit_id": p.get("code_from"),
                    "object": anchor.name,
                    "preset": request["preset"],
                    "error": str(error),
                    "automatic_resample": False,
                }
                motion_path_failures.append(failure)
                motion_report["objects"].append({
                    "object": anchor.name,
                    "preset": request["preset"],
                    "applied": False,
                    "failure": failure,
                })
                continue
            camera_audit = camera_vis.audit_anchor(bpy.context.scene, anchor)
            result["camera_path_attempts"] = 1
            result["camera_visibility"] = camera_audit
            result["automatic_camera_repair"] = False
            motion_report["objects"].append(result)
        motion_report["applied_count"] = sum(
            bool(item.get("applied")) for item in motion_report["objects"])
        report["world_motion"].append(motion_report)
        if idx < len(report.get("results", [])):
            report["results"][idx]["world_motion"] = motion_report
        successful = [item for item in motion_report["objects"] if item.get("applied")]
        if successful:
            max_disp = max(obj["max_world_displacement_m"] for obj in successful)
            print(
                f"[placer] 🌊 world_motion #{idx} {request['preset']}: "
                f"{motion_report['applied_count']} 個 anchor，最大位移 {max_disp:.3f}m",
                flush=True,
            )

    report["camera_visibility"] = camera_vis.snapshot_requested_visibility(
        bpy.context.scene)
    requested_anchors = sorted(
        (obj for obj in bpy.context.scene.objects
         if obj.get("bi_requested_placement_index") is not None),
        key=lambda obj: (int(obj["bi_requested_placement_index"]), obj.name),
    )
    invalid_path_objects = {item["object"] for item in motion_path_failures}
    collision_anchors = [
        anchor for anchor in requested_anchors
        if anchor.name not in invalid_path_objects
    ]
    report["animated_collision_review"] = camera_vis.audit_world_collisions(
        bpy.context.scene,
        collision_anchors,
    )
    report["animated_collision_review"]["excluded_invalid_path_objects"] = sorted(
        invalid_path_objects)
    report["same_factory_collision_review"] = camera_vis.audit_world_collisions(
        bpy.context.scene,
        collision_anchors,
        threshold=0.0,
        same_factory_only=True,
    )
    report["same_factory_collision_review"]["excluded_invalid_path_objects"] = (
        sorted(invalid_path_objects))
    hard_failures = list(report.get("proxy_fit_failures") or [])
    # ★可見格數只警告不擋★(2026-10-19 使用者)：飛鳥 air_glide 往前飛 4m 會飛出畫面
    #   (1019 #06 綠咬鵑 14/72 < 24)，以前整個 case 判死；現在照樣出片，數字留在 report。
    report["camera_visibility_warnings"] = list(report["camera_visibility"]["failures"])
    for warning in report["camera_visibility_warnings"][:5]:
        print(f"[placer] ⚠️ 可見格數不足(只警告): {warning}", flush=True)
    report["motion_path_failures"] = motion_path_failures
    hard_failures.extend(
        f"motion path {item['edit_id'] or item['object']}: {item['error']}"
        for item in motion_path_failures
    )
    animated_pair_keys = {
        tuple(sorted(str(obj) for obj in violation.get("objects", [])))
        for violation in report["animated_collision_review"]["violations"]
    }
    for violation in report["same_factory_collision_review"]["violations"]:
        pair_key = tuple(sorted(str(obj) for obj in violation.get("objects", [])))
        if pair_key in animated_pair_keys:
            continue
        hard_failures.append(
            "same-factory collision "
            f"{violation.get('factory') or violation.get('factories')}: "
            f"{violation['objects']} at frames "
            f"{violation['first_frame']}..{violation['last_frame']}")
    hard_failures.extend(
        "animated collision "
        f"{violation.get('edit_ids') or violation['objects']} at frames "
        f"{violation['first_frame']}..{violation['last_frame']}"
        for violation in report["animated_collision_review"]["violations"]
    )
    report["hard_failures"] = hard_failures
    report["status"] = "NEEDS_AGENT_REVISION" if hard_failures else "PASS"
    if hard_failures:
        if ALLOW_UNQUALIFIED_OUTPUT or ABLATION_NO_PLACEMENT_GATE:
            report["unqualified_output_allowed"] = True
            if ABLATION_NO_PLACEMENT_GATE:
                report["ablation_gate_bypassed"] = "placement_validation"
                print("[placer] ★ablation：擺放品質 FAIL 不擋，原樣輸出★ "
                      + "; ".join(hard_failures[:3]), flush=True)
            print(
                "[placer] ⚠️ connectivity-only: placement quality FAIL retained; "
                "saving unqualified output",
                flush=True,
            )
        else:
            # Persist diagnostics before failing so the Agent can choose new Composer
            # positions. Do not save an automatically moved or silently accepted blend.
            if os.environ.get("BI_SAVE_PLACEMENT_DIAGNOSTIC") == "1":
                diagnostic_blend = Path(OUT_BLEND).with_name(
                    "candidate_needs_agent_revision.blend")
                diagnostic_blend.parent.mkdir(parents=True, exist_ok=True)
                bpy.ops.wm.save_as_mainfile(
                    filepath=str(diagnostic_blend), compress=False)
                # The diagnostic file was serialized while generated geometry
                # was evaluable.  Restoring only the throwaway in-process state
                # after save cannot corrupt that file.
                _restore_validation_collection_visibility()
                report["diagnostic_blend"] = str(diagnostic_blend.resolve())
                report["diagnostic_blend_role"] = (
                    "derived rejected candidate for Agent review; not render-qualified")
            Path(REPORT_JSON).parent.mkdir(parents=True, exist_ok=True)
            Path(REPORT_JSON).write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            raise RuntimeError(
                "requested placement validation failed; Agent must revise Composer JSON: "
                + "; ".join(hard_failures[:5]))

if not DRY_RUN:
    report["serialization_contract"] = {
        "populated_collections_evaluable_at_save": True,
        "collections": sorted(_validation_collection_visibility),
        "reason": (
            "hidden generated hierarchies serialize stale world matrices and "
            "reopen near world origin"
        ),
    }
    # ★粒子壽命拉到整段影片★(2026-09-13 使用者：小丑魚只存在 3 秒，後面消失)。
    #   infinigen 自己的影片是 72 幀 ⇒ fish_school/bug_swarm 的 lifetime=72、cache=1..73。
    #   改壽命之後一定要重烤，不然快取還是舊的。BI_EXTEND_PARTICLES=0 可關掉。
    # 17:50 曾暫時關閉(重烤烤出 0 幀、毀掉好快取)；18:05 找到原因(上一層 unique_assets 也被藏起來)並修好，
    #   重新預設開啟。BI_EXTEND_PARTICLES=0 可關掉。
    if os.environ.get("BI_EXTEND_PARTICLES", "1") != "0":
        try:
            _particles = master_swap.extend_particle_lifetimes(
                bpy.context.scene, log=lambda m: print(m, flush=True))
            report["particle_lifetimes"] = _particles
        except Exception as _error:
            report["particle_lifetimes"] = {"error": str(_error)}
            print(f"[placer] ⚠️ 粒子壽命延長失敗: {_error}", flush=True)
    # ★樹的微幅搖擺★(2026-09-13 使用者看過樣片後同意)：整棵樹繞根部 ±0.8°，剛體旋轉、
    #   關節不會分離、渲染成本幾乎是零(實測 16.9s vs 21.7s/幀)。BI_TREE_SWAY=0 可關掉。
    if os.environ.get("BI_TREE_SWAY", "1") != "0":
        try:
            import tree_sway
            _sway = tree_sway.add_to_scene(
                # ⚠️ 環境變數的預設字串會蓋掉模組預設(2026-09-13 實測 coral 還在用 0.8°) ⇒ 留空就用模組的值
                max_distance_m=float(os.environ.get("BI_TREE_SWAY_DIST_M")
                                     or tree_sway.DEFAULT_MAX_DISTANCE_M),
                degrees=float(os.environ.get("BI_TREE_SWAY_DEG")
                              or tree_sway.DEFAULT_DEGREES),
                fps=float(bpy.context.scene.render.fps or 24))
            report["tree_sway"] = _sway
            if _sway.get("swayed"):
                print(f"[placer] 🌳 {_sway['swayed']} 棵樹加上微幅搖擺 "
                      f"(±{_sway['degrees']}°, 週期 {_sway['period_s']}s)", flush=True)
        except Exception as _error:
            report["tree_sway"] = {"error": str(_error)}
            print(f"[placer] ⚠️ 樹搖擺失敗: {_error}", flush=True)
    # ★鳥類拍翅★(2026-09-13)：官方 FlyingBirdFactory 的 idle 在 catalog 標為會崩，成品 mesh
    #   卻留著 tag_wing 頂點屬性 ⇒ 用 GN 把翅膀繞身體長軸轉，翅根權重 0 不會裂開。
    #   BI_BIRD_FLAP=0 可關掉。
    if os.environ.get("BI_BIRD_FLAP", "1") != "0":
        try:
            import bird_flap
            # ★逐資產排除★(2026-09-13 使用者：老鷹的翅膀動畫看起來怪，拿掉只留快速前飛)。
            #   BI_BIRD_FLAP_EXCLUDE="eagle,xxx"(用 director 的資產 id)。
            _excluded = {x.strip() for x in
                         (os.environ.get("BI_BIRD_FLAP_EXCLUDE") or "").split(",") if x.strip()}
            _flap_skip = set()
            if _excluded:
                for _p in placements:
                    if str(_p.get("code_from") or "") not in _excluded:
                        continue
                    _col = bpy.data.collections.get(str(_p.get("collection") or ""))
                    for _obj in (list(_col.all_objects) if _col else []):
                        _flap_skip.add(_obj.name)
                        _flap_skip.update(c.name for c in _obj.children_recursive)
            _flap = bird_flap.add_to_scene(
                max_degrees=float(os.environ.get("BI_BIRD_FLAP_DEG", "18") or 18),
                period_frames=float(os.environ.get("BI_BIRD_FLAP_FRAMES", "24") or 24),
                skip_names=_flap_skip)
            report["bird_flap"] = _flap
            if _flap.get("flapped"):
                print(f"[placer] 🦅 {_flap['flapped']} 隻鳥加上拍翅 "
                      f"(±{_flap['max_degrees']}°, {_flap['period_frames']} 幀一次)", flush=True)
        except Exception as _error:
            report["bird_flap"] = {"error": str(_error)}
            print(f"[placer] ⚠️ 拍翅失敗: {_error}", flush=True)
    # ★地面動物原地呼吸★(2026-09-13)：Carnivore/Herbivore 的 idle 在 catalog 是 director_enabled=false，
    #   BirdFactory 三個模式都被標為會崩 ⇒ 站著的動物本來完全不動。用整體微幅縮放(±1.2%)當呼吸，
    #   剛體、腳不離地、一個週期回到原樣。有 world_motion 的不加。BI_CREATURE_IDLE=0 可關掉。
    if os.environ.get("BI_CREATURE_IDLE", "1") != "0":
        try:
            import creature_idle
            _idle = creature_idle.add_to_scene(
                amplitude=float(os.environ.get("BI_CREATURE_IDLE_AMP") or creature_idle.DEFAULT_AMPLITUDE),
                fps=float(bpy.context.scene.render.fps or 24))
            report["creature_idle"] = _idle
            if _idle.get("breathing"):
                print(f"[placer] 🫁 {_idle['breathing']} 隻動物加上原地呼吸 "
                      f"(±{_idle['amplitude'] * 100:.1f}%, 週期 {_idle['period_s']}s)", flush=True)
        except Exception as _error:
            report["creature_idle"] = {"error": str(_error)}
            print(f"[placer] ⚠️ 原地呼吸失敗: {_error}", flush=True)
    # ★骨架動畫整體放慢★(2026-09-12 使用者：動作太快像原地爆衝)。GN 拍翅在 catalog 調頻率，
    #   creature 的 sinusoid driver 在這裡統一乘 BI_LOCAL_ANIM_SLOWDOWN(預設 0.4)。
    try:
        import slow_local_animation as sla
        _slow = sla.slow_drivers(float(os.environ.get("BI_LOCAL_ANIM_SLOWDOWN", "0.4") or 0.4))
        report["local_animation_slowdown"] = _slow
        if _slow.get("changed"):
            print(f"[placer] 🐢 骨架動畫放慢 ×{_slow['factor']}: {_slow['changed']} 條 driver"
                  f"({_slow['objects']} 個物件)", flush=True)
    except Exception as _error:            # 放慢失敗不該讓整個 case 死
        report["local_animation_slowdown"] = {"error": str(_error)}
        print(f"[placer] ⚠️ 骨架動畫放慢失敗: {_error}", flush=True)
    print(f"[placer] saving → {OUT_BLEND}", flush=True)
    Path(OUT_BLEND).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND, compress=False)
    report["provenance"]["output_blend"] = _file_identity(OUT_BLEND)
    _output_tag_path = Path(OUT_BLEND).parent / "MaskTag.json"
    tag_system.save_tag(path=str(_output_tag_path))
    report["mask_tag"] = str(_output_tag_path)
    print(f"[placer] tags → {_output_tag_path}", flush=True)

Path(REPORT_JSON).parent.mkdir(parents=True, exist_ok=True)
Path(REPORT_JSON).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
print(f"[placer] report → {REPORT_JSON}", flush=True)
print("PLACER_DONE", flush=True)

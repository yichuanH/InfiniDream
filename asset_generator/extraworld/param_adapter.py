"""對接腳本 adapter —— 把「參數腳本」包成 infinigen 的 AssetFactory。

參數腳本(3_blender_generator.py --param)產出的是：

    def sample_params(rng) -> dict
    def build(params) -> object          # 回傳單一 root object
    params = sample_params(np.random.default_rng(int(os.environ.get("ASSET_SEED","0"))))
    build(params)

pipeline(composer.py / placer.py)要的是：

    Cls(factory_seed).spawn_asset(i, placeholder=..., distance=...)
      → FixedSeed(int_hash((factory_seed, i)))
      → create_asset(i=i, placeholder=..., face_size=..., distance=..., vis_distance=...)

這支只做「機械性的殼」——★不碰幾何、不改參數★。0730 §8-B 判斷 adapter 補不上
「哪個常數該變成 rng」那種語意判斷，而那部分已經由 --param 在生成當下解決了，
所以剩下的純粹是包裝。

用法:
    python param_adapter.py --script <round_xx.py> --name motorcycle \\
        --out-dir output/3_asset_generator/_script_factory/motorcycle
    # 產出 <out-dir>/overlay/(infinigen 鏡像 + 新模組) + overlay/factory.txt
"""
import argparse
import ast
import re
import shutil
import subprocess
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))   # BeyondInfinigen/
from placement_modes import ANCHORINGS  # noqa: E402

REPO_ROOT = HERE.parent.parent
TEMPLATES = Path(os.environ.get("INFINIGEN_TEMPLATES", REPO_ROOT / "third_party" / "Infinigen_templates")).resolve()
CODEBASE = Path(os.environ.get("INFINIGEN_ROOT", REPO_ROOT / "third_party" / "infinigen")).resolve() / "infinigen"        # 真正的 infinigen 套件目錄

# 新資產放這個子套件底下。挑 objects/ 是因為 composer 的 placeholder collection 名只看
# class name，路徑本身不影響替換；放在 objects/ 只是為了跟官方資產同一層好找。
PKG_REL = "assets/objects/generated"


def strip_module_tail(code: str) -> str:
    """拿掉腳本結尾那段 `params = sample_params(...)` / `build(params)` 的立即執行。

    留著的話，光是 import 這個模組就會在 scene 裡建出一個物體 —— 在 populate 中途
    無聲地多長一個資產出來，是最難查的那種 bug。
    """
    tree = ast.parse(code)
    lines = code.splitlines()
    drop = set()
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id in ("build", "sample_params"):
                drop.update(range(node.lineno - 1, node.end_lineno))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == "sample_params":
                drop.update(range(node.lineno - 1, node.end_lineno))
    kept = [ln for n, ln in enumerate(lines) if n not in drop]
    return "\n".join(kept).rstrip() + "\n"


def class_name_for(name: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^0-9a-zA-Z]+", name) if p) + "Factory"


FACTORY_TMPL = '''
# ─────────────────────────────────────────────────────────────────────────────
# infinigen adapter —— 由 param_adapter.py 自動產生，★以上為生成器原始碼，未修改★
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np  # noqa: E402

from infinigen.core.placement.factory import AssetFactory  # noqa: E402
from infinigen.core.util.math import int_hash  # noqa: E402


class {cls}(AssetFactory):
    """`build()` 的 infinigen 介面。幾何完全來自上面的 sample_params/build。"""

    def __init__(self, factory_seed=None, seed=None, coarse=False, **kwargs):
        # 呼叫端有兩套慣例：AssetFactory 基底吃 `factory_seed`，但官方資產大多自己定義
        # `__init__(self, seed, ...)`，composer 也就照著寫 `Factory(seed=seed)`。
        # 冒充某個既有 factory 時會走到後者，所以兩個都收；多餘的 kwargs(season、
        # fruit_chance 之類原資產專屬的參數)一律吞掉，它們對這個資產沒有意義。
        if factory_seed is None:
            factory_seed = seed
        super().__init__(factory_seed, coarse)

    # ★母本池★：只真的 build() N 次，其餘用 linked duplicate(共用 mesh data)。
    #   ⚠️ ★為什麼需要★(2026-08-04 實測)：腳本組的 build() 用 bpy.ops.mesh.primitive_*
    #      建幾何，每台約 200 次 operator，而 bpy.ops ★每次都觸發全場 depsgraph 更新★，
    #      成本隨場景物件數增長 ⇒ 摩托車 20 台 populate 花 51 分 28 秒，
    #      且每台從 72 秒漲到 294 秒。同一輪的糖果樹(改寫官方 TreeFactory，走 bmesh)
    #      只要 2 分 57 秒且完全不漲 —— 差 17 倍，證實瓶頸在 bpy.ops 不在 populate。
    #   composer.py 早就用同一套(N_MASTER=8 + linked duplicate)，這裡照抄。
    n_masters = {n_masters}
    _masters = {{}}          # key -> root object

    _MASTER_STASH = "_bi_master_stash"

    def _stash(self):
        """放母本的 collection —— ★故意不 link 到 scene★，母本因此不會被渲染，
        也不會被 populate / 沉降補正掃到。"""
        import bpy
        c = bpy.data.collections.get({cls}._MASTER_STASH)
        if c is None:
            c = bpy.data.collections.new({cls}._MASTER_STASH)
        return c

    def _reset_tf(self, root):
        """把 root 的變換歸單位，並斷開 parent。

        ★為什麼一定要做★(2026-08-04 抓到的 bug)：spawn_asset 是用
          `butil.parent_to(obj, placeholder, no_inverse=True)` 把資產掛上去的，
          no_inverse 表示★不會清掉 obj 原有的 local transform★，最終世界座標是
          `placeholder.matrix_world @ obj.matrix_basis`。所以 create_asset 回傳的
          物件身上只要帶著任何變換，資產就會從 placeholder 的位置被推開 ——
          實測是整台摩托車浮在半空、離地數十公尺。
        """
        from mathutils import Matrix
        root.parent = None
        root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_basis = Matrix.Identity(4)
        return root

    def _spawn_master(self, key):
        import bpy
        rng = np.random.default_rng(int_hash((self.factory_seed, key)) % (2 ** 32))
        obj = build(sample_params(rng))
        if obj is None:
            raise RuntimeError(
                "build() 沒有回傳 root object —— 參數腳本必須 `return` 那個 root，"
                "否則 spawn_asset 無從得知要把哪個物件掛到 placeholder 底下")
        self._reset_tf(obj)
        # ★母本整棵移進 stash★ —— 母本一旦被交給 populate，就會被 parent 到
        #   placeholder 上、再被沉降補正移動，之後複製它的個體全都會繼承那個
        #   位移。所以母本必須是「永遠不出場」的乾淨樣本。
        stash = self._stash()
        for o in [obj] + list(obj.children_recursive):
            for c in list(o.users_collection):
                c.objects.unlink(o)
            stash.objects.link(o)
        return obj

    def _clone(self, src):
        """整棵階層複製，★共用 mesh data★(不 copy .data) —— 省時間也省顯存。"""
        import bpy
        root = src.copy()
        bpy.context.scene.collection.objects.link(root)
        self._reset_tf(root)          # src.copy() 連 parent 和 local transform 一起帶
        stack = [(src, root)]
        while stack:
            s, d = stack.pop()
            for ch in s.children:
                c = ch.copy()
                bpy.context.scene.collection.objects.link(c)
                c.parent = d
                c.matrix_parent_inverse = ch.matrix_parent_inverse.copy()
                stack.append((ch, c))
        return root

    def create_asset(self, i=0, placeholder=None, **params):
        # spawn_asset 已經在 FixedSeed(int_hash((factory_seed, i))) 裡呼叫這裡，
        # 但 sample_params 吃的是一個 Generator 物件，所以用同一個 hash 明確餵給它 ——
        # 依賴全域 RNG 狀態的話，同樣的 (factory_seed, i) 不保證得到同一個個體。
        if self.n_masters <= 0:                      # 0 = 關掉母本池，每個都真的建
            obj = build(sample_params(
                np.random.default_rng(int_hash((self.factory_seed, i)) % (2 ** 32))))
            if obj is None:
                raise RuntimeError("build() 沒有回傳 root object")
            return self._reset_tf(obj)
        key = (self.factory_seed, i % self.n_masters)
        if key not in {cls}._masters:
            {cls}._masters[key] = self._spawn_master(i % self.n_masters)
        # ★母本本人絕不回傳★ —— 連第一次也走複製，否則母本會被 populate 動到
        return self._clone({cls}._masters[key])
'''


def make_overlay(script: Path, name: str, out_dir: Path,
                 class_name: str | None = None, anchoring: str | None = None,
                 n_masters: int = 8) -> dict:
    """`class_name` 覆寫類別名 —— composer 是靠 class 名去比對 blend 裡的
    `placeholders:<CLASS>(seed)` collection，所以要替換森林裡的樹，這個新資產就得
    叫做 `TreeFactory`（跟 red_bush 冒充 BushFactory 同一招）。"""
    code = strip_module_tail(script.read_text())

    # ★★前提檢查：腳本必須是 --param 模式★★(2026-08-07)
    #   FACTORY_TMPL 產生的 create_asset/_spawn_master 直接呼叫 `build(sample_params(rng))`
    #   ⇒ ★腳本沒有這兩個函式的話，包出來的 overlay 是「能 import、一 populate 就
    #     NameError」★。3_blender_generator.py 的 --param 是 opt-in 旗標，而
    #     3_generator.py 從來沒傳過它 ⇒ 這條線★從沒真的走通過★(只有手動跑的摩托車
    #     ablation 用了 --param)。2026-08-06 的聖誕場景就是死在這裡：
    #     placer 跑了 4 分鐘、把 85 顆石頭都生完了才在 BoxFactory 炸掉。
    #   ⇒ ★在這裡就擋★。壞掉的東西越早出聲越好，尤其後面那段要花好幾分鐘。
    missing = [fn for fn in ("sample_params", "build")
               if not re.search(rf"^def\s+{fn}\s*\(", code, re.M)]
    if missing:
        raise SystemExit(
            f"[adapter] ⛔ {script.name} 缺少 {'/'.join(missing)}() —— "
            f"param_adapter 只能包 ★--param 模式★ 的腳本(FACTORY_TMPL 會呼叫 "
            f"build(sample_params(rng)))。請用 3_blender_generator.py --param 重生此資產。")

    cls = class_name or class_name_for(name)
    slug = re.sub(r"[^0-9a-z_]+", "_", name.lower())
    module_rel = f"{PKG_REL}/{slug}.py"
    fqn = "infinigen." + module_rel[:-3].replace("/", ".") + f".{cls}"

    overlay = out_dir / "overlay"
    subprocess.run(["rm", "-rf", str(overlay)], check=True)
    overlay.mkdir(parents=True)
    # 跟 3_procedural_generator.build_overlay 同一招：cp -rs 的 symlink 鏡像。
    # ★不能只放我們這一個檔★ —— infinigen 是一般套件(有 __init__.py)，overlay/infinigen
    # 會整包遮蔽掉真的那個，於是 infinigen.core.* 全部 import 不到。
    subprocess.run(["cp", "-rs", str(CODEBASE.resolve()), str(overlay / "infinigen")],
                   check=True)

    pkg_dir = overlay / "infinigen" / PKG_REL
    pkg_dir.mkdir(parents=True, exist_ok=True)
    init = pkg_dir / "__init__.py"
    if init.is_symlink():
        init.unlink()
    init.write_text("")
    (pkg_dir / f"{slug}.py").write_text(code + FACTORY_TMPL.format(cls=cls, n_masters=n_masters))

    (overlay / "factory.txt").write_text(
        "# 對接腳本(param_adapter.py)產物 —— placer.py 階段 C 讀這份\n"
        f"fqn: {fqn}\n"
        f"factory: {cls}\n"
        f"entry: {module_rel}\n"
        "role: ENTRY\n"
        "kind: hero\n"
        + (f"anchoring: {anchoring}\n" if anchoring else "")
        + f"note: 參數腳本 {script.name} 包成 AssetFactory；幾何未修改。\n",
        encoding="utf-8")
    (overlay / "overlay_source.json").write_text(
        json.dumps({"base_pkg": str(CODEBASE)}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return {"overlay": str(overlay), "fqn": fqn, "class": cls, "module": module_rel}


PROBE = r"""
import sys, importlib
mod_name, cls_name = sys.argv[-2], sys.argv[-1]
mod = importlib.import_module(mod_name)
Cls = getattr(mod, cls_name)
assert mod.__file__.startswith(sys.argv[-3]), f"loaded from {mod.__file__}, not the overlay"
f = Cls(factory_seed=0)
a = f.spawn_asset(i=0)
b = f.spawn_asset(i=1)
import numpy as np


def dims(o):
    # root 常常是 Empty(dimensions 全 0)，幾何掛在 children 上 —— 量整個階層的世界座標
    # bounding box，否則會誤判成「兩個 seed 一樣大」。
    pts = []
    stack = [o]
    while stack:
        x = stack.pop()
        stack.extend(x.children)
        if x.type == "MESH" and len(x.data.vertices):
            pts += [x.matrix_world @ v.co for v in x.data.vertices]
    if not pts:
        return (0.0, 0.0, 0.0), 0
    arr = np.array([[p.x, p.y, p.z] for p in pts])
    return tuple(round(v, 4) for v in (arr.max(0) - arr.min(0))), len(pts)


da, na = dims(a)
db, nb = dims(b)
print("SPAWN_OK", f"i=0 {da} verts={na}", f"i=1 {db} verts={nb}",
      "DIFFER" if da != db else "IDENTICAL")
"""


def verify(overlay: Path, fqn: str, blender_python: str) -> dict:
    """真的 spawn 兩個 i 出來 —— import 得到不代表 spawn_asset 跑得起來。"""
    mod_name, cls_name = fqn.rsplit(".", 1)
    probe = overlay.parent / "_probe.py"
    probe.write_text(PROBE)
    overlay = overlay.resolve()          # 相對路徑會讓 probe 的來源檢查誤判
    cmd = [blender_python, str(probe), str(overlay), mod_name, cls_name]
    env = {"PYTHONPATH": f"{overlay}:{CODEBASE.parent}"}
    import os
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env=dict(os.environ, **env), timeout=900)
    out = r.stdout + "\n" + r.stderr
    (overlay.parent / "probe.log").write_text(out)
    ok = "SPAWN_OK" in r.stdout
    line = next((l for l in r.stdout.splitlines() if l.startswith("SPAWN_OK")), "")
    return {"ok": ok, "line": line,
            "error": None if ok else out.strip().splitlines()[-1] if out.strip() else "no output"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="參數腳本(round_xx.py)")
    ap.add_argument("--name", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--class-name", help="覆寫 Factory 類別名(冒充既有 factory 以替換場景中的 placeholder)")
    ap.add_argument("--anchoring", choices=sorted(ANCHORINGS),
                    help="這個資產怎麼靠在表面上(寫進 factory.txt，下游擺放時讀)")
    ap.add_argument("--n-masters", type=int, default=8,
                    help="母本池大小：只真的 build() N 次，其餘 linked duplicate(共用 mesh data)。"
                         "0=關掉，每個都真的建。腳本組的 build() 用 bpy.ops，成本隨場景增長，"
                         "這是目前唯一能把 populate 拉回可用速度的手段(2026-08-04)。")
    ap.add_argument("--verify", action="store_true", help="用 blender python 實際 spawn 兩個 i")
    ap.add_argument("--blender-python",
                    default=os.environ.get("BLENDER_PYTHON", sys.executable))
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = make_overlay(Path(a.script), a.name, out_dir, class_name=a.class_name,
                        n_masters=a.n_masters,
                        anchoring=a.anchoring)
    print(f"[adapter] overlay -> {info['overlay']}")
    print(f"[adapter] fqn     -> {info['fqn']}")
    if a.verify:
        v = verify(Path(info["overlay"]), info["fqn"], a.blender_python)
        print(f"[adapter] {'✅' if v['ok'] else '⛔'} {v['line'] or v['error']}")
        info["verify"] = v
    import json
    (out_dir / "adapter.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 1 if a.verify and not info["verify"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

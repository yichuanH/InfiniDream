"""通用母本池 —— 讓 populate 這條路也能重用資產。

★為什麼要自己做★(2026-08-05 查證官方原始碼)：
  ・`populate_collection` 對每個 target 直接呼叫 `factory.spawn_asset(i, placeholder=p)`
    (core/placement/placement.py)，而 `spawn_asset` 每次都進 `create_asset`
    (core/placement/factory.py)，中間★沒有任何快取★。唯一的分支是 `cache_system`
    → `link_fire`，那是預先模擬好的火焰資產，不是通用池。
  ・官方★有★母本池機制 —— `make_asset_collection(spawn_fns, n)` —— 但它服務的是
    ★scatter★(instance_scatter)以及資產內部的子部件(樹的 n_leaf / n_twig / n_branch，
    assets/objects/trees/generate.py:320/370/386)。「樹很快」是因為葉子被重用，
    ★不是整棵樹被重用★。
  ⇒ 所以 populate 這條路上，每個實例都是真的從頭建。BlenderGenerator 那條線由
    param_adapter 在產生的類別裡注入 n_masters 解決(實測摩托車 51:28 → 04:49)，
    但 ProceduralGenerator 改寫的是官方 factory、類別身上沒有那個屬性，
    「設個 8」無處可設。這支模組把同一套做法變成★跟 factory 無關★的包裝。

代價：同一個池裡的實例完全一樣(摩托車 20 台的頂點數是 9392/9512/9536 循環，
      週期正好等於 n_masters=3)。遠景可以，近景要調大 n 或關掉。
      ⚠️ 也因此★不要對官方 factory 無差別開啟★ —— 一片森林 231 棵樹只有 5 種，
        會毀掉 infinigen 本來的多樣性。用在「我們自己生成、而且很慢」的資產上。
"""
from __future__ import annotations

MASTER_STASH = "_bi_master_stash"


def effective_pool_size(requested, local_animation=None):
    """Return a safe pool size for one placement request.

    A linked hierarchy clone can remap object pointers, but Blender's animated
    armature bind/pose state is not safely transferable with ``Object.copy``.
    Building an animated rig directly is slower, but avoids both stretched
    geometry and silently lost local animation.

    ``gn_scene_time`` deliberately keeps the pool: it animates through the shared
    Geometry Nodes group, so every linked duplicate flaps from the one edit.
    """
    size = int(requested or 0)
    request = local_animation or {}
    if request.get("policy") == "inherit" and request.get("mode"):
        return 0
    return size


def _remap_attr(owner, attr, clones):
    """Point one Blender RNA object reference at its cloned counterpart."""
    try:
        source = getattr(owner, attr)
        replacement = clones[source]
    except (AttributeError, KeyError, TypeError):
        return False
    try:
        setattr(owner, attr, replacement)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _retarget_clone_references(clones):
    """Repair intra-hierarchy object pointers left behind by ``Object.copy``.

    Blender remaps ``parent`` only because :func:`_clone` sets it explicitly.
    Modifiers, constraints, pose constraints, and driver variables still point
    at the stashed master hierarchy.  For a rigged pooled asset that means the
    visible copied mesh is deformed by an armature in a different transform
    space, which stretches creatures into long columns.  Keep external targets
    unchanged, but remap every reference whose source is part of this clone.
    """
    repaired = 0
    pointer_attrs = (
        "object", "target", "pole_target", "space_object",
        "mirror_object", "offset_object", "origin",
        "start_cap", "end_cap", "auxiliary_target",
    )
    for cloned in clones.values():
        for modifier in getattr(cloned, "modifiers", ()):
            for attr in pointer_attrs:
                repaired += int(_remap_attr(modifier, attr, clones))

        for constraint in getattr(cloned, "constraints", ()):
            for attr in pointer_attrs:
                repaired += int(_remap_attr(constraint, attr, clones))

        pose = getattr(cloned, "pose", None)
        for bone in getattr(pose, "bones", ()) if pose is not None else ():
            for constraint in getattr(bone, "constraints", ()):
                for attr in pointer_attrs:
                    repaired += int(_remap_attr(constraint, attr, clones))

        animation = getattr(cloned, "animation_data", None)
        for fcurve in getattr(animation, "drivers", ()) if animation else ():
            driver = getattr(fcurve, "driver", None)
            for variable in getattr(driver, "variables", ()) if driver else ():
                for target in getattr(variable, "targets", ()):
                    repaired += int(_remap_attr(target, "id", clones))
    return repaired


def _reset_tf(root):
    """把 root 的變換歸單位並斷開 parent。

    ★為什麼一定要做★(2026-08-04 param_adapter 抓到的 bug)：spawn_asset 是用
      `butil.parent_to(obj, placeholder, no_inverse=True)` 掛上去的，no_inverse
      表示★不清掉 obj 原有的 local transform★，最終世界座標是
      `placeholder.matrix_world @ obj.matrix_basis`。create_asset 回傳的物件身上
      只要帶著任何變換，資產就會被推離 placeholder。
    """
    from mathutils import Matrix
    root.parent = None
    root.matrix_parent_inverse = Matrix.Identity(4)
    root.matrix_basis = Matrix.Identity(4)
    return root


def _stash(root):
    """母本整棵移進一個★不 link 到 scene★的 collection。

    母本一旦被交給 populate，就會被 parent 到 placeholder 上、再被沉降補正移動，
    之後複製它的個體全都會繼承那個位移。所以母本必須是「永遠不出場」的乾淨樣本。
    """
    import bpy
    c = bpy.data.collections.get(MASTER_STASH) or bpy.data.collections.new(MASTER_STASH)
    for o in [root] + list(root.children_recursive):
        for col in list(o.users_collection):
            col.objects.unlink(o)
        c.objects.link(o)
    return root


def _clone(src):
    """整棵階層複製，★共用 mesh data★(不 copy .data) —— 省時間也省顯存。

    Object.copy() 不會自動重接 ARMATURE modifier 等 object pointer；完整建完
    source→clone 對照後必須另做一次 remap，否則 copied mesh 仍由母本 rig 變形。
    """
    import bpy
    root = src.copy()
    bpy.context.scene.collection.objects.link(root)
    _reset_tf(root)                   # src.copy() 連 parent 和 local transform 一起帶
    clones = {src: root}
    stack = [(src, root)]
    while stack:
        s, d = stack.pop()
        for ch in s.children:
            c = ch.copy()
            bpy.context.scene.collection.objects.link(c)
            c.parent = d
            c.matrix_parent_inverse = ch.matrix_parent_inverse.copy()
            clones[ch] = c
            stack.append((ch, c))
    _retarget_clone_references(clones)
    return root


def pooled(cls, n_masters: int, verbose: bool = True, vary_seed: bool = False):
    """回傳一個「先建 n_masters 個母本、其餘 linked duplicate」的 factory 類別。

    n_masters <= 0 就原樣回傳(關掉池)。★類名與 __qualname__ 一律保持不變★ ——
    populate_all 用 `__name__` 跟 placeholder 配對(placement.py)，改名就完全找不到 box。
    """
    if n_masters is None or n_masters <= 0:
        return cls

    class Pooled(cls):
        _bi_masters = {}
        _bi_n = n_masters

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # 記下建構參數：母本 k>0 要用「同樣參數、不同 factory_seed」的兄弟實例來生
            self._bi_init_args = args[1:]            # args[0] 是 factory_seed
            self._bi_init_kwargs = dict(kwargs)
            self._bi_siblings = {}

        def _bi_master_factory(self, k):
            """★母本之間要有差異★(2026-09-13 使用者：皮艇顏色沒有變化)。

            很多生成資產在 __init__ 裡用 FixedSeed(factory_seed) 抽顏色/比例(皮艇 deck_hue)，
            原本 n 個母本都從同一個實例 create_asset(i) ⇒ ★n 個母本同一種顏色★(跟珊瑚一片藍同一個病)。
            母本 0 維持用自己(行為不變)；母本 k>0 用 factory_seed 錯開的兄弟實例。建不出來就退回自己。
            """
            # ★範圍收窄★(87 風險評估)：官方 factory 的 factory_seed 決定「物種」(同一群混成很多種會很雜、
            #   也跟 infinigen 原樣不同)；生物類的 factory_seed 決定整個 genome(比例差太多會跟擺放預留不合)。
            #   ⇒ 只有 placer 明確傳 vary_seed=True(生成的 overlay、非生物類)才錯開 seed。
            if k == 0 or not vary_seed:
                return self
            sib = self._bi_siblings.get(k)
            if sib is not None:
                return sib
            base = getattr(self, "factory_seed", None)
            seed = (int(base or 0) * 1000003 + k * 7919) % (2 ** 31)
            try:
                kwargs = dict(self._bi_init_kwargs)
                kwargs.pop("factory_seed", None)
                sib = cls(seed, *self._bi_init_args, **kwargs)
            except Exception as error:               # 建不出來就維持舊行為，不要弄壞這個 case
                print(f"[pool] ⚠️ {cls.__name__}: 母本 {k} 換 seed 建構失敗，沿用同一個實例: "
                      f"{str(error)[:80]}", flush=True)
                sib = self
            self._bi_siblings[k] = sib
            return sib

        def create_asset(self, i=0, placeholder=None, **params):
            import bpy
            n = type(self)._bi_n
            if n <= 0:
                return super().create_asset(i=i, placeholder=placeholder, **params)

            key = (cls.__name__, getattr(self, "factory_seed", None), i % n)
            m = type(self)._bi_masters.get(key)
            if m is not None and m.name not in bpy.data.objects:
                m = None                        # 上一份 blend 的殘留，重建
            if m is None:
                maker = self._bi_master_factory(i % n)
                if maker is self:
                    obj = super().create_asset(i=i % n, placeholder=placeholder, **params)
                else:
                    obj = cls.create_asset(maker, i=i % n, placeholder=placeholder, **params)
                # ★就地改 placeholder 的 factory 不能池化★：那種 factory 回傳的就是
                #   placeholder 本人(populate_collection 用 `if p is not obj` 判斷)，
                #   把它 stash 起來等於把場上的 box 搬走。碰到就整個關掉，不要硬做。
                if obj is placeholder:
                    type(self)._bi_n = 0
                    print(f"[pool] ⚠️ {cls.__name__} 的 create_asset 回傳 placeholder 本人 "
                          f"⇒ ★關閉母本池★，改回逐個生成", flush=True)
                    return obj
                _stash(_reset_tf(obj))
                type(self)._bi_masters[key] = obj
                m = obj
                if verbose:
                    print(f"[pool] {cls.__name__}: 建立母本 {i % n + 1}/{n}"
                          f"(factory_seed={getattr(maker, 'factory_seed', None)})", flush=True)
            # ★母本本人絕不回傳★ —— 連第一次也走複製，否則母本會被 populate 動到
            return _clone(m)

    Pooled.__name__ = cls.__name__
    Pooled.__qualname__ = getattr(cls, "__qualname__", cls.__name__)
    Pooled.__module__ = cls.__module__
    return Pooled

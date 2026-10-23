"""Pre-render pass: make rigged assets actually deform in the final render.

★為什麼需要這一支★(2026-08-10)
Infinigen 把 creature 母本放在 `assets:<Factory>(<seed>)` 且 hide_render=True，
另外把★只有 MESH★再 link 一份到 `particleassets:...children`(hide_render=False)
給粒子系統當實例來源。ARMATURE 物件★只留在 render 隱藏的那個 collection★。

⇒ 渲染時 mesh 經由 particleassets 正常實例化出來(看得到魚)，但它身上 ARMATURE
   modifier 指向的 armature ★不在 render depsgraph 裡★ ⇒ 身體永遠 rest pose。
   結果就是「魚會被 BOIDS 推著位移，但不會擺尾」。

★這個失效是靜默的★ —— driver 都在、log 全綠、資產層的 animatability 掃描也判 CLEAN，
只有最終畫面是死的。它發生在★部署階段★，不是資產階段。

armature 本身不產生任何像素，所以讓它 render-visible 是零成本的。

⚠️ 判斷「看不看得見」不能只看物件自己的 hide_render —— collection 是樹狀的，
   任何一層 hide_render 都會擋掉，所以要往上走到 scene collection。
"""

import os

import bpy

HOST_COLLECTION = "animation_rig_visibility"
# 設 BI_SKIP_ANIM_VIS=1 可以停用這個 pass —— 用來做「修 vs 不修」的對照渲染，
# 相機與所有其他參數完全相同，唯一變因就是這一支。
SKIP_ENV = "BI_SKIP_ANIM_VIS"


def _collection_render_visible(coll, cache, parents_of):
    if coll.name in cache:
        return cache[coll.name]
    cache[coll.name] = False                      # cycle guard
    if coll.hide_render:
        return False
    root = bpy.context.scene.collection
    if coll == root:
        cache[coll.name] = True
        return True
    ok = any(_collection_render_visible(p, cache, parents_of)
             for p in parents_of.get(coll.name, ()))
    cache[coll.name] = ok
    return ok


def repair_armature_visibility(dry_run=False, verbose=True):
    """Ensure every render-visible mesh's ARMATURE target is render-visible too.

    Returns (repaired, already_ok) as lists of (mesh_name, armature_name).
    """
    if os.environ.get(SKIP_ENV) == "1":
        if verbose:
            print(f"[anim-vis] disabled via {SKIP_ENV}=1 — rigged bodies will "
                  f"render in rest pose", flush=True)
        return [], []
    root = bpy.context.scene.collection
    parents_of = {}
    for c in list(bpy.data.collections) + [root]:
        for ch in c.children:
            parents_of.setdefault(ch.name, []).append(c)

    cache = {}

    def visible(obj):
        if obj.hide_render:
            return False
        return any(_collection_render_visible(c, cache, parents_of)
                   for c in obj.users_collection)

    host = bpy.data.collections.get(HOST_COLLECTION)
    if host is None and not dry_run:
        host = bpy.data.collections.new(HOST_COLLECTION)
        root.children.link(host)

    repaired, already = [], []
    for m in bpy.data.objects:
        if m.type != "MESH" or not visible(m):
            continue
        for mod in m.modifiers:
            if mod.type != "ARMATURE" or mod.object is None:
                continue
            a = mod.object
            if visible(a):
                already.append((m.name, a.name))
                continue
            repaired.append((m.name, a.name))
            if dry_run:
                continue
            a.hide_render = False
            a.hide_viewport = False
            if host.name not in {c.name for c in a.users_collection}:
                host.objects.link(a)
            cache.clear()                          # visibility changed

    if verbose:
        print(f"[anim-vis] armature links already fine: {len(already)}", flush=True)
        print(f"[anim-vis] armature links repaired:     {len(repaired)}"
              f"{' (dry run)' if dry_run else ''}", flush=True)
        for mesh, arma in repaired[:8]:
            print(f"[anim-vis]   {arma} -> drives {mesh}", flush=True)
    return repaired, already

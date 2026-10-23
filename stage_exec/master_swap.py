"""★swarm 的母本替換★ —— 換掉粒子系統的 instance_collection(2026-08-06)

【為什麼需要這條路】
  swarm(FishSchoolFactory / AntSwarmFactory，全 repo 只有這兩個 BoidSwarmFactory 子類)
  在 ★coarse 階段就 populate 完了★：placeholder 被消耗成 0 個，unique_assets 底下是
  一個掛著 particle system 的 emitter。沒有 box 可長 ⇒ hero 的 replace/add 都不適用
  (populate_all 也會跳過已實現的 collection)。

【手法】跟★金色蘑菇★同構(apply_plan.py:588 換 GN 的 COLLECTION_INFO socket)，
  只是屬性換成 particle_system.settings.instance_collection：

     改前  instance_collection ──▶ particleassets:FishSchoolFactory(2816589).children  (3 條官方魚)
     改後  instance_collection ──▶ particleassets:<新名字>                              (3 條生成的魚)

  ★472 條魚不是 472 個物件★ —— 是 3 條母本被粒子系統 instance 出來的。
  所以要換整群外觀只需要建 3 條的幾何，而粒子位置/boid 模擬完全不碰。

【2026-08-06 探測到的事實(scratchpad/probe_swarm.py，唯讀)】
  ・emitter 就在 unique_assets:<Factory>(seed) 裡，名字是 <Factory>(seed).spawn_asset(0)
  ・instance_collection 可寫、render_type=COLLECTION、各 3 個母本
  ・bake ★存在 .blend 內部★(use_disk_cache=False / use_external=False / is_outdated=False)
  ・⚠️ ★背景模式下 len(ps.particles) 恆為 0★(evaluated / frame_set 都試過) ⇒
    「逐粒子比對位置」這個驗證★做不到★，改用 is_outdated + settings 全屬性比對。
"""

import bpy


def _settings_snapshot(s):
    """particle settings 的全部可讀屬性 -> {name: repr}。用來證明我們只動了一個欄位。"""
    out = {}
    for p in s.bl_rna.properties:
        if p.identifier in ("rna_type",):
            continue
        try:
            v = getattr(s, p.identifier)
        except Exception:
            continue
        try:
            out[p.identifier] = repr(v)
        except Exception:
            out[p.identifier] = "<unrepr>"
    return out


def find_emitter(collection_name):
    """從 manifest 的 collection 名字找到帶粒子系統的 emitter 物件。"""
    col = bpy.data.collections.get(collection_name)
    if col is None:
        return None, f"找不到 collection {collection_name!r}"
    hits = [o for o in col.objects
            if getattr(o, "particle_systems", None) and len(o.particle_systems)]
    if not hits:
        return None, f"{collection_name} 裡沒有帶 particle system 的物件"
    if len(hits) > 1:
        return None, f"{collection_name} 裡有 {len(hits)} 個 emitter，不敢猜"
    return hits[0], None


def swap_masters(collection_name, master_collection_name, FacClass,
                 n_masters=None, dry_run=False, log=print):
    """把 swarm 的母本換成 FacClass 生的。回傳 report dict(feasible/err/...)。

    ⚠️ ★前提要 assert 不要猜★ —— 0805 十四節說「動手前先驗」，這裡把它寫成程式碼：
       instance_collection 必須真的指向 manifest 上那個 collection，且 render_type
       必須是 COLLECTION。不符就不動，回報原因。
    """
    from infinigen.core.placement.factory import make_asset_collection
    from infinigen.assets.objects.creatures.util.boid_swarm import (
        creature_col_to_particle_col,
    )

    rep = {"collection": collection_name, "feasible": True}
    emitter, err = find_emitter(collection_name)
    if emitter is None:
        return {**rep, "feasible": False, "err": err}
    rep["emitter"] = emitter.name

    ps = emitter.particle_systems[0]
    s = ps.settings
    old = s.instance_collection

    # ── 前提檢查 ──────────────────────────────────────────────────────────
    if s.render_type != "COLLECTION":
        return {**rep, "feasible": False,
                "err": f"render_type={s.render_type}，不是 COLLECTION ⇒ 不是 instance 母本的形式"}
    if old is None:
        return {**rep, "feasible": False, "err": "instance_collection 是 None"}
    if master_collection_name and old.name != master_collection_name:
        return {**rep, "feasible": False,
                "err": f"instance_collection 指向 {old.name!r}，"
                       f"manifest 說是 {master_collection_name!r} —— 兩邊不一致，不敢動"}
    rep.update(old_collection=old.name, old_n_masters=len(old.objects),
               particle_count=s.count)

    n = int(n_masters) if n_masters else len(old.objects)
    rep["n_masters"] = n
    if dry_run:
        return {**rep, "dry_run": True}

    # ── 換之前先存證 ──────────────────────────────────────────────────────
    before = _settings_snapshot(s)
    pc = ps.point_cache
    rep["cache_before"] = {"is_baked": pc.is_baked, "is_outdated": pc.is_outdated,
                           "frames": [pc.frame_start, pc.frame_end]}

    # ── 生新母本 ──────────────────────────────────────────────────────────
    #   ★這一步是真的建模★ —— 呼叫 overlay 的 factory 跑 n 次長出 n 份幾何。
    #   便宜的原因是 n=3，不是 472：那 472 份從來不是物件，是 render 時的 instance。
    log(f"[swap] 生新母本 {n} 個 ({FacClass.__name__}) …")
    raw = make_asset_collection(FacClass(0), n=n, name=f"swapped:{collection_name}",
                               verbose=False)

    # ★預處理直接用官方函式★ —— 它做三件事：每個 root 取第一個 MESH、
    #   root.location.z -= 100(母本不能入鏡)、group 起來設 hide_viewport=True /
    #   hide_render=False。自己重寫遲早漏一步，然後巨大的母本會浮在場景某處被拍到。
    new_col = creature_col_to_particle_col(raw, name=f"swapped:{collection_name}")
    rep["new_collection"] = new_col.name
    rep["new_n_masters"] = len(new_col.objects)

    # ── ★換指向★(整個手法就這一行) ────────────────────────────────────────
    s.instance_collection = new_col

    # ── 驗證 ──────────────────────────────────────────────────────────────
    after = _settings_snapshot(s)
    changed = sorted(k for k in before
                     if k in after and before[k] != after[k])
    rep["settings_changed"] = changed
    rep["cache_after"] = {"is_baked": pc.is_baked, "is_outdated": pc.is_outdated,
                          "frames": [pc.frame_start, pc.frame_end]}
    # 只有 instance_collection 可以變(material_slot 是它的★衍生欄位★ —— 那個 enum 的
    # 選項來自被參照的 collection，換 collection 它必然跟著變，不是我們另外動的)。
    rep["only_instance_changed"] = (
        [c for c in changed if c not in ("instance_collection", "material_slot")] == [])
    # 模擬有沒有被判定失效 —— 背景模式讀不到粒子，這是唯一可得的直接訊號
    rep["cache_ok"] = bool(pc.is_baked and not pc.is_outdated)
    # 母本不能入鏡：z 應該被推到 -100 附近(creature_col_to_particle_col 做的)
    #   ⚠️ ★不可以用 matrix_world 量★ —— 2026-08-06 實測它讀到 +0.0，而權威路徑與
    #      實際頂點座標都是 -100.4~-99.6，害我一度以為 z-=100 沒生效。
    #      這是本專案第四次被 matrix_world 的過期快取騙(見 placement_modes.world_points)。
    import placement_modes as pm
    zs = []
    for o in new_col.objects:
        P = pm.world_points([o])                      # recompute=True，自己乘矩陣
        if P is not None:
            zs.append(float(P[:, 2].max()))
    rep["new_master_z_top"] = [round(z, 1) for z in zs]
    rep["masters_hidden"] = bool(zs and max(zs) < -50)
    rep["masters_flags"] = {"hide_viewport": new_col.hide_viewport,
                            "hide_render": new_col.hide_render}
    return rep


def _cache_frames(point_cache) -> int:
    """point_cache.info 形如 '121 frames in memory (3.4 MiB).' ⇒ 121；讀不到回 0。"""
    import re as _re
    match = _re.match(r"\s*(\d+)\s+frames?", str(getattr(point_cache, "info", "") or ""))
    return int(match.group(1)) if match else 0


def _layer_collection_chain(layer_collection, name, path=()):
    if layer_collection.collection.name == name:
        return list(path) + [layer_collection]
    for child in layer_collection.children:
        found = _layer_collection_chain(child, name, tuple(path) + (layer_collection,))
        if found:
            return found
    return None


def extend_particle_lifetimes(scene, *, log=print) -> list:
    """把粒子系統的壽命/快取拉到整段影片(2026-09-13)。

    ★為什麼需要★：infinigen 自己的影片是 72 幀，所以 fish_school / bug_swarm 的
    settings.lifetime=72、point_cache=1..73。我們的影片是 120 幀 ⇒ 第 73 幀之後魚全部消失。

    ★怎麼烤才烤得出東西★(17:50 實測，踩過三次 0 幀)：
      infinigen populate 完會把發射器所在的 `unique_assets:X` 和★上一層 `unique_assets`★都設成
      hide_viewport=True ⇒ 發射器不被評估(matrix_world=0) ⇒ 任何方式重烤都得到 '0 frames in memory'，
      而且 free_bake 已經把原本 73 幀的好快取清掉(報告卻寫 baked=True)。
      正確做法 = 打開★整條★ layer collection 祖先鏈 → 確認 visible_get() → 照 infinigen
      core/placement/particles.bake(真的選取 + active、frame_end+1、override scene/active_object/point_cache)
      → 還原可見性。s1 實測：121 幀、第 1 幀位置與原快取完全一致、第 110 幀全員還在。
    ★保險★：發射器沒有真的變可見就不動它(不 free_bake，保住原快取)；烤完幀數不夠就回報 ok=False。
    """
    import bpy
    import infinigen.core.util.blender as butil

    changed = []
    end = int(scene.frame_end)
    view_layer = bpy.context.view_layer
    for obj in list(scene.objects):
        for ps in getattr(obj, "particle_systems", []):
            settings, cache = ps.settings, ps.point_cache
            need_life = float(settings.lifetime) < end
            need_cache = int(cache.frame_end) < end
            if not (need_life or need_cache):
                continue
            row = {"object": obj.name, "system": ps.name,
                   "lifetime_before": float(settings.lifetime),
                   "cache_before": [int(cache.frame_start), int(cache.frame_end)],
                   "frames_before": _cache_frames(cache)}
            restore = [("object", obj, obj.hide_viewport)]
            try:
                obj.hide_viewport = False
                for coll in obj.users_collection:
                    for lc in _layer_collection_chain(view_layer.layer_collection, coll.name) or []:
                        restore.append(("layer", lc, (lc.collection.hide_viewport, lc.exclude,
                                                      lc.hide_viewport)))
                        lc.collection.hide_viewport = False
                        lc.exclude = False
                        lc.hide_viewport = False
                view_layer.update()
                if not obj.visible_get():
                    row.update(ok=False, skipped=True,
                               error="emitter still not visible after unhiding the collection chain "
                                     "— keep original cache untouched")
                    log(f"[particles] ⚠️ {obj.name[:40]}: 打開 collection 鏈後仍不可見 ⇒ 不重烤、保住原快取")
                    changed.append(row)
                    continue
                if need_life:
                    settings.lifetime = end + 10       # 多給 10 幀，尾巴不要剛好死掉
                with butil.SelectObjects(obj):
                    override = {"scene": scene, "active_object": obj, "point_cache": cache}
                    with bpy.context.temp_override(**override):
                        if cache.is_baked:
                            bpy.ops.ptcache.free_bake()
                    scene.frame_end = end + 1          # 照 infinigen：烤到 frame_end+1
                    try:
                        with bpy.context.temp_override(**override):
                            bpy.ops.ptcache.bake(bake=True)
                    finally:
                        scene.frame_end = end
                frames = _cache_frames(cache)
                ok = bool(cache.is_baked and frames >= end)
                row.update(lifetime_after=float(settings.lifetime),
                           cache_after=[int(cache.frame_start), int(cache.frame_end)],
                           frames_after=frames, baked=bool(cache.is_baked), ok=ok)
                mark = "🐟" if ok else "⛔"
                log(f"[particles] {mark} {obj.name[:40]}: 壽命 {row['lifetime_before']:.0f}→"
                    f"{settings.lifetime:.0f} 幀、快取 {row['frames_before']}→{frames} 幀"
                    f"{'' if ok else '(幀數不夠 ⇒ 這群魚後段會不見)'}")
            except Exception as error:      # 烤不動就回報，不要弄壞這個 case
                row.update(ok=False, error=str(error))
                log(f"[particles] ⚠️ {obj.name[:40]} 重烤失敗: {str(error)[:90]}")
            finally:
                for kind, target, state in reversed(restore):
                    if kind == "object":
                        target.hide_viewport = state
                    else:
                        target.collection.hide_viewport, target.exclude, target.hide_viewport = state
                view_layer.update()
            changed.append(row)
    return changed

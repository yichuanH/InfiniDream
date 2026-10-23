#!/usr/bin/env python
"""逐資產釘選(BI_ASSET_SCALE / BI_ASSET_MOTION)的共用入口 —— 2026-09-13。

★為什麼要獨立一支★：釘選必須在★擺放之前★套上去，不然畫面大小沒變、而且擺放沒有
為放大後的箱子預留空間(6m 的條紋獏用 2.4m 的框去找位置 ⇒ 跟旁邊的東西疊)。
第 8 步的順序是 simple_volume_placement → apply_plan，兩支都會呼叫這裡；
用 edit 上的 `bi_asset_scale_applied` 當記號，重複呼叫不會重複乘。

★位移釘選也一樣★：擺放要照「會走動」預留掃過範圍(_ground_motion_clearance)，
釘選若等到 apply_plan 才套，擺放時看到的還是 preset=none ⇒ 10 隻紅鶴照 1.2m 排、走起來就互撞。

格式：BI_ASSET_SCALE='{"striped_okapi":2.5,"white_calla_lily":0.75}'
      BI_ASSET_MOTION='{"golden_pangolin":"ground_forward","flamingo":{"preset":"ground_wander","intensity":"subtle"}}'
"""

from __future__ import annotations

import json
import os

MARKER = "bi_asset_scale_applied"
MOTION_MARKER = "bi_asset_motion_applied"
MASTERS_MARKER = "bi_asset_masters_applied"


def pins() -> dict:
    raw = (os.environ.get("BI_ASSET_SCALE") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"[pins] ⚠️ BI_ASSET_SCALE 不是合法 JSON，忽略: {raw[:80]}", flush=True)
        return {}
    out = {}
    for key, value in (data or {}).items():
        try:
            factor = float(value)
        except (TypeError, ValueError):
            continue
        if factor > 0 and abs(factor - 1.0) > 1e-6:
            out[str(key)] = factor
    return out


def factor_for(scales: dict, *names) -> float:
    for name in names:
        if name and str(name) in scales:
            return scales[str(name)]
    return 1.0


def apply_to_hero_edits(hero_edits, *, log=print) -> dict:
    """把釘選乘到 hero edit 的 proxy_dimensions_m 上(冪等)。回傳實際套用的 {id: 倍率}。"""
    scales = pins()
    if not scales:
        return {}
    applied = {}
    for edit in hero_edits or []:
        key = next((str(k) for k in (edit.get("code_from"), edit.get("target_factory"),
                                     edit.get("factory")) if k and str(k) in scales), None)
        if not key:
            continue
        factor = scales[key]
        if edit.get(MARKER):
            continue                      # 這一筆已經乘過了(另一支腳本做的)
        spec = edit.get("placement_spec")
        if not isinstance(spec, dict):
            log(f"[pins]   ⚠️ {key}: 這筆 edit 沒有 placement_spec ⇒ 尺寸釘選 ×{factor} ★沒有套用★")
            continue
        dims = spec.get("proxy_dimensions_m")
        if not (isinstance(dims, list) and len(dims) == 3):
            # ★director 沒宣告尺寸時也要能釘★(2026-09-15)：原本這裡直接 continue，
            #   而且★不出聲★ —— cave 的螢火蟲就是這樣：BI_ASSET_SCALE={"firefly":4.0} 有傳進去，
            #   proxy 卻還是 0.5m、擺放結果跟沒釘一模一樣(6/14 隻、框外 6)，查了三輪才發現。
            #   沒宣告時 simple_volume_placement 會退到 placement_contract.inferred_proxy_dimensions()
            #   (再不行才 proxy_half_extents)，所以這裡就拿同一個推斷值乘上倍率寫回去，
            #   兩邊看到的尺寸才一致。推不出來就★大聲講★，不要再靜默失效。
            inferred = None
            try:
                import placement_contract as plc
                inferred = plc.inferred_proxy_dimensions(
                    factory=edit.get("factory"), code_from=edit.get("code_from"),
                    anchoring=edit.get("anchoring"),
                    semantic_target=spec.get("semantic_target"))
            except Exception as error:                         # noqa: BLE001
                log(f"[pins]   ⚠️ {key}: 推斷 proxy 尺寸失敗({error})")
            if not (isinstance(inferred, (list, tuple)) and len(inferred) == 3):
                log(f"[pins]   ⚠️ {key}: director 沒宣告 proxy_dimensions_m、也推不出預設尺寸 "
                    f"⇒ 尺寸釘選 ×{factor} ★沒有套用★(擺放會用 factory 的預設框)")
                continue
            dims = [float(d) for d in inferred]
            log(f"[pins]   📐 {key}: director 沒宣告尺寸，用推斷值 "
                f"{[round(d, 3) for d in dims]} 當基準")
        spec["proxy_dimensions_m"] = [float(d) * factor for d in dims]
        edit[MARKER] = factor
        applied[key] = factor
        log(f"[pins]   📏 {key}: proxy {[round(float(d), 3) for d in dims]} ×{factor} ⇒ "
            f"{[round(d, 3) for d in spec['proxy_dimensions_m']]}")
    return applied


def motion_pins() -> dict:
    raw = (os.environ.get("BI_ASSET_MOTION") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"[pins] ⚠️ BI_ASSET_MOTION 不是合法 JSON，忽略: {raw[:80]}", flush=True)
        return {}
    return {str(k): v for k, v in (data or {}).items() if v}


def apply_motions_to_hero_edits(hero_edits, *, log=print) -> dict:
    """把位移釘選寫進 hero edit 的 world_motion(冪等)。回傳 {id: preset}。

    地面位移只支援 anchoring=conform，director 給 upright/tilted 的(孔雀/紅鶴)一併改掉；
    anchoring 真的不合的就跳過並印警告，不讓整個 case 死掉。
    """
    wanted = motion_pins()
    if not wanted:
        return {}
    import world_motion as wm
    applied = {}
    for edit in hero_edits or []:
        key = next((str(k) for k in (edit.get("code_from"), edit.get("target_factory"),
                                     edit.get("factory")) if k and str(k) in wanted), None)
        if not key or edit.get(MOTION_MARKER):
            continue
        value = wanted[key]
        preset = str(value if isinstance(value, str)
                     else (value or {}).get("preset") or "none")
        intensity = str("normal" if isinstance(value, str)
                        else (value or {}).get("intensity") or "normal")
        anchoring = str(edit.get("anchoring") or "")
        if preset in wm.GROUND_PRESETS and anchoring in ("upright", "tilted"):
            log(f"[pins]   🌀 {key}: anchoring {anchoring} ⇒ conform(地面位移需要)")
            edit["anchoring"] = anchoring = "conform"
        # 想讓地上的東西改成在水裡/空中飄(使用者：螃蟹在後面飄，不要在地上看不到)
        wanted_anchor = {"submerged": {"aquatic_bob", "aquatic_swim", "aquatic_hover",
                                       "aquatic_wander"},
                         "airborne": {"air_glide", "air_hover", "airborne_wander",
                                      "airborne_glide", "stationary_bob"}}
        for target, presets in wanted_anchor.items():
            if preset in presets and anchoring != target:
                log(f"[pins]   🌀 {key}: anchoring {anchoring} ⇒ {target}({preset} 需要)")
                edit["anchoring"] = anchoring = target
        allowed = wm.PRESET_ANCHORINGS.get(preset)
        if preset != "none" and allowed and anchoring not in allowed:
            log(f"[pins]   ⚠️ {key}: {preset} 不支援 anchoring={anchoring}"
                f"(只支援 {sorted(allowed)}) ⇒ 不改")
            continue
        params = dict((edit.get("world_motion") or {}).get("params") or {})
        if isinstance(value, dict) and isinstance(value.get("params"), dict):
            params.update(value["params"])        # 例如 {"speed_scale": 0.3}(使用者：紅鶴放慢 0.3)
        edit["world_motion"] = {"preset": preset, "intensity": intensity, "params": params}
        edit[MOTION_MARKER] = preset
        applied[key] = preset
        log(f"[pins]   🌀 {key}: world_motion ⇒ {preset}/{intensity}")
    return applied


FLYING_NORMALIZED_MARKER = "bi_flying_motion_normalized"
# 鳥：快速滑過去(air_soar)；昆蟲：往前飄(air_glide)。都不要原地上下浮。
SOAR_FACTORIES = {"FlyingBirdFactory", "BirdFactory"}
GLIDE_FACTORIES = {"DragonflyFactory"}


def normalize_flying_motions(hero_edits, *, log=print) -> dict:
    """★會飛的不要原地上下浮★ —— 2026-09-13 從 placer 移到這裡(第 8 步、apply_plan 打 stamp 之前)。

    原本 placer 在讀 manifest 之後才把 air_hover 改成 air_glide/air_soar，但 apply_plan 已經照 director
    原本的 preset 打了 bi_world_motion_* stamp ⇒ placer 用改過的 preset 找 anchor 一隻都對不上
    ⇒ forest 的 luna moth、plain_sunflower 的 bee 整段沒位移(87 抓到)。
    放在這裡：manifest、stamp、擺放 envelope、placer、validate_final_scene 看到的都是同一個 preset。
    使用者用 BI_ASSET_MOTION 明確釘選過的 edit 不動(MOTION_MARKER)。冪等。
    """
    changed = {}
    for edit in hero_edits or []:
        if edit.get(FLYING_NORMALIZED_MARKER) or edit.get(MOTION_MARKER):
            continue
        if str(edit.get("anchoring") or "") != "airborne":
            continue
        factory = str(edit.get("factory") or "")
        wm_req = dict(edit.get("world_motion") or {})
        preset = str(wm_req.get("preset") or "none")
        target = None
        if factory in SOAR_FACTORIES and preset in ("air_hover", "air_glide"):
            target = "air_soar"
        elif factory in GLIDE_FACTORIES and preset == "air_hover":
            target = "air_glide"
        if not target:
            continue
        edit["world_motion"] = {"preset": target, "intensity": "normal",
                                "params": wm_req.get("params") or {}}
        edit[FLYING_NORMALIZED_MARKER] = f"{preset}->{target}"
        key = str(edit.get("code_from") or factory)
        changed[key] = f"{preset}->{target}"
        log(f"[pins]   {'🦅' if target == 'air_soar' else '🦋'} {key}({factory}): {preset} ⇒ {target}/normal"
            f"(會飛的不要原地浮)")
    return changed


def apply_all(hero_edits, *, log=print) -> dict:
    """尺寸 + 位移 + 會飛的 preset 正規化，兩支腳本都呼叫這個(冪等)。"""
    return {"scales": apply_to_hero_edits(hero_edits, log=log),
            "motions": apply_motions_to_hero_edits(hero_edits, log=log),
            "flying": normalize_flying_motions(hero_edits, log=log)}


def masters_pins() -> dict:
    raw = (os.environ.get("BI_ASSET_MASTERS") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"[pins] ⚠️ BI_ASSET_MASTERS 不是合法 JSON，忽略: {raw[:80]}", flush=True)
        return {}
    out = {}
    for key, value in (data or {}).items():
        try:
            out[str(key)] = max(1, int(value))
        except (TypeError, ValueError):
            continue
    return out


def apply_masters_to_edits(*edit_lists, log=print) -> dict:
    """釘母本池大小(使用者 2026-09-13：珊瑚母本可以放 10 個嗎，多一點)。

    母本數 = 這個撒點系統會生出幾種不同的個體。director 通常給 5~6，同一種珊瑚就只有
    6 個造型/配色。這裡讓場景設定檔可以直接指定。hero/extra/official_scatter/swarm 都吃。
    """
    wanted = masters_pins()
    if not wanted:
        return {}
    applied = {}
    for edits in edit_lists:
        for edit in edits or []:
            key = next((str(k) for k in (edit.get("code_from"), edit.get("target_factory"),
                                         edit.get("target"), edit.get("factory"))
                        if k and str(k) in wanted), None)
            if not key or edit.get(MASTERS_MARKER):
                continue
            before = edit.get("n_masters")
            edit["n_masters"] = wanted[key]
            edit[MASTERS_MARKER] = wanted[key]
            applied[key] = wanted[key]
            log(f"[pins]   🎨 {key}: 母本池 {before} ⇒ {wanted[key]}")
    return applied


DENSITY_MARKER = "bi_asset_density_applied"


def density_pins() -> dict:
    raw = (os.environ.get("BI_ASSET_DENSITY") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"[pins] ⚠️ BI_ASSET_DENSITY 不是合法 JSON，忽略: {raw[:80]}", flush=True)
        return {}
    return {str(k): str(v) for k, v in (data or {}).items() if v}


def apply_density_to_edits(*edit_lists, log=print) -> dict:
    """釘撒點密度等級(使用者 2026-09-13：forest_seed2 蘑菇要 very dense)。冪等。"""
    wanted = density_pins()
    if not wanted:
        return {}
    applied = {}
    for edits in edit_lists:
        for edit in edits or []:
            key = next((str(k) for k in (edit.get("code_from"), edit.get("target"))
                        if k and str(k) in wanted), None)
            if not key or edit.get(DENSITY_MARKER):
                continue
            before = edit.get("density_level")
            edit["density_level"] = wanted[key]
            edit[DENSITY_MARKER] = wanted[key]
            applied[key] = wanted[key]
            log(f"[pins]   🌱 {key}: 密度等級 {before} ⇒ {wanted[key]}")
    return applied

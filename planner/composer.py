#!/usr/bin/env python
"""Composer Agent —— pipeline 第 5 步：★文字 plan → 可執行的 JSON★。

★兩層 agent 的第二層(0729 3-B 定案)★
    planner   prompt + 場景清單 → 文字 plan   ← 審美判斷，沒有機械驗證
    ★本支★   文字 plan → JSON                ← ★有 --dry-run 免費驗證★
  ⇒ 格式錯只要重跑這一層，不必重跑整個審美推理。

════════════════════════════════════════════════════════════════════════
★★輸出的 JSON 有兩個檔，因為執行層本來就分兩階段★★
════════════════════════════════════════════════════════════════════════
  ★edit_plan.json★ → apply_plan.py(階段 A，改 coarse blend)
      {"hero_edits":  [{"op":"delete|scale|move|add|attrs", ...}],
       "extra_edits": [{"op":"replace|delete|density|scale|create", "target":..., ...}]}
      ⇒ ★HERO 動 box 的只有四種 op，沒有 replace★
      ⇒ ★op="attrs" 不動 box★：只把 replace 的 anchoring/n_masters 帶給 placer。
        每個 appearance.placements 都要有一筆對應的 attrs，check() 會擋。
        (2026-08-06 補：先前這兩個值寫在 appearance.json 而★沒有程式在讀★，
         於是「換上去之後怎麼擺」在 director → apply_plan 之間靜靜掉了。)
      ⇒ ★EXTRA 全部在這裡完成，改完直接 render，~30 秒不用 populate★

  ★appearance.json★ → placer.py(階段 C，populate 時換 HERO 外觀)
      {"placements": [{"collection":"placeholders:TreeFactory(7836289)",
                       "factory":"TreeFactory", "seed":7836289,
                       "code":"generated/<slug>", "look":"..."}]}
      ⇒ ★HERO 的外觀替換是 per-collection，走 manifest 的 code 欄★
        (2026-07-29 實測：populate_all 用類名過濾做不到，改用 populate_collection)
      ⇒ code 欄由階段 B(asset generator)產出後填入；本支先寫 look + 佔位路徑。

⚠️ ★為什麼 HERO 的外觀不能寫進 hero_edits★
   apply_plan.py 的 HERO_OPS = {delete, scale, move, add}，★沒有 replace★，
   而且它用 colls_for(factory) ★只認 factory 名★，無法指定單一 collection。
   ⇒ 硬塞會被 validate() 擋下，或誤改到同名的另一個 collection。

用法:
  python composer.py --plan <adhoc.json> --manifest <scene_manifest.json> --out-dir <dir>
  python composer.py --case candy_forest        # 直接吃 out_e2e/<case>/
  python composer.py --all-cases
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OPUS_MODEL = "claude-opus-4-8"
COMPOSER_MAX_TOKENS = int(os.environ.get("COMPOSER_MAX_TOKENS", "12000"))
sys.path.insert(0, str(ROOT))
import world_motion as wm  # noqa: E402
import local_animation as la  # noqa: E402
import placement_contract as plc  # noqa: E402
import placement_modes as pm  # noqa: E402
from placement_region_contract import is_map_managed_anchoring  # noqa: E402
import scatter_catalog as _sc  # noqa: E402

HERO_OPS = ["delete", "scale", "move", "add"]
# ★不動 box、只帶擺放屬性★(2026-08-06) —— hero 的 replace 不是階段 A 的動作，
#   但它的 anchoring/n_masters 必須傳到 placer，而 appearance.json 沒有程式在讀。
HERO_ATTR_OPS = ["attrs"]
# 貼地屬性的唯一定義在 BeyondInfinigen/placement_modes.py；不要在這裡複製值域。
ANCHORINGS = list(pm.ANCHORINGS)
EXTRA_OPS = ["replace", "delete", "density", "scale", "create"]   # apply_plan.py:81
# ★2026-09-07★：原本寫死 ["mushroom"]，於是 composer 會把 director 選好的
#   花海/草地 create 當成非法 target 擋掉。改由 scatter_catalog 導出同一份清單，
#   director / composer / apply_plan 三邊看到的可建系統從此是同一個來源。
CREATABLE_EXTRA = sorted(_sc.SCATTERS)


def _synthetic_add_factory_name(src: dict) -> str:
    """Return a stable factory name for add assets with no catalog factory."""
    text = str(src.get("name") or src.get("id") or "generated asset")
    words = re.findall(r"[0-9A-Za-z]+", text)
    base = "".join(word[:1].upper() + word[1:] for word in words)
    if not base:
        base = "GeneratedAsset"
    if not base.endswith("Factory"):
        base += "Factory"
    return base


def _add_factory_name(src: dict) -> tuple[str, bool]:
    fac = src.get("factory") or src.get("host_factory")
    if fac:
        return str(fac), False
    return _synthetic_add_factory_name(src), True


def _env() -> dict:
    merged: dict = {}
    for f in (ROOT / ".env", ROOT.parent / "ExtraWorld" / ".env",
              ROOT.parent / "infinigen_o" / ".env", ROOT.parent / "OtherTest" / ".env"):
        if f.is_file():
            merged.update({k: v for k, v in dotenv_values(f).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


ENV = _env()


def _claude():
    import anthropic
    key = ENV.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found")
    base = (ENV.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


SYSTEM = """You are the COMPOSER for a 3D scene-editing pipeline. You convert a written
edit plan into the exact JSON the execution layer runs. You add no creative judgement —
the plan already decided what to do. Your job is a faithful, schema-correct translation.

You are given the plan AND the factual scene inventory it was written against, so every
name you emit can be checked against something that really exists.

THE EXECUTION LAYER HAS TWO STAGES, so you emit TWO objects.

STAGE A — edits to the coarse blend (`edit_plan`). Key names are exact:
  hero_edits[] — moving/removing/adding placeholder boxes:
    {"op":"delete", "factory":"<FactoryName>", "rule":"frac:<0-1>"|"keep:<n>"|"keep_inview:<n>"}
    {"op":"scale",  "factory":"<FactoryName>", "value":<number>}
    {"op":"move",   "factory":"<FactoryName>", "delta":[<x>,<y>,<z>]}
    {"op":"add",    "factory":"<FactoryName>", "n":<count>, "radius":<metres>,
                    "anchoring":"<from the plan item>", "n_masters":<from the plan item>,
                    "world_motion":<from the plan item>,
                    "placement_spec":<from the plan item>,
                    "screen_positions":<omit for map-managed add anchorings; otherwise one [x,y] per instance>}
    {"op":"attrs",  "target_factory":"<FactoryName>(<seed>)",
                    "anchoring":"<from the plan item>", "n_masters":<from the plan item>,
                    "world_motion":<from the plan item>}
    ** No hero "replace" exists — the look belongs to stage C (appearance). **
    ** Replacement granularity is the complete factory category. If TreeFactory is
       replaced, emit appearance + attrs entries for EVERY TreeFactory collection in
       the inventory. A requested count does not create a partial-replacement mode. **
    ** BUT a replaced object still needs to know HOW IT SITS on the ground, and that
       cannot live in `appearance` (nothing reads it). So for EVERY entry you emit in
       `appearance.placements`, ALSO emit one `attrs` entry here carrying that plan
       item's `anchoring` and `n_masters` verbatim. `attrs` moves no boxes; it exists
       only to carry those two values down to the placer.
       `target_factory` must be "<FactoryName>(<seed>)" — the SAME factory and seed as
       the appearance entry, so the two can be matched up. **
    ** For `add`, the factory name comes from the plan item's `factory`; if that is null,
       use its `host_factory` instead (a synthetic name for something with no catalog
       factory, e.g. a sleigh). `add` builds its own neutral boxes, so ANY name works —
       never drop an add just because `factory` is null. **
    ** `add` carries its OWN anchoring/n_masters/world_motion/placement_spec inline (it creates the boxes, so the
       values are stamped on them directly — do NOT emit a separate `attrs` for an add).
       Copy them verbatim from the plan item. An added object that loses its anchoring
       is placed with the fallback and will sit wrong on any slope. **
    ** Copy placement_spec.surface_policy semantic type choices verbatim. They are the
       Director-authored support contract. Exact mesh-name lists are injected
       deterministically from SCENE INVENTORY.available_surface_types after your reply;
       never invent mesh names or surface types. **
    ** For map-managed adds, omit screen_positions: deterministic map-first code
       creates the initialization points and then owns every exact 2D/3D cell.
       Map-managed add anchorings are conform, upright, tilted, rooted, afloat,
       submerged, and airborne. Only non-map-managed future anchoring modes should
       estimate exactly n screen_positions inside placement_spec.screen_bounds.
       Coordinates are normalized camera/render coordinates with (0,0) at the
       BOTTOM-LEFT: x increases rightward and y increases upward.
       Preserve placement_spec.proxy_dimensions_m verbatim; it is the Director's
       conservative pre-generation placeholder size and is not yours to re-estimate.
       Use placement_spec.arrangement only as a simple visual hint: scatter means dispersed,
       field means a dense plant/flower patch, row means aligned, cluster means compact,
       school means a compact 3D underwater group, and single means the region center.
       These are proposals, not
       geometry claims; deterministic raycast and Machine Review validate every point.
       If your coordinates are missing or invalid, the executor uses a deterministic
       fallback and records that fact. **
    ** `world_motion` is independent of Factory/bone animation. It is required and must be
       copied verbatim. Controlled v3 accepts only the presets already present in the plan. **
    ** `factory` is a bare name; this layer cannot target a single seed. If the plan asks
       to delete/scale/move only one collection of a multi-seed factory, emit it at factory
       level and record the imprecision in `warnings`. **
  extra_edits[] — ground scatter:
    {"op":"replace", "target":"<ScatterName>", "code":"generated/<slug>"}
    {"op":"delete",  "target":"<ScatterName>"}
    {"op":"density", "target":"<ScatterName>", "value":<number>}
    {"op":"scale",   "target":"<ScatterName>", "value":<number>}
    {"op":"create",  "target":"<creatable type>", "density":<number>,
                     "code":"generated/<slug>", "n_masters":<count>}
    ** `create` works only for the creatable types listed in the inventory. **
    ** `target` must match an inventory scatter, with any seed parentheses stripped. **

  GROUND SCATTER — how a plan edit becomes an extra_edit:
    A plan edit with "placement": "ground_scatter" is NOT a hero. It has no
    placement_spec, no screen positions and no anchoring; never emit hero ops for
    it and never drop it as unrepresentable.
      action "add"     -> {"op":"create", "target":"<the edit's scatter_system>",
                           "code":"generated/<slug>", "n_masters":<count>}
      action "replace" -> {"op":"replace", "target":"<inventory scatter name>",
                           "code":"generated/<slug>"}
      action "delete"  -> {"op":"delete",  "target":"<inventory scatter name>"}
    ** `target` for "create" is the edit's `scatter_system` field VERBATIM (for
       example "flowers", "grass", "ivy", "coral_reef"). Do not translate it into
       a Factory name and do not invent a new one. **
    ** The engine decides where it lands from the edit's `scatter_region`; you do
       not author positions for a ground scatter. **

STAGE C — HERO appearance, applied per collection during populate (`appearance`):
  placements[] — one entry per collection whose LOOK changes:
    {"collection":"<exact collection string>", "factory":"<FactoryName>", "seed":<int>,
     "code":"generated/<slug>", "look":"<the look, verbatim from the plan>"}
  ** This is how one species is restyled while another is left untouched. **

CODE SLUGS
  Every `code` is a directory an asset generator will later fill: "generated/<slug>",
  lowercase a-z0-9_, short and descriptive of the look. Reuse one slug when two entries
  need an identical look; use distinct slugs when the looks differ.

RULES
  1. Emit only ops from the lists above, with exactly those key names.
  2. Every factory/target/collection name must appear in the inventory you are given.
     The one exception is `add`, which may name a factory the scene does not have.
  3. Do not invent numbers. If the plan gives a count, use it; if it gives none and the
     op needs one, pick the smallest sensible value and say so in `warnings`.
  4. If a plan item cannot be expressed in this schema, put it in `dropped` with the
     reason. Never approximate it into a different op silently.

Return ONE JSON object and nothing else:
{
  "edit_plan": {"hero_edits": [...], "extra_edits": [...]},
  "appearance": {"placements": [...]},
  "warnings": ["<anything lossy or assumed>"],
  "dropped": [{"item": "<plan item n>", "why": "<why it has no representation>"}]
}"""


def normalize_execution_contract(out: dict, plan: dict, manifest: dict) -> dict:
    """Make generated-code routing deterministic without changing edit semantics.

    A HERO replacement is deliberately factory-wide: every inventory collection for
    that factory receives the same generated code.  This is the previously validated
    Composer -> Placer behavior and avoids inventing a partial-replacement component.
    """
    ep = out.setdefault("edit_plan", {})
    hero = list(ep.setdefault("hero_edits", []))
    placements = out.setdefault("appearance", {}).setdefault("placements", [])

    available_entries = manifest.get("available_surface_types", [])
    available_types = [entry.get("type") for entry in available_entries]

    def resolve_surface_policy(value):
        policy = plc.validate_surface_policy_types(value, available_types)
        by_type = {entry.get("type"): list(entry.get("mesh_names", []))
                   for entry in available_entries}
        required_names = list(policy.get("required_surface_names", []))
        forbidden_names = list(policy.get("forbidden_surface_names", []))
        for surface_type in policy["required_surface_types"]:
            required_names.extend(by_type.get(surface_type, []))
        for surface_type in policy["forbidden_surface_types"]:
            forbidden_names.extend(by_type.get(surface_type, []))
        policy["required_surface_names"] = list(dict.fromkeys(required_names))
        policy["forbidden_surface_names"] = list(dict.fromkeys(forbidden_names))
        overlap = (set(policy["required_surface_names"])
                   & set(policy["forbidden_surface_names"]))
        if overlap:
            # The current registry is mesh-level.  In scenes such as desert
            # (sand/rock) and plain (grass/soil), distinct semantic types can
            # intentionally resolve to the same terrain mesh.  That mesh cannot
            # simultaneously be required and forbidden.  Preserve the positive
            # support requirement and drop only the indistinguishable negative
            # constraint; asking the Agent again cannot add missing mesh detail.
            policy["forbidden_surface_names"] = [
                name for name in policy["forbidden_surface_names"]
                if name not in overlap
            ]
            out.setdefault("warnings", []).append(
                "mesh-level surface registry cannot distinguish required from "
                f"forbidden semantic types on {sorted(overlap)}; required wins")
        policy = plc.validate_surface_policy_names(
            policy, [entry.get("name") for entry in manifest.get(
                "raycast_surfaces", [])])
        return policy

    replacements = [e for e in plan.get("edits", [])
                    if e.get("feasible", True) and e.get("placement") == "hero"
                    and e.get("action") == "replace" and e.get("target_factory")]
    for src in replacements:
        fac = src["target_factory"]
        matched = [p for p in placements if p.get("factory") == fac]
        template = matched[0] if matched else {}
        code = template.get("code") or f"generated/{src.get('id') or fac.lower()}"
        look = template.get("look") or src.get("spec") or src.get("name") or fac

        inventory = [c for c in manifest.get("collections", [])
                     if c.get("factory") == fac]
        placements[:] = [p for p in placements if p.get("factory") != fac]
        keys = set()
        for coll in inventory:
            seed = int(coll["seed"])
            key = f"{fac}({seed})"
            keys.add(key)
            placements.append({
                "collection": coll["collection"],
                "factory": fac,
                "seed": seed,
                "code": code,
                "look": look,
                "code_from": src.get("id"),
            })

        hero = [e for e in hero if not (
            e.get("op") == "attrs"
            and (e.get("target_factory") or "").split("(", 1)[0] == fac)]
        for key in sorted(keys):
            hero.append({
                "op": "attrs",
                "target_factory": key,
                "anchoring": src.get("anchoring"),
                "n_masters": src.get("n_masters"),
                "world_motion": src.get("world_motion"),
                "code_from": src.get("id"),
            })

    additions = [e for e in plan.get("edits", [])
                 if e.get("feasible", True) and e.get("placement") == "hero"
                 and e.get("action") == "add"]
    normalized_add_targets = set()
    for src in additions:
        fac, synthetic_factory = _add_factory_name(src)
        if synthetic_factory:
            out.setdefault("warnings", []).append(
                f"{src.get('id')}: plan item has factory=null and host_factory=null; "
                f"used synthetic add factory name {fac!r}")
        candidates = [e for e in hero
                      if e.get("op") == "add"]
        target = (next((e for e in candidates
                        if e.get("code_from") == src.get("id")), None)
                  or next((e for e in candidates
                           if not e.get("code_from")
                           and e.get("factory") == fac), None))
        if target is None:
            target = {
                "op": "add",
                "factory": fac,
                "n": int(src.get("count") or 1),
                "radius": 10.0,
            }
            hero.append(target)
            out.setdefault("warnings", []).append(
                f"Composer model omitted add {src.get('id')}; deterministic contract restored it")
        normalized_add_targets.add(id(target))
        target["factory"] = fac
        target["code_from"] = src.get("id")
        target["anchoring"] = src.get("anchoring")
        target["n_masters"] = src.get("n_masters")
        target["world_motion"] = src.get("world_motion")
        target["local_animation"] = la.normalize_request(
            src.get("local_animation"), factory=fac)
        count = int(src.get("count") or target.get("n") or 1)
        raw_spec = src.get("placement_spec") if isinstance(
            src.get("placement_spec"), dict) else {}
        dense_field_adjusted = False
        dense_count = plc.dense_field_instance_count(
            count=count, factory=fac, code_from=src.get("id"),
            semantic_target=str(raw_spec.get("semantic_target") or "").strip(),
            arrangement=str(raw_spec.get("arrangement") or ""))
        if dense_count != count:
            out.setdefault("warnings", []).append(
                f"{src.get('id')}: dense botanical field count {count} -> {dense_count}")
            count = dense_count
            dense_field_adjusted = True
        target["n"] = count
        spec = plc.normalize_placement_spec(
            raw_spec, count=count,
            anchoring=src.get("anchoring"),
            factory=fac,
            code_from=src.get("id"))
        spec["surface_policy"] = resolve_surface_policy(spec["surface_policy"])
        target["placement_spec"] = spec
        composer_points = target.get("screen_positions")
        if (dense_field_adjusted and isinstance(composer_points, list)
                and len(composer_points) != count):
            composer_points = None
        if composer_points is None and is_map_managed_anchoring(src):
            points = plc.initial_screen_positions(spec, count)
            target["screen_positions_source"] = "deterministic_map_first_seed"
        elif composer_points is None:
            raise ValueError(
                f"Composer omitted screen_positions for {src.get('id')}")
        else:
            points = plc.normalize_screen_positions(
                composer_points, spec=spec, count=count)
            target["screen_positions_source"] = "composer"
        target["screen_positions"] = points
        # Screen coordinates describe what the camera sees. Project against the
        # first visible surface, then enforce the semantic surface policy.
        target["screen_position_projection"] = "visible_surface"

    filtered_hero = []
    for edit in hero:
        if (edit.get("op") == "add"
                and id(edit) not in normalized_add_targets
                and is_map_managed_anchoring(edit)
                and not edit.get("code_from")):
            out.setdefault("warnings", []).append(
                "removed orphan map-managed add without code_from: "
                f"{edit.get('factory')!r}")
            continue
        filtered_hero.append(edit)
    hero = filtered_hero

    # The LLM may report an add asset's look as "dropped" because a brand-new
    # placeholder has no pre-existing collection/seed for appearance.  That was
    # true before code_from routing existed.  Now the generated/sampled asset is
    # deterministically routed to the new add collection, so failing the entire
    # Composer stage for that obsolete representation warning is incorrect.
    add_ids = {str(e.get("id")) for e in additions if e.get("id")}
    # ★已經寫進 edits 的東西不能同時列在 dropped★(2026-09-13 cave 實測)：LLM 把
    #   hanging_vines 正確放進 official_scatter_edits，卻又在 dropped 說「plan 裡沒有這個 edit」，
    #   composer 因此回傳 1、整個 case 死在第 7 步。凡是真的出現在任何 edits 陣列的 code_from
    #   一律從 dropped 拿掉。
    emitted_ids = set(add_ids)
    for key in ("hero_edits", "extra_edits", "swarm_edits", "official_scatter_edits"):
        for edit in (out.get(key) or []):
            for field in ("code_from", "director_edit_id", "target"):
                value = edit.get(field)
                if value:
                    emitted_ids.add(str(value))
    out["dropped"] = [
        item for item in out.get("dropped", [])
        if not any(
            str(item.get("item") or "") == known
            or str(item.get("item") or "").startswith(known + " ")
            or str(item.get("item") or "").startswith(known + ":")
            for known in emitted_ids
        )
    ]

    ep["hero_edits"] = hero

    # ★官方魚群★(2026-09-11)：完全確定性，不經過 LLM(main 會先把它們從 LLM 看到的
    #   plan 拿掉)。replace = placer 用 master_swap 換那群的母本；add = apply_plan
    #   照 generate_nature 的 fish_school 在相機附近建新群。code 由 fillextracode 解析。
    swarm_ids = set()
    swarm = []
    for src in plan.get("edits", []):
        if (src.get("placement") != "fish_school" or not src.get("feasible", True)
                or src.get("action") not in ("replace", "add")):
            continue
        swarm_ids.add(src.get("id"))
        swarm.append({
            "op": src["action"],
            "target": src.get("target_factory") if src["action"] == "replace" else None,
            "count": src.get("count") if src["action"] == "add" else None,
            "n_masters": src.get("n_masters") or 3,
            "code": f"generated/{src.get('id')}",
            "code_from": src.get("id"),
        })
        if src["action"] == "add":
            swarm[-1]["density_level"] = src.get("scatter_density") or "normal"

    # ★官方撒點(植物/石頭 add)★：同樣確定性、不經 LLM。apply_plan 用官方
    #   scatter_placeholders 在相機附近撒點，fillcode 依 code_from 填 code。
    scatter = []
    for src in plan.get("edits", []):
        if (src.get("placement") != "official_scatter" or not src.get("feasible", True)
                or src.get("action") != "add"):
            continue
        swarm_ids.add(src.get("id"))
        scatter.append({
            "density_level": src.get("scatter_density") or "normal",
            "n_masters": src.get("n_masters") or 5,
            "code": f"generated/{src.get('id')}",
            "code_from": src.get("id"),
        })
    if swarm_ids:
        ep["hero_edits"] = [e for e in ep["hero_edits"]
                            if e.get("code_from") not in swarm_ids]
        ep["extra_edits"] = [e for e in ep.get("extra_edits", [])
                             if e.get("code_from") not in swarm_ids]
    ep["swarm_edits"] = swarm
    ep["official_scatter_edits"] = scatter
    # ★確定性路由的項目也不能留在 dropped★(2026-09-13 cave 第二次實測)：上面那道過濾跑在
    #   official_scatter/fish_school 組裝★之前★，那時 out 裡還沒有這些 edit，所以
    #   hanging_vines 仍被留在 dropped ⇒ composer 又回傳 1。這些項目根本不經過 LLM，
    #   LLM 說「plan 裡沒有」是必然的，不該算失敗。
    routed_ids = {str(i) for i in swarm_ids if i}
    for edit in scatter + swarm:
        for field in ("code_from", "target_factory"):
            if edit.get(field):
                routed_ids.add(str(edit[field]))
    if routed_ids:
        out["dropped"] = [
            item for item in out.get("dropped", [])
            if not any(str(item.get("item") or "").split(":")[0].split(" ")[0] == known
                       for known in routed_ids)
        ]
    return out


def inventory_block(manifest: dict) -> str:
    lines = ["CAMERA-VISIBLE SEMANTIC SURFACE TYPES (complete allowed menu):"]
    for entry in manifest.get("available_surface_types", []):
        lines.append(
            f"  {entry.get('type'):20} meshes={entry.get('mesh_names', [])}")
    lines.extend(["", "HERO collections (exact strings — use these verbatim):"])
    for c in manifest.get("collections", []):
        lines.append(f"  {c['collection']:46}  factory={c['factory']:20} "
                     f"seed={c['seed']:<12} total={c['total']:<5} in_view={c['in_view']}")
    lines.append("")
    lines.append("EXTRA / ground scatter present in this scene:")
    for s in manifest.get("scatters", []):
        lines.append(f"  {s.get('label', '?'):46}  density={s.get('density')} "
                     f"masters={s.get('masters')}")
    lines.append("")
    usable = _sc.systems_for_surfaces(
        [entry.get("type") for entry in manifest.get("available_surface_types", [])])
    lines.append("Creatable-from-nothing EXTRA scatter systems "
                 f"(suited to this scene's surfaces): {', '.join(usable)}")
    return "\n".join(lines)


def _parse_json_response(text: str, *, label: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"{label} did not return a JSON object: {text[:500]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{label} returned invalid JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno})"
        ) from error


def compose(
    plan: dict,
    manifest: dict,
    model: str = OPUS_MODEL,
    correction: str | None = None,
    debug_dir: Path | None = None,
    attempt: int = 1,
) -> dict:
    user = (f"=== SCENE INVENTORY (every name you emit must come from here) ===\n"
            f"{inventory_block(manifest)}\n\n"
            f"=== THE PLAN TO TRANSLATE ===\n"
            f"{json.dumps(plan, indent=2, ensure_ascii=False)}\n\n"
            f"Translate it now. Return the JSON only.")
    if correction:
        user += ("\n\nYOUR PREVIOUS OUTPUT FAILED DETERMINISTIC VALIDATION:\n"
                 f"{correction}\nReturn a complete corrected JSON object. Do not omit "
                 "any requested edit. Supply screen_positions only for non-map-managed "
                 "anchoring modes.")
    resp = _claude().messages.create(
        model=model, max_tokens=COMPOSER_MAX_TOKENS, system=SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"raw_response_attempt_{attempt}.txt").write_text(
            text, encoding="utf-8")
    return _parse_json_response(text, label="Composer")


def check(out: dict, manifest: dict) -> list:
    """★確定性檢查★ —— LLM 只判斷，schema 由這裡決定(0727 4-A)。
    真正的擋關是 apply_plan.py --dry-run；這裡先抓掉明顯不合法的。"""
    warns = []
    colls = {c["collection"] for c in manifest.get("collections", [])}
    facs = {c["factory"] for c in manifest.get("collections", [])}
    scats = {s.get("label", "") for s in manifest.get("scatters", [])}

    ep = out.get("edit_plan", {})
    attr_targets = set()
    for e in ep.get("hero_edits", []):
        if e.get("op") in HERO_ATTR_OPS:
            t = e.get("target_factory")
            attr_targets.add(t)
            if not t:
                warns.append(f"attrs 少了 target_factory：{e}")
            elif t.split("(")[0] not in facs:
                warns.append(f"attrs target_factory {t!r} 不在場景清單裡")
            if e.get("anchoring") not in ANCHORINGS:
                warns.append(f"attrs anchoring {e.get('anchoring')!r} 不合法(可用 {ANCHORINGS})")
            try:
                request = wm.validate_runtime_request(e.get("world_motion"))
                if request["preset"] != "none":
                    warns.append("attrs/replacement 的 world_motion 必須為 none（v3 只支援 add）")
            except ValueError as error:
                warns.append(f"world_motion 不合法: {error}")
            continue
        if e.get("op") not in HERO_OPS:
            warns.append(f"hero op {e.get('op')!r} 不合法(可用 {HERO_OPS + HERO_ATTR_OPS})")
        if e.get("op") != "add" and e.get("factory") not in facs:
            warns.append(f"hero factory {e.get('factory')!r} 不在場景清單裡")
        # ★add 也要帶 anchoring★(2026-08-06 端到端實測抓到) —— 舊 prompt 的 add 規格
        #   只有 factory/n/radius，於是 LLM 忠實照著吐、把 director 給的
        #   anchoring='conform' n_masters=3 丟掉，而且它自己在 warnings 裡寫
        #   「has no representation in this schema」。其實 apply_plan 的 add 路徑
        #   一直支援(會 stamp bi_anchoring)，是規格漏寫。
        #   掉了不會報錯，只會用 DEFAULT_ANCHORING=rooted 靜靜擺錯 ⇒ 這裡出聲。
        if e.get("op") == "add" and e.get("anchoring") not in ANCHORINGS:
            # ★補預設值而不是判 plan 無效★(2026-10-20：1019 重跑 08 三次都因這條死在 composer)
            fac = str(e.get("factory") or "")
            default_anchor = "rooted" if any(k in fac for k in (
                "Flower", "Tree", "Bush", "Grass", "Kelp", "Coral", "Cactus", "Plant",
                "Fern", "Mushroom", "Seaweed", "Monocot")) else "conform"
            print(f"[composer] ⚠️ add {fac!r} anchoring={e.get('anchoring')!r} 不合法 → 補 {default_anchor!r}",
                  flush=True)
            e["anchoring"] = default_anchor
        if e.get("op") == "add":
            if not isinstance(e.get("factory"), str) or not e.get("factory").strip():
                warns.append(f"add 少了 factory：{e}")
            if is_map_managed_anchoring(e) and not e.get("code_from"):
                warns.append(
                    f"map-managed add {e.get('factory')!r} 少了 code_from")
            try:
                count = int(e.get("n"))
                if count < 1:
                    raise ValueError("n must be >= 1")
                spec = plc.normalize_placement_spec(
                    e.get("placement_spec"), count=count,
                    anchoring=e.get("anchoring"),
                    factory=e.get("factory"),
                    code_from=e.get("code_from"))
                plc.validate_surface_policy_types(
                    spec["surface_policy"],
                    [x.get("type") for x in manifest.get(
                        "available_surface_types", [])])
                plc.validate_surface_policy_names(
                    spec["surface_policy"],
                    [x.get("name") for x in manifest.get("raycast_surfaces", [])])
                points = e.get("screen_positions")
                if (not is_map_managed_anchoring(e)
                        and spec["arrangement"] in {"single", "row"}
                        and points is None):
                    warns.append(
                        f"add {e.get('factory')!r} 的 {spec['arrangement']} 少了 screen_positions")
                if points is not None:
                    plc.normalize_screen_positions(points, spec=spec, count=count)
            except (TypeError, ValueError) as error:
                warns.append(f"add {e.get('factory')!r} placement_spec 不合法: {error}")
        if e.get("op") in ("add", "attrs"):
            try:
                request = wm.validate_runtime_request(e.get("world_motion"))
                context_error = wm.execution_context_error(
                    request, action=e.get("op"), anchoring=e.get("anchoring"))
                if context_error:
                    warns.append(
                        f"{e.get('op')} {e.get('factory') or e.get('target_factory')!r}: "
                        f"{context_error}")
            except ValueError as error:
                warns.append(f"world_motion 不合法: {error}")
        if e.get("op") == "add":
            try:
                la.normalize_request(e.get("local_animation"),
                                     factory=e.get("factory"))
            except ValueError as error:
                warns.append(f"local_animation 不合法: {error}")
        if "target" in e:
            warns.append(f"hero_edits 用了 'target'，應該是 'factory'：{e}")
    for e in ep.get("extra_edits", []):
        if e.get("op") not in EXTRA_OPS:
            warns.append(f"extra op {e.get('op')!r} 不合法(可用 {EXTRA_OPS})")
        if e.get("op") == "create" and e.get("target") not in CREATABLE_EXTRA:
            warns.append(f"create {e.get('target')!r}：只有 {CREATABLE_EXTRA} 能憑空建")
        t = str(e.get("target", ""))
        if e.get("op") != "create" and t and not any(t in s or s.startswith(t) for s in scats):
            warns.append(f"extra target {t!r} 對不上任何 scatter：{sorted(scats)}")
    for p in out.get("appearance", {}).get("placements", []):
        if p.get("collection") not in colls:
            warns.append(f"appearance collection {p.get('collection')!r} 不在場景清單裡")
        if not str(p.get("code", "")).startswith("generated/"):
            warns.append(f"code {p.get('code')!r} 應為 generated/<slug>")
        # ★每個 appearance 都要有一筆對應的 attrs★ —— 否則換上去的東西不知道怎麼貼地，
        #   而且★不會報錯★(2026-08-05 端到端才發現的那個靜默失敗)。
        key = f"{p.get('factory')}({p.get('seed')})"
        if key not in attr_targets:
            warns.append(f"appearance {key} ★沒有對應的 attrs★ ⇒ anchoring 會掉，"
                         f"placer 收不到貼地屬性")
    return warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan")
    ap.add_argument("--manifest")
    ap.add_argument("--case")
    ap.add_argument("--all-cases", action="store_true")
    ap.add_argument("--out-dir")
    ap.add_argument("--model", default=OPUS_MODEL)
    args = ap.parse_args()

    e2e = HERE / "out_e2e"
    if args.all_cases:
        jobs = sorted(d.name for d in e2e.iterdir() if (d / "adhoc.json").is_file())
    elif args.case:
        jobs = [args.case]
    else:
        jobs = [None]

    status = 0
    for name in jobs:
        if name:
            d = e2e / name
            plan_p, man_p = d / "adhoc.json", d / "scene_manifest.json"
            out_dir = Path(args.out_dir) if args.out_dir else d
        else:
            plan_p, man_p = Path(args.plan), Path(args.manifest)
            out_dir = Path(args.out_dir or ".")
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
        manifest = json.loads(man_p.read_text(encoding="utf-8"))

        print(f"\n{'='*74}\n[composer] {name or plan_p}\n{'='*74}")
        # fish_school edits are translated deterministically (normalize_execution_
        # contract); the LLM never sees them, so it cannot drop them as unrepresentable.
        llm_plan = {**plan, "edits": [e for e in plan.get("edits", [])
                                      if e.get("placement") not in (
                                          "fish_school", "official_scatter")]}
        correction = None
        out = None
        if not llm_plan["edits"]:
            # ★整份 plan 都是確定性路線時不要問 LLM★(2026-09-11)：0911 desert 的四個
            #   資產全走 official_scatter，濾掉之後 plan 變成空的，LLM 回「沒有東西可
            #   翻譯」並把四個全標成無法表達 ⇒ composer rc=1，整個 case 死。
            print("[composer] 全部都是 fish_school / official_scatter ⇒ 不呼叫 LLM，"
                  "直接用確定性規則產出")
            out = normalize_execution_contract(
                {"edit_plan": {"hero_edits": [], "extra_edits": []},
                 "appearance": {"placements": []}}, plan, manifest)
            schema_errors = check(out, manifest)
            if schema_errors:
                raise SystemExit(f"deterministic composer contract failed: {schema_errors}")
        for attempt in range(1, 4):
            if out is not None:
                break
            try:
                raw = compose(
                    llm_plan,
                    manifest,
                    model=args.model,
                    correction=correction,
                    debug_dir=out_dir,
                    attempt=attempt,
                )
                out = normalize_execution_contract(raw, plan, manifest)
                schema_errors = check(out, manifest)
                if schema_errors:
                    raise ValueError("; ".join(schema_errors))
                if attempt > 1:
                    print(f"[composer] corrected invalid JSON contract on attempt {attempt}")
                break
            except (TypeError, ValueError) as error:
                correction = str(error)
                print(f"[composer] invalid contract attempt {attempt}/3: {error}")
        if out is None:
            raise SystemExit(
                f"Composer failed deterministic contract after 3 attempts: {correction}")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "edit_plan.json").write_text(
            json.dumps(out.get("edit_plan", {}), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        (out_dir / "appearance.json").write_text(
            json.dumps(out.get("appearance", {}), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

        ep = out.get("edit_plan", {})
        print(f"\n--- edit_plan.json  (→ apply_plan.py，階段 A) ---")
        print(json.dumps(ep, indent=2, ensure_ascii=False))
        print(f"\n--- appearance.json (→ placer.py 的 manifest code 欄，階段 C) ---")
        for p in out.get("appearance", {}).get("placements", []):
            print(f"  {p.get('collection'):46} → {p.get('code')}")
            print(f"      look: {p.get('look', '')[:100]}")
        if out.get("warnings"):
            print("\n  ⚠️ composer 自己標的:")
            for w in out["warnings"]:
                print(f"      {w}")
        if out.get("dropped"):
            print("\n  ⛔ 無法表達而丟棄:")
            for dd in out["dropped"]:
                print(f"      {dd.get('item')}: {dd.get('why')}")
            if (os.environ.get("COMPOSER_ALLOW_DROPPED") == "1"
                    or os.environ.get("PLACEMENT_ONLY_E2E") == "1"):
                print("  ⚠️ placement-only/composer override: dropped items kept as warnings")
            else:
                status = 1
        warns = check(out, manifest)
        if warns:
            print("\n  ⚠️ ★確定性檢查抓到★:")
            for w in warns:
                print(f"      {w}")
            status = 1
        else:
            print("\n  ✅ 確定性檢查通過(名稱/op 都對得上場景清單)")
        print(f"\n  → {out_dir}/edit_plan.json, appearance.json")
    return status


if __name__ == "__main__":
    sys.exit(main())

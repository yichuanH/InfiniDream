"""
Director —— BeyondInfinigen 的 Agent 1(場景編輯規劃)。

★與 ExtraWorld/2_director.py 的根本差異★
  ExtraWorld  : 「從零規劃一個新場景」——無中生有，沒有既存場景可參照。
  BeyondInfinigen: 「★編輯一個已經存在的場景★」——場景已經跑好了，
                   有 blend(可掃出實際有什麼)、有 render 圖(看得到現在長什麼樣)。
  ⇒ routing table(featured × organic × FactoryList) ★沿用★，但多了一個
    ExtraWorld 沒有的軸：★這東西場景裡已經有了嗎★ → 決定 action(replace/add/delete)。

【INPUT ★一個資料夾★】BeyondInfinigen/input/<scene>/
    scene.blend    跑好的 Infinigen 場景(建議 symlink，blend 動輒數 GB)
                   也接受 fine/scene.blend 或 coarse/scene.blend
    render.png     該場景的一張 render 圖(也接受 .jpg / 任一張 *.png)
    prompt.txt     一句話：要把場景改成什麼樣(可用 --prompt 覆寫)

  scene_manifest ★不是外部輸入★——director 自己跑 scan_scene.py 掃 blend 得到。

【OUTPUT】BeyondInfinigen/output/<scene>/1_director/
    scene_manifest.json   場景實際有什麼(確定性掃出來的，★算一次兩處用★:
                          餵 director 判斷 + 餵下游 replacer 算 distance/可見集合)
    edit_plan.json        結構化編輯計畫(0724 第十節決策1，不是自然語言計畫書)
  下游對接：action → 選底層工具(replacer/adder/deleter)
            factory → replacer 的 target
            spec  → 餵已寫好的 generator agent 生 code

【正式 BeyondInfinigen routing table】
  所有可生成資產一律 -> ProceduralGenerator。
  Director 仍保留 category label 供分析/紀錄，但不再選 BlenderGenerator；
  即使 FactoryList 沒有接近項，也交給 ProceduralGenerator 自動挑一個最接近的
  Infinigen starter factory 當改寫起點。

用法:
  python director.py forest                    # = input/forest → output/forest/1_director
  python director.py input/forest              # 給路徑也行
  python director.py forest --prompt "..."     # 覆寫 prompt.txt
  python director.py forest --manifest <已掃好的.json>   # 跳過掃描(4.8GB blend 要幾分鐘)
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
import placement_classes as pc  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # BeyondInfinigen/
INPUT_ROOT = ROOT / "input"                          # input/<scene>/
# ★2026-08-03 改為專案優先★：output/<專案>/1_director/ (見 paths.py)
sys.path.insert(0, str(ROOT))
from paths import init_project, stage_dir  # noqa: E402
from project_namer import name_project  # noqa: E402
import world_motion as wm  # noqa: E402
import local_animation as la  # noqa: E402
import placement_contract as plc
import scatter_catalog as sc  # noqa: E402
import official_scatter as osc  # noqa: E402

OUTPUT_ROOT = ROOT / "output"                        # 實際輸出 = OUTPUT_ROOT/<scene>/1_director/
FACTORYLIST = ROOT / "asset_generator" / "extraworld" / "FactoryList.txt"
SCAN_SCRIPT = HERE / "scan_scene.py"

OPUS_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
DIRECTOR_MAX_TOKENS = int(os.environ.get("DIRECTOR_MAX_TOKENS", "12000"))
# bpy 4.2.0 環境(0723 memory: render/掃場景必用此版)。可用 BPY_PYTHON 環境變數覆寫。
BPY_PYTHON = os.environ.get("BPY_PYTHON", os.environ.get("BI_PYTHON", sys.executable))

# category key -> (num, label, generator). Legacy ExtraWorld is intentionally
# independent; BeyondInfinigen's formal route now sends every generated asset
# through ProceduralGenerator. BlenderGenerator remains a standalone research
# path, not a formal pipeline routing decision.
FORMAL_ASSET_GENERATOR = "ProceduralGenerator"
ROUTES = {
    "featured_organic":       (1, "featured organic",            FORMAL_ASSET_GENERATOR),
    "featured_geometric":     (2, "featured geometric",          FORMAL_ASSET_GENERATOR),
    "generic_not_in_factory": (3, "generic, not in FactoryList", FORMAL_ASSET_GENERATOR),
    # User-requested objects must not silently degrade to an arbitrary official
    # seed. A catalog match selects the closest code base; it does not prove the
    # requested species/object already looks like that factory. Even a generic
    # request therefore rewrites the selected factory and gets visual QA.
    "generic_in_factory":     (4, "generic, in FactoryList; rewrite required",
                               FORMAL_ASSET_GENERATOR),
}

INDOOR_SURFACE_HINTS = (
    ("countertop", (
        "countertop", "counter top", "counter", "cabinet top",
        "cabinet/countertop", "dresser top")),
    ("bed_top", (
        "bed top", "bedspread", "mattress", "comforter")),
    ("shelf", (
        "shelf", "bookcase")),
    ("seat", (
        "chair seat", "sofa seat", "seat cushion", "on the seat")),
    ("floor", (
        "floor", "ground")),
)
INDOOR_PROXY_DIMENSION_HINTS_M = (
    ("vase", [0.12, 0.12, 0.28]),
    ("speaker", [0.25, 0.12, 0.16]),
    ("pillow", [0.30, 0.20, 0.12]),
    ("rug", [1.20, 0.80, 0.05]),
    ("chair", [0.35, 0.35, 0.70]),
)
BACK_TO_WALL_HINTS = (
    "back-to-wall", "back to wall", "backs to the wall",
    "backs set close to the wall", "against the wall")


def route(has_feature: bool, is_organic: bool, in_factory: bool) -> str:
    if has_feature:
        return "featured_organic" if is_organic else "featured_geometric"
    return "generic_in_factory" if in_factory else "generic_not_in_factory"


def _edit_text(edit: dict) -> str:
    return " ".join(
        str(edit.get(key) or "")
        for key in (
            "id", "name", "placement_reason", "placement_mode_reason",
            "factory_reason", "spec", "reason")
    ).casefold()


def _has_hint(text: str, hint: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(hint.casefold()) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _infer_missing_indoor_surface_type(edit: dict, available_types) -> str | None:
    available = {str(value) for value in available_types or [] if value}
    if not available:
        return None
    text = _edit_text(edit)
    for surface_type, hints in INDOOR_SURFACE_HINTS:
        if surface_type in available and any(_has_hint(text, hint) for hint in hints):
            return surface_type
    return None


def _infer_missing_indoor_proxy_dimensions(edit: dict) -> list[float] | None:
    text = _edit_text(edit)
    factory = str(edit.get("factory") or "").casefold()
    for hint, dimensions in INDOOR_PROXY_DIMENSION_HINTS_M:
        if hint in factory or _has_hint(text, hint):
            return list(dimensions)
    return None


def _infer_back_to_wall_mode(edit: dict) -> bool:
    text = _edit_text(edit)
    return any(_has_hint(text, hint) for hint in BACK_TO_WALL_HINTS)


# ------------------------------------------------------------------------------- env
def _env() -> dict:
    merged: dict = {}
    for envfile in (HERE.parent / ".env", HERE.parent.parent / "ExtraWorld" / ".env"):
        if envfile.is_file():
            merged.update({k: v for k, v in dotenv_values(envfile).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


ENV = _env()


def _claude():
    import anthropic
    key = ENV.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found (BeyondInfinigen/.env, ExtraWorld/.env, env)")
    base = (ENV.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


# --------------------------------------------------------------------- factory catalog
_ROW_RE = re.compile(r"^\s+([A-Za-z_]\w*)\s+\[(ENTRY|PART)\s*\]\s+(.*?)\s+(\S+\.py)\s*$")


def load_catalog() -> dict:
    """{FactoryName: {role, desc, file}}。沿用 ExtraWorld 的解析(含 ELEMENT 提升)。
    FactoryList 把兩種東西都標 [PART]：ELEMENT(單片葉/單朵花/單顆果，可獨立生成)
    和真正的 sub-part(骨架/身體部位/engine)。描述以 "(element" 開頭的是前者。"""
    cat = {}
    for line in FACTORYLIST.read_text(encoding="utf-8").splitlines():
        m = _ROW_RE.match(line)
        if m:
            name, role, desc, relpath = m.groups()
            desc = desc.strip()
            if role == "PART" and desc.startswith("(element"):
                role = "ELEMENT"
            cat[name] = {"role": role, "desc": desc, "file": relpath}
    return cat


# ------------------------------------------------------------------------ scene scan
def resolve_input(arg: str) -> tuple[Path, Path, Path, str]:
    """接受場景名(forest)或路徑(input/forest) → (scene_dir, blend, render, prompt)。

    一個 input 資料夾 = 跑一個場景需要的全部東西，見檔頭。"""
    p = Path(arg)
    scene_dir = p if p.is_dir() else INPUT_ROOT / arg
    if not scene_dir.is_dir():
        raise SystemExit(f"找不到 input 資料夾: {scene_dir}\n"
                         f"  建一個: {INPUT_ROOT}/<scene>/ 放 scene.blend + render.png + prompt.txt")
    scene_dir = scene_dir.resolve()

    # blend: 直接放的 scene.blend，或 infinigen 原生的 fine/ coarse/ 佈局
    blend = next((c for c in (scene_dir / "scene.blend",
                              scene_dir / "fine" / "scene.blend",
                              scene_dir / "coarse" / "scene.blend") if c.is_file()), None)
    if blend is None:
        raise SystemExit(f"{scene_dir} 裡找不到 scene.blend (也找過 fine/ 和 coarse/)")

    # render: 優先 render.png/jpg，否則任一張圖
    render = next((c for c in (scene_dir / "render.png", scene_dir / "render.jpg") if c.is_file()), None)
    if render is None:
        imgs = sorted(q for ext in ("*.png", "*.jpg") for q in scene_dir.glob(ext))
        if not imgs:
            raise SystemExit(f"{scene_dir} 裡找不到 render 圖 (render.png / render.jpg / 任一張 *.png)")
        render = imgs[0]
        print(f"[director] 沒有 render.png，改用 {render.name}")

    prompt_file = scene_dir / "prompt.txt"
    prompt = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.is_file() else ""
    return scene_dir, blend, render, prompt


def scan_scene(blend: Path, out_json: Path) -> dict:
    """跑 scan_scene.py(bpy 子進程)掃 blend → scene_manifest。
    ★這是 director 自己做的，不是外部輸入★——input 資料夾只放 blend + 圖 + prompt。"""
    if out_json.is_file():
        print(f"[director] 沿用已存在的 manifest: {out_json}")
        return json.loads(out_json.read_text(encoding="utf-8"))

    print(f"[director] 掃描場景 {blend} ...(4.8GB blend 需要幾分鐘)")
    r = subprocess.run([BPY_PYTHON, str(SCAN_SCRIPT), "--", str(blend), str(out_json)],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if "SCAN_DONE" not in r.stdout:
        sys.stderr.write(r.stderr)
        raise SystemExit("[director] scan_scene 失敗")
    return json.loads(out_json.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- plan
# 擺放模式的唯一定義在 placement_modes.py —— 這裡只 import，不要複製一份。
from placement_modes import (ANCHORINGS, DEFAULT_ANCHORING,  # noqa: E402
                             SCATTER_REGIONS, DEFAULT_SCATTER_REGION)

# 母本池大小的合法範圍與預設(見 master_pool.py)。
# 5 = 「幾個各自看得清楚的個體」那一檔；上限只是防呆，n 大於實例數等於沒開池。
DEFAULT_N_MASTERS = 5
MAX_N_MASTERS = 40
# ★replace 的距離上限★(2026-09-11)：Director 看得到整個場景(不只相機內)，但目標最近的
#   實例若比這遠，換了畫面上也看不出來 ⇒ 降級成 add(加在相機附近)。
HERO_REPLACE_MAX_DIST_M = float(os.environ.get("BI_REPLACE_MAX_DIST_M", "60"))
# 魚群的 emitter 距離上限；群本身寬 4m 起跳、會游動，所以比 hero 寬鬆一點。
SWARM_REPLACE_MAX_DIST_M = float(os.environ.get("BI_SWARM_REPLACE_MAX_DIST_M", "45"))
# ★一個場景只新增一群魚★(2026-09-12 使用者要求)：0912 coral_reef 兩條魚都變成
#   add，兩群新魚壓在原有的魚上面，畫面全是互相穿插的魚。想要少量的魚就在場景
#   設定的 counts 釘數量 —— 釘了會強制 action=add placement=hero，
#   而且★自動排除在魚群路由之外★(fixed_director.py: a["id"] not in pinned)。
MAX_NEW_FISH_SCHOOLS = 1


PLAN_SYSTEM = """You are the DIRECTOR of a 3D scene-EDITING pipeline. A 3D scene has ALREADY
been generated by Infinigen. You are given: a render of that scene, the scene's factory
manifest (what the scene actually contains, scanned deterministically from the .blend), a
catalog of every available Infinigen AssetFactory, and a one-line edit request.

Your job: decide WHAT to change and HOW each change gets built. You do not build anything.

For each thing the request implies, decide these axes:

A. ACTION — what kind of edit is it?
   - "replace": the scene ALREADY has this kind of object (it is in the manifest) and the
     request wants it to become something else. The new object reuses the existing
     placeholders' positions. This is the cheapest and most reliable action.
   - "add": the scene does NOT have this kind of object and it must be newly introduced.
     Requires deciding where to put it (handled downstream) — more expensive.
   - "delete": the request wants existing objects removed.
   Judge "already in the scene" ONLY from the manifest's factory list, never from the image
   alone. If the request's object maps to a factory that appears in the manifest -> replace.

B. FEATURE — is the object's exact appearance precisely described (a stated colour, pattern,
   material, distinctive part), so it must be REPRODUCED (has_feature=true)? Or is only its
   kind given, with no concrete look (has_feature=false)?

C. ORGANIC — is it a natural / grown / anatomical thing (living things and their natural
   parts)? A manufactured, hard-surface or geometric artefact is NOT organic. Judge by what
   the object fundamentally IS, not by where it appears.

D. FACTORY — the ONE closest matching Infinigen [ENTRY] factory in the catalog, or null.
   Match on what the object fundamentally IS by structure or anatomy, not on wording or
   surface details. A creature that does not exist in reality still maps to the factory
   whose ANATOMY is closest (a unicorn is a quadruped herbivore; a dragon is closest to a
   reptile). Prefer an [ENTRY]. Choose an "(element" [PART] ONLY when the requested object
   literally IS that single element. Never choose any other [PART] (base skeleton, body
   part, engine). Choose null if nothing is a reasonable match. Never invent a name.

E. SPEC — a precise, self-contained build instruction for the downstream generator agent.
   This is the ONLY free-text field that matters downstream, so make it count. State what
   the object must look like and, when the object differs from its base factory, state
   WHAT STRUCTURALLY DIFFERS (added/removed/reshaped parts, proportions), not just colour.
   Do not describe placement or how many — those are separate fields.

F. COUNT — how many of this object the request implies. For "replace", null means "all of
   them in view". For "add", give a concrete small number.

G. PLACEMENT — which of the three placement classes this thing belongs to. The rules are
   given below; they decide whether the edit is even POSSIBLE, so get this right before
   worrying about how it looks. For "grime" you must also name the HOST it grows on.

Return ONE JSON object and nothing else:
{
  "scene_reading": "<one or two sentences: what the render currently shows>",
  "edits": [
    {
      "id": "<short_snake_case_id>",
      "name": "<short human name>",
      "action": "replace" | "add" | "delete",
      "target_factory": "<for replace/delete: the factory name FROM THE MANIFEST being acted on; null for add>",
      "placement": "hero" | "grime" | "ground_scatter" | "fish_school" | "official_scatter",
      "placement_reason": "<one short sentence: why this class>",
      "grime_host": "<for placement=grime ONLY: trees|boulders|bushes|cactus; null otherwise>",
      "anchoring": "<for placement=hero ONLY: rooted|upright|tilted|conform|afloat|airborne|submerged; null otherwise>",
      "placement_spec": {
        "arrangement": "single|scatter|field|row|cluster|school",
        "semantic_target": "<short visible support/region, e.g. ice floe, river bank, open air>",
        "screen_region": "auto|left|center|right|lower_left|lower_center|lower_right|upper_left|upper_center|upper_right|full_frame",
        "screen_bounds": [<xmin>,<ymin>,<xmax>,<ymax>],
        "row_axis": "horizontal|vertical",
        "orientation": "random|parallel",
        "proxy_dimensions_m": [<conservative full length>,<full width>,<full height>],
        "surface_policy": {
          "required_surface_types": ["<type copied from available_surface_types>"],
          "forbidden_surface_types": ["<type copied from available_surface_types>"],
          "max_slope_deg": <0..90 or null>,
          "require_full_footprint_support": <true|false>
        }
      },
      "world_motion": {"preset":"none|aquatic_bob|aquatic_swim|air_glide|air_hover|ground_wander|ground_forward", "intensity":"subtle|normal|strong"},
      "scatter_region": "<for placement=grime|ground_scatter ONLY: land|underwater|lower|upward|all; null otherwise>",
      "scatter_system": "<for placement=ground_scatter ONLY: one name from the SCATTER SYSTEMS menu; null otherwise>",
      "scatter_density": "<for placement=ground_scatter ONLY: sparse|normal|dense|very_dense; null otherwise>",
      "placement_mode_reason": "<one short sentence: why that anchoring / region>",
      "n_masters": <int 1-40: how many DISTINCT copies to build; the rest are cheap duplicates. null for delete>,
      "animate_in_place": <true|false: should this thing move ITSELF (wings beating, body
                           swimming) while it stays where it is? You only choose whether,
                           never how -- the engine knows which mechanism each asset has and
                           silently does nothing for assets that have none.>,
      "has_feature": <true|false>,
      "is_organic": <true|false>,
      "factory": "<closest catalog factory name, or null>",
      "factory_reason": "<one short sentence>",
      "spec": "<the build instruction, per E above>",
      "count": <integer or null>,
      "reason": "<one short sentence on why this edit serves the request>"
    }
  ]
}

Rules:
- Only emit edits the request actually asks for. Do not invent extra scenery.
- For "replace", target_factory MUST be one of the factory names in the manifest.
- The manifest lists EVERYTHING of that kind in the scene, not only what the camera sees:
  "dist_min" is the nearest instance to the camera, "in_view" how many are in frame. When
  choosing what to replace, prefer the factory whose instances are CLOSEST to the camera
  (near but just outside the frame is fine). A factory whose nearest instance is farther
  than {replace_max} m would change nothing visible — use action="add" near the camera
  instead (finalize downgrades such a replace to add anyway).
- ★ FISH use placement="fish_school" ★, never hero. The engine has an official boid school
  system (each school is 30-400 fish instanced from 3 master fish, flocking together):
    replace - target_factory = one "target" from the manifest "swarms" list with
              kind="fish_school" (e.g. "FishSchoolFactory(2816589)"). Its masters become
              the requested fish; the school's count, flocking and position stay official.
              ★Prefer a school with "center_in_view" true; a school that is merely
              "near_frame" still has fish inside the picture (a 383-fish school spreads
              several metres, so its centre can sit just outside the edge) and is a far
              better target than adding a second school on top of it★ -- replacing one the
              camera cannot see adds nothing to the picture. Among those prefer the
              nearest, and treat anything past {swarm_max} m as out of reach. One edit
              per school.
    ★ If no school is in view, use add ★: it builds a new school a few metres in front
              of the camera, which is always visible.
    add     - builds a NEW official-style school a few metres in front of the camera.
              "count" = number of schools (1-3, usually 1). "scatter_density"
              (sparse|normal|dense) says how big each school is; the exact number of
              fish is decided by the engine, not by you.
  When the scene already has a fish school within reach, replace it (one per requested fish
  species) rather than adding a new one. anchoring, placement_spec, world_motion are null /
  none for fish_school; set factory to "FishFactory".
- ★ ADDING trees, bushes, boulders/rocks, cacti, kelp, succulents or other plants/rocks
  uses placement="official_scatter" ★ (action="add"). The engine scatters them the way it
  scatters its own forests — official density and minimum spacing over the ground — but
  only in the area around the camera, so they are near and many. You choose ONLY
  "scatter_density": sparse | normal | dense (how thick the stand is); count,
  placement_spec and anchoring are null. Use placement="hero" instead only when the
  request singles out ONE landmark ("a giant ancient oak", "a balanced hoodoo rock"). To
  change the trees/rocks the scene ALREADY has, action="replace" with placement="hero"
  (it keeps every existing position) is still preferred when that factory is within reach.
- ★ The "factories" list only has HERO factories ★ (it is scanned from the `placeholders:`
  collections); fish/bug schools are in "swarms". Grime and ground scatters are real parts
  of the scene even though they do NOT appear there — never conclude "the scene has no
  moss" from the manifest alone.
- A ground_scatter IS now fully actionable (add / replace / delete). When the request asks
  ★FIRST read the SCATTER SYSTEMS menu and see whether one of them already IS the thing
  being asked for.★ If the request is for MANY of something and a menu entry matches it
  structurally, use placement="ground_scatter" with that system. This is NOT limited to
  plants or to ground cover: the menu also carries whole animal groups. A dense swarm of
  jellyfish is the "jellyfish" system (it suspends them 4-8m up in the open water, it does
  not stick them to the seabed); a reef is "coral_reef"; shells and molluscs and urchins
  are all there too. Choosing hero for those means one placeholder per animal, which is
  far more expensive and is not what "a swarm of" / "a field of" / "covered in" asks for.
  So: a carpet of flowers, a grassy field, moss over rocks, pebbles on the ground, a swarm
  of jellyfish, a bed of coral — all ground_scatter. Pick "scatter_system" from the menu,
  choosing the system whose STRUCTURE is closest to what is asked for; use "spec" to say
  how it must LOOK (that is what restyles the scattered asset). Set "scatter_region" to
  land or underwater.
  ★ A menu entry marked "★只能官方外觀★" CANNOT be restyled. ★ Its master asset is
  fixed, so "spec" is ignored for it and the engine ships the official look — a request
  for glowing bubbles routed to "pebbles" silently produces ordinary gravel, with no
  error anywhere. So when the request asks for something the official asset is NOT:
  pick the closest system that IS marked "可換外觀", even if another entry is
  structurally nearer; and if no restylable system is close enough, use placement="hero"
  instead and say why in "placement_mode_reason". Only choose a "★只能官方外觀★" entry
  when the official asset already IS the thing being asked for (fallen leaves ->
  ground_leaves, twigs -> ground_twigs, loose stones -> pebbles).
  Choose hero instead only when the request needs a COUNTABLE, individually readable few
  ("three deer", "a single jellyfish drifting past"), or when the thing must be ANIMATED --
  ground_scatter cannot carry world_motion, so if the edit needs aquatic_bob / air_glide /
  ground_wander it has to be hero. Set "scatter_density" from the WORDS IN THE REQUEST -- "densely covered",
  "carpet of", "a few scattered" -- not from a number:
      sparse      noticeably thinner than this biome normally has
      normal      whatever the engine does by default (use this when the request
                  does not say anything about how much)
      dense       clearly more than default, the cover reads as the subject
      very_dense  as thick as the system will go
  Systems marked "密度固定" in the menu ignore it; pick a level anyway and it is
  dropped. NEVER invent a numeric density -- the unit is not instances per square
  metre and it differs per system, so a number you pick is meaningless.
- "anchoring" says HOW the object meets the surface it rests on. Pick from what the object
  IS, not from this particular scene — the same object anchors the same way in any scene:
    rooted    - grows out of the ground exactly where it already is, and is being replaced
                by something of the same kind: a tree becoming a different tree. Its base
                is buried rather than resting on the surface, so it must NOT be re-settled.
                Only use this for replace, and only when old and new are the same kind of
                thing. When in doubt use upright instead.
    upright   - stands vertically no matter how steep the ground is, because its footprint
                is TOO NARROW for the slope to matter: lamp posts, signs, bollards, a
                standing person. Roughly: the base is under half a metre across.
    tilted    - rests roughly on the ground but leans with it a little: bushes, rocks,
                mushrooms, debris. Use this when neither upright nor conform reads right.
    conform   - follows the slope of whatever it rests on. Use this for ANYTHING whose
                contact points are spread more than about a metre apart, because on a
                slope a rigid object that stays vertical must leave some of them hanging
                in the air: vehicles, benches, fallen logs, crates, rugs, AND four-legged
                animals — a giraffe's hooves span ~2.5m, so on a 24 degree slope standing
                vertical leaves whole legs off the ground. Leaning with the hill is what
                a real animal does; it does not look like falling over.
    afloat    - rests on a visible liquid surface while remaining upright, with a small
                amount of hull immersion: boats, ships, rafts, and other surface
                watercraft. Never use submerged for a vessel that floats on the surface.
    airborne  - does not touch the ground at all; sits some distance above it: flying
                birds, insect swarms, clouds, floating lanterns.
    submerged - sits inside a body of water rather than on the ground: fish, jellyfish.
  For action="replace", do NOT default to however the object being replaced sat. Ask what
  the NEW object needs: replacing trees with motorcycles means anchoring flips from
  rooted to conform, because a motorcycle is not a tree. Trees -> glowing trees stays
  rooted; trees -> giraffes becomes conform, since a giraffe rests ON the ground on four
  widely spaced hooves where a tree grows OUT of it on a single buried base.
  Emit null for grime and ground_scatter — those use "scatter_region" instead.
- "n_masters" is how many DISTINCT copies of the object get built; every other instance is
  a cheap duplicate of one of them. Building is the expensive part, so this trades variety
  against time. Judge it from how the object is READ in the scene, not from how many there
  will be (that is decided later, and a value larger than the instance count simply means
  every copy is distinct):
    ~20  a mass of the same thing the viewer scans as texture, where repetition is what
         would give it away: a forest of trees, a field of grass, a crowd.
    ~5   a handful of individually readable objects: a few animals, parked vehicles,
         scattered crates. Five distinct ones is already hard to spot as repetition.
    1    the object appears once, or all copies genuinely should be identical
         (manufactured, mass-produced things).
  When unsure prefer the smaller number — it only costs variety, never correctness.
- "scatter_region" says WHICH PART of the surface a grime / ground_scatter covers. Grime
  and ground scatters are the same machinery pointed at different surfaces (an asset vs
  the terrain), so they share this vocabulary and have no anchoring:
    land       - the land part of the terrain: grass, ferns, ground mushrooms.
    underwater - the submerged part of the terrain: seaweed, corals, shells.
    lower      - the lower portion of the host object: moss climbing a trunk.
    upward     - the upward-facing parts of the host: lichen on the top of a boulder.
    all        - the whole surface, unfiltered: ivy swallowing an entire tree.
  Emit null for hero.
- "animate_in_place" is motion of the asset's OWN body, separate from "world_motion"
  (which moves the whole thing through the scene). Say true for anything alive that would
  look wrong frozen -- a fish holding still in the water, an insect with motionless wings,
  a bird -- and false for objects that should be still (a parked vehicle, furniture, a
  rock). It costs build time on rigged creatures, so do not ask for it on a distant crowd
  you added only as texture. It needs several frames to be visible; on a single-frame
  still the engine drops it automatically.
- "world_motion" is independent world/root displacement, not a bone animation. Follow the
  controlled WORLD MOTION block below exactly. This field is required for every edit.
- "placement_spec" is required for action="add" + placement="hero" and null otherwise.
  It describes the intended composition; it does not claim semantic geometry was detected.
  Use "single" for exactly one instance, "scatter" for a loose distribution, "field"
  for a dense plant carpet or flower patch, "row" for an ordered line such as houses
  along a river bank, "cluster" for a compact group, and "school" only for multiple
  submerged animals that should stay together in 3D.
  These values select deterministic packing algorithms; they do not request another
  Agent call. For a row, choose its image axis and use orientation="parallel" when the
  objects should face consistently. Name the
  actual visible semantic target: "ice floe" is meaningfully different from "water",
  "dining table top" is more useful than "furniture", and "river bank" is more useful
  than "ground". screen_bounds are normalized render
  coordinates (bottom-left origin) and must enclose the region where all instance centers
  may be proposed; keep them inside x=[0.05,0.95], y=[0.05,0.90]. For conform,
  upright, tilted, rooted, afloat, submerged, and airborne additions,
  deterministic region-map code chooses and retries
  exact 2D surface cells or 3D frustum voxels without another Agent call; deterministic
  validators enforce camera/overlap rules.
- proxy_dimensions_m is the conservative full [length,width,height] in metres of one
  generated object before its mesh exists. Estimate the ordinary physical object, not
  the whole group and not its motion path. Existing catalog placeholders are measured
  directly when available; this estimate sizes the neutral placeholder for a genuinely
  new factory (for example an igloo or submarine). It is also retained as an audit record;
  final generated geometry is revalidated from its actual bounds.
- placement_spec.surface_policy is the machine-enforced support contract shared by the
  initial placement and every later candidate batch. Choose required_surface_types and
  forbidden_surface_types only from SCENE MANIFEST.available_surface_types. That list was
  produced deterministically from this scene registry plus camera raycasts; never invent
  a global type that is absent from it. Standing polar bears should require ice/snow and
  forbid liquid when those types are available; boats/ships/rafts must require liquid and
  use anchoring=afloat. Indoor support surfaces use the same mechanism: a vase on a
  dining table should require tabletop; a laptop or kettle on a kitchen counter should
  require countertop; a plush toy on a bed should require bed_top; a small object on a
  shelf should require shelf; an object on a chair/sofa should require seat; a toy or
  floor lamp on the room floor should require floor. For small indoor objects on raised
  furniture supports (tabletop/countertop/shelf/bed_top/seat), set
  require_full_footprint_support=true. Keep this decision coarse: estimate only whether
  the object belongs on floor, tabletop/countertop, shelf, bed_top, or seat. The executor
  uses a tolerant 3x3 support check and re-settles the real asset so it does not float;
  do not invent fine mesh, edge-distance, or exact plane rules. Leave a type list empty
  only when no
  available type expresses the
  requirement. Do not author mesh-name lists; Composer resolves chosen types to exact
  mesh names. Use max_slope_deg for objects that need a
  reasonably level support (about 20 degrees for a standing four-legged animal). Set
  require_full_footprint_support=true for grounded and afloat rigid objects; this is
  checked geometrically and is not a request to guess surface semantics.
  Every required/forbidden type MUST be copied from available_surface_types.
- Keep every free-text field short and concrete. No markdown, no prose outside the JSON.

""" + pc.as_prompt_block() + "\n\n" + wm.director_prompt_block()


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


def plan(
    prompt: str,
    manifest: dict,
    render_png: Path,
    catalog: dict,
    *,
    correction: str | None = None,
    debug_dir: Path | None = None,
    attempt: int = 1,
) -> dict:
    """(render 圖 + prompt + scene_manifest + FactoryList) → 結構化 edit_plan。"""
    img_b64 = base64.standard_b64encode(render_png.read_bytes()).decode()
    media = "image/png" if render_png.suffix.lower() == ".png" else "image/jpeg"

    manifest_brief = {
        "camera": manifest.get("camera"),
        "factories": manifest.get("factories", []),
        "swarms": manifest.get("swarms", []),
        "raycast_surfaces": manifest.get("raycast_surfaces", []),
        "available_surface_types": manifest.get("available_surface_types", []),
    }
    content = [
        {"type": "text", "text": "RENDER OF THE CURRENT SCENE:"},
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": img_b64}},
        {"type": "text",
         "text": "SCENE MANIFEST (scanned from the .blend — this is what the scene ACTUALLY "
                 "contains; 'total' is how many placeholders exist, 'in_view' how many the "
                 "camera currently sees):\n\n"
                 + json.dumps(manifest_brief, ensure_ascii=False, indent=2)},
        {"type": "text",
         "text": "SCATTER SYSTEMS available for THIS scene's surfaces (for "
                 "placement='ground_scatter'). These are infinigen's own terrain "
                 "scatter systems; picking one means the whole ground cover is placed "
                 "deterministically by the engine, with no per-instance placeholders:\n\n"
                 + sc.catalog_lines(
                     [entry.get("type")
                      for entry in manifest.get("available_surface_types", [])])},
        {"type": "text", "text": "FACTORY CATALOG (FactoryList.txt):\n\n"
                                 + FACTORYLIST.read_text(encoding="utf-8")},
        {"type": "text", "text": f"EDIT REQUEST:\n\n{prompt}\n\nReturn the JSON now."},
    ]
    if correction:
        content.append({
            "type": "text",
            "text": (
                "YOUR PREVIOUS OUTPUT FAILED DETERMINISTIC VALIDATION:\n"
                f"{correction}\n\n"
                "Return one complete, valid JSON object only. Do not omit any "
                "requested item. Keep free-text fields concise."
            ),
        })
    resp = _claude().messages.create(
        model=OPUS_MODEL, max_tokens=DIRECTOR_MAX_TOKENS,
        system=PLAN_SYSTEM.replace("{replace_max}", f"{HERO_REPLACE_MAX_DIST_M:g}")
                          .replace("{swarm_max}", f"{SWARM_REPLACE_MAX_DIST_M:g}"),
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"raw_response_attempt_{attempt}.txt").write_text(
            text, encoding="utf-8")
    return _parse_json_response(text, label="director")


_VAGUE_SMALL_COUNT_RE = re.compile(
    r"\b(?:a few|few|some|several|a handful of)\b|幾(?:棵|個|隻|朵|台|株)?|少數|一些",
    re.IGNORECASE,
)
_EXPLICIT_ALL_RE = re.compile(
    r"\b(?:all|every|entire)\b|全部|所有|整片|每一", re.IGNORECASE)


def _normalize_requested_count(edit: dict, prompt: str) -> int | None:
    if edit.get("action") == "replace":
        # User wording is authoritative.  The model may eagerly turn "幾棵 /
        # a few" into 5, which made identical prompts non-deterministic.
        if _EXPLICIT_ALL_RE.search(prompt):
            return None
        if _VAGUE_SMALL_COUNT_RE.search(prompt):
            return 3
    raw = edit.get("count")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        pass
    if edit.get("action") != "replace":
        return None
    text = f"{prompt} {edit.get('name', '')} {edit.get('reason', '')}"
    if _EXPLICIT_ALL_RE.search(text):
        return None
    # ``null`` used to mean all-in-view.  That is unsafe for vague-small wording:
    # "a few trees" became 106 replacements in the P0 smoke scene.
    return 3 if _VAGUE_SMALL_COUNT_RE.search(text) else None


def finalize(raw: dict, manifest: dict, catalog: dict, prompt: str = "") -> dict:
    """套 routing table + 驗證 LLM 的判斷(factory 存在？target 真的在場景裡？)。
    ★routing 是確定性的★——LLM 只給三個軸，路由本身是查表，不讓 LLM 直接選 generator。"""
    scene_factories = {f["factory"] for f in manifest.get("factories", [])}
    by_name = {f["factory"]: f for f in manifest.get("factories", [])}
    used_swarms = set()                # 每個魚群只能被一筆 replace 認領

    edits = []
    for e in raw.get("edits", []):
        fac = e.get("factory")
        if fac and (fac not in catalog or catalog[fac]["role"] not in ("ENTRY", "ELEMENT")):
            print(f"  [warn] '{fac}' 不是 catalog 裡的 ENTRY/ELEMENT — 當作 no factory")
            fac = None

        action = e.get("action", "add")
        target = e.get("target_factory")

        # ---- PLACEMENT: LLM 判斷，但用原始碼查表覆核（同 factory 的驗證邏輯）----
        llm_place = e.get("placement")
        derived, derived_why = pc.classify(fac, f"{e.get('name','')} {e.get('id','')}")
        if llm_place not in pc.CLASSES:
            placement, place_why = derived, f"(derived) {derived_why}"
            if llm_place is not None:
                print(f"  [warn] {e.get('id')}: placement={llm_place!r} 無效 → 用查表結果 {derived!r}")
        elif llm_place == "official_scatter" and osc.group_of(fac or e.get("factory")):
            # TreeFactory/BushFactory/... 在 HERO_FACTORIES 裡，官方本來就是整片撒點，
            # 只有 add 走 hero grid 才只剩 1-3 棵(2026-09-11)
            placement, place_why = llm_place, e.get("placement_reason", "")
        elif llm_place == "fish_school" and fac and "Fish" in fac:
            # FishFactory 在 HERO_FACTORIES 裡，但官方魚群的母本也是它 ⇒ 不能被
            # 「factory 身分為準」拉回 hero(2026-09-11，否則魚永遠只能 add 3-5 條)
            placement, place_why = llm_place, e.get("placement_reason", "")
        elif llm_place != derived and fac in pc.HERO_FACTORIES:
            # factory 身分是硬證據，勝過 LLM 的判斷
            placement = derived
            place_why = (f"(corrected) LLM said {llm_place!r} but {fac} is populated via "
                         f"populate_all -> hero")
            print(f"  [warn] {e.get('id')}: placement {llm_place!r} → {derived!r}（factory 身分為準）")
        else:
            placement = llm_place
            place_why = e.get("placement_reason", "")

        # ---- ACTION 可行性：ground_scatter 在 populate 階段根本動不到 ----
        ok, why = pc.action_ok(placement, action)
        blocked = None
        if not ok:
            blocked = f"action='{action}' 對 placement='{placement}' 不可行：{why}"
            print(f"  [warn] {e.get('id')}: {blocked}")

        # ---- replace/delete 的 target 檢查 ----
        # ★ manifest 只含 HERO ★ —— 只有 hero 才該用「不在 manifest 就降級」這條規則。
        # grime/ground_scatter 本來就不會出現在 placeholders: 裡，降級是錯的。
        if action in ("replace", "delete") and placement == "hero":
            if target not in scene_factories:
                print(f"  [warn] {e.get('id')}: target_factory='{target}' 不在場景 manifest 裡 "
                      f"→ 降級成 action=add")
                action, target = "add", None
            elif action == "replace" and not by_name[target].get("in_view"):
                # ★看 in_view，不是 dist_min★(2026-09-12)：dist_min 含★相機背後★的
                #   實例(canyon dist_min 7.95m 但 nearest_in_view 65m)，用看不到的
                #   東西授權 replace，換完畫面毫無變化。畫面內一個都沒有就加在相機前。
                print(f"  [warn] {e.get('id')}: {target} 畫面內 0 個"
                      f"(total={by_name[target].get('total')}, "
                      f"nearest_in_view={by_name[target].get('nearest_in_view')}m)，"
                      f"換了看不到 → 降級成 action=add")
                action, target = "add", None

        grime_host = e.get("grime_host")
        if placement == "grime":
            if grime_host not in pc.GRIME_HOSTS:
                print(f"  [warn] {e.get('id')}: grime_host={grime_host!r} 無效 → 預設 'trees'")
                grime_host = "trees"
        else:
            grime_host = None

        has_feature = bool(e.get("has_feature"))
        is_organic = bool(e.get("is_organic"))
        cat_key = route(has_feature, is_organic, fac is not None)
        num, label, generator = ROUTES[cat_key]

        # ---- ANCHORING: 只對 hero 有意義；無效值退回保守預設而不是猜 ----
        #   grime/ground_scatter 有自己的擺放系統(grime 是套在宿主物件表面的 GN
        #   修改器，不是 placeholder)，所以這裡一律 None。
        anchoring = scatter_region = anchor_why = None
        n_masters = None
        scatter_system = None
        scatter_density = None
        appearance_official_only = False
        if placement in ("grime", "ground_scatter") and action != "delete":
            r = e.get("scatter_region")
            if r in SCATTER_REGIONS:
                scatter_region, anchor_why = r, e.get("placement_mode_reason", "")
            else:
                scatter_region = DEFAULT_SCATTER_REGION[placement]
                anchor_why = f"(default) LLM 給的 scatter_region={r!r} 無效"
                print(f"  [warn] {e.get('id')}: scatter_region={r!r} 無效 → 用預設 {scatter_region!r}")
        # ---- SCATTER SYSTEM：ground_scatter 撒的是哪一套 infinigen 原生系統 ----
        #   ★這是 add 能不能成立的關鍵★ —— 執行層(apply_plan op="create")靠這個名字
        #   查 scatter_catalog 決定 import 哪個模組。給錯或沒給就沒有東西可建，
        #   所以這裡驗一次，讓它在 director 就失敗而不是拖到 step 8。
        if placement == "ground_scatter" and action != "delete":
            # ★官方當 grime 的不能走 ground_scatter★(2026-09-09)
            #   舊 plan 或模型硬選都可能出現。這裡明確擋下並指路，
            #   不要讓它撒到地形上(那不是 infinigen 的用法)。
            if sc.is_official_grime(e.get("scatter_system")):
                ok = False
                blocked = (
                    f"scatter_system={e.get('scatter_system')!r} 在 infinigen 裡是"
                    f" grime(貼在樹/岩石等資產表面，用 scatter_lower/scatter_upward)，"
                    f"不是地形 scatter ⇒ 應該用 placement=\"grime\" + "
                    f"scatter_region=lower|upward，不是 ground_scatter")
                print(f"  [warn] {e.get('id')}: {blocked}")
            spec = sc.get(e.get("scatter_system"))
            if spec is None:
                if action == "add":
                    ok = False
                    blocked = (f"scatter_system={e.get('scatter_system')!r} 不在 catalog 裡"
                               f"(可選: {', '.join(sorted(sc.SCATTERS))})")
                    print(f"  [warn] {e.get('id')}: {blocked}")
                else:
                    print(f"  [warn] {e.get('id')}: scatter_system 無效，"
                          f"replace 走既有 scatter 名稱比對")
            else:
                scatter_system = str(e.get("scatter_system")).strip().lower()
                # ★生成端要造的是「這套 scatter 的母本 factory」★ —— 不是 director
                #   自己從 FactoryList 挑的那個。花海的母本是 FlowerPlantFactory
                #   (grassland/flowerplant.py)，不是 FlowerFactory；造錯了 overlay
                #   patch 上去會對不到符號，外觀替換靜靜失效。
                symbol = spec["factory_symbol"]
                if symbol and fac != symbol:
                    print(f"  [scatter] {e.get('id')}: factory {fac!r} → {symbol!r} "
                          f"(scatter_system={scatter_system} 的母本)")
                    fac = symbol
                if not symbol:
                    # ⚠️ ★不要在這裡把 fac 設成 None★ —— route() 在上面已經用
                    #    `fac is not None` 決定了 category / generator，事後改會讓
                    #    兩者對不上。執行層自己會依 factory_symbol 決定要不要換
                    #    外觀，沒有 symbol 就用官方長相，這裡不必動 fac。
                    #
                    # ★但這件事必須留在 plan 裡，不能只印一行就過去★(2026-09-09)：
                    #   6 支沒有 factory_symbol(chopped_trees / decorative_plants /
                    #   ground_leaves / ground_twigs / monocots / pebbles)。director
                    #   若為了「結構最接近」挑中它們(例如「發光泡泡」挑 pebbles)，
                    #   spec 會被★完全忽略★而撒出官方碎石，全程沒有任何錯誤。
                    #   寫成欄位之後，下游與 report 才看得到「外觀沒有照要求做」。
                    appearance_official_only = True
                    print(f"  [scatter] {e.get('id')}: ★{scatter_system} 不支援換外觀★，"
                          f"spec 會被忽略，將使用官方預設長相 "
                          f"(要指定外觀請改挑「可換外觀」的系統或改用 hero)")
                # ★密度語意等級★(2026-09-08) —— 只收四個詞，不收數字。
                #   單位反直覺(不是株/m²，而且每支語意不同：per_area / per_volume /
                #   clipped)，讓 LLM 猜數字的結果是 0907 寫 133.312、0908 直接不寫。
                #   換算表在 scatter_catalog.density_for()，normal == 官方預設。
                lv = str(e.get("scatter_density") or "").strip().lower()
                if lv not in sc.DENSITY_LEVELS:
                    if lv:
                        print(f"  [warn] {e.get('id')}: scatter_density={lv!r} 無效 "
                              f"→ 用 {sc.DEFAULT_DENSITY_LEVEL}")
                    lv = sc.DEFAULT_DENSITY_LEVEL
                scatter_density = lv
                if not sc.density_is_tunable(scatter_system):
                    print(f"  [scatter] {e.get('id')}: {scatter_system} 的 apply() "
                          f"不吃 density(或收了是死參數) → 密度走官方預設")
        elif placement == "hero" and action != "delete":
            a = e.get("anchoring")
            if a in ANCHORINGS:
                anchoring, anchor_why = a, e.get("placement_mode_reason", "")
            else:
                anchoring = DEFAULT_ANCHORING
                anchor_why = f"(default) LLM 給的 anchoring={a!r} 無效"
                print(f"  [warn] {e.get('id')}: anchoring={a!r} 無效 → 用預設 {DEFAULT_ANCHORING!r}")

        # ★母本池大小★：只影響「建幾個相異個體」，給錯不會壞畫面(大於實例數 = 等於沒開)，
        #   所以無效值退到保守的預設而不是擋下來。
        if placement == "hero" and action != "delete":
            try:
                n_masters = int(e["n_masters"])
                if not (1 <= n_masters <= MAX_N_MASTERS):
                    raise ValueError(n_masters)
            except (KeyError, TypeError, ValueError):
                n_masters = DEFAULT_N_MASTERS
                print(f"  [warn] {e.get('id')}: n_masters={e.get('n_masters')!r} 無效 "
                      f"→ 用預設 {DEFAULT_N_MASTERS}")

        # ---- OFFICIAL SCATTER：植物/石頭的 add 走官方撒點(相機附近) ----
        if placement == "official_scatter":
            if action != "add":
                # replace/delete 沿用 hero 的既有路徑(保留所有原位置)
                placement = "hero"
                print(f"  [warn] {e.get('id')}: official_scatter 只負責 add → "
                      f"placement=hero action={action}")
            elif not osc.group_of(fac or e.get("factory")):
                placement = "hero"
                print(f"  [warn] {e.get('id')}: {fac or e.get('factory')!r} 不是官方撒點的"
                      f"植物/石頭 → 改走 hero")
            else:
                anchoring, anchor_why = None, "official placeholder scatter"
                scatter_density = osc.level_of(e.get("scatter_density"))
                n_masters = 5

        # ---- FISH SCHOOL：官方 boid 魚群的 replace(換母本) / add(相機附近新建一群) ----
        swarm_stats = None
        if placement == "fish_school":
            anchoring, anchor_why = None, "official boid school"
            n_masters = 3                     # 官方每群 3 條母本
            if action == "add":
                scatter_density = osc.level_of(e.get("scatter_density"))
            if action == "replace":
                schools = [s for s in manifest.get("swarms", [])
                           if s.get("kind") == "fish_school"
                           and s["target"] not in used_swarms]
                # ★看得到最優先，但「中心在框內」不是唯一標準★(2026-09-12 修正)
                #   0911 我改成只收 center_in_view，結果 0912 coral_reef 三群的中心
                #   全在右緣外一點點(x≈1.01-1.08)⇒ 全被拒 ⇒ 兩條魚都變成 add ⇒
                #   ★憑空多出兩群魚跟原有的互相穿插★(使用者回報「明明左上角就有魚」)。
                #   ⚠️ 一群 383 隻的魚散開好幾公尺，★中心在框外不代表沒有魚在框內★。
                #   ⇒ 中心在框內的優先(近的先)，其次是貼著畫面邊緣的(near_frame)，
                #     兩種都沒有才新建一群。
                def _by_distance(rows):
                    return sorted(rows, key=lambda s: s.get("distance_m") or 1e9)
                schools = (
                    _by_distance([s for s in schools if s.get("center_in_view")])
                    + _by_distance([s for s in schools
                                    if not s.get("center_in_view")
                                    and s.get("near_frame")]))
                pick = next((s for s in schools if s["target"] == target), None)
                if pick is None and schools:
                    pick = schools[0]
                    print(f"  [warn] {e.get('id')}: target_factory={target!r} 不是畫面內"
                          f"可用的魚群 → 改用 {pick['target']}"
                          f"({pick.get('distance_m')}m，中心在畫面內)")
                if pick is None or pick["distance_m"] > SWARM_REPLACE_MAX_DIST_M:
                    print(f"  [warn] {e.get('id')}: 沒有畫面內、{SWARM_REPLACE_MAX_DIST_M:g}m "
                          f"以內的魚群 → 改成 action=add，在相機正前方新建一群")
                    action, target = "add", None
                else:
                    target = pick["target"]
                    used_swarms.add(target)
                    swarm_stats = {k: pick.get(k) for k in (
                        "distance_m", "particle_count", "center_in_view", "n_masters")}

        # ---- WORLD MOTION：與骨架動畫分離；受控 v3 只對 add + 合法 habitat 開放 ----
        world_motion, world_motion_reason = wm.normalize_director_request(
            e.get("world_motion"), action=action, anchoring=anchoring)
        if str(world_motion_reason).startswith("fallback"):
            print(f"  [warn] {e.get('id')}: {world_motion_reason}")

        # ★Agent 只決定「要不要在原地動」，不決定怎麼動★(2026-09-11)
        #   怎麼動仍然是查表：有 animation_mode 的走 constructor(魚 idle、四足 run)，
        #   沒有 armature 但 GN 有 Scene Time 的走 gn_scene_time(蝴蝶/蜜蜂翅膀)，
        #   兩者都沒有的就是 none —— LLM 說要動也不會憑空生出能力。
        animate_in_place = e.get("animate_in_place")
        if animate_in_place is None:
            animate_in_place = True          # 舊行為：有就繼承
        local_animation = (
            la.director_request(fac, enabled=bool(animate_in_place))
            if action != "delete" else {"policy": "none", "mode": None})

        # ★add 要有一個「宿主 factory 名」★(2026-08-06 端到端抓到；2026-08-31
        #   擴充到全 procedural pipeline)
        #   憑空造的東西(雪橇、機器人)在 catalog 裡沒有對應 factory ⇒ fac=None，
        #   於是 composer 說「an 'add' op requires a factory name」把整筆丟掉。
        #   但 apply_plan 的 add ★自己建中性 box★、collection 叫 placeholders:<名字>(seed)
        #   (apply_plan.py:373 註解：「全新 factory 給任何值都可以」)，
        #   後續 overlay route shim 可以把 procedural 自動選到的真 factory 包成這個名字。
        #   ⇒ 缺的只是一個名字。
        #   ★用合成名不要借既有名★ —— 借 BoulderFactory 的話 overlay 會讓場景裡
        #   ★所有★官方 Boulder 也變成雪橇(overlay 遮蔽整個 package)。
        host_factory = None
        if action == "add" and generator == FORMAL_ASSET_GENERATOR and not fac:
            slug = re.sub(r"[^0-9a-zA-Z]+", " ", str(e.get("name") or "asset")).title()
            host_factory = re.sub(r"\s+", "", slug) + "Factory"
            print(f"  [host] {e.get('id')}: 無對應 catalog factory ⇒ "
                  f"合成宿主名 {host_factory}")

        requested_count = _normalize_requested_count(e, prompt)
        if action == "add" and requested_count is None:
            requested_count = 1
            print(f"  [warn] {e.get('id')}: add count 無效 → 用 1")
        if placement == "official_scatter":
            requested_count = None            # 數量由官方密度 × 相機附近的地面決定
        if placement == "fish_school":
            # add 的 count = 幾群(每群幾條由引擎照官方算)；replace 沒有 count
            requested_count = (max(1, min(int(requested_count or 1), MAX_NEW_FISH_SCHOOLS))
                               if action == "add" else None)
        placement_spec = None
        if action == "add" and placement == "hero":
            raw_placement_spec = (
                e.get("placement_spec")
                if isinstance(e.get("placement_spec"), dict) else {})
            dense_count = plc.dense_field_instance_count(
                count=requested_count,
                factory=fac,
                code_from=e.get("code_from") or e.get("id"),
                semantic_target=str(
                    raw_placement_spec.get("semantic_target") or "").strip(),
                arrangement=str(raw_placement_spec.get("arrangement") or ""),
            )
            if dense_count != requested_count:
                print(
                    f"  [field] {e.get('id')}: dense field count "
                    f"{requested_count} → {dense_count}")
                requested_count = dense_count
            try:
                placement_spec = plc.normalize_placement_spec(
                    raw_placement_spec, count=requested_count,
                    anchoring=anchoring, factory=fac,
                    code_from=e.get("code_from") or e.get("id"))
            except ValueError as error:
                semantic_target = str(
                    raw_placement_spec.get("semantic_target") or "").strip()
                fallback = {"arrangement": "single" if requested_count == 1
                            else "field" if plc.uses_dense_field_request(
                                factory=fac,
                                code_from=e.get("code_from") or e.get("id"),
                                semantic_target=semantic_target,
                                count=requested_count,
                            ) else "scatter"}
                placement_spec = plc.normalize_placement_spec(
                    fallback, count=requested_count, anchoring=anchoring,
                    factory=fac, code_from=e.get("code_from") or e.get("id"))
                print(f"  [warn] {e.get('id')}: placement_spec 無效 ({error}) → "
                      f"用 {placement_spec['arrangement']}")
            if (placement_spec.get("placement_mode") != "back_to_wall"
                    and _infer_back_to_wall_mode(e)):
                placement_spec["placement_mode"] = "back_to_wall"
                print(
                    f"  [mode-infer] {e.get('id')}: "
                    "wall text → back_to_wall")
            available_surface_types = [
                x.get("type") for x in manifest.get(
                    "available_surface_types", [])
            ]
            surface_policy = placement_spec["surface_policy"]
            if not surface_policy.get("required_surface_types"):
                inferred_surface_type = _infer_missing_indoor_surface_type(
                    e, available_surface_types)
                if inferred_surface_type is not None:
                    surface_policy = dict(surface_policy)
                    surface_policy["required_surface_types"] = [
                        inferred_surface_type]
                    placement_spec["surface_policy"] = surface_policy
                    print(
                        f"  [surface-infer] {e.get('id')}: "
                        f"empty policy → {inferred_surface_type}")
            if placement_spec.get("proxy_dimensions_m") is None:
                inferred_dimensions = _infer_missing_indoor_proxy_dimensions(e)
                if inferred_dimensions is not None:
                    placement_spec["proxy_dimensions_m"] = inferred_dimensions
                    print(
                        f"  [proxy-infer] {e.get('id')}: "
                        f"missing proxy → {inferred_dimensions}")
            # Unlike a cosmetic layout typo, inventing a machine surface name
            # must stop at the Director boundary; silently deleting it would
            # change the semantic contract.
            placement_spec["surface_policy"] = plc.validate_surface_policy_types(
                placement_spec["surface_policy"],
                available_surface_types)
            # Backward-compatible explicit-name policies remain legal only for
            # names independently marked selectable by the scanner.
            placement_spec["surface_policy"] = plc.validate_surface_policy_names(
                placement_spec["surface_policy"],
                [x.get("name") for x in manifest.get("raycast_surfaces", [])
                 if x.get("policy_selectable")])

        entry = {
            "id": e.get("id"),
            "name": e.get("name"),
            "action": action,
            "target_factory": target,
            "host_factory": host_factory,
            "placement": placement,
            "placement_reason": place_why,
            "grime_host": grime_host,
            "anchoring": anchoring,
            "placement_spec": placement_spec,
            "world_motion": world_motion,
            "world_motion_reason": world_motion_reason,
            "local_animation": local_animation,
            "n_masters": n_masters,
            "scatter_system": scatter_system,
            "scatter_density": scatter_density,
            "scatter_region": scatter_region,
            # 這支 scatter 不吃 spec，撒出來一定是官方長相(見上面的說明)
            "appearance_official_only": appearance_official_only,
            "placement_mode_reason": anchor_why,
            "feasible": ok,
            "blocked_reason": blocked,
            "generator": None if action == "delete" else generator,
            "category": None if action == "delete" else {"num": num, "key": cat_key, "label": label},
            "has_feature": has_feature,
            "is_organic": is_organic,
            "factory": fac,
            "factory_reason": e.get("factory_reason", ""),
            "factory_file": catalog[fac]["file"] if fac else None,
            "spec": e.get("spec", ""),
            "count": requested_count,
            "reason": e.get("reason", ""),
        }
        if swarm_stats:
            entry["swarm_stats"] = swarm_stats
        # replace 的 target 附上場景實測數據，下游 replacer 直接可用(算一次兩處用)
        if target and target in by_name:
            t = by_name[target]
            entry["scene_stats"] = {"total": t["total"], "in_view": t["in_view"],
                                    "seeds": t["seeds"], "nearest_in_view": t["nearest_in_view"]}
        edits.append(entry)

    return {"scene_reading": raw.get("scene_reading", ""), "edits": edits}


def apply_library_routing(edits: list[dict], *, log=print) -> dict:
    """對每個要生成的 edit 多問一句：庫裡已經有了嗎？

    ★為什麼放在 director★：這裡本來就在做「場景裡有沒有 → replace/add」與
      「路由到哪個 generator」的查表判斷。「庫裡有沒有 → 用/改/生」是同一類
      決定，而且同樣不需要 Composer 知道 —— 被 REUSE 的項目在 plan 裡跟新生成的
      長得一模一樣。

    只加欄位，★不改任何既有欄位★：action / factory / generator / anchoring 全部
    保持原樣，庫不見了也還是一份合法的 plan。
    """
    sys.path.insert(0, str(ROOT))
    from asset_generator.library import librarian as lb

    # ★ground_scatter 不能用 factory 當庫存查詢的 key★(2026-09-08)
    #   scatter edit 的 factory 是 catalog 改正過的★母本符號★(flowers →
    #   FlowerPlantFactory)，那是「插進哪個 scatter 機制」的識別碼，不是外觀的
    #   識別碼。資產庫是按「生成時的 factory」建索引的(庫裡的向日葵是
    #   FlowerFactory)，兩者本來就不會相等 ⇒ lookup 的 factory 硬過濾把唯一
    #   正確的候選擋掉，判成 FRESH 又生一次。
    #   而 scatter 換外觀★不要求類名相同★(apply_plan 是 setattr 到模組符號上，
    #   0907 的 smoke 就是拿 FlowerFactory 的 overlay 塞進 FlowerPlantFactory)，
    #   所以這裡放掉硬過濾，讓確定性 prefilter 用關鍵字/字面相似度排序，
    #   最終語意判斷仍由 router 做，且它只能從我們給的候選裡挑。
    items = []
    for e in edits:
        if not e.get("feasible", True):
            continue
        if e.get("generator") not in ("ProceduralGenerator", "BlenderGenerator"):
            continue
        fac = e.get("factory") or e.get("host_factory")
        if e.get("placement") == "ground_scatter":
            fac = None
        items.append({"id": e.get("id"), "factory": fac,
                      "request": e.get("spec") or e.get("name") or ""})
    if not items:
        return {}

    routes = lb.route_requests(items, log=log)
    for e in edits:
        r = routes.get(e.get("id"))
        if not r:
            continue
        e["library_verdict"] = r["verdict"]
        e["library_variant"] = r["variant_id"]
        e["library_why"] = r["why"]
    n = {"REUSE": 0, "ADAPT": 0, "FRESH": 0}
    for r in routes.values():
        n[r["verdict"]] = n.get(r["verdict"], 0) + 1
    log(f"[director] 資產庫：REUSE {n.get('REUSE',0)} / "
        f"ADAPT {n.get('ADAPT',0)} / FRESH {n.get('FRESH',0)}")
    return routes


def main():
    ap = argparse.ArgumentParser(
        description="Director: input/<scene>/ (blend + render 圖 + prompt) -> 結構化 edit_plan")
    ap.add_argument("scene", help="場景名(forest) 或 input 資料夾路徑(input/forest)")
    ap.add_argument("--prompt", default=None, help="覆寫 prompt.txt")
    ap.add_argument("-o", "--out", default=None,
                    help="輸出目錄(預設 output/<scene>/1_director)")
    ap.add_argument("--project", help="專案名(預設用輸入資料夾名)")
    ap.add_argument("--name-project", action="store_true",
                    help="改由 project_namer 從 prompt 摘要專案名")
    ap.add_argument("--manifest", default=None, help="用已掃好的 scene_manifest.json，跳過掃描")
    # ★預設關閉★：接線期間不改變既有 pipeline 的行為。BI_LIBRARY=1 或 --library
    # 才會查庫；--no-library 一律能把它關掉。
    ap.add_argument("--library", dest="library", action="store_true", default=None,
                    help="查擴充資產庫，決定 REUSE / ADAPT / FRESH")
    ap.add_argument("--no-library", dest="library", action="store_false",
                    help="不查庫(預設)")
    args = ap.parse_args()
    use_library = (os.environ.get("BI_LIBRARY", "0") == "1"
                   if args.library is None else args.library)

    scene_dir, blend, render, prompt = resolve_input(args.scene)
    if args.prompt:
        prompt = args.prompt
    if not prompt:
        raise SystemExit(f"沒有 prompt — 在 {scene_dir}/prompt.txt 寫一句話，或用 --prompt 給")

    # ★專案名在這裡定案一次★，之後每個 stage 都寫進同一個 run 底下。
    #   輸入資料夾名(input/forest)已經是個好名字，所以它是預設；只有 --project
    #   或明確要求時才叫 agent 從 prompt 重新摘要一個。
    project = args.project or (name_project(prompt) if args.name_project else scene_dir.name)
    if args.out:
        # An explicit stage directory means the caller already owns the output
        # layout (notably pipeline/run_e2e.sh).  Older code still called
        # init_project(project, ...), leaving an unrelated
        # output/<case>/project.json while every real result lived elsewhere.
        # Keep the metadata beside the caller-owned stages instead.
        out_dir = Path(args.out).resolve()
        run_root = out_dir.parent
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "project.json").write_text(json.dumps({
            "project": project,
            "input": prompt,
            "created": datetime.now().isoformat(timespec="seconds"),
            "output_root": str(run_root),
            "layout": "caller_owned",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        run_root = init_project(project, prompt)
        out_dir = stage_dir(project, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[director] project: {project}   ->  {run_root}")
    print(f"[director] input : {scene_dir}")
    print(f"[director]   blend={blend.name}  render={render.name}")
    print(f"[director] output: {out_dir}")

    # 1) 場景感知(確定性)：掃 blend → manifest
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        print(f"[director] 用指定的 manifest: {args.manifest}")
    else:
        manifest = scan_scene(blend, out_dir / "scene_manifest.json")

    print(f"[director] 場景裡有 {len(manifest.get('factories', []))} 種 factory:")
    for f in manifest.get("factories", []):
        print(f"    {f['factory']:28} total={f['total']:5}  in_view={f['in_view']:4}")

    # 2) 規劃(agent)：render 圖 + prompt + manifest + FactoryList → edit_plan
    catalog = load_catalog()
    print(f"[director] 規劃中 ({OPUS_MODEL}) ... prompt: {prompt!r}")
    correction = None
    result = None
    for attempt in range(1, 4):
        try:
            raw = plan(
                prompt,
                manifest,
                render,
                catalog,
                correction=correction,
                debug_dir=out_dir,
                attempt=attempt,
            )
            result = finalize(raw, manifest, catalog, prompt)
            if attempt > 1:
                print(f"[director] corrected invalid plan on attempt {attempt}")
            break
        except (TypeError, ValueError) as error:
            correction = str(error)
            print(f"[director] invalid plan attempt {attempt}/3: {error}")
    if result is None:
        raise SystemExit(
            f"Director failed deterministic contract after 3 attempts: {correction}")

    if use_library:
        try:
            apply_library_routing(result["edits"])
        except Exception as ex:
            # 查庫失敗★不可以擋住整條 pipeline★ —— 它是加速手段，不是必要步驟。
            # 沒有 verdict 的 plan 下游一律當 FRESH 處理，行為與關掉庫完全相同。
            print(f"[director] ⚠️ 資產庫查詢失敗，全部當 FRESH：{type(ex).__name__}: {ex}")
    else:
        print("[director] 資產庫：未啟用(BI_LIBRARY=1 或 --library 開啟)")

    plan_json = {
        "input": {"scene_dir": str(scene_dir), "blend": str(blend),
                  "render": str(render), "prompt": prompt},
        "scene_reading": result["scene_reading"],
        "edits": result["edits"],
    }
    (out_dir / "edit_plan.json").write_text(
        json.dumps(plan_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n場景解讀: {result['scene_reading']}")
    print("\n=== EDIT PLAN ===")
    n_blocked = 0
    for e in result["edits"]:
        gen = e["generator"] or "-"
        num = e["category"]["num"] if e["category"] else "-"
        tgt = e["target_factory"] or e["factory"] or "-"
        place = e["placement"]
        if e["placement"] == "grime" and e.get("grime_host"):
            place += f"@{e['grime_host']}"
        elif e.get("anchoring") or e.get("scatter_region"):
            place += f"/{e.get('anchoring') or e['scatter_region']}"
        mark = "" if e["feasible"] else "  ⛔"
        print(f"  [{num}] {e['action']:8} {e['id']:22} {gen:19} "
              f"{place:16} factory={e['factory']}  target={tgt}  x{e['count']}{mark}")
        print(f"      spec: {e['spec'][:110]}")
        if not e["feasible"]:
            n_blocked += 1
            print(f"      ⛔ {e['blocked_reason']}")
    if n_blocked:
        print(f"\n  ⚠ {n_blocked} 個 edit 在 populate 階段做不到（見上方 ⛔）——"
              f"它們需要改 coarse 重跑，下游 replacer 接不住。")
    print(f"\n[director] edit_plan.json -> {out_dir}")


if __name__ == "__main__":
    main()

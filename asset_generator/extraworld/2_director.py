"""Director — Agent 2 of the ExtraWorld pipeline.

Reads Agent 1's scene plan (output/<scene>/1_scene_planner/scene_plan.json) and, for
every object type, DECIDES how it will be built. It is a pure decision engine plus a
reference-image generator; it does not build any 3D asset itself.

For each object type it determines three axes and routes them to a generator:

  A. FEATURE — is the object's exact appearance precisely described/shown (a stated or
     visible colour, pattern, material, distinctive part), so it must be REPRODUCED
     (has_feature=true), or is only its kind given with no concrete look (has_feature=false)?
  B. ORGANIC — is it a natural/organic/anatomical thing, or a manufactured hard-surface one?
  C. FACTORY — the closest matching Infinigen [ENTRY] factory in FactoryList.txt, or none.

Routing table (feature x organic x factory-membership):

  All generated assets now route to ProceduralGenerator. The feature/organic/factory
  category is kept for audit labels, but generator choice is no longer a branch:
  ProceduralGenerator rewrites the closest available Infinigen starter factory, and can
  auto-pick that starter when the catalog has no clean direct match.

Then, per object, it produces:
  - reference images (single 3/4 view + a multi-view sheet) for each generated object —
    cut from the real scene image when there is one, else generated from the description;
    these refs guide ProceduralGenerator even when the object has no close factory match.
  - the instance grouping: how many to make, and whether the copies share one seed
    (identical) or get distinct seeds (varied).

All decisions + paths are written to output/<scene>/2_director/generation_list.json, with
per-object folders holding the reference images.

    python 2_director.py minimal_dining_corner
    python 2_director.py output/minimal_dining_corner            # scene dir
    python 2_director.py path/to/1_scene_planner/scene_plan.json
"""
import argparse
import base64
import io
import json
import os
import random
import re
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
# ★2026-07-30 搬進 BeyondInfinigen/asset_generator/extraworld/ 時加★
#   原本靠 HERE.parent == Animal/ 找 .env；搬家後 HERE.parent = asset_generator/。
REPO_ROOT = HERE.parents[1]
FACTORYLIST = HERE / "FactoryList.txt"
SCENE_CATALOG = HERE / "scene_catalog.json"   # env-layer (gin) producible things

OPUS_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image-preview"

# category key -> (number, human label, generator)
FORMAL_ASSET_GENERATOR = "ProceduralGenerator"
ROUTES = {
    "featured_organic":       (1, "featured organic",              FORMAL_ASSET_GENERATOR),
    "featured_geometric":     (2, "featured geometric",            FORMAL_ASSET_GENERATOR),
    "generic_not_in_factory": (3, "generic, not in FactoryList",   FORMAL_ASSET_GENERATOR),
    "generic_in_factory":     (4, "generic, in FactoryList",       FORMAL_ASSET_GENERATOR),
}


def route(has_feature: bool, is_organic: bool, in_factory: bool) -> str:
    if has_feature:
        return "featured_organic" if is_organic else "featured_geometric"
    return "generic_in_factory" if in_factory else "generic_not_in_factory"


# --------------------------------------------------------------------------------- env
def _env() -> dict:
    merged: dict = {}
    # ★2026-07-30★ HERE.parent 在搬家後不再是 Animal/ ⇒ 改用 ANIMAL_ROOT。
    #   也吃 BeyondInfinigen/.env(這個 repo 自己那份)。
    for envfile in (REPO_ROOT / ".env", HERE / ".env"):
        if envfile.is_file():
            merged.update({k: v for k, v in dotenv_values(envfile).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL", "GEMINI_API_KEY", "GEMINI_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


ENV = _env()


def _claude():
    import anthropic
    key = ENV.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found (infinigen_o/.env, OtherTest/.env, ExtraWorld/.env, env)")
    base = (ENV.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


def _gemini():
    from openai import OpenAI
    key = ENV.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY not found for image generation.")
    return OpenAI(api_key=key,
                  base_url=ENV.get("GEMINI_BASE_URL",
                                   "https://generativelanguage.googleapis.com/v1beta/openai/"))


# --------------------------------------------------------------------- factory catalog
_ROW_RE = re.compile(r"^\s+([A-Za-z_]\w*)\s+\[(ENTRY|PART)\s*\]\s+(.*?)\s+(\S+\.py)\s*$")


def load_catalog() -> dict:
    """{FactoryName: {"role": ENTRY|ELEMENT|PART, "desc": str, "file": relpath}}.

    FactoryList tags two very different things as [PART]: ELEMENTS (a single leaf / blossom
    / berry / seed / needle / fruit), which its own header says are "generatable alone", and
    base skeletons / creature body parts / engines, which are not whole objects. The list
    already distinguishes them in the description prefix, so promote ELEMENT to its own role
    — an object that IS a single element (an apple, a maple leaf) can then use its factory.
    """
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


# --------------------------------------------------------------------------- classify
CLASSIFY_SYSTEM = """You are the build director for a 3D scene pipeline. You are given a
catalog of every available Infinigen AssetFactory (each tagged [ENTRY] = a whole placeable
object, or [PART] = a sub-part only) and a list of object TYPES from a scene. For EACH
object type, decide two independent things and return them.

Some [PART] rows are ELEMENTS — their description starts with "(element" — meaning a single
leaf, blossom, berry, seed, needle or fruit that a plant/tree [ENTRY] instances. These can
be spawned on their own. The other [PART] rows (base skeletons, creature body parts,
engines) cannot.

Return ONE JSON object and nothing else:
{
  "objects": [
    {
      "id": "<the object id, copied exactly>",
      "is_organic": <true|false>,
      "factory": "<the single closest factory name from the catalog, or null>",
      "factory_reason": "<one short sentence>",
      "color_variation": "<per_seed|fixed>",
      "color_reason": "<one short sentence>",
      "note": "<one short sentence>"
    }
  ]
}

Definitions (apply them literally; judge only from the given description):
- is_organic = the object is a natural, grown, or anatomical thing (living things and
  their natural parts). A manufactured, hard-surface, or geometric artefact is NOT organic.
- factory = the ONE factory whose description best matches this object's kind by structure
  or anatomy (match on what it fundamentally IS, not on exact wording or surface details).
  Prefer an [ENTRY]. Choose an "(element" [PART] ONLY when the requested object literally IS
  that single element — "a golden apple" is one fruit, so its fruit factory is right, while
  "an apple tree" is a whole tree and must take the tree [ENTRY] (which instances the fruit
  itself). Never choose any other [PART] (base skeleton, body part, engine). Choose null if
  nothing is a reasonable match. Never invent a name.
- color_variation = whether DIFFERENT INSTANCES of this object should come out in
  different colours. The generated factory is spawned once per instance with a different
  seed, so this decides whether the seed is allowed to move the hue.
    "per_seed" = a real population of this thing is visibly multi-coloured, and a viewer
      would expect a scattered group to show several hues (reef anemones and corals,
      wildflower patches, tulips, garden roses, hydrangeas, painted boats, parked cars,
      beach umbrellas, plastic toys).
    "fixed"    = this thing is essentially one colour in the world, or the request names a
      specific colour, or the scene implies one cultivar / one batch / one species, so a
      group of them must look consistent (sunflowers, lavender, monarch butterflies, polar
      bears, "a red canoe", a planted row of one crop, a uniform hedge).
  Judge the POPULATION, not the single reference image: a request for "a purple anemone"
  is still "fixed" because the user asked for purple. When unsure prefer "fixed" — wrongly
  varied colour is more jarring than wrongly uniform colour.
  NOTE: traits other than colour (size, count, proportion, pose) must ALWAYS vary with the
  seed; color_variation only governs hue.
"""


def classify(object_types: list[dict], catalog: dict) -> dict:
    """Return {id: {is_organic, factory, factory_reason, note}} from the LLM. Note that
    the FEATURE axis is NOT decided here — it is taken from Agent 1's `appearance` field,
    the single source of truth, so a generic object's default-elaborated prompt cannot be
    mis-read as 'featured'."""
    brief = [{"id": t["id"], "name": t.get("name"), "prompt": t.get("prompt"),
              "count": t.get("count", 1)} for t in object_types]
    content = [
        {"type": "text", "text": "CATALOG (FactoryList.txt):\n\n" + FACTORYLIST.read_text(encoding="utf-8")},
        {"type": "text", "text": "OBJECT TYPES:\n\n" + json.dumps(brief, ensure_ascii=False, indent=2)
                                 + "\n\nReturn the JSON now."},
    ]
    resp = _claude().messages.create(
        model=OPUS_MODEL, max_tokens=3000, system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise SystemExit(f"classifier did not return JSON:\n{text}")
    decisions = {}
    for o in json.loads(m.group(0)).get("objects", []):
        fac = o.get("factory")
        if fac and (fac not in catalog or catalog[fac]["role"] not in ("ENTRY", "ELEMENT")):
            print(f"  [warn] '{fac}' is not an ENTRY or ELEMENT in the catalog — treating as no factory")
            fac = None
        cvar = str(o.get("color_variation") or "").strip().lower()
        if cvar not in ("per_seed", "fixed"):
            if cvar:
                print(f"  [warn] '{o['id']}' color_variation='{cvar}' 不是 per_seed/fixed"
                      f" — 當成 fixed")
            cvar = "fixed"          # ★保守預設★：亂變色比全同色礙眼
        decisions[o["id"]] = {
            "is_organic": bool(o.get("is_organic")),
            "factory": fac,
            "factory_reason": o.get("factory_reason", ""),
            "color_variation": cvar,
            "color_reason": o.get("color_reason", ""),
            "note": o.get("note", ""),
        }
    return decisions


# --------------------------------------------------------------------- instance groups
def instance_grouping(t: dict) -> dict:
    """count + seed strategy for a type. instances_vary -> distinct seeds; else one seed."""
    count = int(t.get("count", 1) or 1)
    vary = bool(t.get("instances_vary"))
    rng = random.Random(t["id"])                     # deterministic per id
    if vary and count > 1:
        seeds, seen = [], set()
        while len(seeds) < count:
            s = rng.randint(1, 99999)
            if s not in seen:
                seen.add(s)
                seeds.append(s)
        return {"count": count, "seed_mode": "different", "seeds": seeds}
    seed = rng.randint(1, 99999)
    return {"count": count, "seed_mode": "same", "seeds": [seed] * count}


# --------------------------------------------------------------------- ref-image gen
def _call_image(instruction: str, image_bytes: bytes | None = None) -> bytes:
    """One Gemini image call: text instruction (+ optional single input image) -> PNG bytes."""
    content = [{"type": "text", "text": instruction}]
    if image_bytes is not None:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    client = _gemini()
    delay = 15
    for attempt in range(1, 6):
        try:
            r = client.chat.completions.create(
                model=GEMINI_IMAGE_MODEL, messages=[{"role": "user", "content": content}])
            break
        except Exception as e:
            if attempt == 5:
                raise
            if any(k in str(e) for k in ("503", "500", "429", "UNAVAILABLE", "INTERNAL",
                                         "timeout", "timed out", "ReadTimeout")):
                print(f"    image API error (attempt {attempt}/5): {str(e)[:70]}; retry {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                raise
    data = _extract_image(r)
    if data is None:
        raise RuntimeError("image model returned no image")
    return data


def _extract_image(response) -> bytes | None:
    try:
        msg = response.choices[0].message
        content = msg.content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            return base64.b64decode(url.split(",", 1)[1])
                    if part.get("type") == "image":
                        d = part.get("image", "") or part.get("data", "")
                        if d:
                            return base64.b64decode(d)
        if isinstance(content, str):
            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', content)
            if m:
                return base64.b64decode(m.group(1))
        if getattr(response, "model_extra", None):
            for cand in response.model_extra.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data", {})
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
    except Exception:
        pass
    return None


def _stitch_horizontal(images_bytes: list[bytes], gap: int = 24, bg=(255, 255, 255)) -> bytes:
    from PIL import Image
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in images_bytes]
    h = min(i.height for i in imgs)
    imgs = [i.resize((round(i.width * h / i.height), h)) for i in imgs]
    w = sum(i.width for i in imgs) + gap * (len(imgs) - 1)
    sheet = Image.new("RGB", (w, h), bg)
    x = 0
    for i in imgs:
        sheet.paste(i, (x, 0))
        x += i.width + gap
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def generate_refs(obj_prompt: str, out_dir: Path, scene_image: Path | None) -> dict:
    """Make target_single.png (3/4 view) + target_multi.png (front|3-4|side) for a
    featured object. Cuts it from the scene image when there is one, else from the text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if scene_image is not None:
        instr = (
            f"Generate a clean studio product photo of ONLY this one object from the image: "
            f"{obj_prompt}. Isolate it on a plain pure-white background. Show the WHOLE object, "
            f"every part visible, in a 3/4 view from the front and slightly the right, seen a "
            f"little from above, upright. No other objects, no people, no clutter. Preserve its "
            f"real design, shape, proportions, colours and materials exactly as in the source image. "
            # ★「一個」是硬性規定，會覆蓋 obj_prompt 自己的數量詞★(2026-08-27)：
            #   見 3_procedural_generator.auto_reference 的同一條註解 —— "ONLY this
            #   object" 只擋得住旁邊多出別的東西，擋不住物體本身就是複數。
            f"Render exactly ONE single specimen of it — one individual object, alone in "
            f"the frame. This overrides any quantity in the description above: if it asks "
            f"for a group, cluster, swarm, grove, school, pair or any number greater than "
            f"one, ignore that and show ONE representative individual only. "
        )
        canon = _call_image(instr, scene_image.read_bytes())
    else:
        instr = (
            f"Generate a clean studio product photo of ONLY this object: {obj_prompt}. "
            f"Isolate it on a plain pure-white background. Show the WHOLE object, every part "
            f"visible, in a 3/4 view from the front and slightly the right, seen a little from "
            f"above, upright. No other objects, no people, no clutter, photorealistic. "
            # ★「一個」是硬性規定，會覆蓋 obj_prompt 自己的數量詞★(2026-08-27)：
            #   見 3_procedural_generator.auto_reference 的同一條註解 —— "ONLY this
            #   object" 只擋得住旁邊多出別的東西，擋不住物體本身就是複數。
            f"Render exactly ONE single specimen of it — one individual object, alone in "
            f"the frame. This overrides any quantity in the description above: if it asks "
            f"for a group, cluster, swarm, grove, school, pair or any number greater than "
            f"one, ignore that and show ONE representative individual only. "
        )
        canon = _call_image(instr)

    single = out_dir / "target_single.png"
    multi = out_dir / "target_multi.png"
    single.write_bytes(canon)

    def other_view(view: str) -> bytes:
        return _call_image(
            f"This image is a product photo of a single {obj_prompt} on a pure-white background. "
            f"Generate the EXACT SAME object — identical design, proportions, colours and materials "
            f"— shown in a {view}. Keep it isolated on pure white, whole object visible, upright, "
            f"same scale. Still exactly one single specimen, never a group or a second copy.", canon)

    try:
        front = other_view("straight-on front view")
        side = other_view("side profile view")
        multi.write_bytes(_stitch_horizontal([front, canon, side]))
    except Exception as e:
        print(f"    multi-view failed, falling back to single: {e}")
        multi.write_bytes(canon)
    return {"single": str(single.relative_to(out_dir.parent.parent)),
            "multi": str(multi.relative_to(out_dir.parent.parent))}


# ------------------------------------------------------------------- layer pre-gate
def load_env_catalog() -> dict:
    """The env-layer catalog: things Infinigen can PRODUCE via gin (scatter/populate/
    creatures/particles). Membership authority is the GLOBAL controllable_catalog — it is
    biome-independent (any biome can override any knob), so this gate never needs the biome."""
    return json.loads(SCENE_CATALOG.read_text(encoding="utf-8"))


LAYER_SYSTEM = """You are the FIRST-LEVEL router for a 3D scene pipeline. Before deciding HOW
to build each thing, you decide WHERE it is built:

  - "env"    -> the ENVIRONMENT layer produces it by setting an Infinigen gin knob (a scatter
                / populate / creature / particle already in the catalog). Cheap, comes with
                perfect ground-truth, but it is a STATISTICAL MASS with random placement and
                only the catalog's normal appearance.
  - "object" -> the ASSET layer generates it individually (modelled / procedurally adapted /
                sampled) and places it. Use this for anything the env layer cannot deliver.

You are given the CONTROLLABLE CATALOG (every thing the env layer can produce, with keywords
and the gin knobs) and a list of scene ENTITIES. For EACH entity decide its layer with these
rules (route to "object" if ANY of 2-4 fails):

  1. IN CATALOG?  Does the entity match a catalog item (by its keywords / kind)? If NOT in the
     catalog at all -> "object" (the env layer simply cannot make it).
  2. MASS, not individual?  Is it ambient/background scenery where you only care about "lots
     of them" and never about one specific instance? (grass covering the ground, mushrooms
     dotting a forest) -> ok for "env". A single hero/landmark/named instance you would point
     at -> "object".
  3. PLACEMENT-AGNOSTIC?  Fine to scatter randomly? If it must sit at a specific spot or in a
     specific relation (in front, on that rock, held) -> "object".
  4. NORMAL LOOK?  Its appearance is an ordinary member of that kind? If it needs a look the
     catalog can't express (glowing, metallic, a species that doesn't exist, a stated exact
     colour/pattern) -> "object".

Return ONE JSON object and nothing else:
{
  "entities": [
    {
      "id": "<entity id, copied exactly>",
      "layer": "env" | "object",
      "catalog_name": "<the matched catalog item name, or null>",
      "gin": ["<the gin knob(s) from the matched item, or []>"],
      "amount": "<for env only: sparse | normal | many | dense — how much of it>",
      "reason": "<one short sentence>"
    }
  ]
}
"""


def route_layer(object_types: list[dict], env_catalog: dict) -> dict:
    """id -> {"layer","catalog_name","gin","amount","reason"}. Fails safe to "object"."""
    if not object_types:
        return {}
    brief = [{"id": t["id"], "name": t.get("name"), "prompt": t.get("prompt"),
              "count": t.get("count", 1), "appearance": t.get("appearance")}
             for t in object_types]
    user = ("CONTROLLABLE CATALOG (env layer can produce these):\n\n"
            + json.dumps(env_catalog.get("controllable_catalog", []), ensure_ascii=False, indent=2)
            + "\n\nSCENE ENTITIES:\n\n"
            + json.dumps(brief, ensure_ascii=False, indent=2)
            + "\n\nRoute every entity now.")
    resp = _claude().messages.create(
        model=OPUS_MODEL, max_tokens=2000, system=LAYER_SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    out: dict = {}
    if m:
        for e in json.loads(m.group(0)).get("entities", []):
            out[e["id"]] = {
                "layer": "env" if e.get("layer") == "env" else "object",
                "catalog_name": e.get("catalog_name"),
                "gin": e.get("gin") or [],
                "amount": e.get("amount"),
                "reason": e.get("reason", ""),
            }
    # anything the model omitted -> build it as an object (safe default)
    for t in object_types:
        out.setdefault(t["id"], {"layer": "object", "catalog_name": None,
                                 "gin": [], "amount": None, "reason": "unrouted -> default object"})
    return out


# --------------------------------------------------------------------------- resolve
def resolve_planner_dir(arg: str) -> Path:
    """Accept a scene name, a scene dir, a 1_scene_planner dir, or a scene_plan.json path."""
    p = Path(arg)
    candidates = []
    if p.is_file() and p.name == "scene_plan.json":
        return p.parent
    if p.is_dir():
        candidates += [p / "scene_plan.json", p / "1_scene_planner" / "scene_plan.json"]
    candidates.append(HERE / "output" / arg / "1_scene_planner" / "scene_plan.json")
    for c in candidates:
        if c.is_file():
            return c.parent
    raise SystemExit(f"could not find scene_plan.json for '{arg}' (looked in: "
                     + ", ".join(str(c) for c in candidates) + ")")


def main():
    ap = argparse.ArgumentParser(description="Director: scene plan -> per-object build decisions + refs")
    ap.add_argument("scene", help="scene name, scene dir, or a scene_plan.json path")
    ap.add_argument("--no-image", action="store_true", help="skip generating reference images")
    args = ap.parse_args()

    planner_dir = resolve_planner_dir(args.scene)
    scene = json.loads((planner_dir / "scene_plan.json").read_text(encoding="utf-8"))
    scene_name = scene.get("scene_name", "scene")
    object_types = scene.get("object_types", [])

    # the real scene image (if the input was an image/video), for cutting object refs
    scene_image = None
    inp = scene.get("input", {})
    if inp.get("image"):
        cand = planner_dir / inp["image"]
        scene_image = cand if cand.is_file() else None

    out_dir = planner_dir.parent / "2_director"
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog()

    # PRE-GATE: env (gin / scene gen) vs object (asset gen) — before any per-object decision.
    # env-routed things are produced by the environment layer as gin knobs; object-routed
    # things flow into the existing feature x organic x factory routing below.
    env_catalog = load_env_catalog()
    print(f"[director] routing {len(object_types)} entities env-vs-object with {OPUS_MODEL} ...")
    layer = route_layer(object_types, env_catalog)
    env_items = [{"id": t["id"], "name": t.get("name"), "prompt": t.get("prompt"),
                  "count": t.get("count", 1), **layer[t["id"]]}
                 for t in object_types if layer[t["id"]]["layer"] == "env"]
    object_types = [t for t in object_types if layer[t["id"]]["layer"] == "object"]
    print(f"[director] -> env layer: {len(env_items)}   object layer: {len(object_types)}")
    for e in env_items:
        print(f"  [env]    {e['id']:20} gin={e['gin']} amount={e.get('amount')}  ({e['reason']})")

    print(f"[director] classifying {len(object_types)} object types with {OPUS_MODEL} ...")
    decisions = classify(object_types, catalog)

    manifest = {"scene_name": scene_name, "source": inp, "environment": env_items, "objects": []}
    print("\n=== BUILD DECISIONS ===")
    for t in object_types:
        d = decisions.get(t["id"], {"is_organic": False, "factory": None,
                                    "factory_reason": "", "color_variation": "fixed",
                                    "color_reason": "", "note": ""})
        # FEATURE axis comes from Agent 1's appearance judgment (single source of truth)
        has_feature = t.get("appearance") == "specific"
        cat_key = route(has_feature, d["is_organic"], d["factory"] is not None)
        num, label, generator = ROUTES[cat_key]
        grouping = instance_grouping(t)

        entry = {
            "id": t["id"], "name": t.get("name"), "prompt": t.get("prompt"),
            "has_feature": has_feature, "is_organic": d["is_organic"],
            "category": {"num": num, "key": cat_key, "label": label},
            "generator": generator,
            "factory": d["factory"], "factory_reason": d["factory_reason"],
            # ★每個 seed 是否換顏色★(2026-09-13)：由 Director 用語意判斷，
            #   ProceduralGenerator 讀這個欄位決定要不要把 shader 接上 per-seed 色相。
            "color_variation": d.get("color_variation", "fixed"),
            "color_reason": d.get("color_reason", ""),
            "instance_grouping": grouping,
            "note": d["note"], "refs": None, "description": t.get("prompt"),
        }

        print(f"  [{num}] {t['id']:20} {generator:19} "
              f"x{grouping['count']}/{grouping['seed_mode']:9} factory={d['factory']}"
              f" colour={d.get('color_variation', 'fixed')}")

        # Every generated asset now gets a visual target. ProceduralGenerator follows the
        # image target while rewriting the closest starter factory, and this improves
        # quality over text-only generation. When the input was an image/video we cut the
        # object from the real scene; for a TEXT-only scene we let Gemini render an example.
        needs_ref = generator in ("BlenderGenerator", "ProceduralGenerator")
        if needs_ref and not args.no_image:
            src = "from the scene image" if scene_image else "with Gemini (text-to-image)"
            print(f"      generating reference images {src} ...")
            try:
                entry["refs"] = generate_refs(t.get("prompt", t.get("name", t["id"])),
                                              out_dir / t["id"], scene_image)
            except Exception as e:
                print(f"      ref generation failed: {e}")
        else:
            # persist the short description for downstream generators
            (out_dir / t["id"]).mkdir(parents=True, exist_ok=True)
            (out_dir / t["id"] / "description.txt").write_text(
                t.get("prompt", "") + "\n", encoding="utf-8")

        manifest["objects"].append(entry)

    (out_dir / "generation_list.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n[director] generation_list.json -> {out_dir}")


if __name__ == "__main__":
    main()

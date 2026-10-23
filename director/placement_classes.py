"""PLACEMENT CLASSES — where a thing lives in an Infinigen scene, and what you may do to it.

★ Why this exists ★
  The scene has THREE placement mechanisms, not two, and they differ in what edits are even
  possible. director.py used to check only "is target_factory in the manifest?", and since
  scan_scene.py reads the `placeholders:` collection, every non-hero thing looked absent and
  got silently downgraded to action="add" — which is the wrong operation for something the
  GN scatter already produces thousands of.

  All three are resolved from real source (generate_nature.py compose_nature / populate_scene,
  configs_nature/base_nature.gin), NOT from guesswork:

  HERO           populate_all(<Factory>) in populate_scene()
                 -> each instance is an independent mesh with its OWN placeholder recorded in
                    `placeholders:<factory>`. Position is stored data.
                 -> replace / add / delete all work: read loc/rot off the placeholder.
                 ✅ verified: swapping trees works (0726 experiments).

  GRIME          apply_grime() in populate_scene(), gated by populate_scene.<x>_chance
                 -> NOT free-standing. Applied ONTO already-populated hero objects
                    (trees / boulders / bushes / cactus) as a surface, per-instance, with
                    `<grime>_on_<target>_per_instance_chance` (default 0.4).
                 -> editable at populate time: swap the surface class, or change the chance.
                 -> "where" is not a coordinate, it is WHICH HOST it sits on.

  GROUND_SCATTER `<Scatter>().apply(terrain)` inside compose_nature() = the COARSE stage
                 -> master pool of n meshes + a geometry-nodes scatter rule. Individual
                    positions exist ONLY as GN evaluation, never as objects.
                 -> cannot be edited at populate time. Changing it means re-running coarse.

★ The mushroom trap ★
  `mushroom` is BOTH. populate_scene.mushroom_chance is ground_mushroom.Mushrooms applied as
  GRIME onto tree trunks / boulders. compose_nature.mushroom_chance is the GROUND_SCATTER on
  the terrain. Same word, two mechanisms, different editability. Any prompt mentioning
  mushrooms is ambiguous until you know whether it means "on the trees" or "on the ground".
"""

# ---------------------------------------------------------------------------- HERO
# populate_scene(): placement.populate_all(<Factory>, ...) -> real placeholders
HERO_FACTORIES = {
    # vegetation / terrain features
    "TreeFactory": "trees",
    "BushFactory": "bushes",
    "BoulderFactory": "boulders",
    "CactusFactory": "cactus",
    "KelpMonocotFactory": "kelp",
    "CloudFactory": "clouds",
    "GlowingRocksFactory": "glowing_rocks",
    # creatures — all populated the same way
    "BeetleFactory": "beetles",
    "BirdFactory": "bird",
    "CarnivoreFactory": "carnivore",
    "CrabFactory": "crab",
    "CrustaceanFactory": "crustacean",
    "DragonflyFactory": "dragonfly",
    "FishFactory": "fish",
    "FlyingBirdFactory": "flyingbird",
    "HerbivoreFactory": "herbivore",
    "SnakeFactory": "snake",
}

# ---------------------------------------------------------------------------- GRIME
# populate_scene(): grime_types, applied ONTO populated hero objects
GRIME = {
    "slime_mold": dict(cls="slime_mold.SlimeMold", gin="populate_scene.slime_mold_chance"),
    "lichen": dict(cls="lichen.Lichen", gin="populate_scene.lichen_chance"),
    "ivy": dict(cls="ivy.Ivy", gin="populate_scene.ivy_chance"),
    "mushroom": dict(cls="ground_mushroom.Mushrooms", gin="populate_scene.mushroom_chance",
                     ambiguous_with="ground_scatter:mushroom"),
    "moss": dict(cls="moss.MossCover", gin="populate_scene.moss_chance"),
    "snow_layer": dict(cls="snow_layer.Snowlayer", gin="populate_scene.snow_layer_chance",
                       note="applied to every hero instance, no per-instance chance gate"),
}

# which hero types grime can land on (populated.items() in apply_grime)
GRIME_HOSTS = ["trees", "boulders", "bushes", "cactus"]

# grime_selection_funcs: only these two have a selection function; others get the whole object
GRIME_SELECTION = {"trees": "scatter_lower", "boulders": "scatter_upward"}

# --------------------------------------------------------------------- GROUND_SCATTER
# compose_nature(): <Scatter>().apply(terrain) — coarse stage, GN instancing, no placeholders
GROUND_SCATTER = {
    "grass", "monocots", "ferns", "flowers", "ground_leaves", "ground_twigs",
    "chopped_trees", "pebbles", "rocks", "pinecone", "pine_needle", "mushroom",
    "decorative_plants", "coral_reef", "seaweed", "urchin", "seashell", "jellyfish",
    "moss", "lichen", "ivy", "snow_layer",
}

CLASSES = ("hero", "grime", "ground_scatter", "fish_school", "official_scatter")

# which actions each class can actually support, and why
ACTION_SUPPORT = {
    "hero": {
        "replace": (True, "placeholders record loc/rot — a new asset inherits the position"),
        "add": (True, "place a new object and record it like any hero"),
        "delete": (True, "bpy.data.objects.remove on the instance"),
    },
    "grime": {
        "replace": (True, "swap the surface class applied in populate_scene's apply_grime"),
        "add": (True, "raise populate_scene.<x>_chance / the per-instance chance"),
        "delete": (True, "set populate_scene.<x>_chance = 0"),
    },
    # ⚠️ ★2026-09-07 修正★：這三筆原本全判 False，理由是「scatter 是 coarse 建的」。
    #    但 apply_plan / apply_extra_plan ★早就實作了這三種操作★，而且都不用重跑
    #    coarse、不用 populate —— 因為 GN scatter 的位置是 render 時算的，blend 裡
    #    只有一個空殼 + COLLECTION_INFO 指標：
    #      replace  換母本集合指標(位置分佈完全不變，保位置是免費的)
    #      delete   Overall Density = 0
    #      add      op="create"，用 scatter_catalog 的系統憑空長一個新的出來
    #    先前判 False 讓 director 只能把「撒一片花海」標成不可行，而執行層其實做得到。
    "ground_scatter": {
        "replace": (True, "swap the master collection the GN scatter points at — "
                          "positions are computed at render time and do not change"),
        "add": (True, "op='create' builds a whole new GN scatter over the terrain "
                      "from the scatter catalog; no coarse re-run, no populate"),
        "delete": (True, "set the scatter's Overall Density to 0"),
    },
    # ★官方 boid 魚群★(2026-09-11)：generate_nature 的 fish_school stage，每群 30-400 條
    #   都是 3 條母本的粒子 instance。
    #     replace  換現有魚群粒子系統的 instance_collection(master_swap)，群游/數量不動
    #     add      照官方參數在相機附近新建一群，母本用我們的魚
    "fish_school": {
        "replace": (True, "swap the masters an existing boid school instances — "
                          "particle count, flocking and positions are untouched"),
        "add": (True, "build a new official-style boid school near the camera "
                      "whose masters are the requested fish"),
        "delete": (False, "a baked boid school cannot be removed piecemeal"),
    },
    # ★官方撒點(樹/灌木/石頭/仙人掌/海帶)★(2026-09-11)：generate_nature 的
    #   scatter_placeholders_mesh(密度 + 最小間距)，只多一個「相機附近」的窗。
    #   replace 仍走 hero(沿用全部既有位置)，這個 class 只負責 add。
    "official_scatter": {
        "replace": (False, "replacing existing ones is placement=hero action=replace"),
        "add": (True, "Poisson-scatter many placeholders near the camera with the "
                      "official density and spacing of that factory"),
        "delete": (False, "use placement=hero action=delete"),
    },
}


def classify(factory: str | None, name_hint: str = "") -> tuple[str, str]:
    """-> (placement_class, reason). Factory identity wins; the name hint is the fallback
    for things with no factory (which is exactly when we must guess from wording)."""
    if factory and factory in HERO_FACTORIES:
        return "hero", f"{factory} is populated via populate_all -> has placeholders"

    hint = (name_hint or "").lower()
    for g in GRIME:
        if g.replace("_", " ") in hint or g in hint:
            amb = GRIME[g].get("ambiguous_with")
            if amb:
                return "grime", (f"'{g}' is applied as grime onto hero surfaces in "
                                 f"populate_scene — ★ but it is AMBIGUOUS: {amb} also exists "
                                 f"as a terrain scatter created in coarse ★")
            return "grime", f"'{g}' is applied onto hero object surfaces in populate_scene"

    for s in GROUND_SCATTER:
        if s.replace("_", " ") in hint or s in hint:
            return "ground_scatter", (f"'{s}' is a GN terrain scatter created in coarse — "
                                      f"no per-instance placeholders")

    if factory:
        return "hero", (f"{factory} is not a known scatter/grime; treating as hero "
                        f"(it will be placed as an independent object)")
    return "hero", "no factory and no scatter/grime match — will be built and placed as a hero"


def action_ok(placement_class: str, action: str) -> tuple[bool, str]:
    return ACTION_SUPPORT.get(placement_class, {}).get(
        action, (True, "unknown class/action — allowed by default"))


def as_prompt_block() -> str:
    """The placement rules, rendered for the director's system prompt."""
    return f"""\
PLACEMENT CLASSES — an Infinigen scene builds things three different ways, and this decides
which edits are even possible. Assign one to every edit.

  "hero"  — built individually in the POPULATE stage (populate_all). Each instance is its own
            mesh with a recorded placeholder, so its position is stored data.
            Factories: {", ".join(sorted(HERO_FACTORIES))}
            ✅ replace / add / delete all supported.

  "grime" — a surface applied ONTO hero objects during POPULATE (moss on a trunk, lichen on a
            boulder). It is not free-standing and has no position of its own — it lives on a
            HOST ({" / ".join(GRIME_HOSTS)}).
            Kinds: {", ".join(sorted(GRIME))}
            ✅ supported, but you must say WHICH HOST it sits on, not where it is.

  "ground_scatter" — created in the COARSE stage as a geometry-nodes scatter over the terrain
            (grass, pinecones, leaf litter, pebbles...). Individual positions exist only inside
            the GN rule; there are NO placeholders.
            ❌ NOT editable in this pipeline — it would require re-running coarse. If the
            request needs one, still report it, mark it ground_scatter, and say so plainly.

★ AMBIGUITY YOU MUST RESOLVE ★
  "mushroom" exists BOTH as grime (growing on tree trunks / boulders) and as a ground scatter
  (dotting the terrain). Decide from the prompt which one is meant. If the prompt says "on the
  trees" -> grime; "on the ground" / "covering the forest floor" -> ground_scatter; if it is
  genuinely unclear, choose grime (the editable one) and note the ambiguity in `reason`.
"""

"""Scene catalog: every Infinigen scene the picker may choose from.

Cues are grounded in the real gin / constraint sources, not invented:
  outdoor -> infinigen_examples/configs_nature/scene_types/<name>.gin
  indoor  -> infinigen/core/tags.py Semantics + home_constraints.home_furniture_constraints

★ The `supported` flag is the honest capability line ★ — an unsupported entry can still be
picked when it is clearly the only fit, but the runner will warn. See NOTES for why.
"""

# ---------------------------------------------------------------- outdoor (14 scene_types)
# cue = what the biome actually produces, read off its gin file.
OUTDOOR = {
    "arctic": dict(
        cue="flat ice/snow plain, ice materials everywhere, sparse rock, no trees, cold",
        supported=True),
    "canyon": dict(
        cue="deep carved rocky canyon walls (tiles Canyons/Canyon), arid, high camera altitude",
        supported=True),
    "cave": dict(
        cue="underground cavern, chunky rock, glowing rocks, mushrooms and ferns, dim",
        supported=True),
    "cliff": dict(
        cue="tall cliff faces with a big vertical drop (tile heights -15/-15/+10), rugged",
        supported=True),
    "coast": dict(
        cue="sea shore / sandy beach meeting water, wet sand, coastal (not open ocean)",
        supported=True),
    "coral_reef": dict(
        cue="UNDERWATER coral reef, corals/seashells/urchins, camera submerged",
        supported=True, underwater=True),
    "desert": dict(
        cue="sand dunes, cactus, very sparse grass, few trees, dry and bright",
        supported=True),
    "forest": dict(
        cue="dense trees on forest soil, ferns, grass, mushrooms, leaf litter, twigs",
        supported=True),
    "kelp_forest": dict(
        cue="UNDERWATER kelp forest, tall kelp stalks, low relief seabed",
        supported=True, underwater=True),
    "mountain": dict(
        cue="tall mountains and peaks, boulders, flying creatures, sparse trees",
        supported=True),
    "plain": dict(
        cue="open flat grassland / meadow, ground creatures, wide sky",
        supported=True),
    "river": dict(
        cue="river channel cutting through eroded, vegetated banks",
        supported=True),
    "snowy_mountain": dict(
        cue="high-altitude mountains ABOVE THE TREELINE: snowfall baked into the terrain "
            "(land_processes='snowfall'), boulders, flying birds, snow particles — but NO "
            "vegetation of any kind and no snow_layer scatter. Distant/aerial framing "
            "(min_terrain_distance=40, camera pitched down).",
        supported=True,
        caveat="includes disable_assets/no_assets.gin, then re-enables ONLY boulders, "
               "flying creatures and snow particles -> no trees/grass/ferns/moss/snow_layer. "
               "Also `mountain.shader.snowy = 0` upstream (official TODO: terrain flickering), "
               "so the rock is NOT rendered with the snowy material."),
    "under_water": dict(
        cue="fully submerged open sea floor, no trees/bushes, camera pitched down underwater",
        supported=True, underwater=True),
}

# ---------------------------------------------------------------- indoor (room types)
# ★ Only these 5 have furniture constraints written in home_furniture_constraints ★ —
# generate_indoors.py:167-176 says so explicitly. Others solve to a near-empty room.
INDOOR = {
    "bedroom": dict(
        cue="bed, wardrobe, nightstand, personal sleeping room",
        semantics="Bedroom", supported=True),
    "living-room": dict(
        cue="sofa, coffee table, TV, shelving, social/lounge space",
        semantics="LivingRoom", supported=True),
    "kitchen": dict(
        cue="counters, cabinets, sink, appliances, food prep",
        semantics="Kitchen", supported=True),
    "bathroom": dict(
        cue="toilet, sink, bathtub/shower, tiled wet room",
        semantics="Bathroom", supported=True),
    "dining-room": dict(
        cue="dining table with chairs, eating space",
        semantics="DiningRoom", supported=True),
    # --- below: room types EXIST as tags but have NO furniture constraints ---
    "office": dict(
        cue="desk and office chair, work room",
        semantics="Office", supported=False),
    "garage": dict(
        cue="vehicle bay, storage, utility",
        semantics="Garage", supported=False),
    "balcony": dict(
        cue="outdoor terrace attached to a room",
        semantics="Balcony", supported=False),
    "closet": dict(
        cue="small storage / walk-in wardrobe",
        semantics="Closet", supported=False),
    "utility": dict(
        cue="laundry / mechanical / service room",
        semantics="Utility", supported=False),
    "staircase-room": dict(
        cue="stairwell connecting floors",
        semantics="StaircaseRoom", supported=False),
}

NOTES = """\
Capability notes the picker must respect (these are REAL limits, verified in source):
  - snowy_mountain.gin includes disable_assets/no_assets.gin and re-enables only boulders /
    flying creatures / snow particles. It is an ABOVE-THE-TREELINE scene: no vegetation at
    all. Its rock also renders WITHOUT the snowy shader (official TODO, flickering), so it
    may read as bare grey mountain rather than white. If a prompt wants snow WITH trees,
    `mountain` (which keeps vegetation) is usually the better base, with snow noted as a gap.
  - Indoor room types outside {bedroom, living-room, kitchen, bathroom, dining-room} have no
    furniture constraints (generate_indoors.py:167-176) and solve to a nearly empty room.
  - coral_reef / kelp_forest / under_water put the camera UNDERWATER. Only pick them when the
    prompt is genuinely subaquatic.
"""


def _env_list_block() -> str | None:
    """★ The authoritative outdoor listing: resolved from the real gin files by
    build_env_list.py, NOT hand-written prose. ★ Returns None if it has not been built."""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parent / "ENV_LIST.json"
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))

    out = []
    for name in sorted(data):
        d = data[name]
        extra = OUTDOOR.get(name, {})
        head = f"  {name}"
        if extra.get("underwater"):
            head += "   [UNDERWATER — camera is submerged]"
        out.append(head)

        t = d.get("terrain", {})
        bits = []
        for k, label in (("LandTiles.tiles", "tiles"),
                         ("LandTiles.land_processes", "land process"),
                         ("Terrain.ground_collection", "ground material"),
                         ("scene.waterbody_chance", "waterbody chance"),
                         ("water.is_ocean", "ocean"),
                         ("nishita_lighting.sun_elevation", "sun elevation"),
                         ("keep_cam_pose_proposal.min_terrain_distance", "min terrain dist")):
            if k in t:
                bits.append(f"{label}={t[k]}")
        if bits:
            out.append("      setting: " + "; ".join(bits))
        if d["always"]:
            out.append("      ALWAYS  : " + ", ".join(d["always"]))
        if d["likely"]:
            out.append("      usually : " + ", ".join(d["likely"]))
        if d["sometimes"]:
            out.append("      sometimes: " + ", ".join(d["sometimes"]))
        if d["never"]:
            out.append("      NEVER   : " + ", ".join(d["never"]))
        if extra.get("caveat"):
            out.append(f"      NOTE    : {extra['caveat']}")
    return "\n".join(out)


def as_prompt_block() -> str:
    """The catalog rendered for the LLM, with support flags made explicit."""
    resolved = _env_list_block()
    if resolved is not None:
        lines = ["OUTDOOR BIOMES (run via generate_nature.py). Each entry lists what the biome",
                 "ACTUALLY produces, resolved from its real gin file (base_nature.gin + any",
                 "included gin + the biome's own bindings). ALWAYS/usually/sometimes/NEVER are",
                 "the resolved spawn chances — treat NEVER as a hard fact about the biome.",
                 "",
                 resolved, ""]
    else:
        lines = ["OUTDOOR BIOMES (Infinigen scene_types, run via generate_nature.py):"]
        for name, d in OUTDOOR.items():
            flag = ""
            if d.get("underwater"):
                flag += "  [UNDERWATER]"
            if d.get("caveat"):
                flag += f"  [CAVEAT: {d['caveat']}]"
            lines.append(f"  {name:16} {d['cue']}{flag}")
        lines.append("")
    lines.append("INDOOR ROOM TYPES (run via generate_indoors.py):")
    for name, d in INDOOR.items():
        flag = "" if d["supported"] else "  [NO FURNITURE CONSTRAINTS -> solves to a near-empty room]"
        lines.append(f"  {name:16} {d['cue']}{flag}")
    lines.append("")
    lines.append(NOTES)
    return "\n".join(lines)

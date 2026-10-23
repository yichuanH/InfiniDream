"""Controlled world-space motion contract and executor.

World motion is deliberately independent from a Factory's local rig animation:

* local animation deforms an asset in place (idle swim, run, walk cycle),
* world motion moves the populated placeholder anchor through the scene.

The module is safe to import outside Blender.  ``apply_world_motion`` imports
``bpy`` lazily and is the only function that mutates a scene.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "director" / "animation_catalog.json"

# Director only sees the compact public vocabulary.  The legacy names remain
# executable so existing manifests and already generated .blend files do not
# become invalid when the vocabulary is simplified.
DIRECTOR_PRESETS = {
    "none",
    "aquatic_bob",
    "aquatic_swim",
    "air_glide",
    "air_hover",
    "ground_wander",
    "ground_forward",
}
LEGACY_PRESETS = {
    "none",
    # ★鳥類的快速滑翔★(2026-09-13 使用者：老鷹現在像往上飄，要的是快速往前飛過去、
    #   像鳥在滑行)。跟 air_glide 的差別：距離大很多、★沒有淨爬升★、上下起伏很小。
    "air_soar",
    # ★水面上往前漂★(2026-09-13 使用者：帆船可以往前移動嗎)。afloat 之前沒有任何可用的
    #   preset；水面是平的，所以不需要 ground 那套逐幀 raycast，方向用物件自己的 yaw。
    "afloat_forward",
    "aquatic_hover",
    "aquatic_wander",
    "airborne_wander",
    "directed_traverse",
    "airborne_glide",
    "orbit",
    "stationary_bob",
    "terrain_wander",
    "directed_ground_traverse",
}
# ground_hop has an executor for later experiments, but is intentionally absent
# from Director's compact six-motion vocabulary and this overnight matrix.
INTERNAL_PRESETS = {"ground_hop"}
EXECUTABLE_PRESETS = DIRECTOR_PRESETS | LEGACY_PRESETS | INTERNAL_PRESETS
INTENSITIES = {"subtle", "normal", "strong"}
# Minimum inclusive scene-frame counts that keep each preset's control-frame
# fractions distinct.  This is a delivery requirement, not a render-video
# requirement: a still-only smoke may skip video rendering while retaining the
# short motion range needed to author and validate the requested action.
MINIMUM_FRAME_COUNTS = {
    "none": 1,
    "aquatic_bob": 5,
    "aquatic_swim": 5,
    "air_glide": 5,
    "air_hover": 5,
    "ground_wander": 17,
    "ground_forward": 5,
    "aquatic_hover": 5,
    "aquatic_wander": 6,
    "airborne_wander": 6,
    "directed_traverse": 3,
    "airborne_glide": 5,
    "orbit": 9,
    "stationary_bob": 5,
    "terrain_wander": 17,
    "directed_ground_traverse": 5,
    "ground_hop": 5,
    "afloat_forward": 5,
    "air_soar": 5,
}
LOOPING_PRESETS = {
    "aquatic_bob",
    "air_hover",
    "ground_wander",
    "ground_hop",
    "aquatic_hover",
    "aquatic_wander",
    "airborne_wander",
    "orbit",
    "stationary_bob",
    "terrain_wander",
}

GROUND_PRESETS = {
    "ground_wander",
    "ground_forward",
    "ground_hop",
    "terrain_wander",
    "directed_ground_traverse",
}

# Ground motion is deliberately restricted to ``conform``.  Its executor
# raycasts every path key, settles the real populated geometry, records contact
# gaps/slopes, and keyframes both location and terrain-aligned rotation.
PRESET_ANCHORINGS = {
    "aquatic_bob": {"submerged"},
    "aquatic_swim": {"submerged"},
    "air_glide": {"airborne"},
    "air_hover": {"airborne"},
    "ground_wander": {"conform"},
    "ground_forward": {"conform"},
    "ground_hop": {"conform"},
    "aquatic_hover": {"submerged"},
    "aquatic_wander": {"submerged"},
    "airborne_wander": {"airborne"},
    "directed_traverse": {"submerged", "airborne"},
    "airborne_glide": {"airborne"},
    "orbit": {"submerged", "airborne"},
    "stationary_bob": {"airborne"},
    "terrain_wander": {"conform"},
    "directed_ground_traverse": {"conform"},
    "afloat_forward": {"afloat"},
    "air_soar": {"airborne"},
}

# (horizontal radius in metres, vertical amplitude in metres)
HOVER_AMPLITUDES = {
    "subtle": (0.18, 0.22),
    "normal": (0.35, 0.40),
    "strong": (0.60, 0.65),
}

INTENSITY_SCALE = {"subtle": 0.6, "normal": 1.0, "strong": 1.6}
WANDER_CIRCLE_SEGMENTS = 16      # ground_wander 圓形路徑的段數(需要 ≥ segments+1 個不同幀)
WANDER_SPIN_DEG = 72.0           # ground_wander 整段影片原地轉幾度(0.2 圈，使用者 2026-09-14「非常慢」)
WANDER_PIVOT_RADIUS_M = 0.02     # 轉身時位置只繞這麼小的閉合圈(> MIN_WORLD_DISPLACEMENT_M/2，看不出來)
# ★水下與空中位移一律放慢 0.2 倍★(使用者 2026-09-12：河豚/海馬 48 幀游 3.2m、蝴蝶飛 2.6m 都太快很怪)。
#   路徑是「整段幀範圍走完一趟」，放慢 = 同樣幀數只走 0.2 倍的距離/振幅。
AQUATIC_SPEED_FACTOR = 0.2
# ★一次性滑翔不套慢速因子★(2026-09-13 使用者：老鷹/蜂鳥「原地抖」很怪，鳥應該快速往前飛)。
#   慢速因子本來是為了收掉「貼在鏡頭前快速滑過」的 closed loop(bob/hover)，
#   但 air_glide 是一次性前進，0.2 讓老鷹 5 秒只飛 0.5m ⇒ 看起來像在原地抽動。
ONE_WAY_GLIDE_PRESETS = {"air_glide", "airborne_glide", "air_soar"}
AIR_GLIDE_SPEED_FACTOR = 1.0
# ★2026-09-15 使用者：只有★鳥★要快速滑翔★ —— 「鳥類才有這種翅膀不動快速滑翔的需求」，
#   蝴蝶/蜻蜓/蜜蜂都不要。分界線不用新寫：placer.py:176 的
#   AIRBORNE_SOAR_FACTORIES={FlyingBirdFactory,BirdFactory} 已經把鳥指到 air_soar、
#   昆蟲指到 air_glide ⇒ ★只加速 air_soar 就等於只加速鳥★。
#   (原本一度把三個 one-way preset 一起調成 2.0，會連 canyon/cliff 的蝴蝶與 river 的
#    蜻蜓一起變快 ⇒ 收回。per-scene 的 speed_scale 上限是 MAX_SPEED_SCALE=1.5，到不了 2.0。)
#   ⚠️ build_placement_region_map.py:497 的 air_soar swept envelope 基數是 14.0×scale，
#      ×2 之後是 28m ⇒ 擺放要更多空地，擁擠場景可能把鳥推遠或少放。
AIR_SOAR_SPEED_FACTOR = 2.0


# ★水中一次性游動也不套慢速因子★(2026-09-13 使用者：紅手魚看起來在原地做骨架動畫)。
#   0.2 是 0912 配 48 幀(2 秒)影片調的；現在影片 120 幀，同樣路徑攤到 2.5 倍時間 ⇒
#   紅手魚 5 秒只游 0.41m、河豚 0.34m，完全看不出在游。原地的 bob/hover 維持 0.2。
ONE_WAY_SWIM_PRESETS = {"aquatic_swim"}
# ★同一筆 edit 會「平行運動」的 preset★(同一個 phase_seed ⇒ 方向、速度一模一樣 ⇒ 相對位置不變、不互撞)。
#   placer 決定要不要共用 seed、simple_volume 決定同群之間能不能只比本體，★兩邊都讀這一份★。
#   ★一次性直線移動也算平行★(87 建議)：一對穿山甲並肩往前走很自然，而且可以排得近、兩隻都看得到。
#   ★原地繞圈(ground_wander/aquatic_bob/air_hover…)不算★：一群企鵝同步繞圈會像機器人，
#   所以各走各的方向，擺放時同群之間要比「含掃過範圍」的箱子。
#   (2026-09-13 desert：兩隻穿山甲 ground_forward 方向隨機、擺放卻只比本體 ⇒ 第 33–81 幀互撞)
PARALLEL_MOTION_PRESETS = {"air_glide", "airborne_glide", "air_soar", "aquatic_swim",
                           "ground_forward", "directed_ground_traverse", "afloat_forward"}
AQUATIC_SWIM_SPEED_FACTOR = 1.0


def speed_factor_for(preset: str) -> float:
    """擺放的 swept envelope 與這裡算路徑用的是同一個因子(見 build_placement_region_map)。"""
    preset = str(preset or "none")
    if preset == "air_soar":
        return AIR_SOAR_SPEED_FACTOR
    if preset in ONE_WAY_GLIDE_PRESETS:
        return AIR_GLIDE_SPEED_FACTOR
    if preset in ONE_WAY_SWIM_PRESETS:
        return AQUATIC_SWIM_SPEED_FACTOR
    if preset.startswith(("aquatic", "air")):
        return AQUATIC_SPEED_FACTOR
    return 1.0
GROUND_MOTION_MAX_SLOPE_DEG = 35.0   # 地面位移的坡度上限；超過就改成靜止(不再讓 case 失敗)
# 位移驗證的下限：水下乘 0.2 後 subtle bob 只剩約 0.04m，原本的 0.05 會把它擋掉(10_coral_anim 實測)
MIN_WORLD_DISPLACEMENT_M = 0.01

# Agent-editable path controls.  These are deliberately tiny compared with a
# keyframe schema: the executor still owns every frame of the deterministic
# path, while the visual Agent may rotate it or shorten/lengthen its travel.
HEADING_PRESETS = {
    "aquatic_swim", "air_glide", "ground_wander", "ground_forward",
    "aquatic_wander", "airborne_wander", "directed_traverse",
    "airborne_glide", "orbit", "terrain_wander",
    "directed_ground_traverse", "afloat_forward", "air_soar",
}
SPEED_PRESETS = set(HEADING_PRESETS)
MIN_SPEED_SCALE = 0.25
MAX_SPEED_SCALE = 1.5


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def director_prompt_block() -> str:
    """Small prompt block; do not send blocked/proposed presets to the LLM."""
    return """\
WORLD MOTION — independent root/world-space displacement. It does NOT select or imply a
bone/rig animation. Keep the choice simple. Director may select exactly:

  none               Keep the placed anchor fixed (default and safest).
  aquatic_bob        Closed vertical bob for a hovering underwater subject.
  aquatic_swim       One-way gentle forward swim for an underwater subject.
  air_glide          One-way smooth forward glide with a gentle altitude arc.
  air_hover          Closed vertical hover for an airborne subject.
  ground_wander      Closed, small terrain-following path for a grounded subject.
  ground_forward     One-way short terrain-following path for a grounded subject.

Emit an object with exactly these keys:
  "world_motion": {"preset":"<one name above>", "intensity":"subtle|normal|strong"}

Every non-none preset is only for action="add". For anchoring="submerged", choose only
aquatic_bob or aquatic_swim. For anchoring="airborne", choose only air_glide or air_hover.
For anchoring="conform", choose only ground_wander or ground_forward.
For replacements, other grounded anchors (rooted/upright/tilted), groups, or uncertain
cases, emit none. Prefer subtle unless pronounced movement is explicit. Do not invent
paths, target objects, or parameters; the executor derives a deterministic bounded path.
"""


def _raw_request(value) -> tuple[str, str, dict]:
    if value is None:
        return "none", "subtle", {}
    if isinstance(value, str):
        return value, "subtle", {}
    if not isinstance(value, dict):
        raise ValueError("world_motion must be an object, string preset, or null")
    return (
        str(value.get("preset") or "none"),
        str(value.get("intensity") or "subtle"),
        value.get("params") or {},
    )


def validate_runtime_request(value) -> dict:
    """Return the canonical v2 request, raising on unsupported behavior.

    Runtime validation is strict: a malformed manifest must stop before an
    expensive fine-terrain run instead of silently changing the requested motion.
    """
    preset, intensity, params = _raw_request(value)
    if preset not in EXECUTABLE_PRESETS:
        raise ValueError(
            f"world_motion preset {preset!r} is not executable in controlled v3; "
            f"available={sorted(EXECUTABLE_PRESETS)}"
        )
    if intensity not in INTENSITIES:
        raise ValueError(
            f"world_motion intensity {intensity!r} is invalid; "
            f"available={sorted(INTENSITIES)}"
        )
    if not isinstance(params, dict):
        raise ValueError("world_motion params must be an object")
    unknown = sorted(set(params) - {"heading_deg", "speed_scale"})
    if unknown:
        raise ValueError(f"unsupported world_motion params: {unknown}")
    canonical_params = {}
    if "heading_deg" in params:
        if preset not in HEADING_PRESETS:
            raise ValueError(f"{preset} does not accept heading_deg")
        try:
            heading = float(params["heading_deg"])
        except (TypeError, ValueError) as error:
            raise ValueError("heading_deg must be numeric") from error
        if not math.isfinite(heading) or not (0.0 <= heading < 360.0):
            raise ValueError("heading_deg must be in [0, 360)")
        canonical_params["heading_deg"] = heading
    if "speed_scale" in params:
        if preset not in SPEED_PRESETS:
            raise ValueError(f"{preset} does not accept speed_scale")
        try:
            speed = float(params["speed_scale"])
        except (TypeError, ValueError) as error:
            raise ValueError("speed_scale must be numeric") from error
        if not math.isfinite(speed) or not (MIN_SPEED_SCALE <= speed <= MAX_SPEED_SCALE):
            raise ValueError(
                f"speed_scale must be in [{MIN_SPEED_SCALE}, {MAX_SPEED_SCALE}]")
        canonical_params["speed_scale"] = speed
    return {"preset": preset, "intensity": intensity, "params": canonical_params}


def required_frame_count(value) -> int:
    """Return the minimum inclusive frame count for a canonical request."""
    request = validate_runtime_request(value)
    return MINIMUM_FRAME_COUNTS[request["preset"]]


def validate_frame_range(value, frame_start: int, frame_end: int) -> dict:
    """Validate that a deliverable range can represent the requested motion."""
    request = validate_runtime_request(value)
    start, end = int(frame_start), int(frame_end)
    if start < 1 or end < start:
        raise ValueError(f"invalid frame range: {start}..{end}")
    available = end - start + 1
    required = MINIMUM_FRAME_COUNTS[request["preset"]]
    if available < required:
        raise ValueError(
            f"{request['preset']} requires at least {required} distinct deliverable "
            f"frames; frame range {start}..{end} provides {available}. "
            "Use a longer still-only smoke range or request world_motion=none.")
    return request


def execution_context_error(request, *, action: str, anchoring: str | None) -> str | None:
    """Return why a canonical request is unsafe in the current placement context."""
    canonical = validate_runtime_request(request)
    preset = canonical["preset"]
    if preset == "none":
        return None
    if action != "add":
        return "controlled v3 supports non-none world motion only for add"
    allowed = PRESET_ANCHORINGS[preset]
    if anchoring not in allowed:
        return (
            f"{preset} requires anchoring in {sorted(allowed)}; "
            f"received {anchoring!r}"
        )
    return None


def validate_execution_context(request, *, action: str, anchoring: str | None) -> dict:
    """Return a canonical request or raise before populate/fine-terrain work."""
    canonical = validate_runtime_request(request)
    error = execution_context_error(canonical, action=action, anchoring=anchoring)
    if error:
        raise ValueError(error)
    return canonical


def normalize_director_request(value, *, action: str, anchoring: str | None) -> tuple[dict, str]:
    """Conservatively normalize an LLM choice and explain any fallback."""
    try:
        request = validate_runtime_request(value)
    except ValueError as error:
        return ({"preset": "none", "intensity": "subtle", "params": {}},
                f"fallback to none: {error}")
    if request["preset"] == "none":
        return request, "no world-space displacement requested"
    error = execution_context_error(request, action=action, anchoring=anchoring)
    if error:
        return ({"preset": "none", "intensity": "subtle", "params": {}},
                f"fallback to none: {error}")
    return request, "validated controlled-v3 world motion"


def _key_frames(frame_start: int, frame_end: int, fractions: list[float], preset: str) -> list[int]:
    span = frame_end - frame_start
    frames = [frame_start + round(span * fraction) for fraction in fractions]
    if len(set(frames)) != len(fractions):
        raise ValueError(f"{preset} requires at least {len(fractions)} distinct frames: {frames}")
    return frames


def _world_translation(obj) -> tuple[float, float, float]:
    value = obj.matrix_world.translation
    return (float(value.x), float(value.y), float(value.z))


def _motion_path(preset, intensity, rng, Vector, params=None):
    """Build deterministic local offsets, frame fractions, and report metadata."""
    params = params or {}
    heading_deg = params.get("heading_deg")
    angle = (math.radians(float(heading_deg)) if heading_deg is not None
             else rng.uniform(0.0, math.tau))
    direction = Vector((math.cos(angle), math.sin(angle), 0.0))
    sideways = Vector((-direction.y, direction.x, 0.0))
    z_axis = Vector((0.0, 0.0, 1.0))
    variation = rng.uniform(0.85, 1.15)
    speed_scale = float(params.get("speed_scale", 1.0))
    scale = INTENSITY_SCALE[intensity] * variation * speed_scale
    scale *= speed_factor_for(preset)

    if preset == "aquatic_bob":
        vertical = 0.32 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [Vector((0.0, 0.0, 0.0)), z_axis * vertical,
                   Vector((0.0, 0.0, 0.0)), -z_axis * vertical,
                   Vector((0.0, 0.0, 0.0))]
        metadata = {"vertical_amplitude_m": vertical}
    elif preset == "air_hover":
        vertical = 0.28 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [Vector((0.0, 0.0, 0.0)), z_axis * vertical,
                   Vector((0.0, 0.0, 0.0)), -z_axis * vertical,
                   Vector((0.0, 0.0, 0.0))]
        metadata = {"vertical_amplitude_m": vertical}
    elif preset == "aquatic_hover":
        horizontal, vertical = HOVER_AMPLITUDES[intensity]
        horizontal *= variation
        vertical *= variation
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [
            Vector((0.0, 0.0, 0.0)),
            direction * horizontal + z_axis * vertical,
            sideways * (horizontal * 0.65),
            -direction * horizontal - z_axis * vertical,
            Vector((0.0, 0.0, 0.0)),
        ]
        metadata = {"horizontal_radius_m": horizontal, "vertical_amplitude_m": vertical}
    elif preset == "stationary_bob":
        vertical = 0.35 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [Vector((0.0, 0.0, 0.0)), z_axis * vertical,
                   Vector((0.0, 0.0, 0.0)), -z_axis * vertical,
                   Vector((0.0, 0.0, 0.0))]
        metadata = {"vertical_amplitude_m": vertical}
    elif preset in {"aquatic_wander", "airborne_wander"}:
        horizontal = (1.2 if preset == "aquatic_wander" else 1.6) * scale
        vertical = (0.40 if preset == "aquatic_wander" else 0.60) * scale
        fractions = [0.0, 0.20, 0.40, 0.60, 0.80, 1.0]
        offsets = [
            Vector((0.0, 0.0, 0.0)),
            direction * (horizontal * 0.65) + z_axis * (vertical * 0.50),
            direction * (horizontal * 0.15) + sideways * horizontal - z_axis * (vertical * 0.25),
            -direction * (horizontal * 0.75) + sideways * (horizontal * 0.25) + z_axis * (vertical * 0.70),
            -direction * (horizontal * 0.25) - sideways * (horizontal * 0.75) - z_axis * (vertical * 0.60),
            Vector((0.0, 0.0, 0.0)),
        ]
        metadata = {"horizontal_radius_m": horizontal, "vertical_amplitude_m": vertical}
    elif preset == "orbit":
        radius = 1.0 * scale
        vertical = 0.15 * scale
        fractions = [i / 8 for i in range(9)]
        center = -direction * radius
        offsets = [
            center + direction * (radius * math.cos(math.tau * fraction))
            + sideways * (radius * math.sin(math.tau * fraction))
            + z_axis * (vertical * math.sin(2 * math.tau * fraction))
            for fraction in fractions
        ]
        metadata = {"orbit_radius_m": radius, "vertical_amplitude_m": vertical}
    elif preset == "directed_traverse":
        distance = 3.0 * scale
        vertical = 0.35 * scale
        fractions = [0.0, 0.50, 1.0]
        offsets = [Vector((0.0, 0.0, 0.0)),
                   direction * (distance * 0.5) + z_axis * vertical,
                   direction * distance]
        metadata = {"travel_distance_m": distance, "vertical_arc_m": vertical}
    elif preset in {"aquatic_swim", "air_glide", "airborne_glide"}:
        aquatic = preset == "aquatic_swim"
        # ★空中滑翔 6m 起跳★(2026-09-13)：4.0×0.6×0.2 = 0.5m/5 秒看不出在飛。
        distance = (3.0 if aquatic else 6.0) * scale
        vertical = (0.18 if aquatic else 0.50) * scale
        side = (0.12 if aquatic else 0.30) * scale
        # ★淨爬升★：使用者要蝴蝶往上飛、鳥不要原地浮。水中不爬升(魚會衝出水面)。
        climb = 0.0 if aquatic else 0.30 * distance
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [
            direction * (distance * fraction)
            + z_axis * (vertical * math.sin(math.pi * fraction) + climb * fraction)
            + sideways * (side * math.sin(math.pi * fraction))
            for fraction in fractions
        ]
        metadata = {"travel_distance_m": distance, "vertical_arc_m": vertical,
                    "side_arc_m": side, "net_climb_m": climb}
    elif preset in {"ground_wander", "terrain_wander"}:
        # Z is solved from the rendered terrain for every path key.  The small
        # radius is intentional: this is a controlled root-motion primitive,
        # not obstacle-aware navigation.
        # ★正方形 → 圓形★(2026-09-13 使用者：原地打轉走成正方形，轉角很卡像跳針)。
        #   原本 5 個控制點線性內插 ⇒ 折線。改成 16 段的圓(經過起點、首尾同點 ⇒ 閉合誤差仍 0)。
        # ★繞圈 → 原地慢慢轉★(2026-09-14 使用者：繞圈一格一格很卡、像只有五幀)。
        #   原因：朝向取自 16 段折線的切線 ⇒ 每段換一次朝向 = 16 次跳格，半徑又小到幾乎不動。
        #   現在：朝向逐幀連續轉 WANDER_SPIN_DEG×speed_scale(預設 0.2 圈)，位置只繞 2cm 的閉合小圈
        #   (看不出來，只是讓位移/閉合契約照舊成立，preset 名字不變 ⇒ 下游白名單都不用改)。
        horizontal = WANDER_PIVOT_RADIUS_M
        spin_deg = WANDER_SPIN_DEG * float(params.get("speed_scale", 1.0)) * variation
        segments = WANDER_CIRCLE_SEGMENTS
        center = direction * horizontal
        fractions = [k / segments for k in range(segments + 1)]
        offsets = [
            center + (-direction * math.cos(math.tau * f) + sideways * math.sin(math.tau * f)) * horizontal
            for f in fractions
        ]
        offsets[0] = Vector((0.0, 0.0, 0.0))
        offsets[-1] = Vector((0.0, 0.0, 0.0))
        metadata = {"horizontal_radius_m": horizontal, "path_shape": "spin_in_place",
                    "spin_deg": spin_deg, "pivot_heading_rad": angle}
    elif preset == "air_soar":
        # 一直線滑過去：距離大、上下只有一點點起伏、不爬升(使用者：不要往上飄)
        # ★路徑以擺放點為中心★(2026-09-14 使用者：老鷹從畫面中間起飛、1.3 秒就飛出去 ⇒ 要從最左邊飛進來)。
        #   擺放點是「看得到的位置」⇒ 從 -distance/2 飛到 +distance/2 = 從左邊入畫、橫越、從右邊出去。
        distance = 14.0 * scale
        vertical = 0.25 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [
            direction * (distance * (fraction - 0.5))
            + z_axis * (vertical * math.sin(math.pi * fraction))
            for fraction in fractions
        ]
        metadata = {"travel_distance_m": distance, "vertical_arc_m": vertical,
                    "net_climb_m": 0.0, "path_centered_on_anchor": True}
    elif preset == "afloat_forward":
        # 水面是平的 ⇒ 只在水平面上直走，不動 Z(帆船/獨木舟往前漂)
        distance = 6.0 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [direction * (distance * fraction) for fraction in fractions]
        metadata = {"travel_distance_m": distance}
    elif preset in {"ground_forward", "directed_ground_traverse"}:
        distance = 2.2 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [direction * (distance * fraction) for fraction in fractions]
        metadata = {"travel_distance_m": distance}
    elif preset == "ground_hop":
        height = 0.45 * scale
        fractions = [0.0, 0.25, 0.50, 0.75, 1.0]
        offsets = [Vector((0.0, 0.0, 0.0)), z_axis * height,
                   Vector((0.0, 0.0, 0.0)), z_axis * height,
                   Vector((0.0, 0.0, 0.0))]
        metadata = {"hop_height_m": height, "hop_count": 2.0}
    else:  # validate_runtime_request makes this unreachable.
        raise ValueError(f"no path builder for {preset!r}")

    # (2026-09-13 使用者：位移動畫不用錯開起始相位，錯開骨架/翅膀動畫就好 ⇒ 這裡不旋轉控制點)
    metadata.update({
        "heading_deg": float(math.degrees(angle) % 360.0),
        "speed_scale": speed_scale,
        "aquatic_speed_factor": speed_factor_for(preset),
        "heading_source": "agent_param" if heading_deg is not None else "deterministic_seed",
    })
    return fractions, offsets, metadata


def _clear_motion_action(anchor) -> None:
    """把這支自己打上去的 BI_WORLD_MOTION action 拿掉，讓物件回到靜止。"""
    data = getattr(anchor, "animation_data", None)
    action = getattr(data, "action", None)
    if action is not None and str(action.name).startswith("BI_WORLD_MOTION::"):
        data.action = None
        try:
            import bpy
            bpy.data.actions.remove(action)
        except (ImportError, RuntimeError, ReferenceError):
            pass


def apply_world_motion(anchor, request, *, scene, phase_seed: int = 0,
                       terrain=None, measure=None) -> dict:
    """Apply one controlled-v3 preset to a parentless populated placeholder anchor."""
    import bpy
    from mathutils import Vector

    canonical = validate_frame_range(
        request, int(scene.frame_start), int(scene.frame_end))
    preset = canonical["preset"]
    result = {
        "world_motion_contract_version": 3,
        "object": anchor.name,
        "preset": preset,
        "intensity": canonical["intensity"],
        "applied": preset == "none",
        "action": None,
        "max_world_displacement_m": 0.0,
    }
    if preset == "none":
        return result
    if anchor.parent is not None:
        raise ValueError(
            f"world motion anchor {anchor.name!r} has parent {anchor.parent.name!r}; "
            "controlled v3 requires a parentless anchor so location is world-space"
        )
    transform_constraints = {
        "COPY_LOCATION", "COPY_TRANSFORMS", "CHILD_OF", "FOLLOW_PATH",
        "CLAMP_TO", "LIMIT_LOCATION", "TRANSFORM",
    }
    conflicts = [constraint.name for constraint in anchor.constraints
                 if (not constraint.mute and constraint.type in transform_constraints
                     and constraint.type != "FOLLOW_PATH")]
    if conflicts:
        raise ValueError(
            f"world motion anchor {anchor.name!r} has conflicting constraints: {conflicts}")
    old_action = getattr(getattr(anchor, "animation_data", None), "action", None)
    if old_action is not None and not old_action.name.startswith("BI_WORLD_MOTION::"):
        raise ValueError(
            f"world motion would overwrite existing action {old_action.name!r} on {anchor.name!r}"
        )
    # Official airborne factories may attach Follow Path to the new placeholder
    # anchor during populate. An explicit controlled world-motion request is the
    # position controller for this anchor, so disable only that replaceable root
    # controller. Constraints on asset children (rig/local animation) are not
    # touched, and every other transform constraint remains a hard conflict.
    disabled_constraints = []
    evaluated_world = anchor.matrix_world.copy()
    if any(not constraint.mute and constraint.type == "FOLLOW_PATH"
           for constraint in anchor.constraints):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_world = anchor.evaluated_get(depsgraph).matrix_world.copy()
    for constraint in anchor.constraints:
        if not constraint.mute and constraint.type == "FOLLOW_PATH":
            constraint.mute = True
            disabled_constraints.append({
                "name": constraint.name,
                "type": constraint.type,
                "reason": "replaced by explicit controlled world motion",
            })
    if disabled_constraints:
        bpy.context.view_layer.update()
        anchor.matrix_world = evaluated_world
        bpy.context.view_layer.update()
    rng = random.Random(int(phase_seed))
    motion_params = dict(canonical["params"])
    if preset == "afloat_forward" and motion_params.get("heading_deg") is None:
        # 船要往「船頭」的方向走，不然會側著漂
        motion_params["heading_deg"] = math.degrees(float(anchor.rotation_euler.z))
    fractions, offsets, metadata = _motion_path(
        preset, canonical["intensity"], rng, Vector, motion_params)
    frames = _key_frames(
        int(scene.frame_start), int(scene.frame_end), fractions, preset)

    original_frame = int(scene.frame_current)
    base = anchor.location.copy()
    base_rotation = anchor.rotation_euler.copy()
    action_name = f"BI_WORLD_MOTION::{preset}::{anchor.name}"
    if old_action is not None:
        bpy.data.actions.remove(old_action)
    anchor.animation_data_create()
    action = bpy.data.actions.new(action_name)
    anchor.animation_data.action = action

    try:
        terrain_reports = []
        ground_preset = preset in GROUND_PRESETS
        hop_preset = preset == "ground_hop"
        looping = preset in LOOPING_PRESETS
        if ground_preset:
            if not terrain or not measure:
                raise ValueError(f"{preset} requires terrain and populated geometry")
            import placement_modes as pm

            prepared = pm.prepare_terrains(terrain)
            if not prepared:
                raise ValueError(f"{preset} found no evaluated terrain geometry")
            lo, hi = pm.world_bbox(measure)
            if lo is None:
                raise ValueError(f"{preset} cannot measure populated asset geometry")
            footprint = float(max(hi[0] - lo[0], hi[1] - lo[1]) / 2)
            # Solve every frame, not just sparse control points.  Linear
            # interpolation between two correctly settled keys can still cut
            # through a ridge or float over a depression.
            path_frames = list(range(int(scene.frame_start), int(scene.frame_end) + 1))
            path_offsets = []
            control_index = 0
            for frame in path_frames:
                while (control_index + 1 < len(frames)
                       and frame > frames[control_index + 1]):
                    control_index += 1
                if control_index + 1 >= len(frames):
                    path_offsets.append(offsets[-1].copy())
                    continue
                left_frame, right_frame = frames[control_index], frames[control_index + 1]
                alpha = ((frame - left_frame) / (right_frame - left_frame)
                         if right_frame != left_frame else 0.0)
                path_offsets.append(offsets[control_index].lerp(
                    offsets[control_index + 1], alpha))
            spin_rad = (math.radians(float(metadata["spin_deg"]))
                        if metadata.get("path_shape") == "spin_in_place" else None)
            if spin_rad is not None:
                # 位置也逐幀解析算(不從 16 個控制點線性內插)，朝向逐幀連續轉 ⇒ 沒有跳格
                span = max(1, path_frames[-1] - path_frames[0])
                pivot = float(metadata["pivot_heading_rad"])
                d = Vector((math.cos(pivot), math.sin(pivot), 0.0))
                s = Vector((-d.y, d.x, 0.0))
                r = float(metadata["horizontal_radius_m"])
                path_offsets = []
                for frame in path_frames:
                    f = (frame - path_frames[0]) / span
                    path_offsets.append(d * r + (-d * math.cos(math.tau * f) + s * math.sin(math.tau * f)) * r)
                path_offsets[0] = Vector((0.0, 0.0, 0.0))
                path_offsets[-1] = Vector((0.0, 0.0, 0.0))
            for index, (frame, offset) in enumerate(zip(path_frames, path_offsets)):
                target = base + offset
                if spin_rad is not None:
                    heading = None
                    yaw = float(base_rotation.z) + spin_rad * (frame - path_frames[0]) / max(1, path_frames[-1] - path_frames[0])
                elif looping and index == len(path_offsets) - 1:
                    # A closed ground path must return to the same pose as well
                    # as the same XY.  Different final yaw changes which foot is
                    # lowest and therefore changes settled Z, creating a visible
                    # loop seam even when the horizontal offset is exactly zero.
                    heading = path_offsets[1] - path_offsets[0]
                elif index + 1 < len(path_offsets):
                    heading = path_offsets[index + 1] - offset
                else:
                    heading = offset - path_offsets[index - 1]
                if spin_rad is None:
                    heading_xy = math.hypot(float(heading.x), float(heading.y))
                    yaw = (math.atan2(float(heading.y), float(heading.x))
                           if heading_xy > 1e-6 else float(base_rotation.z))
                support = pm.support_normal(terrain, target, footprint)
                normal, ground_z = support
                if ground_z is None:
                    raise RuntimeError(f"{preset} terrain ray miss at frame {frame}")
                slope = math.degrees(math.acos(max(-1.0, min(1.0, float(normal.z)))))
                if slope > GROUND_MOTION_MAX_SLOPE_DEG:
                    # ★路徑走到太陡就原地不動，不要讓整個 case 失敗★(2026-09-12 forest：
                    #   相機前的坡全是 45°，狐狸放在那裡才看得到，ground_wander 卻要求 ≤35°)。
                    #   擺放已經允許到 60°(simple_volume_placement.RELAXED_SLOPE_DEG)，
                    #   這裡只把「會滑下去」的 wander 收掉，物件本身照樣在畫面上。
                    result.update(applied=False, skipped=True,
                                  skip_reason=f"ground slope {slope:.1f}° > {GROUND_MOTION_MAX_SLOPE_DEG}°",
                                  max_world_displacement_m=0.0)
                    _clear_motion_action(anchor)
                    pm.place(anchor, terrain, base, "conform", float(base_rotation.z),
                             footprint_radius=footprint, support=support, bbox_objs=measure)
                    print(f"[world-motion] ⚠️ {anchor.name}: {result['skip_reason']} ⇒ 改成靜止", flush=True)
                    return result
                pm.place(anchor, terrain, target, "conform", yaw,
                         footprint_radius=footprint, support=support, bbox_objs=measure)
                hop_lift = max(0.0, float(offset.z)) if hop_preset else 0.0
                if hop_lift:
                    anchor.location.z += hop_lift
                gap, n_hit, n_points = pm.contact_gap(measure, prepared)
                if gap is None or n_hit <= 0:
                    # ★繞圈走到冰緣外(腳下打不到地形)也改成原地不動，不要判死整個 case★
                    #   (2026-09-13 arctic：企鵝 ground_wander 第 31 幀 contact audit ray miss ⇒ placer FAIL)。
                    #   做法同上面的坡度過陡：清掉位移、放回起點，物件照樣在畫面上。
                    result.update(applied=False, skipped=True,
                                  skip_reason=f"contact audit ray miss at frame {frame}",
                                  max_world_displacement_m=0.0)
                    _clear_motion_action(anchor)
                    base_support = pm.support_normal(terrain, base, footprint)
                    pm.place(anchor, terrain, base, "conform", float(base_rotation.z),
                             footprint_radius=footprint, support=base_support, bbox_objs=measure)
                    print(f"[world-motion] ⚠️ {anchor.name}: {result['skip_reason']} ⇒ 改成靜止", flush=True)
                    return result
                landing = not hop_preset or hop_lift <= 1e-4
                if landing and abs(float(gap)) > 0.30:
                    raise RuntimeError(
                        f"{preset} contact gap {gap:+.4f}m exceeds 0.30m at frame {frame}")
                if hop_preset and not landing and abs(float(gap) - hop_lift) > 0.30:
                    raise RuntimeError(
                        f"{preset} airborne gap {gap:+.4f}m disagrees with "
                        f"lift {hop_lift:.4f}m at frame {frame}")
                if spin_rad is not None:
                    # ★原地轉：最後一幀的位置硬設回第一幀★(2026-09-14 arctic/coast：轉了 72° 之後腳底最低點不同
                    #   ⇒ conform 落地的 Z 差 0.5mm~1.2cm ⇒ 閉合檢查 1e-4 FAIL)。差 1cm 看不出來，contact gap 上面已檢查過。
                    if index == 0:
                        spin_first_location = anchor.location.copy()
                    elif index == len(path_offsets) - 1:
                        anchor.location = spin_first_location
                anchor.keyframe_insert(data_path="location", frame=frame, group="World Motion")
                anchor.keyframe_insert(data_path="rotation_euler", frame=frame,
                                       group="World Motion")
                terrain_reports.append({
                    "frame": frame,
                    "ground_z_m": float(ground_z),
                    "slope_deg": float(slope),
                    "contact_gap_m": float(gap),
                    "contact_hits": int(n_hit),
                    "contact_points": int(n_points),
                    "hop_lift_m": hop_lift,
                    "landing": landing,
                })
            frames = path_frames
        else:
            for frame, offset in zip(frames, offsets):
                anchor.location = base + offset
                anchor.keyframe_insert(data_path="location", frame=frame, group="World Motion")
        interpolation = "LINEAR" if preset in {
            "directed_traverse", "ground_wander", "ground_forward", "ground_hop",
            "terrain_wander", "directed_ground_traverse"
        } else "BEZIER"
        for curve in action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = interpolation
                if interpolation == "BEZIER":
                    point.handle_left_type = "AUTO_CLAMPED"
                    point.handle_right_type = "AUTO_CLAMPED"

        samples = []
        for frame in frames:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            samples.append({"frame": frame, "world": _world_translation(anchor)})
        origin = Vector(samples[0]["world"])
        max_displacement = max((Vector(sample["world"]) - origin).length
                               for sample in samples)
        final_displacement = (Vector(samples[-1]["world"]) - origin).length
        if max_displacement < MIN_WORLD_DISPLACEMENT_M:
            raise RuntimeError(
                f"world motion validation found only {max_displacement:.6f}m displacement"
            )
        if looping and final_displacement > 1e-4:
            raise RuntimeError(
                f"looping world motion closes with {final_displacement:.6f}m error"
            )
        if not looping and final_displacement < MIN_WORLD_DISPLACEMENT_M:
            raise RuntimeError(
                f"one-way world motion ends only {final_displacement:.6f}m from start"
            )
    finally:
        scene.frame_set(original_frame)
        bpy.context.view_layer.update()

    anchor["bi_world_motion_preset"] = preset
    anchor["bi_world_motion_intensity"] = canonical["intensity"]
    anchor["bi_world_motion_params"] = json.dumps(canonical["params"], sort_keys=True)
    anchor["bi_world_motion_base"] = list(base)
    result.update(
        applied=True,
        action=action.name,
        frames=frames,
        samples=samples,
        max_world_displacement_m=float(max_displacement),
        final_world_displacement_m=float(final_displacement),
        looping=looping,
        closed_loop_error_m=float(final_displacement) if looping else None,
        path_type="closed_bounded" if looping else "one_way",
        disabled_anchor_constraints=disabled_constraints,
        **metadata,
    )
    if terrain_reports:
        landing_reports = [item for item in terrain_reports if item.get("landing", True)]
        result.update(
            terrain_following=True,
            terrain_samples=terrain_reports,
            terrain_ray_miss_count=0,
            max_abs_ground_contact_gap_m=max(
                abs(item["contact_gap_m"]) for item in terrain_reports),
            max_slope_deg=max(item["slope_deg"] for item in terrain_reports),
            max_abs_landing_contact_gap_m=max(
                abs(item["contact_gap_m"]) for item in landing_reports),
            airborne_frame_count=sum(
                1 for item in terrain_reports if not item.get("landing", True)),
        )
    return result

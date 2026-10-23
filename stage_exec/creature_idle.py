#!/usr/bin/env python
"""地面動物的原地呼吸(2026-09-13 使用者要求)。

★為什麼不用官方骨架★：director/animation_catalog.json 裡 Carnivore/Herbivore 只有 run 是
user_verified_success，idle 是 source_declared_only 且 director_enabled=false；BirdFactory
三個模式都被標成會崩。所以「站著的動物」目前拿不到任何官方的原地動畫。

★這裡的做法★(參考 08-20 experiments/agent_rigging_poc 的 external_breathe)：
對 spawn_asset 根物件的 scale 打一個很小的正弦 driver ——
    scale_z = 1 + a*sin(ωt)，scale_x/y = 1 - 0.35*a*sin(ωt)
像胸腔起伏。整隻一起縮放(剛體)，★關節不會分離★；原點實測在腳底(local_z 從 0.01 起)，
所以腳不會浮起來。預設 a=0.012(1.2%)、週期 4 秒。
"""

from __future__ import annotations

import hashlib
import math

CREATURE_HINTS = ("Carnivore", "Herbivore", "Bird", "Fish", "Beetle", "Lobster",
                  "Crab", "Chameleon", "Snake", "Dragonfly", "Clam")
# 2026-09-13 使用者：動物呼吸都加大(1.2% 在遠處完全看不出來) ⇒ 3%
DEFAULT_AMPLITUDE = 0.03
DEFAULT_PERIOD_S = 4.0


def _phase(name: str) -> float:
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def add(obj, *, amplitude: float = DEFAULT_AMPLITUDE,
        period_s: float = DEFAULT_PERIOD_S, fps: float = 24.0) -> bool:
    """給一隻動物加原地呼吸。已經有 scale driver 就不重複加。"""
    if obj is None:
        return False
    data = getattr(obj, "animation_data", None)
    if data is not None and any(d.data_path == "scale" for d in getattr(data, "drivers", [])):
        return False
    period_frames = max(4.0, float(period_s) * float(fps) * (0.8 + 0.4 * _phase(obj.name)))
    omega = 2.0 * math.pi / period_frames
    offset = _phase(obj.name + "o") * period_frames
    base = tuple(obj.scale)
    for axis in range(3):
        gain = float(amplitude) * (1.0 if axis == 2 else -0.35)
        fcurve = obj.driver_add("scale", axis)
        driver = fcurve.driver
        driver.type = "SCRIPTED"
        driver.expression = (
            f"{base[axis]:.6f}*(1+{gain:.6f}*sin({omega:.6f}*(frame+{offset:.2f})))")
    return True


def add_to_scene(*, amplitude: float = DEFAULT_AMPLITUDE,
                 period_s: float = DEFAULT_PERIOD_S, fps: float = 24.0,
                 skip_names=()) -> dict:
    """所有沒有位移動畫的動物都加原地呼吸。回傳統計。"""
    import bpy
    from mathutils import Vector
    skip = set(skip_names)
    done, skipped = [], 0
    for obj in bpy.context.scene.objects:
        if ".spawn_asset(" not in obj.name:
            continue
        if not any(hint in obj.name for hint in CREATURE_HINTS):
            continue
        # 已經有位移動畫的不要再加(避免和 world_motion 疊在一起)
        anchor = obj.parent
        if anchor is not None and str(anchor.get("bi_world_motion_preset") or "none") != "none":
            skipped += 1
            continue
        if obj.name in skip:
            skipped += 1
            continue
        meshes = [child for child in obj.children_recursive if child.type == "MESH"]
        if not meshes and obj.type == "MESH":
            meshes = [obj]
        if not meshes:
            skipped += 1
            continue
        inverse = obj.matrix_world.inverted()
        zs = [(inverse @ (mesh.matrix_world @ Vector(corner))).z
              for mesh in meshes for corner in mesh.bound_box]
        height = max(zs) - min(zs)
        if height <= 1e-6 or abs(min(zs)) > 0.2 * height:
            skipped += 1              # 原點不在腳底，縮放會讓它浮起來
            continue
        if add(obj, amplitude=amplitude, period_s=period_s, fps=fps):
            done.append(obj.name)
    return {"breathing": len(done), "skipped": skipped,
            "amplitude": float(amplitude), "period_s": float(period_s)}

#!/usr/bin/env python
"""整棵樹以根部為軸的微幅搖擺(2026-09-12)。

★為什麼不是位移葉子★：成品場景的樹是烤好的單一 mesh(最近的那幾棵 400 萬~1100 萬頂點)。
用 Geometry Nodes 逐幀位移頂點，實測畫面差只有 0.36(看不出來)，渲染卻慢 70%(BVH 每幀重建)。

★這裡的做法★：樹物件的原點就在根部(實測 bbox 最低點誤差 <0.5m)，所以對 rotation_euler
的 X/Y 打一個很小的正弦 driver = 整棵樹繞著根部左右晃。剛體旋轉：
  - 關節絕對不會分離(整棵一起轉)
  - 根部不動
  - 幾何不用重算，只有物件變換，渲染成本幾乎是零
每棵樹用自己的名字算相位與頻率，不會整片同步擺動。
"""

from __future__ import annotations

import hashlib
import math

# ★0.8→1.6°★(2026-09-13 使用者：搖擺幅度大一點)。7m 的樹在樹梢約 ±20cm
DEFAULT_DEGREES = 1.6
DEFAULT_PERIOD_S = 6.0     # 一個來回幾秒
DEFAULT_MAX_DISTANCE_M = 150.0   # 超過這個距離的樹不加(看不出來，白付成本)
DEFAULT_MIN_VERTICES = 500


def _phase(name: str, salt: str) -> float:
    digest = hashlib.sha256(f"{name}|{salt}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def add(obj, *, degrees: float = DEFAULT_DEGREES,
        period_s: float = DEFAULT_PERIOD_S, fps: float = 24.0) -> bool:
    """對一棵樹加上繞根部的微幅搖擺 driver。已經有就不重複加。"""
    if obj is None:
        return False
    data = getattr(obj, "animation_data", None)
    if data is not None and any(
            d.data_path == "rotation_euler" for d in getattr(data, "drivers", [])):
        return False
    obj.rotation_mode = "XYZ"
    magnitude = math.radians(float(degrees))
    # 每棵樹的頻率抖動 ±25%，相位隨機 ⇒ 不會整片同步
    wobble = 0.75 + 0.5 * _phase(obj.name, "freq")
    period_frames = max(4.0, float(period_s) * float(fps) * wobble)
    omega = 2.0 * math.pi / period_frames
    base = obj.rotation_euler.copy()
    for axis in (0, 1):
        fcurve = obj.driver_add("rotation_euler", axis)
        driver = fcurve.driver
        driver.type = "SCRIPTED"
        offset = _phase(obj.name, f"phase{axis}") * period_frames
        # X 與 Y 差 90 度 ⇒ 樹梢畫出一個小橢圓，比單軸來回自然
        quarter = period_frames * 0.25 * axis
        driver.expression = (
            f"{base[axis]:.6f}+{magnitude:.6f}*sin({omega:.6f}*(frame+{offset + quarter:.3f}))")
    return True


def _sway_target(obj):
    """要轉的是★整棵★，不是單一片葉子。

    (2026-09-13 desert：19 棵棕櫚只有 3 塊 mesh 過門檻，其餘 201 塊 <500 頂點被跳過 ⇒
     畫面幾乎全靜止。往上找到 spawn_asset 根，整棵一起轉，關節不會分離。)
    """
    target = obj
    node = obj
    while node is not None:
        if ".spawn_asset(" in node.name:
            target = node
        node = node.parent
    return target


# 每一類的擺幅倍率(相對 degrees)：仙人掌是硬的，只給一點點
SWAY_GROUPS = (
    (("Tree", "Palm", "Branch"), 1.0),
    (("Bush", "Shrub", "Fern"), 0.9),
    (("Cactus",), 0.3),
)


def add_to_scene(*, camera=None, max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
                 min_vertices: int = DEFAULT_MIN_VERTICES, degrees: float = DEFAULT_DEGREES,
                 period_s: float = DEFAULT_PERIOD_S, name_hints=None,
                 fps: float = 24.0) -> dict:
    """給場景裡看得到的植物加上搖擺(整棵一起轉)。回傳統計。"""
    import bpy
    from mathutils import Vector
    scene = bpy.context.scene
    camera = camera or scene.camera
    origin = camera.matrix_world.translation if camera is not None else None
    groups = (((tuple(name_hints), 1.0),) if name_hints else SWAY_GROUPS)
    done, skipped, skipped_offcentre, skipped_small = [], 0, 0, 0
    targets = {}
    for obj in scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        gain = next((g for hints, g in groups if any(h in obj.name for h in hints)), None)
        if gain is None:
            continue
        if origin is not None:
            centre = sum((obj.matrix_world @ Vector(corner) for corner in obj.bound_box),
                         Vector()) / 8.0
            if (centre - origin).length > float(max_distance_m):
                skipped += 1
                continue
        target = _sway_target(obj)
        # 整棵的頂點數加總才比門檻(單片葉子本來就 <500)
        entry = targets.setdefault(target.name, {"object": target, "verts": 0, "gain": gain})
        entry["verts"] += len(obj.data.vertices)
        entry["gain"] = max(entry["gain"], gain)
    for entry in targets.values():
        target = entry["object"]
        if entry["verts"] < int(min_vertices):
            skipped_small += 1
            continue
        meshes = [m for m in (target, *target.children_recursive) if m.type == "MESH"]
        if not meshes:
            skipped_small += 1
            continue
        lowest = min((m.matrix_world @ Vector(corner)).z
                     for m in meshes for corner in m.bound_box)
        offset = abs(float(target.matrix_world.translation.z) - lowest)
        if offset > 2.5:
            skipped_offcentre += 1
            continue
        gain = entry["gain"] * (1.0 if offset <= 1.0 else 0.5)
        if add(target, degrees=degrees * gain, period_s=period_s, fps=fps):
            done.append(target.name)
    return {"swayed": len(done), "candidates": len(targets),
            "skipped_far_or_offcentre": skipped + skipped_offcentre,
            "skipped_far": skipped, "skipped_offcentre": skipped_offcentre,
            "skipped_small": skipped_small,
            "degrees": float(degrees), "period_s": float(period_s)}

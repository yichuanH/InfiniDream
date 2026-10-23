#!/usr/bin/env python
"""鳥類拍翅(2026-09-13)。

★為什麼不用骨架★：官方 FlyingBirdFactory 的 idle 模式在 director/animation_catalog.json
被標成會崩(user_reported_failure)，08-20 的外掛骨架 PoC(experiments/agent_rigging_poc)
需要在「零件還沒合併前」抓翅膀，而成品場景裡整隻鳥已經併成一個 mesh，接不進去。

★這裡的做法★：成品 mesh 還留著頂點屬性 tag_wing / tag_body / tag_tail。
用 Geometry Nodes 把「標記為翅膀」的頂點繞著身體長軸(local X)旋轉：

    angle = max_angle * sin(2π·frame/period) * sign(y) * weight(|y|)
    weight = clamp(|y| / 翼展半長) ^ 1.5        # 翅根 0、翅尖 1

翅根的權重是 0 ⇒ 和身體接合處不會裂開；兩邊 sign 相反 ⇒ 一起上下拍。
"""

from __future__ import annotations

import math

NODE_GROUP_PREFIX = "BI_BIRD_FLAP"
WING_TAG = "tag_wing"


def _group(name: str, *, max_degrees: float, period_frames: float,
           half_span: float, phase_frames: float):
    import bpy
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    interface = group.interface
    interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    gin, gout = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")

    position = nodes.new("GeometryNodeInputPosition")
    sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(position.outputs[0], sep.inputs[0])

    # weight = clamp(|y| / half_span) ** 1.5
    absy = nodes.new("ShaderNodeMath"); absy.operation = "ABSOLUTE"
    links.new(sep.outputs["Y"], absy.inputs[0])
    norm = nodes.new("ShaderNodeMath"); norm.operation = "DIVIDE"; norm.use_clamp = True
    links.new(absy.outputs[0], norm.inputs[0])
    norm.inputs[1].default_value = max(1e-3, float(half_span))
    weight = nodes.new("ShaderNodeMath"); weight.operation = "POWER"
    links.new(norm.outputs[0], weight.inputs[0]); weight.inputs[1].default_value = 1.5

    # 只動標記為翅膀的頂點
    tag = nodes.new("GeometryNodeInputNamedAttribute")
    tag.data_type = "FLOAT"; tag.inputs["Name"].default_value = WING_TAG
    masked = nodes.new("ShaderNodeMath"); masked.operation = "MULTIPLY"
    links.new(weight.outputs[0], masked.inputs[0])
    links.new(tag.outputs["Attribute"], masked.inputs[1])

    # sin(2π·(frame+phase)/period)
    scene_time = nodes.new("GeometryNodeInputSceneTime")
    shifted = nodes.new("ShaderNodeMath"); shifted.operation = "ADD"
    links.new(scene_time.outputs["Frame"], shifted.inputs[0])
    shifted.inputs[1].default_value = float(phase_frames)
    omega = nodes.new("ShaderNodeMath"); omega.operation = "MULTIPLY"
    links.new(shifted.outputs[0], omega.inputs[0])
    omega.inputs[1].default_value = 2.0 * math.pi / max(2.0, float(period_frames))
    wave = nodes.new("ShaderNodeMath"); wave.operation = "SINE"
    links.new(omega.outputs[0], wave.inputs[0])

    # angle = max_angle * wave * weight * sign(y)
    sign = nodes.new("ShaderNodeMath"); sign.operation = "SIGN"
    links.new(sep.outputs["Y"], sign.inputs[0])
    amp = nodes.new("ShaderNodeMath"); amp.operation = "MULTIPLY"
    links.new(wave.outputs[0], amp.inputs[0])
    amp.inputs[1].default_value = math.radians(float(max_degrees))
    a2 = nodes.new("ShaderNodeMath"); a2.operation = "MULTIPLY"
    links.new(amp.outputs[0], a2.inputs[0]); links.new(masked.outputs[0], a2.inputs[1])
    angle = nodes.new("ShaderNodeMath"); angle.operation = "MULTIPLY"
    links.new(a2.outputs[0], angle.inputs[0]); links.new(sign.outputs[0], angle.inputs[1])

    # 繞身體長軸(local X)轉
    rotate = nodes.new("ShaderNodeVectorRotate")
    rotate.rotation_type = "AXIS_ANGLE"
    links.new(position.outputs[0], rotate.inputs["Vector"])
    rotate.inputs["Center"].default_value = (0.0, 0.0, 0.0)
    rotate.inputs["Axis"].default_value = (1.0, 0.0, 0.0)
    links.new(angle.outputs[0], rotate.inputs["Angle"])

    set_pos = nodes.new("GeometryNodeSetPosition")
    links.new(gin.outputs[0], set_pos.inputs["Geometry"])
    links.new(rotate.outputs["Vector"], set_pos.inputs["Position"])
    links.new(set_pos.outputs["Geometry"], gout.inputs[0])
    return group


def add(obj, *, max_degrees: float = 18.0, period_frames: float = 24.0,
        phase_frames: float = 0.0) -> bool:
    """給一隻已經合併好的鳥 mesh 加拍翅。沒有 tag_wing 就不動它。"""
    if obj is None or obj.type != "MESH" or obj.data is None:
        return False
    if WING_TAG not in {a.name for a in obj.data.attributes}:
        return False
    name = f"{NODE_GROUP_PREFIX}_{abs(hash(obj.name)) % 100000}"
    if any(m.type == "NODES" and getattr(m.node_group, "name", "").startswith(NODE_GROUP_PREFIX)
           for m in obj.modifiers):
        return False
    ys = [abs(v.co.y) for v in obj.data.vertices]
    half_span = max(ys) if ys else 1.0
    group = _group(name, max_degrees=max_degrees, period_frames=period_frames,
                   half_span=half_span, phase_frames=phase_frames)
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = group
    return True


def add_to_scene(*, max_degrees: float = 18.0, period_frames: float = 24.0,
                 name_hints=("flying_bird",), skip_names=()) -> dict:
    """場景裡所有帶 tag_wing 的鳥都加拍翅，每隻相位不同。

    skip_names：不要加拍翅的物件名(2026-09-13 使用者：老鷹的翅膀動畫看起來怪，
    改成只用快速前飛的位移)。由 placer 依 BI_BIRD_FLAP_EXCLUDE 的資產 id 展開。
    """
    import bpy
    skip = set(skip_names)
    done, skipped = [], 0
    for index, obj in enumerate(bpy.context.scene.objects):
        if obj.type != "MESH" or obj.hide_render or obj.data is None:
            continue
        if not any(hint in obj.name for hint in name_hints):
            continue
        if obj.name in skip:
            skipped += 1
            continue
        if add(obj, max_degrees=max_degrees, period_frames=period_frames,
               phase_frames=(index * 7) % int(max(2, period_frames))):
            done.append(obj.name)
    return {"flapped": len(done), "skipped_by_request": skipped,
            "max_degrees": float(max_degrees),
            "period_frames": float(period_frames)}

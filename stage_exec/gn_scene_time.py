"""Activate a populated asset's own Scene-Time animation (wing flap).

★為什麼需要這一支★(2026-09-11)
  DragonflyFactory(蝴蝶/蜻蜓/蜜蜂都走它)★沒有 animation_mode、也沒有 armature★，
  但它的 geometry nodes 本來就用 Scene Time 驅動翅膀：
      SceneTime.Seconds → MATH MULTIPLY(頻率) → MATH SINE → MATH MULTIPLY(振幅)
  問題是★生成的變體常常把振幅寫成 0★(anim_robustness tier-0 實測：blue_morpho /
  violet_peacock 的 Flap Mag 被改成 0.0，全程沒有任何警告)，而官方預設的 20-50Hz
  在 24fps 下也會頻閃。0820 的 idle-adapter PoC 把它正規化成 1Hz / 0.22 之後
  蝴蝶就正常拍翅(glowing_blue_butterfly / luna_moth_r3 / mecha_dragonfly 都渲過)。

★與 PoC 的差別★：PoC 掃 `bpy.data.node_groups` 全部改。正式場景裡那會連
  ★場景原有的官方蜻蜓★一起改掉，所以這裡只走「這批資產的 modifier 用到的節點群」
  (含巢狀 group)，其餘一律不碰。

只改 instantiated 的 socket 值，不動 procedural source。
"""

from __future__ import annotations

import math

import bpy

SCENE_TIME_NODE = "GeometryNodeInputSceneTime"


def _node_groups_of(objects) -> list:
    """Node groups reachable from these objects' NODES modifiers (incl. nested)."""
    pending, seen = [], []
    for obj in objects:
        for child in (obj, *getattr(obj, "children_recursive", ())):
            for modifier in getattr(child, "modifiers", ()):
                group = getattr(modifier, "node_group", None)
                if modifier.type == "NODES" and group is not None:
                    pending.append(group)
    while pending:
        group = pending.pop()
        if any(group is known for known in seen):
            continue
        seen.append(group)
        for node in getattr(group, "nodes", ()):
            nested = getattr(node, "node_group", None)
            if nested is not None:
                pending.append(nested)
    return seen


def _wing_chain(scene_time):
    """(frequency socket, amplitude socket) of one SceneTime -> sine -> scale chain."""
    seconds = scene_time.outputs.get("Seconds")
    for link in list(getattr(seconds, "links", ())):
        frequency = link.to_node
        if frequency.type != "MATH" or frequency.operation != "MULTIPLY":
            continue
        frequency_socket = next(
            (socket for socket in frequency.inputs[:2] if not socket.is_linked), None)
        sine = next(
            (out.to_node for out in frequency.outputs[0].links
             if out.to_node.type == "MATH" and out.to_node.operation == "SINE"), None)
        if frequency_socket is None or sine is None:
            continue
        amplitude = next(
            (out.to_node for out in sine.outputs[0].links
             if out.to_node.type == "MATH" and out.to_node.operation == "MULTIPLY"), None)
        if amplitude is None:
            continue
        amplitude_socket = next(
            (socket for socket in amplitude.inputs[:2] if not socket.is_linked), None)
        if amplitude_socket is None:
            continue
        yield frequency, frequency_socket, amplitude, amplitude_socket, sine


def _insert_per_object_phase(group, frequency, sine) -> bool:
    """把 sin(ωt) 變成 sin(ωt + φ(物件位置))，★每隻的起始相位就不一樣了★。

    (2026-09-13 使用者：每隻蝴蝶的動畫開始時間要不一樣、兩隻蜻蜓不要一起動)
    母本池/linked duplicate 讓多隻共用同一個節點群 ⇒ 不能把相位寫成常數。
    用 Self Object → Object Info 的 Location 當相位來源：同一個節點群、每個物件算出
    不同的值，而且完全確定性(位置一樣就一樣)。
    """
    nodes, links = group.nodes, group.links
    if any(n.bl_idname == "GeometryNodeSelfObject" for n in nodes):
        return False                      # 這個節點群已經加過了
    target = sine.inputs[0]
    if not target.is_linked:
        return False
    self_obj = nodes.new("GeometryNodeSelfObject")
    info = nodes.new("GeometryNodeObjectInfo")
    links.new(self_obj.outputs[0], info.inputs["Object"])
    sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(info.outputs["Location"], sep.inputs[0])
    mx = nodes.new("ShaderNodeMath"); mx.operation = "MULTIPLY"
    links.new(sep.outputs["X"], mx.inputs[0]); mx.inputs[1].default_value = 1.7
    my = nodes.new("ShaderNodeMath"); my.operation = "MULTIPLY"
    links.new(sep.outputs["Y"], my.inputs[0]); my.inputs[1].default_value = 2.3
    phase = nodes.new("ShaderNodeMath"); phase.operation = "ADD"
    links.new(mx.outputs[0], phase.inputs[0]); links.new(my.outputs[0], phase.inputs[1])
    shifted = nodes.new("ShaderNodeMath"); shifted.operation = "ADD"
    links.new(frequency.outputs[0], shifted.inputs[0])
    links.new(phase.outputs[0], shifted.inputs[1])
    links.new(shifted.outputs[0], target)
    return True


def apply(objects, *, frequency_hz: float, flap_magnitude: float,
          per_object_phase: bool = True) -> dict:
    """Normalise every Scene-Time driven chain these objects own. -> report dict."""
    controls = []
    groups = _node_groups_of(objects)
    for group in groups:
        for node in [n for n in getattr(group, "nodes", ())
                     if n.bl_idname == SCENE_TIME_NODE]:
            for (frequency, frequency_socket, amplitude, amplitude_socket,
                 sine) in _wing_chain(node):
                old_frequency = float(frequency_socket.default_value) / (2 * math.pi)
                old_amplitude = float(amplitude_socket.default_value)
                frequency_socket.default_value = 2 * math.pi * float(frequency_hz)
                amplitude_socket.default_value = float(flap_magnitude)
                phased = False
                if per_object_phase:
                    try:
                        phased = _insert_per_object_phase(group, frequency, sine)
                    except Exception as error:   # 加不上就維持同步，不要弄壞這個 case
                        print(f"[gn] ⚠️ 逐物件相位加不上: {error}", flush=True)
                controls.append({
                    "per_object_phase": bool(phased),
                    "node_group": group.name,
                    "scene_time": node.name,
                    "frequency_node": frequency.name,
                    "amplitude_node": amplitude.name,
                    "old_frequency_hz": round(old_frequency, 4),
                    "new_frequency_hz": float(frequency_hz),
                    "old_flap_magnitude": round(old_amplitude, 4),
                    "new_flap_magnitude": float(flap_magnitude),
                })
    report = {
        "kind": "normalized_native_scene_time",
        "node_groups_scanned": [group.name for group in groups],
        "controls": controls,
        "applied": len(controls),
    }
    if not controls:
        # ⚠️ 不要靜靜通過 —— 靜止的蝴蝶跟「沒接上」長得一模一樣
        report.update(status="SKIPPED",
                      reason="no connected SceneTime -> sine -> amplitude chain found")
    return report


def measure_deformation(objects, frames) -> dict:
    """Max evaluated-vertex displacement (object space) across frames, as evidence."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    scene = bpy.context.scene
    reference, maximum = None, 0.0
    for frame in frames:
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        points = []
        for obj in objects:
            for child in (obj, *getattr(obj, "children_recursive", ())):
                if child.type != "MESH":
                    continue
                evaluated = child.evaluated_get(depsgraph)
                mesh = evaluated.to_mesh()
                points.extend(tuple(vertex.co) for vertex in mesh.vertices)
                evaluated.to_mesh_clear()
        if reference is None:
            reference = points
            continue
        if len(points) != len(reference):
            return {"frames": list(frames), "comparable": False}
        maximum = max(maximum, max(
            (math.dist(a, b) for a, b in zip(points, reference)), default=0.0))
    return {"frames": list(frames), "comparable": True,
            "max_vertex_displacement_m": round(maximum, 6)}

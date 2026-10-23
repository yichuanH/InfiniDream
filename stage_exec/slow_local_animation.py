#!/usr/bin/env python
"""把場景裡「按時間跑」的骨架動畫整體放慢(2026-09-12 使用者：動作太快像原地爆衝)。

兩種來源：
  1. infinigen creature 的 sinusoid driver：expression 形如 `0.0200*sin((3.7198*frame+-1959.3770)/(2*pi))`
     ——把 frame 的係數乘上 factor 就是放慢。
  2. GN 的 Scene Time 鏈：由 stage_exec/gn_scene_time.py 另外處理(director/animation_catalog.json 的 frequency_hz)。
"""

from __future__ import annotations

import re

_FREQ = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*\*\s*frame\b")


def slow_drivers(factor: float = 0.4) -> dict:
    """把所有 animation driver 的 frame 係數乘 factor。回傳統計。"""
    import bpy
    if not (0.0 < float(factor) < 1.0):
        return {"factor": float(factor), "changed": 0, "skipped": "factor out of range"}
    changed, objects = 0, set()
    for holder in list(bpy.data.objects) + list(bpy.data.shape_keys):
        data = getattr(holder, "animation_data", None)
        for fcurve in list(getattr(data, "drivers", []) or []):
            driver = fcurve.driver
            expression = str(getattr(driver, "expression", "") or "")
            if "frame" not in expression:
                continue
            def _scale(match):
                return f"{float(match.group(1)) * float(factor):.6f}*frame"
            new_expression = _FREQ.sub(_scale, expression)
            if new_expression != expression:
                driver.expression = new_expression
                changed += 1
                objects.add(getattr(holder, "name", "?"))
    return {"factor": float(factor), "changed": changed, "objects": len(objects)}

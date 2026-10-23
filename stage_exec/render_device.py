"""Cycles 渲染裝置設定 —— ★唯一來源★，而且★退回 CPU 一定要吵★。

★為什麼要這一支★(2026-09-08)
  run_e2e.sh 長期寫死 `GPUS=2,3`("★這台機器固定用 2,3★")，但這台機器現在
  只有 GPU 0 和 1。CUDA_VISIBLE_DEVICES=2 看不到任何裝置 ⇒
      preferences.get_devices() 只回 CPU
      for d in devices: d.use = d.type != "CPU"   ← 把全部關掉
      scene.cycles.device = "GPU"                  ← Cycles 找不到可用 GPU
  ⇒ ★靜默退回 CPU★，不報錯、不警告。實測後果：
      同一個 forest 場景 baseline 用 OptiX 渲一張 17 秒，
      我們的 pipeline 用 CPU 渲一張 ★82 分鐘★(log 裡 Building OptiX 出現 0 次、
      Building BVH 出現 117 次)。
  而且我們據此誤判了好幾輪「是不是資產太重」。⇒ 這種退回必須大聲講出來。
"""
from __future__ import annotations

import os

CPU_FALLBACK_BANNER = "BI_RENDER_DEVICE_FALLBACK_CPU"


def configure(scene, *, label: str = "render") -> str:
    """把 scene 設成 GPU(OptiX→CUDA)；失敗就退 CPU 並★印出醒目警告★。

    回傳實際使用的裝置字串("OPTIX" / "CUDA" / "CPU")。
    """
    import bpy

    requested = os.environ.get("BI_RENDER_DEVICE", "GPU").strip().upper()
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "(unset)")

    if requested == "CPU":
        scene.cycles.device = "CPU"
        print(f"[{label}] BI_RENDER_DEVICE=CPU（明確指定，不是退回）", flush=True)
        return "CPU"

    # ★BI_RENDER_DEVICE=CUDA：跳過 OptiX，直接用 CUDA★(2026-09-14 晚)
    #   forest seed0 第 12 步連三次死在「Failed to build OptiX acceleration structure /
    #   System is out of GPU memory」(729 個物件 + 粒子系統，光 BVH 就要 16~36G 顯存)。
    #   ★OptiX 的 BVH 不能溢出到主機記憶體，CUDA 可以★ ⇒ 顯存不夠時改走 CUDA 就能用主機 RAM 渲完
    #   (慢好幾倍，但會完成)。不設這個環境變數時行為完全不變，仍然 OptiX 優先。
    order = ("CUDA",) if requested == "CUDA" else ("OPTIX", "CUDA")
    if requested == "CUDA":
        print(f"[{label}] BI_RENDER_DEVICE=CUDA（明確指定，跳過 OptiX；顯存不夠時可溢出到主機 RAM）",
              flush=True)

    enabled = 0
    backend = None
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        # OptiX 優先(RTX 卡上明顯較快)，不行再試 CUDA。
        for candidate in order:
            try:
                prefs.compute_device_type = candidate
            except TypeError:
                continue
            prefs.get_devices()
            enabled = 0
            # ★只開「這個 backend 型別」的裝置★(2026-09-14 早上)：選 OPTIX 時 Cycles 仍會把同一張卡列兩次
            #   (CUDA_... 與 CUDA_..._OptiX)。舊寫法「非 CPU 全開」⇒ 同一張 4090 同時當 CUDA + OptiX 兩個裝置用，
            #   log 顯示「可用裝置 2 個 (CUDA_VISIBLE_DEVICES=1)」、場景上傳兩份 ⇒ 夜裡 5 個第 12 步全部 GPU OOM。
            for device in prefs.devices:
                device.use = device.type == candidate
                if device.use:
                    enabled += 1
            if enabled:
                backend = candidate
                break
    except Exception as error:  # noqa: BLE001
        print(f"[{label}] GPU 設定發生例外: {error}", flush=True)

    if enabled and backend:
        scene.cycles.device = "GPU"
        print(f"[{label}] ✅ GPU={backend} 可用裝置 {enabled} 個 "
              f"(CUDA_VISIBLE_DEVICES={visible})", flush=True)
        return backend

    # ★★退回 CPU —— 這是效能懸崖，不可以安靜地發生★★
    scene.cycles.device = "CPU"
    names = []
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        names = [f"{d.type}:{d.name}" for d in prefs.devices]
    except Exception:  # noqa: BLE001
        pass
    print("", flush=True)
    print("=" * 78, flush=True)
    print(f"⛔ {CPU_FALLBACK_BANNER}  [{label}] ★找不到可用的 GPU，退回 CPU 渲染★",
          flush=True)
    print(f"   CUDA_VISIBLE_DEVICES = {visible}", flush=True)
    print(f"   Cycles 看得到的裝置  = {names or '(無)'}", flush=True)
    print("   ⚠️ CPU 渲染會慢一到兩個數量級（實測同場景 17 秒 → 82 分鐘）。", flush=True)
    print("   常見原因：GPUS 指到不存在的卡（本機只有 0 和 1），或該卡被別人佔滿。", flush=True)
    print("   確認方式：nvidia-smi -L  /  nvidia-smi --query-compute-apps=pid,used_memory --format=csv",
          flush=True)
    print("=" * 78, flush=True)
    print("", flush=True)
    return "CPU"

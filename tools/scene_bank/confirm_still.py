"""重跑後的驗收圖：★直接用 scene.camera，不套任何 offset★。

相機已經在 bake_camera.py 那一步寫進 blend 了。這裡如果再套一次 offset，就會重演
2026-08-09「擺放相機 != 出圖相機」的 bug。這支腳本存在的意義，一半就是當那條規則的
可執行版本：★下游一律讀 scene.camera★。

驗收要看的是：
  ・構圖是否等於你在 preview 挑的那一張（應該逐字相同 —— 靜態 bake 的定義）
  ・地形有沒有平面粗三角形（有的話代表 fine_terrain 沒對齊，重跑失敗）
  ・原本的積木狀 placeholder 有沒有變成真的樹

用法:
  python confirm_still.py <scene.blend> <out.png> [--samples 32] [--res 900x506]
"""

import argparse
import sys
from pathlib import Path

import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("blend")
    ap.add_argument("out_png")
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--res", default="900x506")
    ap.add_argument("--frame", type=int, default=1)
    return ap.parse_args(argv)


def setup_gpu(scene):
    scene.render.engine = "CYCLES"
    scene.cycles.use_denoising = True
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = d.type != "CPU"
        scene.cycles.device = "GPU"
    except Exception as e:  # noqa: BLE001
        print("GPU setup warning:", e)


def main():
    args = parse_args()
    out = Path(args.out_png)
    out.parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=args.blend)
    scene = bpy.context.scene

    cam = scene.camera
    if cam is None:
        cams = [o for o in scene.objects if o.type == "CAMERA"]
        if not cams:
            raise SystemExit("no camera in blend")
        cam = scene.camera = cams[0]

    w, h = (int(x) for x in args.res.lower().split("x"))
    scene.render.resolution_x = w
    scene.render.resolution_y = h
    scene.render.resolution_percentage = 100
    scene.cycles.samples = args.samples
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    setup_gpu(scene)
    scene.frame_set(args.frame)

    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    pos = cam.evaluated_get(deps).matrix_world.translation.copy()
    print(f"[confirm] camera {cam.name} @ ({pos.x:.2f},{pos.y:.2f},{pos.z:.2f}) "
          f"frame={args.frame}", flush=True)

    # 去掉副檔名：Blender 會自己補 .png
    scene.render.filepath = str(out.with_suffix(""))
    bpy.ops.render.render(write_still=True)
    print("CONFIRM_DONE", out, flush=True)


if __name__ == "__main__":
    main()

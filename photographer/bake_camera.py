"""★把選定的相機真的寫進 blend（bake）★(2026-08-09)

★為什麼一定要 bake，不能渲染時才套 offset★
  今天最大的 bug 就是「兩台相機沒人對齊」：擺放用場景原生相機、出圖用 render_view
  抬高後的相機。誤差被距離放大 —— forest 落點 10m 時看得到(以為修好了)、
  snowy_mountain 落點 150m 時 14 個物件全部消失。
  ⇒ ★選完就寫進場景，之後沒有人再套 offset★。下游(apply_plan / placer / render)
    一律讀 scene.camera，這樣「相機」才只有一份定義。
  ⇒ bake 之後 populate 的 cull 與 ★LOD(近的高精度)★ 也會自動跟著新相機，
    因為 filter_populate_targets / spawn_asset(distance=) 讀的就是 scene.camera。

★保留軌跡，不塌成靜態★
  相機是一條 72 格的動畫(camrig.0Action)。render_view / composer_video 套 offset 之前
  都會 animation_data_clear() ⇒ ★「抬高的相機 + 原生軌跡」現有工具做不出來★。
  這裡的做法是★逐格★把 offset 疊在原軌跡上再重新 key，兩者兼得。

用法:
  python bake_camera.py -- <in.blend> <out.blend> <camera_choice.json> [--static]
  (--static: 只保留 frame 1 的姿態，不保留軌跡)
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
IN_BLEND, OUT_BLEND, CHOICE_JSON = argv[0], argv[1], argv[2]
# 預設★靜態★：與預覽逐字相同的姿態(見下面 base 的說明)。
# --trajectory: 逐格把 offset 疊在原軌跡上(會動，但★就不等於預覽那一張★)。
TRAJ = "--trajectory" in argv

cfg = json.loads(Path(CHOICE_JSON).read_text(encoding="utf-8"))["cfg"]
AERIAL, PITCH = float(cfg["aerial"]), float(cfg["pitch"])
BACK, SIDE, YAW = float(cfg["back"]), float(cfg["side"]), float(cfg["yaw"])
NATIVE_POSE = not any((AERIAL, PITCH, BACK, SIDE, YAW))
print(f"[bake] 套用 {cfg}", flush=True)

bpy.ops.wm.open_mainfile(filepath=IN_BLEND)
scene = bpy.context.scene
cam = scene.camera or next(o for o in scene.objects if o.type == "CAMERA")
scene.camera = cam
rig = cam
while rig.parent is not None:
    rig = rig.parent
n_fr = scene.frame_end or 1

# ★立體雙相機：rig 底下不只一台★(2026-08-19)
#   infinigen 的 camrig 掛的是一對相機(camera_0_0 / camera_0_1，相距 4cm)。
#   ⚠️ 第一版只擺 scene.camera，把 rig 的俯仰歸零、搬進 camera_0_0 的 local rotation，
#      ★camera_0_1 沒人動★ —— 它被留在一個水平 rig 底下用預設朝向，變成★垂直向下★。
#      畫面上看不出來(渲染只用 scene.camera)，但 fine_terrain 會炸：
#        get_caminfo() 拿的是 [c for rig in camera_rigs for c in rig.children]，
#        get_expanded_fov() 把★所有相機所有 frame★的視錐取聯集 ⇒ 74°+ > 90°
#        ⇒ SphericalMesher 的 assert 掛掉(river/seed1 實測)。
#   ⇒ ★rig 底下每一台相機都要跟著同一個姿態走。★立體基線在 location，不在 rotation，
#     所以只要同步 rotation_euler，兩台的相對位移自然保留。
siblings = [o for o in rig.children_recursive
            if o.type == "CAMERA" and o is not cam]
print(f"[bake] 相機 {cam.name} / rig {rig.name} / frames 1~{n_fr}"
      f" / 同 rig 的其他相機 {[o.name for o in siblings] or '無'}", flush=True)

# ``preview_angles`` deliberately treats the all-zero ``ground`` preset as a
# special case: it leaves the scene's evaluated camera completely untouched.
# Applying the generic ``90 - pitch`` formula here would instead force that
# camera onto the horizon.  River exposed the mismatch particularly clearly:
# the selected preview looked down into the valley, while the baked camera saw
# almost only sky and every placement ray missed.  Capture the exact evaluated
# frame-1 matrices before animation/constraints are removed so the static bake
# has byte-for-byte the same pose contract as the selected preview.
native_rig_world = native_cam_world = None
if NATIVE_POSE and not TRAJ:
    scene.frame_set(1)
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    native_rig_world = rig.evaluated_get(deps).matrix_world.copy()
    native_cam_world = cam.evaluated_get(deps).matrix_world.copy()
    print("[bake] ground/all-zero = freeze exact evaluated native frame-1 pose",
          flush=True)


def posed(base_loc, base_rot_z):
    """★與 render_view.py L124-136 逐字同構★ —— 只有一份公式，不要在這裡自創。"""
    rig.location = base_loc.copy()
    rig.location.z += AERIAL
    cam.rotation_mode = "XYZ"
    cam.rotation_euler = (math.radians(90 - PITCH), 0.0,
                          base_rot_z + math.radians(YAW))
    rig.rotation_mode = "XYZ"
    rig.rotation_euler = (0.0, 0.0, rig.rotation_euler.z)
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    m = cam.evaluated_get(deps).matrix_world
    fwd = -Vector((m[0][2], m[1][2], m[2][2])).normalized()
    right = Vector((m[0][0], m[1][0], m[2][0])).normalized()
    rig.location += -fwd * BACK + right * SIDE
    bpy.context.view_layer.update()
    return rig.location.copy(), Vector(cam.rotation_euler), \
        Vector(rig.rotation_euler)


# ── ① ★只記錄 base，不做任何計算★ ────────────────────────────────────────
#    ⚠️ ★第一版在這裡就套 offset，錯的★：動畫與 constraint 還在，仍然驅動
#       rig.location / cam.rotation_euler ⇒ 寫進去的值在求值時被蓋掉，算出來是垃圾。
#       preview_angles 是★先清動畫再套 offset★，順序不是風格問題。
#    ⚠️ ★base 的定義必須與 preview_angles 逐字相同★，否則「預覽看到的」不等於
#       「bake 出來的」。它抓的是★任何 frame_set 之前★的原始屬性值(= blend 存檔時
#       的姿態)，並且五個 angle 共用同一個 base。第二版改成逐格抓 ⇒ frame 1 算出
#       (-62.3,-72.6,17.4) 而預覽報的是 (-65.5,-68.8,16.1)，對不上。
# ★★ 2026-08-19 實測補充：base 的來源 blend 會影響結果 ★★
#   preview 是在 ★fine/scene.blend★ 上渲的，bake 讀的是 ★coarse/scene.blend★。
#   兩邊都用「存檔時的 rig 位置」當 base，而兩個 blend 存檔時停在★不同 frame★
#   ⇒ base 不一樣 ⇒ 同一個 preset 算出不同位置。
#   實測 20 個重跑案例中 18 個吻合(誤差 <0.07m = preview.log 只印一位小數的捨入)，
#   但 arctic/seed2 差 7.40m、arctic/seed3 差 4.06m。
#   在 coarse 上重跑 preview 會得到與 bake 一致的值 ⇒ ★bake 的算式沒錯，是 base 來源不同★。
#
#   ⚠️ ★不要直接改成 frame 1★：frame 1 的位置在 coarse/fine 是一致的(較穩健)，
#      但改了會讓另外 18 個案例全部位移(arctic/seed1 差 10.71m、seed4 差 4.96m)，
#      反而不符使用者已經看過並挑選的 preview。要改就得連 preview 一起改、重挑。
base_loc0 = rig.location.copy()
base_rot_z0 = cam.rotation_euler.z
base = {f: (base_loc0, base_rot_z0) for f in range(1, n_fr + 1)}
if TRAJ:
    # 逐格 base = 原軌跡的姿態。★這會動，但就不等於預覽那一張★。
    for f in range(1, n_fr + 1):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        base[f] = (rig.location.copy(), cam.rotation_euler.z)

# ── ①b ★先 frame_set(1) 再清動畫★ —— 這一步不是多餘的：
#    preview_angles 的順序是「抓 base → frame_set(frame) → 清動畫 → 套 offset」，
#    所以它清完之後 rig.rotation_euler.z 留下的是 ★frame 1 的 yaw★。
#    少了這一步就會用存檔時(frame 72)的 yaw，back/side 的方向偏掉 ⇒ 實測差 1.17m。
if not TRAJ:
    scene.frame_set(1)
    bpy.context.view_layer.update()

# ── ② 清掉原動畫與約束(constraint 會蓋掉 keyframe) ────────────────────────
for c in list(cam.constraints):
    cam.constraints.remove(c)
for c in list(rig.constraints):
    rig.constraints.remove(c)
cam.animation_data_clear()
rig.animation_data_clear()
cam.rotation_mode = "XYZ"
rig.rotation_mode = "XYZ"
for sib in siblings:          # ★立體對的另一台也要清，否則舊 key 會把它拉回去★
    for c in list(sib.constraints):
        sib.constraints.remove(c)
    sib.animation_data_clear()
    sib.rotation_mode = "XYZ"

# ── ③ 這時候才套 offset，並逐格 keyframe ─────────────────────────────────
if NATIVE_POSE and not TRAJ:
    # Root first, then the child camera in world space.  Assigning the camera
    # second lets Blender derive the correct local transform under the frozen
    # rig even when the original pose came from constraints.
    rig.matrix_world = native_rig_world
    bpy.context.view_layer.update()
    if cam is not rig:
        cam.matrix_world = native_cam_world
        bpy.context.view_layer.update()
    baked_cam_world = cam.evaluated_get(
        bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
    native_pose_error = max(
        abs(float(baked_cam_world[row][column]
                  - native_cam_world[row][column]))
        for row in range(4) for column in range(4))
    if native_pose_error > 1e-5:
        raise RuntimeError(
            "native ground camera changed while freezing evaluated pose: "
            f"max matrix error={native_pose_error:.8f}")
    print(f"[bake] native-pose matrix regression error={native_pose_error:.8f}",
          flush=True)
else:
    plan = {f: posed(bl, brz) for f, (bl, brz) in base.items()}
    for f, (rloc, crot, rrot) in plan.items():
        rig.location = rloc
        rig.rotation_euler = rrot
        cam.rotation_euler = crot
        rig.keyframe_insert("location", frame=f)
        rig.keyframe_insert("rotation_euler", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)
        for sib in siblings:   # ★同姿態★ —— 見上面立體雙相機的說明
            sib.rotation_euler = crot
            sib.keyframe_insert("rotation_euler", frame=f)


# ── ④ 驗證：量 evaluated 的世界座標 ──────────────────────────────────────
scene.frame_set(1)
bpy.context.view_layer.update()
# ⚠️ ★.copy() 不可省★ —— matrix_world.translation 是 view，換 frame 之後它會就地變，
#    不 copy 的話「軌跡位移」永遠算出 0.00m(今天第六次踩這個坑，這次踩在診斷上)。
p1 = cam.evaluated_get(
    bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()
print(f"[bake] frame 1 相機 = ({p1.x:.2f},{p1.y:.2f},{p1.z:.2f})"
      f"   模式={'trajectory' if TRAJ else 'static(與預覽同一姿態)'}", flush=True)

scene.frame_set(n_fr)
bpy.context.view_layer.update()
pN = cam.evaluated_get(
    bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()
print(f"[bake] frame {n_fr} 相機 = ({pN.x:.2f},{pN.y:.2f},{pN.z:.2f})"
      f"   軌跡位移 {(Vector(pN)-Vector(p1)).length:.2f}m", flush=True)
scene.frame_set(1)

Path(OUT_BLEND).parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND)
# ⚠️ ★不要在同一個 process 裡重開檔驗證★ —— coarse blend 帶大量 GN 物件，
#    open_mainfile 之後再求值會 segfault(實測 rc=139)。驗證交給獨立的一步。
print(f"[bake] → {OUT_BLEND}", flush=True)
print("BAKE_DONE", flush=True)

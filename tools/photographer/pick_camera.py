"""★photographer —— 在 populate 之前把「用哪台相機」定案★(2026-08-09)

★為什麼需要這一步★
  Infinigen 在 coarse 就決定了一台相機(forest 是 camrig.0 上的 camera_0_0，
  而且是一條 72 格的軌跡)，之後所有跟相機有關的決定都跟著它走：
    ・populate 用它做 cull(filter_populate_targets，沿軌跡判可見性 + 距離)
    ・spawn_asset(distance=...) 用它決定 ★LOD：近的高精度、遠的低精度★
    ・fine_terrain 的 SphericalMesher 也用它烤地形網格精度
  但我們的 pipeline 出圖時★不是用那台★ —— render_view/composer 會把它抬高、加俯角、
  後退、橫移之後才拍。於是「擺放的座標系」與「出圖的座標系」從來沒有對齊過
  (2026-08-09 十一節：xmas 的 14 個物件全部 populate 成功、成圖上一個都沒有)。

★這一步的解法★：★選一次，寫進場景，之後沒有人再套 offset。★
  下游(apply_plan / placer / render)一律讀 scene.camera，不再各自持有一份相機設定。

⚠️ ★只能在小幅範圍內挑★ —— 地形網格精度是 fine_terrain 照當時那台相機烤死的
   (in-view 0.53 / 遮擋 0.13 / 視野外 0.07 marching cube per pixel，★8 倍粗★)。
   相機轉太多，那層粗殼會轉進畫面變成可見的平面三角形。
   tools/speed_up_render/preview_angles.py 的五個 preset 就是為此刻意都貼著 infinigen
   自己的 pose(註解寫 `keep these nudges SMALL`)，所以這裡直接沿用那五個。
   ★要真正自由選視角，必須在生場景時做(fine_terrain 之前)，不是事後改現成場景。★

用法:
  python pick_camera.py <coarse.blend> <out_dir> [--samples 32] [--res 800x450]
                        [--visual-blend <fine/scene.blend>]
                        [--no-agent]         # 只渲圖不叫 agent
產物:
  <out_dir>/angle_*.png     coarse 的五個結構視角
  <out_dir>/visual/angle_*.png  已 populate 幾何的五個真實遮擋視角
                              (有傳 --visual-blend 才會產生)
  <out_dir>/contact.png     併排對照
  <out_dir>/camera_choice.json  {"angle": "raised", "cfg": {...}, "why": "..."}
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent   # repo root (this file lives in tools/<x>/)
PREVIEW = ROOT / "tools" / "speed_up_render" / "preview_angles.py"
BPY_PYTHON = os.environ.get("BPY_PYTHON", os.environ.get("BI_PYTHON", sys.executable))
OPUS_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# ★與 preview_angles.ANGLES 同一份★ —— 那邊是唯一來源，這裡只是為了寫進
#   camera_choice.json 給下游用。改了那邊記得這裡也要拿到同樣的值。
sys.path.insert(0, str(ROOT / "speed_up_render"))


def load_angles() -> dict:
    """從 preview_angles.py 讀 ANGLES，★不要自己抄一份★(抄了就會漂)。"""
    src = PREVIEW.read_text(encoding="utf-8")
    ns: dict = {}
    start = src.index("ANGLES = {")
    end = src.index("\n}\n", start) + 3
    exec(src[start:end], ns)                      # noqa: S102 - 只執行那個 dict 字面值
    return ns["ANGLES"]


def _env() -> dict:
    merged: dict = {}
    for envfile in (ROOT / ".env", ROOT.parent / "ExtraWorld" / ".env"):
        if envfile.is_file():
            merged.update({k: v for k, v in dotenv_values(envfile).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


def _claude():
    import anthropic
    env = _env()
    key = env.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found (BeyondInfinigen/.env)")
    base = (env.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


SYSTEM = """You are choosing the camera for a procedurally generated 3D scene, \
BEFORE the requested edits are populated.

For each of five camera poses you may receive two renders of the SAME base scene:
- STRUCTURE: the coarse scene. Future objects are placeholder BOXES; a box means \
  "a tree/rock/creature will be grown here later". Boxes are emission-coloured by \
  factory and shown as wireframes. Use this for placement coverage and the supplied \
  measurements, not for judging final occlusion.
- POPULATED VISUAL REFERENCE (when available): the already-generated Infinigen \
  geometry before the requested edits. This is the authoritative view for whether \
  trunks, foliage, rocks, or terrain actually block the camera.

HARD VISIBILITY GATE: reject a camera if its populated visual reference is filled by \
nearby foliage/branches, is inside an object, or otherwise has no readable view into \
the scene. A strong placeholder count NEVER overrides a blocked populated image. \
Prefer a slightly more elevated clear view over a numerically dense view inside a tree.

★Each angle comes with measured numbers, and they are ranked★:

PRIMARY — usable ground. The scene will have new objects PLACED into it, so what \
matters most is whether this camera looks at ground you can put things on: \
`usable_near` (share of the frame that is terrain 5-40m away) predicts how big a new \
object will appear; `flat_frac` says whether that ground is level enough to stand on; \
`ground_frac` near 0 means you are staring at sky.

SECONDARY — existing placeholders. `ACTUALLY VISIBLE` counts boxes in frame and not \
hidden behind terrain (`in frustum` is before the occlusion test; a big gap means that \
content sits behind a ridge). This only decides the choice when the plan REPLACES \
things that already exist.

After the hard visibility gate, use the measured numbers to compare the remaining \
angles. Use the images for spatial arrangement and final-geometry occlusion. Never \
claim the frame contains something the numbers do not support; if the near field is \
empty, say so plainly.

Your choice decides much more than the final picture: the engine uses this camera \
to decide which placeholders are worth realising at all, and how much geometric \
detail each one gets (near = high detail, far = low). Objects the camera cannot \
see may never be built.

Pick the angle that gives the best scene to work with:
- shows the MOST distinct scene content (many placeholder clusters, terrain relief,
  any water/rock/vegetation structure) rather than an empty expanse
- has clear FOREGROUND ground area that new objects could be placed on and be seen
  at a useful size (a frame that is all distant terrain is bad, even if pretty)
- is not blocked by a single object or wall of terrain filling the frame
- reads as a coherent view of a place, with some depth

Answer as strict JSON, no prose outside it:
{"angle": "<one of the given names>",
 "why": "<two sentences, ★citing the numbers★: what it shows that the others do not>",
 "runner_up": "<name>",
 "scene_warning": "<'' or a plain statement if NO angle has usable near content, "
                  "e.g. 'nearest placeholder is 86m in every angle; this scene has an "
                  "empty near field and is a poor host for hero objects'>",
 "rejected": {"<name>": "<short reason>", ...}}"""


def render_angles(blend: Path, out_dir: Path, samples: int, res: str,
                  *, structure: bool = True) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [BPY_PYTHON, str(PREVIEW), "--", str(blend), str(out_dir),
           "--samples", str(samples), "--res", res]
    if structure:
        # ★候選圖用場景原本的色調★ —— 它要回答「最終畫面會長怎樣」，
        #   動亮度會讓判斷失真。診斷用的 overview 才用 Standard/-1.5。
        # ★線框 marker box★：位置與大小一眼可讀，又不遮擋 agent 要判斷的地形
        cmd += ["--show-placeholders", "--color", "--marker-box", "--wire"]
        label = "coarse，打開 placeholder"
    else:
        # 不動任何幾何；這五張專門曝光 coarse 線框無法看出的真實樹冠遮擋。
        label = "populated visual reference，真實幾何"
    print(f"[photographer] 渲五個視角({label}) …", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ,
                            "CUDA_VISIBLE_DEVICES": os.environ.get(
                                "CUDA_VISIBLE_DEVICES", "2,3")})
    for line in r.stdout.splitlines():
        if line.startswith("ANGLE ") or line.startswith("opened "):
            print("   " + line, flush=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit(f"[photographer] ⛔ preview_angles rc={r.returncode}")
    pngs = sorted(out_dir.glob("angle_*.png"))
    if not pngs:
        raise SystemExit("[photographer] ⛔ 沒有產出任何 angle_*.png")
    return pngs


def structure_overview(blend: Path, out_png: Path):
    """★高處俯視的 box 總覽★ —— 直接沿用 planner/run_all.sh:65 那組已驗證的呼叫。

    ★為什麼要獨立一張★：五個候選 angle 都是刻意貼著 infinigen 原生 pose 的小幅變化
      (preview_angles 註解 `keep these nudges SMALL`)，所以在 snowy_mountain 這種
      「167 個 1~4m 的石頭散在 28~130m 外白雪上」的場景，box 在候選圖裡★永遠只有幾個
      像素★(實測發光色只佔 0.2%)。那不是渲染設定的問題，是場景。
    ⇒ 分工：★候選圖 = 最終畫面會長怎樣；總覽圖 = 這個場景到底有什麼、在哪裡★。
      總覽用 render_view 的預設俯視(aerial=28 pitch=42 back=18)，它本來就是為了
      「看很多 box」而定的。planner/out/candy_forest/after.png 是成功範例。
    """
    cmd = [BPY_PYTHON, str(ROOT / "stage_exec" / "render_view.py"), "--",
           str(blend), str(out_png), "--show-placeholders", "--color",
           # ★--marker-box★: 無視 factory 原本的 placeholder 形狀，一律畫成 bbox 方塊
           #   (BoulderFactory 的是 50 頂點低模石頭，不加這個就看不出是「標記」)
           "--marker-box",
           # ★明寫 --view Standard --exp -1.5★(與 planner/run_all.sh:65 一致)。
           #   目前這與 render_view 的預設(L77-78)相同，所以是 no-op ——
           #   ⚠️ 但★保留明寫★是刻意的：這張是★診斷圖★，它的判讀依賴發光色 box
           #   看得清楚(AgX 會把發光色去飽和到看不見)。若哪天有人改了 render_view
           #   的預設，明寫的這一份不會跟著漂掉。
           #   ★候選五張則相反★：那是「最終畫面會長怎樣」，一律用場景自己的色調。
           "--view", "Standard", "--exp", "-1.5",
           "--samples", "32", "--res", "960", "540"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, "CUDA_VISIBLE_DEVICES":
                            os.environ.get("CUDA_VISIBLE_DEVICES", "2,3")})
    if r.returncode != 0 or not out_png.is_file():
        print(f"[photographer] ⚠️ 總覽圖失敗 rc={r.returncode}(不致命，繼續)")
        print(r.stdout[-800:], r.stderr[-800:])
        return None
    print(f"[photographer] 結構總覽(俯視看 box) → {out_png}", flush=True)
    return out_png


def contact_sheet(pngs: list, out: Path):
    """併排一張，方便人看；agent 拿的是★個別原圖★不是這張(縮圖會吃掉細節)。"""
    try:
        from PIL import Image
    except ImportError:
        print("[photographer] ⚠️ 沒有 PIL，跳過 contact sheet")
        return None
    ims = [Image.open(p).convert("RGB") for p in pngs]
    w, h = ims[0].size
    cols = 3
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows), "black")
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % cols) * w, (i // cols) * h))
    sheet.save(out)
    return out


def _stat_block(name, st):
    """把量到的數字排成 agent 讀得懂的表 —— ★這是判準，圖只是佐證★。"""
    if not st:
        return "(no measurements)"
    g = st.get("ground") or {}
    near = f"{st['nearest']}m" if st.get("nearest") is not None else "(nothing visible)"
    lines = [
        "    ★PRIMARY — usable ground for placing new objects★",
        f"      ground_frac    {g.get('ground_frac')}   (share of frame that is terrain, "
        f"rest is sky)",
        f"      usable_near    {g.get('usable_near')}   (share of frame that is ground "
        f"5-40m away — this predicts how BIG a new object will look)",
        f"      flat_frac      {g.get('flat_frac')}   (of that near ground, share with "
        f"slope < 25deg)",
        f"      nearest_ground {g.get('nearest_ground')}m   median {g.get('median_ground')}m",
        "    secondary — existing placeholders (only matters when the plan REPLACES "
        "things that are already there)",
        f"      ACTUALLY VISIBLE (not occluded) = {st['visible_total']}"
             f"   in frustum = {st['frustum_total']}   nearest visible = {near}"]
    for fac, v in sorted(st["by_factory"].items(), key=lambda kv: -kv[1]["visible"]):
        lines.append(f"      {fac:24s} visible {v['visible']:3d}"
                     f" / in-frustum {v['in_frustum']:3d} / total {v['total']:<4d}"
                     f"  nearest {v['nearest']}m  median {v['median']}m")
    return "\n".join(lines)


def ask_agent(pngs: list, angles: dict, stats: dict, legend: list,
              overview: Path = None, visual_pngs: list | None = None) -> dict:
    head = "Here are the five candidate camera angles for this scene."
    visual_by_name = {
        p.stem.replace("angle_", ""): p for p in (visual_pngs or [])
    }
    if visual_by_name:
        head += (" Each angle includes a STRUCTURE render followed by a POPULATED "
                 "VISUAL REFERENCE. Apply the hard visibility gate to the latter.")
    if legend:
        head += ("\n\nLegend (box colour -> factory, and how many exist in the whole "
                 "scene):\n  " + "\n  ".join(legend))
    content = [{"type": "text", "text": head}]
    if overview is not None:
        content.append({"type": "text", "text":
                        "\n=== STRUCTURE OVERVIEW (not a candidate) ===\n"
                        "A high top-down view of the SAME scene showing where the "
                        "placeholder boxes actually are. Use it to understand what "
                        "content exists and how it is distributed; the five candidates "
                        "below are near ground level so distant boxes look tiny there."})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(overview.read_bytes()).decode()}})
    for p in pngs:
        name = p.stem.replace("angle_", "")
        cfg = angles.get(name, {})
        content.append({"type": "text",
                        "text": f"\n=== angle `{name}` === {cfg.get('note','')} "
                                f"(aerial={cfg.get('aerial')}m pitch={cfg.get('pitch')}deg "
                                f"back={cfg.get('back')}m side={cfg.get('side')}m "
                                f"yaw={cfg.get('yaw')}deg)\n"
                                + _stat_block(name, stats.get(name))})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(p.read_bytes()).decode()}})
        visual = visual_by_name.get(name)
        if visual is not None:
            content.append({"type": "text", "text":
                            f"POPULATED VISUAL REFERENCE for `{name}` "
                            "(authoritative for real-geometry occlusion):"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(visual.read_bytes()).decode()}})
    resp = _claude().messages.create(
        model=OPUS_MODEL, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user", "content": content}])
    txt = "".join(b.text for b in resp.content if b.type == "text").strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1].lstrip("json").strip()
    # ⚠️ agent 有時會在 JSON 後面多寫幾句 ⇒ ★只取第一個物件★，不要整段丟給 json.loads
    obj, _end = json.JSONDecoder().raw_decode(txt[txt.index("{"):])
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("blend")
    ap.add_argument("out_dir")
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--res", default="960x540")
    ap.add_argument("--visual-blend", default=None,
                    help="optional existing populated/fine blend used to reject "
                         "camera poses blocked by real geometry")
    ap.add_argument("--no-agent", action="store_true")
    a = ap.parse_args()

    out_dir = Path(a.out_dir)
    angles = load_angles()
    pngs = render_angles(Path(a.blend), out_dir, a.samples, a.res)
    sheet = contact_sheet(pngs, out_dir / "contact.png")
    print(f"[photographer] 五張 → {out_dir}"
          + (f"  併排 → {sheet}" if sheet else ""), flush=True)
    if a.no_agent:
        return 0

    visual_pngs = []
    visual_blend = Path(a.visual_blend).resolve() if a.visual_blend else None
    if visual_blend is not None:
        if not visual_blend.is_file():
            raise SystemExit(f"[photographer] ⛔ visual blend 不存在: {visual_blend}")
        visual_dir = out_dir / "visual"
        visual_pngs = render_angles(
            visual_blend, visual_dir, a.samples, a.res, structure=False)
        contact_sheet(visual_pngs, visual_dir / "contact.png")
        print(f"[photographer] 真實幾何遮擋對照 → {visual_dir}", flush=True)

    sf = out_dir / "angle_stats.json"
    meas = json.loads(sf.read_text(encoding="utf-8")) if sf.is_file() else {}
    stats, legend = meas.get("angles", {}), meas.get("legend", [])
    for n, st in sorted(stats.items(),
                        key=lambda kv: -(kv[1].get("ground") or {}).get("usable_near", 0)):
        g = st.get("ground") or {}
        print(f"[photographer]   {n:8s} usable_near={g.get('usable_near')} "
              f"ground={g.get('ground_frac')} flat={g.get('flat_frac')} "
              f"nearest_ground={g.get('nearest_ground')}m  |  既有 placeholder "
              f"visible={st['visible_total']}", flush=True)

    ov = structure_overview(Path(a.blend), out_dir / "overview.png")
    pick = ask_agent(pngs, angles, stats, legend, ov, visual_pngs)
    name = pick["angle"]
    if name not in angles:
        raise SystemExit(f"[photographer] ⛔ agent 選了不存在的 angle: {name}")
    choice = {"angle": name, "cfg": angles[name], "why": pick.get("why", ""),
              "runner_up": pick.get("runner_up"), "rejected": pick.get("rejected", {}),
              "scene_warning": pick.get("scene_warning", ""),
              "measured": stats.get(name), "legend": legend,
              "blend": str(Path(a.blend).resolve()),
              "visual_blend": str(visual_blend) if visual_blend else None,
              "images": [str(p) for p in pngs],
              "visual_images": [str(p) for p in visual_pngs]}
    (out_dir / "camera_choice.json").write_text(
        json.dumps(choice, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n[photographer] ★選定 `{name}`★  {angles[name]}", flush=True)
    print(f"[photographer] 理由: {pick.get('why','')}", flush=True)
    print(f"[photographer] 次選: {pick.get('runner_up')}", flush=True)
    if pick.get("scene_warning"):
        print(f"[photographer] ⚠️ ★場景警告★: {pick['scene_warning']}", flush=True)
    for k, v in (pick.get("rejected") or {}).items():
        print(f"[photographer]   ✗ {k}: {v}", flush=True)
    print(f"[photographer] → {out_dir/'camera_choice.json'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

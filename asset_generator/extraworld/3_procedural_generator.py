"""Procedural Generator — Agent 3 (ExtraWorld / BeyondInfinigen).

For a target object whose exact look must be reproduced, this does NOT model from
scratch. It takes the closest Infinigen starter factory (normally chosen by Agent 2,
or auto-picked from the prompt in universal mode) and has an Opus coding agent REWRITE
that factory's upper-level source so its output matches the request, then renders the
REAL factory. Mechanism (ported self-contained from OtherTest/generate_from_template.py +
search_related.py — see those for the original):

  REFERENCE  Resolve the factory's UPPER-LEVEL editable files (its own geometry/shader
             code under assets/objects/...) -> src; every LOWER-LEVEL infra/engine file
             stays LINKED to the real infinigen. A code_structure.txt maps the knobs.
             Uses the prebuilt template library when present, else builds it fresh
             (static import-graph collect + one Opus code_structure pass) and caches it.
  PLAN       Opus CLI agent (read-only) reads code_structure + the FULL infinigen source
             and writes a plan: which files/knobs to change to satisfy the request.
  GENERATE   Opus CLI coder agent rewrites the upper files into code/src/ (may promote
             extra infra files it needs to edit), keeping each file's relpath and the
             entry class name/spawn signature unchanged.
  OVERLAY    Build code/overlay/infinigen/ : a full symlink-mirror of real infinigen where
             ONLY the rewritten upper files are real copies — i.e. the edited code REPLACES
             the original infinigen files, everything else stays identical. Render it.
  ROUNDS     r1 = plan+generate; r2..rN = visual revision (agent looks at the render +
             target and edits src to fix visual-only bugs). A verifier scores each render
             (early-stop when good enough); a selector picks the best round. Default 3.

Reference images (Agent 2's single-view target) are the ground-truth target. Image gen is
not used here; all reasoning/coding is Opus.

    python 3_procedural_generator.py --scene glowing_tree_plaza --id glowing_tree
    python 3_procedural_generator.py --factory auto --prompt "a piano" \
        --out-dir output/test/piano
    python 3_procedural_generator.py --factory TreeFactory --prompt "..." \
        --target path/to/target_single.png --out-dir output/test/tree
"""
import argparse
import ast
import base64
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
BI_ROOT = HERE.parents[1]
if str(BI_ROOT) not in sys.path:
    sys.path.insert(0, str(BI_ROOT))
from paths import asset_store_dir  # noqa: E402
# ★2026-07-30 搬進 BeyondInfinigen/asset_generator/extraworld/ 時改★
#   原本寫 HERE.parent/"OtherTest"，那是因為這支住在 Animal/ExtraWorld/ ⇒ parent = Animal/。
#   搬過來之後 HERE.parent = asset_generator/ ⇒ ★會找不到，且錯誤訊息不明顯★。
#   ⇒ 改成從 ANIMAL_ROOT 出發，並允許用環境變數覆寫。
# Release layout: the Infinigen checkout and the prebuilt template library are read as
# DATA from INFINIGEN_ROOT / INFINIGEN_TEMPLATES (defaults under third_party/).
TEMPLATES = Path(os.environ.get(
    "INFINIGEN_TEMPLATES", BI_ROOT / "third_party" / "Infinigen_templates")).resolve()
CODEBASE = Path(os.environ.get(
    "INFINIGEN_ROOT", BI_ROOT / "third_party" / "infinigen")).resolve() / "infinigen"
FACTORY_LIB = TEMPLATES / "Factory"                        # prebuilt Entry/Part libraries

# ★ADAPT★：不是從官方 infinigen 出發，而是從擴充資產庫裡一個既有變體出發改
# (黑白孔雀從彩色孔雀改，不必重做開屏/冠羽/眼斑)。None = 照常從官方出發。
# 由 --base-overlay 設定；指向一棵已 materialize 好的 overlay 的 import root。
BASE_OVERLAY: Path | None = None


def _base_overlay_changed() -> list[str]:
    """base overlay 相對官方基底真正改寫的檔(逐位元組比對，與 glue 同一把尺)。"""
    if BASE_OVERLAY is None:
        return []
    import filecmp
    out = []
    root = BASE_OVERLAY / "infinigen"
    for f in sorted(root.rglob("*.py")):
        if f.is_symlink() or "__pycache__" in f.parts:
            continue
        rel = str(f.relative_to(root))
        base = CODEBASE / rel
        if base.is_file() and filecmp.cmp(f, base, shallow=False):
            continue
        out.append(rel)
    return out


def _seed_source(rel: str) -> Path:
    """某個檔案的起點：★變體真正改過的★用變體那份，其餘一律官方 infinigen。

    ⚠️ ★不能只問「overlay 裡有沒有這個檔」★ —— overlay 是完整 symlink 鏡像，
       每一個官方檔在裡面都「存在」。那樣寫的話語意會變成「整棵基底都來自變體」，
       雖然 symlink 跟穿之後內容剛好還是對的，但只要基底換過一次(overlay 是對
       舊 CODEBASE 建的)，就會靜靜地拿到舊版官方碼。
       ⇒ 只認★實體檔★，那才是變體自己的編輯。
    """
    if BASE_OVERLAY is not None:
        cand = BASE_OVERLAY / "infinigen" / rel
        if cand.is_file() and not cand.is_symlink():
            return cand
    return CODEBASE / rel
sys.path.insert(0, str(HERE))
from seed_strip import build_seed_strip  # noqa: E402

FACTORYLIST = HERE / "FactoryList.txt"
RENDER_FACTORY = HERE / "render_factory.py"
RENDER_TURNTABLE = HERE / "render_factory_turntable.py"
STARTER_ROLES = ("ENTRY", "ELEMENT")

BLENDER_PYTHON = os.environ.get("BLENDER_PYTHON", sys.executable)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
RENDER_GPU = os.environ.get("CUDA_VISIBLE_DEVICES", "1")    # pin renders to one free GPU
OPUS_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
DEFAULT_ROUNDS = 3
DEFAULT_MATCH_THRESHOLD = 80


# --------------------------------------------------------------------------------- env
def _env() -> dict:
    merged: dict = {}
    # ★2026-07-30★ HERE.parent 在搬家後不再是 Animal/ ⇒ 改用 ANIMAL_ROOT。
    #   也吃 BeyondInfinigen/.env(這個 repo 自己那份)。
    for envfile in (BI_ROOT / ".env", HERE / ".env"):
        if envfile.is_file():
            merged.update({k: v for k, v in dotenv_values(envfile).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL", "GEMINI_API_KEY", "GEMINI_BASE_URL",
              "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


ENV = _env()


def _base_url() -> str | None:
    base = (ENV.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):                # the SDK / CLI append /v1 themselves
        base = base[:-3]
    return base or None


def _client():
    import anthropic
    key = ENV.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found (infinigen_o/.env, OtherTest/.env, ExtraWorld/.env, env)")
    return anthropic.Anthropic(api_key=key, base_url=_base_url())


def _cli_env() -> dict:
    """ANTHROPIC_* env for the claude CLI coding agent."""
    env = os.environ.copy()
    env["ANTHROPIC_API_KEY"] = (ENV.get("CLAUDE_API_KEY") or "").strip().strip('"')
    base = _base_url()
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    return env


# ══════════════════════════════════════════════════════════════════════════════
# ★自動生 reference 圖(2026-07-30 加)★
#   ⚠️ 原本 3_procedural_generator.py ★只會「吃」ref★(--target)，自己不生 ——
#      生 ref 的 generate_refs() 在 2_director.py，屬於 STAGE 2。
#      我們用 --factory/--prompt 直接跑生成器時★繞過了 director★，
#      ⇒ 兩次實跑(silver_blue_tree / rainbow_tree)都是★純文字，沒有 ref★。
#
#   ★為什麼 ref 重要★：它不只餵給寫 code 的 agent ——
#      ★run_verifier 和 run_selector 也吃★。沒 ref 時 verifier 只能拿文字比對圖，
#      評分基準模糊(rainbow_tree 的 55→82→72 飄忽，且 selector 最後選了分數較低的
#      r3)。PIPELINE_HANDOFF 也記載 ref-guided 品質明顯較好。
#
#   ⇒ 這裡把 Gemini 生圖★內建進來★(移植自 2_director.py，self-contained)，
#     ★預設自動生★，不需要人確認。--no-ref 可關、--target 給了就不生。
# ══════════════════════════════════════════════════════════════════════════════
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL",
                                    "gemini-3.1-flash-image-preview")


def _gemini():
    from openai import OpenAI
    key = ENV.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not found — cannot auto-generate a reference")
    return OpenAI(api_key=key,
                  base_url=ENV.get("GEMINI_BASE_URL",
                                   "https://generativelanguage.googleapis.com/v1beta/openai/"))


def _extract_image(response) -> bytes | None:
    """Gemini 的回應格式有好幾種變體，逐一嘗試。(移植自 2_director.py)"""
    try:
        content = response.choices[0].message.content
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        return base64.b64decode(url.split(",", 1)[1])
                if part.get("type") == "image":
                    d = part.get("image", "") or part.get("data", "")
                    if d:
                        return base64.b64decode(d)
        if isinstance(content, str):
            m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
            if m:
                return base64.b64decode(m.group(1))
        if getattr(response, "model_extra", None):
            for cand in response.model_extra.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data", {})
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
    except Exception:
        pass
    return None


# ★參考圖來源階梯★(2026-10-19 使用者：不必只靠 Gemini，有參考圖就好)：
#   1. GEMINI_API_KEY / GEMINI_BASE_URL(原本那條)
#   2. OPENAI_API_KEY / OPENAI_BASE_URL 的 OpenAI 相容 chat 端點(linkapi 也供
#      gemini-3.1-flash-image-preview；REF_FALLBACK_IMAGE_MODEL 可換)
#   3. 本地 diffusers 模型(REF_LOCAL_IMAGE_MODEL，預設 Tongyi-MAI/Z-Image-Turbo，Apache-2.0)
#   起因：1019 整批 OpenRouter key 回 401 "User not found"，33 個資產沒參考圖。
#   REF_IMAGE_PROVIDERS 可調順序/開關某一層(預設 gemini,openai,local)。
REF_FALLBACK_IMAGE_MODEL = os.environ.get("REF_FALLBACK_IMAGE_MODEL", GEMINI_IMAGE_MODEL)
REF_LOCAL_IMAGE_MODEL = os.environ.get("REF_LOCAL_IMAGE_MODEL", "Tongyi-MAI/Z-Image-Turbo")
LAST_IMAGE_PROVIDER = None


def _openai_compatible():
    from openai import OpenAI
    key = ENV.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not found")
    return OpenAI(api_key=key, base_url=ENV.get("OPENAI_BASE_URL") or None)


def _local_image(instruction: str, prompt: str) -> bytes:
    """本地 diffusers 生圖(REF_LOCAL_IMAGE_MODEL)。

    Z-Image(預設)的 text encoder 是 LLM、吃 512 token ⇒ 直接用完整 instruction
    (含「只畫一個、不要底座/棲地」)。SDXL 之類 CLIP 只吃 77 token ⇒ 只取物件描述 + negative。
    """
    import io
    import torch
    from diffusers import DiffusionPipeline
    llm_encoder = "z-image" in REF_LOCAL_IMAGE_MODEL.lower()
    if llm_encoder:
        pipe = DiffusionPipeline.from_pretrained(REF_LOCAL_IMAGE_MODEL,
                                                 torch_dtype=torch.bfloat16)
    else:
        pipe = DiffusionPipeline.from_pretrained(
            REF_LOCAL_IMAGE_MODEL, torch_dtype=torch.float16, variant="fp16",
            use_safetensors=True)
    try:
        pipe.enable_model_cpu_offload()      # 24GB 卡也放得下；同卡還有別人時不會 OOM
        def _run(p):
            if llm_encoder:
                return p(prompt=instruction, width=1024, height=1024,
                         num_inference_steps=9, guidance_scale=0.0).images[0]
            return None
        image = None
        if llm_encoder:
            # ★共用卡 OOM：等 60s 重試 3 次，最後改 sequential offload(慢但只要 ~3GB)★(2026-10-20)
            for _try in range(4):
                try:
                    image = _run(pipe); break
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    if _try < 3:
                        print(f"[ref] ⚠️ Z-Image CUDA OOM，60s 後重試 ({_try + 1}/3)", flush=True)
                        time.sleep(60)
                    else:
                        print("[ref] ⚠️ 仍 OOM ⇒ 改 sequential cpu offload(低顯存模式)", flush=True)
                        pipe.enable_sequential_cpu_offload()
                        image = _run(pipe)
        if image is None and llm_encoder:
            raise RuntimeError("local image generation produced nothing")
        if llm_encoder:
            pass
        else:
            # 正式流程的 prompt 開頭是 SINGLE LOGICAL INSTANCE CONTRACT 規則文字，
            #   CLIP 77 token 會只讀到規則 ⇒ 只取 SOURCE OBJECT DESCRIPTION 之後的描述。
            text = str(prompt)
            if "SOURCE OBJECT DESCRIPTION:" in text:
                text = text.split("SOURCE OBJECT DESCRIPTION:", 1)[1]
            short = " ".join(text.split()[:30])
            image = pipe(
                prompt=(f"one single {short}. full body, centered, alone, isolated on plain "
                        f"white background, studio product photo, photorealistic"),
                negative_prompt=("multiple, many, two, duplicate, repeated pattern, collage, "
                                 "group, flock, crowd, branch, perch, ground, terrain, "
                                 "pedestal, stand, base, scenery, text, watermark, cropped"),
                num_inference_steps=30, guidance_scale=6.5, width=1024, height=1024,
            ).images[0]
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        del pipe
        torch.cuda.empty_cache()


def _call_image(instruction: str, short_prompt: str = "") -> bytes:
    """依 REF_IMAGE_PROVIDERS 逐層嘗試，第一個拿到圖的就用(記在 LAST_IMAGE_PROVIDER)。"""
    global LAST_IMAGE_PROVIDER
    # local 放最後一層(2026-10-19)：SDXL base 畫鸚鵡一大群+棲木不能用；換成 Z-Image-Turbo
    #   後單隻鸚鵡/碼頭乾淨可用(約 30 秒)，但 prompt 寫「一群 N 隻」時仍會畫好幾隻。
    #   來源記在 target_single.source.txt。
    order = [p.strip() for p in os.environ.get(
        "REF_IMAGE_PROVIDERS", "gemini,openai,local").split(",") if p.strip()]
    errors = []
    for provider in order:
        try:
            if provider == "gemini":
                data = _call_chat_image(_gemini(), GEMINI_IMAGE_MODEL, instruction)
            elif provider == "openai":
                data = _call_chat_image(_openai_compatible(), REF_FALLBACK_IMAGE_MODEL,
                                        instruction)
            elif provider == "local":
                data = _local_image(instruction, short_prompt or instruction)
            else:
                raise ValueError(f"unknown provider {provider!r}")
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {str(e)[:120]}")
            print(f"    [ref] {provider} 失敗，換下一個來源: {errors[-1]}", flush=True)
            continue
        LAST_IMAGE_PROVIDER = provider
        return data
    raise RuntimeError("所有參考圖來源都失敗: " + " | ".join(errors))


def _call_chat_image(client, model: str, instruction: str) -> bytes:
    """One chat-completions image call -> image bytes. 5 次重試(指數退避)。"""
    delay = 15
    for attempt in range(1, 6):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [{"type": "text", "text": instruction}]}])
            break
        except Exception as e:
            if attempt == 5:
                raise
            if any(k in str(e) for k in ("503", "500", "429", "UNAVAILABLE", "INTERNAL",
                                         "timeout", "timed out", "ReadTimeout")):
                print(f"    [ref] image API error ({attempt}/5): {str(e)[:70]}; retry {delay}s",
                      flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                raise
    data = _extract_image(r)
    if data is None:
        raise RuntimeError("image model returned no image")
    return data


# ⚠️ 2026-07-31：★副檔名不等於真實格式★ —— Gemini 回的其實是 JPEG，卻被存成
#    target_single.png。_image_block() 只看副檔名決定 media_type ⇒ 宣告 image/png
#    卻送 JPEG bytes ⇒ Anthropic API 直接 400，0730 那批 10/10 全掛就是這樣來的。
#    ⇒ 一律用 magic bytes 判斷真實格式，不要相信檔名。
_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]


def sniff_media_type(data: bytes) -> str | None:
    """靠 magic bytes 認出真實格式(認不出回 None)。WEBP 要看 RIFF....WEBP。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_to_png(data: bytes) -> bytes:
    """把任何格式的圖轉成★真正的 PNG bytes★，好讓 .png 這個檔名不再說謊。

    轉不動(沒有 Pillow 之類)就原樣回傳 —— 讀取端靠 sniff 仍是安全的。
    """
    if sniff_media_type(data) == "image/png":
        return data
    try:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"[ref] ⚠️ 轉 PNG 失敗({type(e).__name__})，原樣存檔；"
              f"讀取端會自行嗅探真實格式", flush=True)
        return data


def auto_reference(prompt: str, out_dir: Path) -> Path | None:
    """★自動生一張 target reference★，存成 <out_dir>/target_single.png。

    失敗★不中斷整輪★ —— 退回純文字模式(回傳 None)，因為 ref 是加分項不是必要條件。
    """
    dst = Path(out_dir) / "target_single.png"
    if dst.exists():                       # 重跑時沿用，不重複花錢
        print(f"[ref] 沿用既有 {dst.name}", flush=True)
        return dst
    instr = (
        f"Generate a clean studio product photo of ONLY this object: {prompt}. "
        f"Isolate it on a plain pure-white background. Show the WHOLE object, every part "
        f"visible, in a 3/4 view from the front and slightly the right, seen a little from "
        f"above, upright. "
        # ★支撐物與棲地不算 clutter★(2026-08-20)：模型會自動補上「這東西本來就會待的
        #   地方」—— 底下的岩石、盛裝它的容器、停棲的枝條、腳下的地面。它不認為那是
        #   雜物，而是物體的一部分。下游把整張圖當成目標，生成器就會花力氣去長一塊
        #   根本不需要的幾何，verifier 也會照著那張圖扣分。
        #   ⇒ 要講整個★類別★，不能只舉一兩個例子 —— 舉例只會讓模型避開那幾樣，
        #     換個物體照樣補上別的支撐物。
        f"Render the object completely on its own, with nothing supporting, containing, "
        f"holding or surrounding it: no ground or terrain, no base, stand or pedestal, "
        f"no container or holder, no perch or attachment, no habitat or scenery. If this "
        f"object is normally seen resting on, growing from, contained in or attached to "
        f"something, leave that out entirely and show only the object itself. "
        # ★「一個」要當成硬性規定，不是可以被 prompt 蓋掉的預設★(2026-08-27)：
        #   上面的 "ONLY this object" 只擋得住「旁邊多出別的東西」，擋不住「這東西
        #   本身就是複數」—— prompt 寫「a group of five jellyfish」「a grove of
        #   birches」「a school of fish」時，模型照著生一整群，下游就跟著長出一堆
        #   幾何，verifier 也照著那張圖給分。
        #   ⇒ 這條要★明講會覆蓋 prompt 自己的數量詞★，而且要講整個類別
        #     (group / cluster / swarm / grove / school / pair)，只舉一兩個例子
        #     換個字眼照樣失效。
        f"Render exactly ONE single specimen of it — one individual object, alone in the "
        f"frame. This overrides any quantity in the description above: if the description "
        f"asks for a group, cluster, swarm, grove, school, pair or any number greater than "
        f"one, ignore that and show ONE representative individual only. Never a collection, "
        f"never a repeat of the same object, never a partial second copy at the edge. "
        f"No other objects, no people, no clutter, no text, photorealistic."
    )
    try:
        print(f"[ref] 生成 reference 圖中({os.environ.get('REF_IMAGE_PROVIDERS', 'gemini,openai,local')})...", flush=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw = _call_image(instr, short_prompt=prompt)
        got = sniff_media_type(raw)
        data = _normalize_to_png(raw)
        if got != "image/png":
            print(f"[ref] 來源其實是 {got or 'unknown'} ⇒ 已轉成 PNG 再存", flush=True)
        dst.write_bytes(data)
        # 記下是哪一層生的，事後才分得出 Gemini 圖和本地 SDXL 圖
        dst.with_name("target_single.source.txt").write_text(
            f"provider: {LAST_IMAGE_PROVIDER}\n", encoding="utf-8")
        print(f"[ref] ✅ ({LAST_IMAGE_PROVIDER}) -> {dst}", flush=True)
        return dst
    except Exception as e:
        print(f"[ref] ⚠️ 生成失敗，改用純文字模式: {type(e).__name__}: {e}", flush=True)
        return None


def _image_block(path: Path) -> dict:
    raw = Path(path).read_bytes()
    data = base64.standard_b64encode(raw).decode()
    # ★看 magic bytes，不看副檔名★(見上方 sniff_media_type 的註解)。
    # 認不出來才退回副檔名猜測。
    media = sniff_media_type(raw) or (
        "image/png" if Path(path).suffix.lower() == ".png" else "image/jpeg")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


# ---------------------------------------------------------------------- factory catalog
_ROW_RE = re.compile(r"^\s*(\w+)\s+\[(ENTRY|PART)\s*\]\s+(.*?)\s+(assets/\S+\.py)\s*$")


def parse_factory_list() -> dict:
    """{FactoryName: {"role": ENTRY|ELEMENT|PART, "desc": str, "file": relpath}}."""
    cat = {}
    for line in FACTORYLIST.read_text(encoding="utf-8").splitlines():
        m = _ROW_RE.match(line)
        if m:
            name, role, desc, rel = m.groups()
            if role == "PART" and desc.strip().startswith("(element"):
                role = "ELEMENT"
            cat[name] = {"role": role, "desc": desc.strip(), "file": rel}
    return cat


def _starter_catalog_lines(catalog: dict) -> str:
    lines = []
    for name, info in sorted(catalog.items()):
        if info["role"] in STARTER_ROLES:
            lines.append(f"- {name} [{info['role']}] {info['desc']}  ({info['file']})")
    return "\n".join(lines)


def _fallback_factory_from_prompt(prompt: str, catalog: dict) -> tuple[str, str]:
    """Cheap deterministic backup when the LLM picker fails or returns junk."""
    prompt_words = set(re.findall(r"[a-z0-9]+", prompt.lower()))
    prompt_hint_order = _prompt_hint_order(prompt_words)
    prompt_hint_rank = {
        name: len(prompt_hint_order) - i
        for i, name in enumerate(prompt_hint_order)
    }
    default_order = (DEFAULT_FALLBACK_ORDER_ORGANIC
                     if prompt_words & ORGANIC_PROMPT_WORDS
                     else DEFAULT_FALLBACK_ORDER_HARD)
    default_rank = {
        name: len(default_order) - i
        for i, name in enumerate(default_order)
    }
    best: tuple[tuple[int, int, int, str], str] | None = None
    for name, info in catalog.items():
        if info["role"] not in STARTER_ROLES:
            continue
        text = f"{name} {info['desc']}".lower()
        words = set(re.findall(r"[a-z0-9]+", text))
        overlap = len(prompt_words & words)
        name_hit = 1 if name.lower() in prompt.lower() else 0
        hint_bonus = prompt_hint_rank.get(name, 0)
        default_bonus = default_rank.get(name, 0)
        score = (hint_bonus,
                 overlap * 8 + name_hit * 16 + default_bonus,
                 len(words & prompt_words),
                 name)
        if best is None or score > best[0]:
            best = (score, name)
    if best is None:
        raise SystemExit(f"no starter factories available in {FACTORYLIST}")
    return best[1], "fallback semantic-anchor search"


FACTORY_PICK_SYSTEM = """\
You choose the single best Infinigen starter factory for a requested object.

Important rules:
- The input object may be organic or geometric. Ignore that distinction.
- Pick exactly ONE factory from the catalog below.
- Prefer [ENTRY] factories. Pick [ELEMENT] only when the requested object literally is
  that single element.
- Never pick PART rows.
- Never return null if any starter factory is remotely usable. When there is no perfect
  match, choose the least-bad structural starting point anyway.

Return ONLY JSON:
{"factory": "<name>", "reason": "<one short sentence>"}.
"""


ORGANIC_PROMPT_WORDS = {
    "animal", "animals", "bird", "birds", "fish", "frog", "insect", "lizard", "lobster",
    "marine", "nature", "organic", "plant", "plants", "reptile", "sea", "tree", "trees",
    "underwater", "vegetation", "wood", "woodland", "bush", "bushes", "shrub", "shrubs",
    "leaf", "leaves", "branch", "branches", "blossom", "blossoms", "flower", "flowers",
    "fern", "cactus", "coral", "jellyfish", "seaweed", "coconut", "palm", "succulent",
}

PROMPT_HINTS: list[tuple[set[str], list[str]]] = [
    ({"greenhouse", "conservatory", "indoor", "garden", "plant", "plants"},
     ["WindowFactory", "LargePlantContainerFactory", "PlantContainerFactory",
      "TreeFactory", "BushFactory", "FernFactory", "SpiderPlantFactory",
      "SnakePlantFactory", "SucculentFactory"]),
    ({"desert", "dune", "dunes", "sand", "sandy", "arid", "dry"},
     ["CactusFactory", "ColumnarCactusFactory", "GlobularCactusFactory",
      "PrickyPearCactusFactory", "SucculentFactory"]),
    ({"tree", "trees", "forest", "wood", "woodland", "bush", "bushes", "shrub", "shrubs",
      "plant", "plants", "grove", "trunk", "branch", "branches", "leaf", "leaves",
      "flower", "flowers", "blossom", "blossoms", "fern", "palm", "coconut", "succulent",
      "cactus"},
     ["TreeFactory", "GenericTreeFactory", "BushFactory", "PalmTreeFactory",
      "CoconutTreeFactory", "FernFactory", "SpiderPlantFactory", "SnakePlantFactory",
      "SucculentFactory", "CactusFactory"]),
    ({"coral", "reef", "underwater", "ocean", "sea", "marine", "aquarium", "jellyfish",
      "fish", "crab", "lobster", "seaweed", "urchin"},
     ["CoralFactory", "FishSchoolFactory", "FishFactory", "JellyfishFactory",
      "SeaweedFactory", "CrabFactory", "LobsterFactory", "SpinyLobsterFactory",
      "UrchinFactory"]),
    ({"chair", "seat", "sofa", "couch", "stool", "armchair", "office", "bar"},
     ["ChairFactory", "OfficeChairFactory", "ArmChairFactory", "BarChairFactory"]),
    ({"desk", "table", "piano", "keyboard", "instrument", "workbench", "bench", "stand"},
     ["TableTopFactory", "TableDiningFactory", "CoffeeTableFactory", "SideTableFactory",
      "SidetableDeskFactory", "SimpleDeskFactory", "TVStandFactory"]),
    ({"lab", "laboratory", "chemical", "beaker", "flask", "petri", "rack", "experiment"},
     ["RackFactory", "BottleFactory", "JarFactory", "TableTopFactory", "BookStackFactory",
      "VaseFactory"]),
    ({"shelf", "cabinet", "rack", "storage", "bookcase", "drawer"},
     ["LargeShelfFactory", "WallShelfFactory", "CellShelfFactory", "RackFactory",
      "KitchenCabinetFactory", "SingleCabinetFactory", "TVStandFactory"]),
    ({"lamp", "light", "lighting"},
     ["LampFactory", "DeskLampFactory", "FloorLampFactory", "CeilingLightFactory",
      "CeilingClassicLampFactory"]),
    ({"window", "door", "glass"},
     ["WindowFactory", "GlassPanelDoorFactory", "PanelDoorFactory", "LiteDoorFactory",
      "LouverDoorFactory"]),
    ({"bottle", "jar", "cup", "vase", "container", "box", "carton", "can", "bag"},
     ["BottleFactory", "JarFactory", "CupFactory", "VaseFactory", "FoodBoxFactory",
      "CanFactory", "FoodBagFactory", "BoxFactory"]),
    ({"book", "books", "stack", "column"},
     ["BookStackFactory", "BookColumnFactory", "BookFactory"]),
    ({"kitchen", "appliance", "fridge", "refrigerator", "microwave", "oven", "stove",
      "dishwasher", "toaster", "sink", "faucet"},
     ["RefrigeratorFactory", "MicrowaveFactory", "OvenFactory", "DishwasherFactory",
      "StovetopFactory", "ToasterFactory", "BathroomSinkFactory", "StandingSinkFactory",
      "SinkFactory", "TapFactory"]),
    ({"bathroom", "toilet", "bathtub", "shower"},
     ["BathroomSinkFactory", "StandingSinkFactory", "ToiletFactory", "BathtubFactory"]),
    ({"market", "store", "shop", "stall", "booth", "vendor", "food", "night"},
     ["TableDiningFactory", "CoffeeTableFactory", "TableTopFactory", "FoodBoxFactory",
      "FoodBagFactory", "BottleFactory", "CanFactory", "CupFactory", "RackFactory"]),
    ({"motorcycle", "motorbike", "bike", "bicycle", "car", "truck", "vehicle", "boat",
      "ship", "sailboat", "submarine", "parachute", "paraglider", "plane", "aircraft",
      "rocket", "drone"},
     ["TableTopFactory", "TVStandFactory", "RackFactory", "WindowFactory", "ChairFactory"]),
]

DEFAULT_FALLBACK_ORDER_ORGANIC = [
    "TreeFactory", "GenericTreeFactory", "BushFactory", "PalmTreeFactory",
    "CoconutTreeFactory", "FernFactory", "SpiderPlantFactory", "SnakePlantFactory",
    "SucculentFactory", "CactusFactory", "CoralFactory", "FishFactory",
    "FishSchoolFactory", "JellyfishFactory", "SeaweedFactory", "BirdFactory",
    "FlyingBirdFactory", "CrabFactory", "LobsterFactory", "SpinyLobsterFactory",
]

DEFAULT_FALLBACK_ORDER_HARD = [
    "TableTopFactory", "TableDiningFactory", "CoffeeTableFactory", "SideTableFactory",
    "SidetableDeskFactory", "SimpleDeskFactory", "ChairFactory", "OfficeChairFactory",
    "ArmChairFactory", "BarChairFactory", "LargeShelfFactory", "WallShelfFactory",
    "CellShelfFactory", "TVStandFactory", "RackFactory", "LampFactory", "DeskLampFactory",
    "FloorLampFactory", "WindowFactory", "MirrorFactory", "BottleFactory", "JarFactory",
    "CupFactory", "VaseFactory", "BookStackFactory", "RefrigeratorFactory",
    "MicrowaveFactory", "OvenFactory", "DishwasherFactory", "BathroomSinkFactory",
    "StandingSinkFactory", "ToiletFactory", "BathtubFactory",
]


def _prompt_hint_order(prompt_words: set[str]) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    for terms, names in PROMPT_HINTS:
        if not (prompt_words & terms):
            continue
        for name in names:
            if name not in seen:
                order.append(name)
                seen.add(name)
    return order


def choose_starting_factory(prompt: str, catalog: dict) -> tuple[str, dict]:
    """Pick a best starter factory from FactoryList for any prompt."""
    catalog_text = _starter_catalog_lines(catalog)
    user = (
        f"REQUESTED OBJECT:\n{prompt}\n\n"
        f"CATALOG (ENTRY / ELEMENT factories only):\n{catalog_text}\n\n"
        "Return the JSON now."
    )
    try:
        resp = _client().messages.create(
            model=OPUS_MODEL, max_tokens=900, system=FACTORY_PICK_SYSTEM,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        j = _parse_json_obj(text)
        factory = str(j.get("factory") or "").strip()
        reason = str(j.get("reason") or "").strip()
        if factory not in catalog or catalog[factory]["role"] not in STARTER_ROLES:
            fallback_factory, fallback_reason = _fallback_factory_from_prompt(prompt, catalog)
            return fallback_factory, {
                "mode": "auto",
                "selected": fallback_factory,
                "reason": f"llm returned invalid factory; {fallback_reason}",
                "raw": text,
            }
        return factory, {
            "mode": "auto",
            "selected": factory,
            "reason": reason or "auto-picked from the prompt",
            "raw": text,
        }
    except Exception as ex:
        fallback_factory, fallback_reason = _fallback_factory_from_prompt(prompt, catalog)
        return fallback_factory, {
            "mode": "auto",
            "selected": fallback_factory,
            "reason": f"{type(ex).__name__}: {ex}; {fallback_reason}",
            "raw": "",
        }


# ----------------------------------------- static import collection (fresh-build path)
# Ported verbatim (adapted names) from OtherTest/search_related.py — used only when a
# factory has no prebuilt template library, to find its upper editable + infra files.
def _module_to_rel(mod_parts: list[str]) -> str | None:
    cand = CODEBASE.joinpath(*mod_parts).with_suffix(".py")
    if cand.exists():
        return str(cand.relative_to(CODEBASE))
    pkg = CODEBASE.joinpath(*mod_parts, "__init__.py")
    if pkg.exists():
        return str(pkg.relative_to(CODEBASE))
    return None


def _parent_init_chain(relpath: str) -> set[str]:
    mod_parts = relpath[:-3].split("/")
    if mod_parts[-1] == "__init__":
        mod_parts = mod_parts[:-1]
    out = set()
    for i in range(1, len(mod_parts)):
        pkg_init = CODEBASE.joinpath(*mod_parts[:i], "__init__.py")
        if pkg_init.exists():
            out.add(str(pkg_init.relative_to(CODEBASE)))
    return out


def _imported_files(relpath: str) -> set[str]:
    deps: set[str] = set()
    src = (CODEBASE / relpath).read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return deps
    mod_parts = relpath[:-3].split("/")
    is_init = (CODEBASE / relpath).name == "__init__.py"
    pkg_parts = mod_parts if is_init else mod_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("infinigen"):
                    r = _module_to_rel(a.name.split(".")[1:])
                    if r:
                        deps.add(r)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                target = base + (node.module.split(".") if node.module else [])
                r = _module_to_rel(target)
                if r:
                    deps.add(r)
                for a in node.names:
                    r2 = _module_to_rel(target + [a.name])
                    if r2:
                        deps.add(r2)
            elif node.module and node.module.startswith("infinigen"):
                mp = node.module.split(".")[1:]
                r = _module_to_rel(mp)
                if r:
                    deps.add(r)
                for a in node.names:
                    r2 = _module_to_rel(mp + [a.name])
                    if r2:
                        deps.add(r2)
    return deps


def collect_related_files(entry_relpath: str, max_depth: int = 2,
                          skip_init_fanout: bool = True) -> list[str]:
    seen = {entry_relpath}
    frontier = [(entry_relpath, 0)]
    while frontier:
        rel, depth = frontier.pop()
        if depth >= max_depth:
            continue
        deps = _parent_init_chain(rel)
        is_init = (CODEBASE / rel).name == "__init__.py"
        if not (is_init and skip_init_fanout):
            deps |= _imported_files(rel)
        for dep in deps:
            if dep not in seen:
                seen.add(dep)
                frontier.append((dep, depth + 1))
    return sorted(seen)


def _prune_variant_families(files: list[str], entry_stem: str) -> list[str]:
    fam = collections.defaultdict(list)
    for f in files:
        base = f.rsplit("/", 1)[-1][:-3]
        if "_" in base:
            fam[base.split("_", 1)[1]].append(f)
    drop = set()
    for suffix, members in fam.items():
        if len(members) < 3:
            continue
        matched = [m for m in members if m.rsplit("/", 1)[-1].startswith(entry_stem + "_")]
        if matched:
            drop.update(set(members) - set(matched))
    return [f for f in files if f not in drop]


def prune_menu_siblings(upper: list[str], entry_relpath: str, request: str) -> list[str]:
    req = request.lower()
    drop: set[str] = set()
    upper_set = set(upper)
    for rel_file in [entry_relpath] + upper:
        try:
            tree = ast.parse((CODEBASE / rel_file).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("infinigen") and len(node.names) >= 3):
                base = node.module.split(".")[1:]
                members = {}
                for a in node.names:
                    r = _module_to_rel(base + [a.name])
                    if r and r.endswith(f"/{a.name}.py") and r in upper_set:
                        members[a.name] = r
                if len(members) >= 3:
                    matched = [nm for nm in members if nm.lower() in req]
                    if matched:
                        for nm, r in members.items():
                            if nm not in matched:
                                drop.add(r)
    return [f for f in upper if f not in drop]


def _is_upper(rel: str) -> bool:
    """Upper-level 'content' files a request rewrites: the asset's own geometry/shape/
    shader code under assets/objects/. Everything else (shared materials + engine/util)
    is infra you edit the SELECTION of, so it links back to infinigen unchanged.

    ★sim_objects counts too★ —— sim factory(box.py, refrigerator.py ...)也是資產
    自己的 content，只是住在隔壁目錄。漏掉它會讓 sim starter 的 upper 算出 0，
    run_coder 就把 src/ seed 成空的，coder 只好自己猜新檔要放哪：golden_pangolin
    猜 src/ 過關，polar_bear_shanda 猜 code/ 根目錄，被 src.rglob("*.py") 判成
    「produced no files」。同一個 bug，結果由運氣決定。(2026-09-19)
    """
    return rel.startswith(("assets/objects/", "assets/sim_objects/"))


# ------------------------------------------------------- code_structure map (Opus, SDK)
STRUCTURE_SYSTEM = """\
You are an expert Infinigen procedural-generation developer. You are given the
UPPER-LEVEL source files of one factory (the files a user would edit to change
the output) plus a list of INFRA files that are linked back to the original
infinigen package unchanged. Produce a concise map, code_structure.txt, that
tells someone what they can change here. Output PLAIN TEXT in this format:

SUMMARY: <2-4 sentences: what this factory builds, and what the upper-level
files collectively do / how they fit together>

EDITABLE (upper-level files — change these to alter the result):
FILE: <relpath>
  <ClassName or function>: <what it controls>
    knobs: <name>(current value/range) -> <visual effect>; <name2>(...) -> ...
  ...

LINKED TO INFINIGEN (infra — do NOT edit, resolved from the original package):
  <relpath> -> <one line: what engine capability it provides>
  ...

Rules: surface every meaningful tunable constant/parameter as a knob (colors,
counts, sizes, ranges, probabilities). Be specific with parameter names. Note
where a value is sampled randomly vs fixed. Keep it tight; no prose outside the format."""


def generate_code_structure(out_dir: Path, factory: str, prompt: str,
                            entry_rel: str, upper: list[str], infra: list[str]) -> Path:
    blocks = []
    for rel in upper:
        content = (CODEBASE / rel).read_text(encoding="utf-8", errors="replace")
        blocks.append(f"### {rel}\n```python\n{content}\n```")
    infra_list = "\n".join(f"  - {r}" for r in infra)
    user = (f'Factory: {factory}   (entry file: {entry_rel})\n'
            f'Request that selected it: "{prompt}"\n\n'
            f"UPPER-LEVEL FILES (editable):\n\n" + "\n\n".join(blocks) +
            f"\n\nINFRA FILES (linked back to infinigen, not shown):\n{infra_list}\n\n"
            "Write code_structure.txt now.")
    resp = _client().messages.create(
        model=OPUS_MODEL, max_tokens=4000, system=STRUCTURE_SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    path = out_dir / "code_structure.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------- overlay build
def build_overlay(out_dir: Path, upper_files: list[str],
                  src_subdir: str = "code/src", overlay_subdir: str = "code/overlay") -> Path:
    """Full symlink-mirror of real infinigen where EVERY file links back to the original,
    except the upper files, which are de-linked into real copies (seeded from src_subdir).
    Editing those copies changes the render; all else resolves to original infinigen.
    Returns the overlay import root (dir containing `infinigen/`)."""
    overlay = out_dir / overlay_subdir
    # `rm -rf` (not shutil.rmtree): the overlay is a huge cp -rs symlink tree and the
    # render populates __pycache__/*.pyc inside it, which makes Python's fd-based rmtree
    # choke ("Directory not empty") on symlink+pyc dirs. rm -rf handles it robustly.
    subprocess.run(["rm", "-rf", str(overlay)], check=True)
    overlay.mkdir(parents=True)
    real_pkg = CODEBASE.resolve()
    subprocess.run(["cp", "-rs", str(real_pkg), str(overlay / "infinigen")], check=True)
    src_dir = out_dir / src_subdir
    for rel in upper_files:
        link = overlay / "infinigen" / rel
        if link.is_symlink() or link.exists():
            link.unlink()
        seed = src_dir / rel
        if not seed.exists():
            # ★退回時也要看 base overlay★ —— 只退回官方版的話，ADAPT 基底裡
            #   agent 這輪沒動到的檔會被官方版蓋掉，改出來的東西會安靜地
            #   掉回原始外觀(聖誕樹會少掉星星和吊球)。
            seed = _seed_source(rel)
        link.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed, link)
    (overlay / "overlay_source.json").write_text(
        json.dumps({"base_pkg": str(CODEBASE)}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return overlay


# ══════════════════════════════════════════════════════════════════════════════
# ★階段 B → 階段 C 的交接 gate(2026-07-30 加)★
#   placer.py(階段 C) 對 overlay 有兩個硬性要求，這裡在生成結束時就驗掉，
#   ★不要等到跑 populate 才發現★(20 分鐘 vs 幾秒)。
#
#   gate 3  factory.txt 必須在 ★overlay root★
#           ExtraWorld 原本只寫在 case 根目錄，placer 讀的是 code_dir/factory.txt
#           (placer.py:96 / apply_plan.py:165)。內容格式相容，只是位置不同。
#   gate 2  ★一行 import 測★ —— 確認 overlay 真的蓋過官方版，且介面還在。
#           0729 摔過「假成功」：log 全綠、report 正確、★畫面卻是官方的★，
#           原因是只有入口檔被載到，它 import 的仍是官方版。
# ══════════════════════════════════════════════════════════════════════════════
def write_overlay_factory_txt(overlay: Path, factory: str, role: str,
                              entry_rel: str, factory_fqn: str, note: str = "",
                              anchoring: str | None = None) -> Path:
    """把 factory.txt 也寫進 overlay root —— ★placer.py 讀的就是這個位置★。"""
    dst = Path(overlay) / "factory.txt"
    dst.write_text(
        "# asset generator 產物的中繼資料(placer.py 階段 C 讀這份)\n"
        "# ★位置很重要★：placer 假設 factory.txt 就在 code_dir(= overlay root) 裡面。\n"
        f"fqn: {factory_fqn}\n"
        f"factory: {factory}\n"
        f"entry: {entry_rel}\n"
        f"role: {role}\n"
        "kind: hero\n"
        + (f"anchoring: {anchoring}\n" if anchoring else "")
        + (f"note: {note}\n" if note else ""),
        encoding="utf-8")
    return dst


def verify_overlay_importable(overlay: Path, factory_fqn: str, timeout: int = 300) -> dict:
    """★一行 import 測★：幾秒鐘確認 overlay 是不是真的生效，取代 20 分鐘的 populate。

    檢查三件事(全部在一個乾淨子進程裡，PYTHONPATH 排在最前面)：
      ① entry 模組的 __file__ ★指向 overlay★，不是官方 infinigen
      ② Factory class 拿得到，且 ★有 create_asset★(placer 靠它生東西)
      ③ overlay 底下★所有★改寫過的真檔(非 symlink)，凡是被 import 到的，
         也都指向 overlay ⇒ ★這才抓得到「只有入口檔生效」的假成功★

    ⚠️ ★blender 那個 conda env 有一份 editable-install 的 infinigen★
       (site-packages/__editable__.infinigen-1.18.0.pth → PC_Blen/Infinigen)。
       overlay 沒建對的時候 import 會★悄悄落到那一份★，畫面長出官方外觀。
       ⇒ 這正是本檢查要抓的東西；報告裡的 entry_file 會直接指出落到哪。
    """
    overlay = Path(overlay).resolve()
    mod_fqn, cls_name = factory_fqn.rsplit(".", 1)
    probe = f'''
import json, sys, pathlib
ov = pathlib.Path({str(overlay)!r}).resolve()
out = {{"ok": False, "checks": {{}}, "bad": []}}
try:
    import importlib
    m = importlib.import_module({mod_fqn!r})
    f = str(pathlib.Path(m.__file__).resolve())
    out["entry_file"] = f
    out["checks"]["entry_from_overlay"] = str(ov) in f
    cls = getattr(m, {cls_name!r}, None)
    out["checks"]["class_found"] = cls is not None
    out["checks"]["has_create_asset"] = callable(getattr(cls, "create_asset", None))
    # 掃 overlay 裡每一個「真檔」(symlink 是沒改的官方檔，不用驗)
    n_real = n_loaded = 0
    for p in ov.rglob("*.py"):
        if p.is_symlink() or "__pycache__" in p.parts:
            continue
        n_real += 1
        rel = p.relative_to(ov)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        fq = ".".join(parts)
        mod = sys.modules.get(fq)
        if mod is None:
            continue
        n_loaded += 1
        mf = str(pathlib.Path(getattr(mod, "__file__", "") or "").resolve())
        if str(ov) not in mf:
            out["bad"].append({{"module": fq, "file": mf}})
    out["real_files"] = n_real
    out["loaded_from_overlay"] = n_loaded
    out["checks"]["no_official_leak"] = not out["bad"]
    out["ok"] = all(out["checks"].values())
except Exception as e:
    out["error"] = f"{{type(e).__name__}}: {{e}}"
print("__PROBE__" + json.dumps(out))
'''
    env = os.environ.copy()
    # ⚠️ ★PYTHONPATH 必須在 python 啟動前設好★ —— 上層 package 一旦被 import，
    #    __path__ 就固定了，事後 sys.path.insert 完全無效(0729 第二節)。
    # ⚠️ ★不要繼承呼叫端的 PYTHONPATH★ —— 機器上有好幾份 infinigen 複本
    #    (PC_Blen/、infinigen_o/、OtherTest/…)。繼承的話「洩漏到哪一份」會隨
    #    呼叫環境而變，測出來的結果不可重現。這裡固定成 overlay + CODEBASE。
    env["PYTHONPATH"] = f"{overlay}:{CODEBASE.parent}"
    try:
        r = subprocess.run([BLENDER_PYTHON, "-c", probe], env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"import probe timed out after {timeout}s"}
    line = next((l for l in r.stdout.splitlines() if l.startswith("__PROBE__")), None)
    if not line:
        return {"ok": False, "error": "probe produced no result",
                "stderr": (r.stderr or "")[-800:]}
    return json.loads(line[len("__PROBE__"):])


def run_stage_c_gates(overlay: Path, out_dir: Path, factory: str, role: str,
                      entry_rel: str, factory_fqn: str, note: str = "") -> dict:
    """跑完 gate 3 + gate 2，結果寫進 out_dir/stage_c_gate.json 並印出來。"""
    gate = {"overlay": str(overlay)}
    gate["factory_txt"] = str(write_overlay_factory_txt(
        overlay, factory, role, entry_rel, factory_fqn, note))
    print(f"[gate] factory.txt -> {gate['factory_txt']}", flush=True)
    print("[gate] import 測試中(幾秒)...", flush=True)
    probe = verify_overlay_importable(overlay, factory_fqn)
    gate["import_probe"] = probe
    gate["ok"] = bool(probe.get("ok"))
    if probe.get("ok"):
        print(f"[gate] ✅ overlay 可用 —— entry 來自 overlay、create_asset 在、"
              f"{probe.get('loaded_from_overlay', 0)}/{probe.get('real_files', 0)} "
              f"個改寫檔無官方洩漏", flush=True)
    else:
        print(f"[gate] ⛔ ★overlay 有問題，不要送進 placer★: "
              f"{probe.get('error') or probe.get('checks')}", flush=True)
        for b in probe.get("bad", [])[:5]:
            print(f"[gate]    官方版洩漏: {b['module']} -> {b['file']}", flush=True)
    (Path(out_dir) / "stage_c_gate.json").write_text(
        json.dumps(gate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return gate


# ------------------------------------------------------- claude CLI coding agent runner
def colour_directive(mode: str, reason: str = "") -> str:
    """把 Director 的 color_variation 欄位翻成給 agent 的一句話。

    ★為什麼不寫在 prompt 裡當通則★(2026-09-13)：先前試過在 coder prompt 裡列
    「花/珊瑚/蝴蝶要變色」的清單，但那是用分類學在猜 —— 向日葵、薰衣草、帝王蝶
    全都會被誤判，而「一片人工花田該同色、野地花叢該雜色」更是純場景語意。
    這種判斷交給有語意理解的 Director，這裡只負責傳話。
    """
    why = f" ({reason.strip()})" if reason and reason.strip() else ""
    if mode == "per_seed":
        return (
            "\n\nCOLOUR MUST VARY WITH THE SEED" + why + ". Different instances of this "
            "object are spawned with different `factory_seed`s and must come out in "
            "DIFFERENT colours. The renders you are judged on only ever show seed 0, so "
            "hard-coding the reference image's hue looks free — it is not. Find the "
            "factory's per-seed colour hook (a `base_hue`, a sampled `hsv2rgba(...)`, a "
            "palette draw) and KEEP IT WIRED: drive the shader from that hook and widen "
            "or shift its range so every seed lands on a different, still-plausible "
            "colour for this object. Keep saturation/value in a range that survives the "
            "bright studio background; only the hue needs to travel."
        )
    if mode == "fixed":
        return (
            "\n\nCOLOUR IS FIXED" + why + ". Every `factory_seed` must render this object "
            "in the SAME colour — a scattered group of them has to look like one species "
            "or one batch. Pinning the hue here is correct. Size, count, proportion and "
            "shape must still vary with the seed."
        )
    return ""


def _run_cli_agent(cwd: Path, prompt: str, allowed_tools: str, label: str = "agent") -> str:
    """Retry a failed agent from the original src/ snapshot; fail the round if exhausted.

    CLI_AGENT_RETRIES counts retries after the first attempt (default 3). Successful
    calls keep edits; failed calls restore src/, including deleted/new files. Raising
    prevents callers from rendering the seeded factory or accepting a failed revision.
    """
    cmd = [CLAUDE_BIN, "--print", "--model", OPUS_MODEL, "--output-format", "json",
           "--allowedTools", allowed_tools, "-p", prompt]
    retries = max(0, int(os.environ.get("CLI_AGENT_RETRIES", "3")))
    retry_delay = max(0.0, float(os.environ.get("CLI_AGENT_RETRY_DELAY", "60")))
    src = cwd / "src"
    if src.is_symlink():
        raise RuntimeError(f"[cli] {label}: src must be a real directory")
    had_src = src.is_dir()
    with tempfile.TemporaryDirectory(prefix="procedural_agent_") as backup_dir:
        backup = Path(backup_dir) / "src"
        if had_src:
            shutil.copytree(src, backup, symlinks=True)
        for attempt in range(retries + 1):
            success = False
            detail = ""
            try:
                r = subprocess.run(cmd, cwd=str(cwd), env=_cli_env(),
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
                if r.returncode != 0:
                    detail = f"exit {r.returncode}: " + " | ".join(
                        s.strip().replace("\n", " ")[:400]
                        for s in (r.stderr, r.stdout) if s and s.strip())
                else:
                    try:
                        j = json.loads(r.stdout)
                    except (ValueError, TypeError):
                        j = None
                    if not isinstance(j, dict):
                        detail = "invalid JSON reply"
                    elif (j.get("is_error") or
                          j.get("subtype", "success") != "success"):
                        detail = f"CLI error result: {str(j)[:800]}"
                    elif not isinstance(j.get("result"), str) or not j["result"].strip():
                        detail = "empty agent reply"
                    else:
                        cost = j.get("total_cost_usd")
                        if isinstance(cost, (int, float)):
                            print(f"[cli] {label}: ${cost:.4f} "
                                  f"({j.get('num_turns', '?')} turns)", flush=True)
                        success = True
                        return j["result"]
            except OSError as exc:
                detail = f"could not launch CLI: {exc}"
            finally:
                if not success:
                    if src.is_symlink() or src.is_file():
                        src.unlink()
                    elif src.exists():
                        shutil.rmtree(src)
                    if had_src:
                        shutil.copytree(backup, src, symlinks=True)
            print(f"[cli] {label} attempt {attempt + 1}/{retries + 1} failed; "
                  f"src restored: {detail}", flush=True)
            if attempt < retries:
                delay = min(retry_delay * (2 ** attempt), 300.0)
                print(f"[cli] {label}: retrying in {delay:g}s", flush=True)
                time.sleep(delay)
        raise RuntimeError(f"[cli] {label} failed after {retries + 1} attempts: {detail}")


def _snapshot_pkg(pkg: Path) -> dict:
    return {p: (p.stat().st_mtime, p.stat().st_size) for p in pkg.rglob("*.py")}


def _tampered(pkg: Path, snap: dict) -> list[Path]:
    return [p for p, sig in snap.items()
            if not p.exists() or (p.stat().st_mtime, p.stat().st_size) != sig]


# ------------------------------------------------------------------------- agent prompts
PLANNER_AGENT_PROMPT = """\
You are a world-class expert in procedural-generation code, specifically Infinigen \
(Blender geometry-nodes / node_wrangler factories). Write a PLAN for producing the \
requested object by referencing/modifying the closest existing factory's code.

Requested object: "{request}"
Closest factory (entry class must stay spawnable): {factory}
{image_note}
Your working directory contains, all in infinigen's package layout (paths like \
assets/objects/..., core/...):
  - code_structure.txt   A map of this factory's editable knobs + which detail/engine \
    files are linked to infinigen. Read it FIRST to orient.
  - reference_src/        The filtered UPPER-LEVEL editable source files (the primary \
    files a coder would rewrite). Read these.
  - reference/            The FULL real Infinigen source. Read/Glob/Grep ANYTHING here \
    to GROUND your plan in real APIs — do NOT guess a function/class/material name; \
    open the file and confirm it exists and its signature before you name it.

Output the plan as your final reply (markdown), with:
1. STRATEGY: reuse-as-is vs tweak-parameters vs rewrite-a-method vs heavier \
   restructure — and WHY, tied to how far the request is from what the factory does.
2. LOCATE: the exact file + class/method + knob(s) controlling each requested feature, \
   quoted from the source. If a feature needs a detail/infra file (e.g. a material), \
   read it in reference/ and cite the REAL API (verified, not guessed).
3. EDITS: for each file to change, precisely what to change. Keep each file at its \
   relpath; keep the entry factory's class name + __init__ signature UNCHANGED. Note \
   any infra file the coder will need to PROMOTE into src to edit.
4. RELY-ON: which existing generation logic / node-graphs to KEEP (reuse preserves \
   quality); which infra stays linked.
5. RISKS: anything still uncertain after reading the real source.

Never propose rewriting node_wrangler geometry from scratch or plain-bpy primitives — \
that destroys quality. Bias to the smallest change that works."""

CODER_AGENT_PROMPT = """\
You are a world-class expert in procedural-generation code for Infinigen (Blender \
geometry-nodes / node_wrangler factories). Your job: make an existing factory's output \
match the requested object, by reading its real source and editing code.

Requested object: "{request}"
Entry factory (keep its class name + spawn signature UNCHANGED): {factory}
{image_note}
Your working directory has, all in infinigen's package layout (paths like \
assets/objects/..., core/...):
  - code_structure.txt   A map of this factory's editable knobs and which detail/engine \
    files are linked to infinigen. Read it FIRST to orient fast.
  - reference/   FULL, real Infinigen source — READ-ONLY. Read/Glob/Grep anything here \
    to learn the exact APIs (materials, node-groups, helpers) before you use them. You \
    may NOT write here.
  - src/         Your editable output. It is pre-seeded with the primary editable files. \
    EVERY change or new file you make goes here, at the same relpath.

{plan_block}How to work:
1. Read the seeded files in src/{plan_ref}. Read reference/ for anything you need — \
   DON'T GUESS an API. If the change needs a file that is not yet in src/ (e.g. a \
   material module under assets/materials/), first PROMOTE it: read reference/<relpath>, \
   then Write its content to src/<relpath>, then edit src/. Any file present under src/ \
   becomes a real, editable part of the render; every file NOT in src/ stays linked to \
   the original infinigen unchanged.
2. Make the edits in src/ to satisfy the request. You may heavily rewrite files, but: \
   keep each file at its relpath; keep the entry factory's top-level class NAME and \
   `__init__(self, factory_seed, ...)` signature unchanged so it stays spawnable; reuse \
   real infinigen classes / node-groups — NEVER reimplement node_wrangler geometry from \
   scratch or fall back to plain-bpy primitives.
3. Prefer the smallest change that works (tweak knobs, override a method, swap a material \
   generator) over gutting working node-graphs. Promote extra files only when you \
   genuinely need to read or change them.
4. KEEP THE FACTORY RANDOM. The factory is spawned as `Cls(seed).spawn_asset(i)` and must \
   produce a visibly DIFFERENT but equally valid instance for every seed. The infinigen \
   code you are editing already samples its dimensions, counts and colours from the \
   seeded RNG (`uniform`, `randint`, `np.random`, `rg`, ...). Do NOT replace those draws \
   with the single constant that happens to match the reference image — retarget the \
   RANGE instead, so the reference sits inside it and the whole range still reads as the \
   requested object. Ranges should span the small and the large end of the category, and \
   any count that genuinely varies should keep varying. A factory that renders the same \
   object for every seed has failed, however well it matches the reference.
   Whether the seed may also move the COLOUR is stated separately below; everything \
   else (size, count, proportion, pose) must vary with the seed no matter what.

When done, briefly list which src/ files you changed or created."""

REPAIR_AGENT_PROMPT = """\
You are a world-class procedural-generation code expert doing a CRASH REPAIR pass.
The code in src/ was written, but it FAILED TO RENDER — so there is no image to look at.
Your only evidence is the error below, and the code itself.

Requested object: "{request}"
Entry factory (keep its class name + spawn signature UNCHANGED): {factory}

The renderer ran this and it failed:

{error}

In your working directory:
  - src/   The EXACT code that failed. READ the customized files and find what breaks.

Fix the crash with the SMALLEST change that makes it render. Do not redesign the object,
do not add features, do not "improve" anything else — a rendering object that only roughly
matches the request is infinitely more useful than a perfect one that raises. Common causes:
a wrong API name or signature for this Infinigen version, an attribute that does not exist
on this node/socket, an index out of range, a None where a mesh was expected, or a modifier
whose evaluated geometry is empty. Keep every edit inside src/.
"""

REVISE_AGENT_PROMPT = """\
You are a world-class procedural-generation code expert doing a VISUAL revision pass.
The code in src/ was already written and RENDERED; now fix what only shows up after
rendering — the kind of bug you cannot see by reading code, only by looking at the image.

Requested object: "{request}"
Entry factory (keep its class name + spawn signature UNCHANGED): {factory}

In your working directory:
  - current_render.png  The render that the code in src/ CURRENTLY produces. READ IT (it \
    is an image). This is the ground truth of how the code actually looks.{target}
  - src/                The EXACT code that produced current_render.png. READ the files \
    here — especially the ones that were customized — to understand what made the render \
    look this way. This is your editable code; edit here.
  - code_structure.txt  Map of the factory's editable knobs — use it to locate the file \
    and knob behind a given visual feature quickly.
  - reference/          FULL real Infinigen source, READ-ONLY. Read any API before use.

Task:
1. READ current_render.png and compare it to the request{target_cmp}. List the VISUAL \
   problems only visible after rendering — wrong/missing color, a missing or invisible \
   part, wrong density/amount, a material bleeding onto the wrong region, a part too \
   small/large, wrong proportion, etc. Say what you SEE vs what is wanted. TRUST THE \
   RENDER over what the code appears to say.
2. Read the relevant files in src/ to find WHY it looks wrong, then EDIT src/ to fix it \
   (read reference/ for the real API if needed). Density/amount and material-region \
   issues are common and are NOT visible from reading code alone — only from the render.
3. Keep reusing real infinigen classes/node-graphs (no plain-bpy, no from-scratch node \
   graphs). Keep the entry class name + __init__ signature. Promote an infra file into \
   src/ (copy from reference/) only if you must edit it.

Reply with: what you saw wrong in the render, and exactly what you changed and why."""


def _seed_reference_symlink(link: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink() if link.is_symlink() else shutil.rmtree(link)
    link.symlink_to(CODEBASE.resolve())


def run_planner(request: str, factory: str, out_dir: Path, target_image: Path | None,
                colour: str = "") -> str:
    _seed_reference_symlink(out_dir / "reference")
    image_note = ""
    if target_image is not None:
        shutil.copy2(target_image, out_dir / "target_image.png")
        image_note = ("A target image is saved as target_image.png in your working "
                      "directory — Read it; it is the ground-truth target for color, "
                      "texture, and proportion (authoritative over the text).\n")
    prompt = PLANNER_AGENT_PROMPT.format(request=request, factory=factory,
                                         image_note=image_note) + colour
    print(f"[plan] read-only Opus CLI planner in {out_dir} ...")
    return _run_cli_agent(out_dir, prompt, "Read,Glob,Grep", label="planner")


def _write_confinement(code_dir: Path) -> None:
    settings = code_dir / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        '{ "permissions": { '
        '"allow": ["Read", "Glob", "Grep", "Edit(src/**)", "Write(src/**)"], '
        '"deny": ["Edit(reference/**)", "Write(reference/**)", '
        '"Edit(../**)", "Write(../**)"] } }\n')


def run_coder(request: str, factory: str, plan: str | None, code_dir: Path,
              upper: list[str], target_image: Path | None, colour: str = "") -> list[str]:
    """plan=None is the A4 ablation: the planning pass is removed entirely.

    ★Not the same as passing an empty plan★ — a blank {plan} would leave a hollow
    "the plan to implement:" section with nothing under it, which reads as a
    deleted field rather than as a pipeline that never had a planner. The whole
    block, and the sentence telling the coder to read it, come out together.
    """
    src = code_dir / "src"
    if src.exists():
        shutil.rmtree(src)
    for rel in upper:                                       # seed src/ with upper files
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_seed_source(rel), dst)
    _seed_reference_symlink(code_dir / "reference")
    cs_src = code_dir.parent / "code_structure.txt"
    if cs_src.exists():
        shutil.copy2(cs_src, code_dir / "code_structure.txt")
    _write_confinement(code_dir)
    image_note = ""
    if target_image is not None:
        shutil.copy2(target_image, code_dir / "target_image.png")
        image_note = ("A target image is saved as target_image.png in your working "
                      "directory — Read it; it is the ground-truth target for color, "
                      "texture, and proportion (authoritative over the text description).\n")
    plan_block = "" if plan is None else (
        "The plan to implement (from a prior planning pass):\n"
        "--------------------------------------------------------------------\n"
        f"{plan}\n"
        "--------------------------------------------------------------------\n\n")
    prompt = CODER_AGENT_PROMPT.format(request=request, factory=factory,
                                       plan_block=plan_block,
                                       plan_ref="" if plan is None else " and the plan",
                                       image_note=image_note) + colour
    real_pkg = CODEBASE.resolve()
    snap = _snapshot_pkg(real_pkg)
    print(f"[coder] Opus CLI agent in {code_dir} ...")
    _run_cli_agent(code_dir, prompt, "Read,Glob,Grep,Write,Edit", label="coder")
    if _tampered(real_pkg, snap):
        print("[coder] !! WARNING: shared infinigen changed despite read-only rules — unsafe")
    return sorted(str(p.relative_to(src)) for p in src.rglob("*.py"))


def run_repair_round(n: int, request: str, factory: str, code_dir: Path,
                     error_text: str) -> tuple[list[str], str]:
    """★渲染掛掉時的修錯輪★(2026-08-06 加) —— 沒有圖，只餵 traceback。

    以前這種情形直接中止，等於把 --rounds 3 浪費成 1 輪。而「渲染掛掉」其實是
    ★最容易修★的失敗：錯誤訊息直接指到行號，比「圖看起來不太對」明確得多。
    """
    prompt = REPAIR_AGENT_PROMPT.format(request=request, factory=factory,
                                        error=error_text or "(renderer produced no output)")
    _write_confinement(code_dir)
    real_pkg = CODEBASE.resolve()
    snap = _snapshot_pkg(real_pkg)
    print(f"[r{n}] ★repair★ agent fixing the crash (no render to look at) ...")
    reply = _run_cli_agent(code_dir, prompt, "Read,Glob,Grep,Write,Edit", label=f"repair-r{n}")
    if _tampered(real_pkg, snap):
        print(f"[r{n}] !! shared infinigen modified — unsafe")
    src = code_dir / "src"
    return sorted(str(p.relative_to(src)) for p in src.rglob("*.py")), (reply or "(no text)")


def run_revision_round(n: int, request: str, factory: str, code_dir: Path,
                       current_render: Path, target_image: Path | None,
                       hint: str = "", colour: str = "") -> tuple[list[str], str]:
    shutil.copy2(current_render, code_dir / "current_render.png")
    target = target_cmp = ""
    if target_image is not None:
        shutil.copy2(target_image, code_dir / "target_image.png")
        target = ("\n  - target_image.png    The ground-truth TARGET to match "
                  "(authoritative for color/texture/proportion).")
        target_cmp = " and the target image"
    prompt = REVISE_AGENT_PROMPT.format(request=request, factory=factory,
                                        target=target, target_cmp=target_cmp) + colour
    if hint:
        prompt += (f"\n\nA verifier compared the last render to the request and noted "
                   f"what is still off:\n  {hint}\nPrioritise fixing that.")
    _write_confinement(code_dir)
    real_pkg = CODEBASE.resolve()
    snap = _snapshot_pkg(real_pkg)
    print(f"[r{n}] revise agent viewing render + fixing code ...")
    reply = _run_cli_agent(code_dir, prompt, "Read,Glob,Grep,Write,Edit", label=f"revise-r{n}")
    if _tampered(real_pkg, snap):
        print(f"[r{n}] !! shared infinigen modified — unsafe")
    src = code_dir / "src"
    return sorted(str(p.relative_to(src)) for p in src.rglob("*.py")), (reply or "(no text)")


# --------------------------------------------------------------------------------- render
LAST_RENDER_ERROR = ""          # ★最近一次渲染失敗的尾巴★，給修錯輪當線索用


def _render(overlay: Path, fqn: str, out_png: Path, seed: int) -> bool:
    """渲染 overlay。★失敗時把 traceback 留在 LAST_RENDER_ERROR★(2026-08-06 加)。

    以前這裡是 `subprocess.run(cmd, env=env)` —— 輸出直接流到父行程的 stdout，
    程式本身★拿不到錯誤內容★。於是渲染掛掉時，修錯輪沒有任何可餵給 agent 的線索，
    只能中止(實測 christmas_trees：r1 渲染掛掉 ⇒ 「no render to revise from」⇒
    三輪只跑了一輪就放棄)。而 traceback 恰恰是最可修的線索。
    """
    global LAST_RENDER_ERROR
    cmd = [BLENDER_PYTHON, str(RENDER_FACTORY), str(overlay), fqn, str(out_png), str(seed)]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=RENDER_GPU)
    print(f"[render] CUDA_VISIBLE_DEVICES={RENDER_GPU} {' '.join(cmd)}")
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    print(out[-4000:], flush=True)                   # 仍然印出來，行為跟以前一樣
    ok = p.returncode == 0 and out_png.exists()
    if ok:
        LAST_RENDER_ERROR = ""
    else:
        # 只留跟錯誤有關的行 —— Blender 的 Fra:/Mem: 進度洗版會把 traceback 淹掉
        lines = [ln for ln in out.splitlines()
                 if not ln.startswith(("Fra:", "Mem:")) and ln.strip()]
        LAST_RENDER_ERROR = "\n".join(lines[-40:])
    return ok


# Five variants are enough for the pipeline QA gate while keeping this Blender-only
# check from dominating smoke/iteration time.  SEED_CHECK_N remains overrideable for
# experiments that need wider sampling.
SEED_CHECK_N = int(os.environ.get("SEED_CHECK_N", "5"))
SEED_CHANGED_FRAC_MIN = 0.02
# ★顏色多樣性門檻★ —— 平均 RGB 距離(0-255 空間)。實測：寫死顏色的海葵各 seed
#   距離 <2，改成吃 base_hue 之後 seed 之間可以差到 60 以上。12 抓得到「完全沒變」
#   又不會誤殺只有色調微調的資產。coloured_pixel_frac 低於門檻代表畫面本來就幾乎
#   沒有飽和顏色(灰白物件) ⇒ 這條不納入 ok。
SEED_COLOUR_DIST_MIN = 12.0
SEED_COLOUR_PIXEL_FRAC_MIN = 0.02


def check_seed_diversity(overlay: Path, fqn: str, out_dir: Path,
                         n: int = SEED_CHECK_N, colour_expect: str = "auto") -> dict:
    """Render the overlay at seeds 0..n-1 and report how much the object actually changes.

    An infinigen factory takes its variation from `factory_seed`, so this needs no extra
    machinery — `render_factory.py` already spawns `Cls(seed).spawn_asset(seed)`. What it
    checks is whether the REWRITE kept that variation alive: an agent that replaces sampled
    knobs with the constants that happened to match the reference image produces a factory
    that renders the same object for every seed.
    """
    import numpy as np
    from PIL import Image
    work = Path(out_dir) / "seeds"
    work.mkdir(parents=True, exist_ok=True)
    out = {"seeds": list(range(n)), "renders": []}
    imgs = []
    rgbs = []
    for s in range(n):
        png = work / f"seed_{s:02d}.png"
        ok = _render(overlay, fqn, png, s)
        out["renders"].append({"seed": s, "ok": bool(ok)})
        if ok:
            rgb = np.asarray(Image.open(png).convert("RGB"), dtype=np.float32)
            rgbs.append(rgb)
            imgs.append(rgb.mean(axis=2))
    if len(imgs) < 2:
        out["ok"] = False
        out["note"] = "fewer than 2 seeds rendered"
        return out
    # same measure as the blender generator: fraction of pixels that actually move, and the
    # MINIMUM over pairs, so one lively seed cannot carry a set of identical ones.
    changed = [float((np.abs(imgs[0] - imgs[k]) > 8).mean()) for k in range(1, len(imgs))]
    out["changed_pixel_frac"] = [round(c, 4) for c in changed]
    shape_ok = min(changed) >= SEED_CHANGED_FRAC_MIN

    # ★顏色也要查★(2026-09-13) —— 上面那個測度是在★灰階★上做的，兩個 seed 只要
    #   形狀一樣，換了顏色它幾乎讀不出差別。而寫死顏色正是最常見的退化：verifier
    #   每輪只看 seed 0，把 hue 釘死成參考圖的顏色只會讓分數變好，不會被罰
    #   (anemone_purple_bulb 的 shader 收了 base_hue 卻整個沒用到，十個 seed 全是
    #    同一種淡紫)。這裡改成比對「有顏色的像素」的平均 RGB。
    #   ⚠️ 本來就單色的東西(北極熊、白海葵、石頭)不該被這條判死 ⇒ 只有當畫面裡
    #     真的有一片飽和像素時才納入 ok。
    sat_fracs, means = [], []
    for rgb in rgbs:
        sat = rgb.max(axis=2) - rgb.min(axis=2)
        mask = sat > 12
        sat_fracs.append(float(mask.mean()))
        means.append(rgb[mask].mean(axis=0) if mask.any() else np.zeros(3))
    colour_dist = max(float(np.linalg.norm(means[0] - means[k]))
                      for k in range(1, len(means))) if len(means) > 1 else 0.0
    has_colour = (sum(sat_fracs) / len(sat_fracs)) >= SEED_COLOUR_PIXEL_FRAC_MIN
    # ★只有 Director 說「這東西該一株一色」時才把顏色納入 ok★ —— 向日葵、薰衣草、
    #   帝王蝶同色是正確的，不能因為十個 seed 都一樣就判失敗。
    colour_ok = True
    if colour_expect == "per_seed":
        colour_ok = (not has_colour) or colour_dist >= SEED_COLOUR_DIST_MIN
    out["colour_expect"] = colour_expect
    out["colour_dist"] = round(colour_dist, 2)
    out["coloured_pixel_frac"] = round(sum(sat_fracs) / len(sat_fracs), 4)
    out["colour_varies"] = bool(colour_dist >= SEED_COLOUR_DIST_MIN)

    out["ok"] = shape_ok and colour_ok
    notes = []
    if not shape_ok:
        notes.append(f"changing the seed moves only {min(changed):.2%} of the frame "
                     f"(need {SEED_CHANGED_FRAC_MIN:.0%}) — the rewrite most likely "
                     "replaced sampled parameters with constants")
    if not colour_ok:
        notes.append(f"every seed renders the SAME colour (mean-RGB distance "
                     f"{colour_dist:.1f}, need {SEED_COLOUR_DIST_MIN}) — the shader is "
                     "probably ignoring the factory's per-seed hue hook and hard-coding "
                     "the reference image's colour")
    if colour_expect == "fixed" and colour_dist >= SEED_COLOUR_DIST_MIN:
        # 不判失敗，但要講出來：Director 說這東西該同色，實際卻每個 seed 都不一樣。
        notes.append(f"colour was meant to be FIXED but seeds differ by {colour_dist:.1f} "
                     "in mean RGB — the shader is sampling a hue it should pin")
    if notes:
        out["note"] = "; ".join(notes)
    return out


def _frames_to_gif(frames: list, out_gif: Path, fps: int = 20) -> bool:
    from PIL import Image
    if not frames:
        return False
    imgs = [Image.open(f).convert("RGB") for f in frames]
    imgs[0].save(out_gif, save_all=True, append_images=imgs[1:],
                 duration=int(1000 / fps), loop=0, optimize=True)
    return out_gif.exists()


def render_turntable_gif(overlay: Path, fqn: str, out_dir: Path, seed: int,
                         n_frames: int = 36, views: str = "both") -> bool:
    """Render a level + top-down 360 turntable of the (best) overlay and assemble two GIFs:
    final_360.gif (eye-level) and final_360_topdown.gif (3/4 top-down)."""
    frames_dir = out_dir / "final_360_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # A smoke rerun may reuse an output directory that previously held a 36-frame
    # full turntable.  Remove only prior turntable frames so stale PNGs cannot make
    # an 8-frame smoke result look like a 72-frame result.
    for pattern in ("level_*.png", "top_*.png"):
        for old_frame in frames_dir.glob(pattern):
            old_frame.unlink()
    cmd = [BLENDER_PYTHON, str(RENDER_TURNTABLE), str(overlay), fqn,
           str(frames_dir), str(seed), str(n_frames)]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=RENDER_GPU,
               TURNTABLE_VIEWS=views)
    print(f"[turntable] rendering 360 GIF ...")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (out_dir / "turntable.log").write_text(r.stdout + "\n===STDERR===\n" + r.stderr)
    level_frames = sorted(frames_dir.glob("level_*.png"))
    if views == "level":
        return len(level_frames) == n_frames
    level_ok = _frames_to_gif(level_frames, out_dir / "final_360.gif")
    _frames_to_gif(sorted(frames_dir.glob("top_*.png")), out_dir / "final_360_topdown.gif")
    return level_ok


# ------------------------------------------------------------------ verifier / selector
def _parse_json_obj(text: str) -> dict:
    for pat in (r"```(?:json)?\s*\n(\{.*?\})\s*```", r"(\{.*?\})"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return {}


VERIFIER_SYSTEM = """\
You are a strict visual QA verifier. Given a requested object (text) and optionally a
target image, plus ONE candidate render, judge how well the render matches — considering
every requested part being present, correct colors, proportion, density/amount, and
overall fidelity. Be strict: a missing or wrong-colored key feature caps the score well
below 80. Output ONLY JSON: {"match_pct": <0-100 int>, "missing": "<one short phrase:
the single most important thing still wrong or missing, or 'none'>"}."""


def run_verifier(render_path: Path, request: str, target_image: Path | None) -> tuple[int, str]:
    content = []
    if target_image is not None:
        content += [_image_block(target_image), {"type": "text", "text": "^ TARGET image (the goal)."}]
    content += [_image_block(render_path),
                {"type": "text", "text": f'^ Candidate render. Requested object: "{request}". '
                                         f'Score the match (0-100). Reply JSON.'}]
    resp = _client().messages.create(
        model=OPUS_MODEL, max_tokens=250, system=VERIFIER_SYSTEM,
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    j = _parse_json_obj(text)
    pct = int(j.get("match_pct", 0)) if str(j.get("match_pct", "")).strip().isdigit() else 0
    note = str(j.get("missing", "") or text.strip()[:160])
    return pct, note


SELECTOR_SYSTEM = """\
You judge which candidate render best realizes a requested object. You are shown the
target image (if any) and several candidate renders labelled r1, r2, r3, .... Pick the
ONE render that best matches the request/target — correct parts present, correct colors,
right proportion and density, no obvious material or geometry errors, best overall
fidelity. Reply with ONLY a JSON object: {"best_round": <int>, "reason": "<1-2 sentences>"}."""


def run_selector(out_dir: Path, n_rounds: int, request: str, target_image: Path | None) -> int | None:
    content = []
    if target_image is not None:
        content += [_image_block(target_image), {"type": "text", "text": "^ TARGET image (the goal)."}]
    shown = []
    for n in range(1, n_rounds + 1):
        rp = out_dir / f"r{n}" / "render.png"
        if rp.exists():
            content += [_image_block(rp), {"type": "text", "text": f"^ Candidate render r{n}."}]
            shown.append(n)
    if not shown:
        return None
    if len(shown) == 1:
        (out_dir / "best.md").write_text(f"best_round: r{shown[0]}\nreason: only round rendered\n")
        return shown[0]
    content.append({"type": "text", "text":
        f'Requested object: "{request}". Candidates: {", ".join(f"r{n}" for n in shown)}. '
        'Reply JSON {"best_round": int, "reason": str}.'})
    resp = _client().messages.create(
        model=OPUS_MODEL, max_tokens=400, system=SELECTOR_SYSTEM,
        messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    j = _parse_json_obj(text)
    best = int(j["best_round"]) if str(j.get("best_round", "")).strip().isdigit() else shown[-1]
    (out_dir / "best.md").write_text(f"best_round: r{best}\nreason: {j.get('reason','')}\n")
    return best


# ---------------------------------------------------------------- reference resolution
def resolve_reference(name: str, role: str, entry_rel: str, request: str,
                      out_dir: Path, depth: int = 2) -> tuple[Path, list[str]]:
    """Populate reference_src/ + code_structure.txt for the factory; return (ref_dir, upper).
    Reuse the prebuilt library if present, else build fresh + persist to the library."""
    lib_dir = FACTORY_LIB / ("Entry" if role in STARTER_ROLES else "Part") / name
    lib_src, lib_cs = lib_dir / "src", lib_dir / "code_structure.txt"
    reference_dir = out_dir / "reference_src"
    cs = out_dir / "code_structure.txt"
    if reference_dir.exists():
        shutil.rmtree(reference_dir)

    if lib_src.is_dir() and lib_cs.exists():
        shutil.copytree(lib_src, reference_dir)
        shutil.copy2(lib_cs, cs)
        upper = sorted(str(p.relative_to(reference_dir)) for p in reference_dir.rglob("*.py"))
        print(f"[reference] REUSE prebuilt library {lib_dir.relative_to(TEMPLATES)} "
              f"({len(upper)} upper files) — no code_structure API call")
        return reference_dir, upper

    related = collect_related_files(entry_rel, max_depth=depth, skip_init_fanout=True)
    related = _prune_variant_families(related, Path(entry_rel).stem)
    upper = [r for r in related if _is_upper(r)]
    infra = [r for r in related if not _is_upper(r)]
    print(f"[reference] not in library — building: {len(upper)} upper, {len(infra)} infra")
    for rel in upper:
        dst = reference_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CODEBASE / rel, dst)
    generate_code_structure(out_dir, name, request, entry_rel, upper, infra)
    try:                                                    # persist to library for reuse
        if lib_src.exists():
            shutil.rmtree(lib_src)
        lib_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(reference_dir, lib_src)
        shutil.copy2(cs, lib_cs)
        print(f"[reference] saved to library {lib_dir.relative_to(TEMPLATES)}")
    except OSError as e:
        print(f"[reference] could not persist to library ({e}) — continuing")
    return reference_dir, upper


# -------------------------------------------------------------------------- main driver
def generate_procedural(name: str, prompt: str, factory: str | None, out_dir: Path,
                        target_image: Path | None = None, rounds: int = DEFAULT_ROUNDS,
                        seed: int = 0, depth: int = 2,
                        match_threshold: int = DEFAULT_MATCH_THRESHOLD,
                        turntable: bool = True, auto_ref: bool = True,
                        no_seed_check: bool = False,
                        turntable_frames_per_view: int = 36,
                        turntable_views: str = "both",
                        require_reference: bool = False,
                        color_variation: str = "auto",
                        color_reason: str = "",
                        no_plan: bool = False) -> dict:
    """Rewrite `factory`'s upper source to match the request and render the real factory
    over `rounds` (r1 = generate, r2.. = visual revision). If `factory` is omitted or set
    to `auto`, pick the closest starter factory from FactoryList first. Returns the best
    result.

    ★auto_ref(預設 True)★：沒給 target_image 時★自動生一張 reference★，
    之後 planner / coder / revise / verifier / selector ★全部都會拿到 prompt + ref★。

    ★require_reference(2026-08-26 加)★：ref 拿不到就★整個 case 失敗★，不要偷偷
    退回純文字模式。批量跑 paper 數據時，純文字跑出來的 case 一樣會產生 final.png、
    一樣被標成 ok，只在 log 裡留一行 ⚠️ —— 混進統計裡就再也分不出來了。
    ⇒ 寧可少一筆，也不要一筆說不清楚是怎麼跑出來的。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seed_check = None

    # ★ref 要在任何 agent 跑之前就備好★ —— planner 是第一個吃它的。
    if target_image is None and auto_ref:
        target_image = auto_reference(prompt, out_dir)
    if require_reference and (target_image is None or not Path(target_image).is_file()):
        raise SystemExit(f"[{name}] --require-reference: 沒有 reference 圖，"
                         f"拒絕用純文字模式生成(預期 {out_dir / 'target_single.png'})")

    catalog = parse_factory_list()
    factory_selection: dict
    requested_factory = factory
    if not factory or str(factory).strip().lower() == "auto":
        factory, factory_selection = choose_starting_factory(prompt, catalog)
    elif factory not in catalog or catalog[factory]["role"] not in STARTER_ROLES:
        print(f"[factory] requested '{factory}' is not a starter factory — auto-picking")
        factory, factory_selection = choose_starting_factory(prompt, catalog)
    else:
        factory_selection = {
            "mode": "explicit",
            "selected": factory,
            "reason": "passed explicitly",
            "raw": "",
        }
    if factory_selection.get("mode") == "auto":
        print(f"[factory] auto-picked starter -> {factory} ({factory_selection.get('reason','')})")
    else:
        print(f"[factory] using explicit starter -> {factory}")
    role, entry_rel = catalog[factory]["role"], catalog[factory]["file"]
    if not (CODEBASE / entry_rel).exists():
        raise SystemExit(f"factory file missing: {CODEBASE / entry_rel}")
    factory_fqn = "infinigen." + entry_rel[:-3].replace("/", ".") + "." + factory

    (out_dir / "request.txt").write_text(prompt + "\n")
    (out_dir / "factory_selection.json").write_text(
        json.dumps({
            "requested_factory": requested_factory,
            "selected_factory": factory,
            "selection": factory_selection,
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "factory.txt").write_text(f"factory: {factory}\nrole: {role}\nentry: {entry_rel}\n"
                                         f"fqn: {factory_fqn}\n")

    # ★人看的對照組★ —— 把「改造前」render 一張，純粹給人比對改動幅度多大；
    #   不進 reference_src/、不進任何 prompt，任何 agent 都看不到這張圖。
    #   ⚠️ ★ADAPT 的「改造前」是庫存變體，不是官方 factory★(2026-08-20 修)：
    #     先前一律渲官方版，於是黑白孔雀的 before 是一隻普通的鳥，看起來像
    #     「根本沒從庫出發」。code 其實是對的(src 逐位元組等於庫存變體)，
    #     錯的是這張圖 —— 但一張會讓人誤判的證據圖，跟壞掉沒兩樣。
    try:
        orig_png = out_dir / "original_factory_render.png"
        before_root = BASE_OVERLAY if BASE_OVERLAY is not None else CODEBASE.parent
        label = "library variant" if BASE_OVERLAY is not None else "unmodified official factory"
        if _render(before_root, factory_fqn, orig_png, seed):
            print(f"[orig] before-render ({label}) -> {orig_png}")
        else:
            print(f"[orig] before-render ({label}) FAILED (non-fatal)")
    except Exception as e:
        print(f"[orig] ⚠️ before-render 失敗: {type(e).__name__}: {e}")

    reference_dir, upper = resolve_reference(factory, role, entry_rel, prompt, out_dir, depth=depth)

    # ★ADAPT：可編輯檔集合要能吸收基底變體多出來的檔★
    #   upper 來自 factory template(針對官方 factory 算的)。庫存變體可能改到
    #   集合外的檔 —— 聖誕樹就新增了 tree_star.py 與 bauble.py。不補進 upper 的話
    #   build_overlay 不會把它們 de-link 成實體檔 ⇒ overlay 解析回官方版 ⇒
    #   ★改出來的東西安靜地少掉星星和吊球★。
    if BASE_OVERLAY is not None:
        extra = sorted(set(_base_overlay_changed()) - set(upper))
        if extra:
            print(f"[adapt] base overlay 另外改了 {len(extra)} 個 template 以外的檔，"
                  f"併入可編輯集合：{extra}")
            upper = sorted(set(upper) | set(extra))

    code_dir = out_dir / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    coder_upper = prune_menu_siblings(upper, entry_rel, prompt)
    if len(coder_upper) < len(upper):
        print(f"[reference] pruned {len(upper) - len(coder_upper)} irrelevant sibling module(s)")
    upper = coder_upper

    def _snapshot_round(n: int, plan_text: str) -> Path:
        rdir = out_dir / f"r{n}"
        if rdir.exists():
            shutil.rmtree(rdir)
        rdir.mkdir(parents=True)
        if (code_dir / "src").exists():
            shutil.copytree(code_dir / "src", rdir / "code")
        (rdir / f"plan_r{n}.md").write_text(plan_text or "(none)\n")
        return rdir

    colour_note = colour_directive(color_variation, color_reason)
    if colour_note:
        print(f"[colour] color_variation={color_variation}"
              f"{' — ' + color_reason if color_reason else ''}")

    n_rounds = max(1, rounds)
    last_render, hint = None, ""
    for n in range(1, n_rounds + 1):
        if n == 1:
            if no_plan:                                 # ★A4 ablation：拿掉 stage B★
                plan_text = "(no plan: --no-plan, A4 ablation)"
                print("[plan] --no-plan ⇒ skipping the planner entirely")
                (out_dir / "plan.md").write_text(plan_text + "\n")
            else:
                plan_text = run_planner(prompt, factory, out_dir, target_image,
                                        colour=colour_note)
                if not plan_text.strip():
                    raise SystemExit("[plan] planner returned empty")
                (out_dir / "plan.md").write_text(plan_text)
            written = run_coder(prompt, factory, None if no_plan else plan_text,
                                code_dir, upper, target_image, colour=colour_note)
            if not written:
                raise SystemExit("[coder] produced no files")
            print(f"[r1] src/ has {len(written)} file(s)")
        else:
            if last_render is None:
                # ★沒有圖不等於沒線索★(2026-08-06 改) —— 以前這裡直接 break，
                #   把 --rounds 3 浪費成 1 輪(christmas_trees 就是這樣掛的)。
                #   渲染掛掉時改走修錯輪，餵 traceback 給 agent。
                if not LAST_RENDER_ERROR:
                    print("[revise] 沒有 render 也沒有錯誤訊息 — 停止")
                    break
                print(f"[r{n}] 上一輪渲染失敗 ⇒ 走修錯輪(以 traceback 為線索)")
                written, plan_text = run_repair_round(n, prompt, factory, code_dir,
                                                      LAST_RENDER_ERROR)
            else:
                written, plan_text = run_revision_round(n, prompt, factory, code_dir,
                                                        last_render, target_image, hint=hint,
                                                        colour=colour_note)

        overlay = build_overlay(out_dir, written)
        rdir = _snapshot_round(n, plan_text)
        rpng = rdir / "render.png"
        if not _render(overlay, factory_fqn, rpng, seed):
            print(f"[r{n}] render FAILED ⇒ 下一輪走★修錯輪★"
                  f"({len(LAST_RENDER_ERROR.splitlines())} 行錯誤訊息會餵給 agent)")
            continue
        print(f"[r{n}] render -> {rpng}")
        last_render = rpng
        pct, note = run_verifier(rpng, prompt, target_image)
        (rdir / "verify.md").write_text(f"match_pct: {pct}\nmissing: {note}\n")
        print(f"[r{n}] verifier: {pct}% — {note}")
        if pct >= match_threshold:
            print(f"[r{n}] >= {match_threshold}% — stopping early")
            break
        hint = note

    best = run_selector(out_dir, n_rounds, prompt, target_image)
    best_render = (out_dir / f"r{best}" / "render.png") if best else last_render
    if best_render and best_render.exists():
        shutil.copy2(best_render, out_dir / "final.png")

    # 360 turntable GIF of the BEST round: rebuild the overlay from that round's code
    # snapshot (out_dir/r{best}/code), then orbit-render the real factory.
    gif = None
    if turntable and best:
        best_code = out_dir / f"r{best}" / "code"
        if best_code.is_dir():
            written_best = sorted(str(p.relative_to(best_code)) for p in best_code.rglob("*.py"))
            try:
                overlay = build_overlay(out_dir, written_best, src_subdir=f"r{best}/code")
                if render_turntable_gif(overlay, factory_fqn, out_dir, seed,
                                        n_frames=turntable_frames_per_view,
                                        views=turntable_views):
                    if turntable_views == "level":
                        print(f"[{name}] turntable stills -> "
                              f"{turntable_frames_per_view} level views")
                    else:
                        gif = str(out_dir / "final_360.gif")
                        print(f"[{name}] turntable GIF -> {gif}")
            except Exception as e:
                print(f"[{name}] turntable failed: {e}")

    # ★階段 C 交接 gate(2026-07-30)★ —— 用 BEST round 的 code 重建 overlay 後才驗，
    #   因為送進 placer 的就是這一份。turntable 已經建過同一份就沿用，省一次 cp -rs。
    best_code = out_dir / f"r{best}" / "code" if best else None
    overlay = None                      # ⚠️ 迴圈裡的 overlay 可能未綁定(所有 round 都失敗)
    if best_code and best_code.is_dir():
        try:
            # ★一定要用 best round 的 code 重建★ —— 迴圈變數指向的是「最後一輪」，
            #   而送進 placer 的是「最好的一輪」，兩者常常不同。
            written_best = sorted(str(p.relative_to(best_code))
                                  for p in best_code.rglob("*.py"))
            overlay = build_overlay(out_dir, written_best, src_subdir=f"r{best}/code")
            gate = run_stage_c_gates(overlay, out_dir, factory, role, entry_rel,
                                     factory_fqn, note=prompt.strip().splitlines()[0][:120])
        except Exception as e:                      # gate 失敗不該吃掉已經生好的結果
            print(f"[gate] ⚠️ gate 執行失敗: {type(e).__name__}: {e}", flush=True)
            gate = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # ★diversity★ —— gate 只驗 overlay 結構，完全不看「換個 seed 會不會變」
        try:
            if gate.get("ok") and not no_seed_check:
                print(f"\n=== [{name}] seed diversity (i=0..{SEED_CHECK_N - 1}) ===", flush=True)
                seed_check = check_seed_diversity(overlay, factory_fqn, out_dir,
                                                  colour_expect=color_variation)
                mark = "✅" if seed_check.get("ok") else "⚠️"
                print(f"[{name}] {mark} changed_pixel_frac: "
                      f"{seed_check.get('changed_pixel_frac')} {seed_check.get('note', '')}",
                      flush=True)
                (out_dir / "seed_diversity.json").write_text(
                    json.dumps(seed_check, ensure_ascii=False, indent=2))
                strip = build_seed_strip(out_dir / "seeds",
                                         out_dir / f"strip_{name}_1x{SEED_CHECK_N}.png")
                if strip:
                    print(f"[{name}] seed strip: {strip}", flush=True)
        except Exception as e:
            print(f"[seed] ⚠️ diversity 檢查失敗: {type(e).__name__}: {e}", flush=True)
            seed_check = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        gate = {"ok": False, "error": "沒有 best round 的 code，overlay 沒建起來"}
        print(f"[gate] ⚠️ {gate['error']}", flush=True)

    result = {"name": name, "factory": factory, "seed_diversity": seed_check, "best_round": f"r{best}" if best else None,
              "final": str(out_dir / "final.png") if best_render and best_render.exists() else None,
              "last_render": str(last_render) if last_render else None,
              "turntable_gif": gif,
              "turntable_frames_per_view": turntable_frames_per_view,
              "turntable_views": turntable_views,
              "turntable_expected_total_frames": (
                  turntable_frames_per_view * (1 if turntable_views == "level" else 2)),
              # 用了哪張 ref(自動生的或 --target 給的)；None = 純文字模式
              "target_image": str(target_image) if target_image else None,
              # ★placer 要的兩樣東西 + 它們是否驗證通過★
              "overlay": str(overlay) if overlay else None,
              "factory_fqn": factory_fqn,
              "factory_selection": factory_selection,
              "stage_c_ready": bool(gate.get("ok")),
              "stage_c_gate": gate}
    print(f"\n[{name}] done -> {out_dir}"
          f"  {'✅ 可送進 placer' if gate.get('ok') else '⛔ 尚未通過階段 C gate'}")

    # ★自動入庫★：夠好的資產自己進擴充庫，依 Factory 分類。
    #   hook 放在★這裡★而不是 3_generator.py，是因為單獨跑的批次
    #   (experiments/assets/run_procedural_code_edits_0818.py) 直接 subprocess
    #   叫本檔、繞過 orchestrator ⇒ 放 orchestrator 只涵蓋得到 e2e 那一條。
    result["library"] = _auto_ingest_library(out_dir)
    return result


def _auto_ingest_library(out_dir: Path) -> dict:
    """把生成產物交給資產庫。★任何失敗都只記 log★，不影響生成結果。"""
    try:
        import sys as _sys
        beyond = Path(__file__).resolve().parents[2]
        if str(beyond) not in _sys.path:
            _sys.path.insert(0, str(beyond))
        from asset_generator.library import librarian as lb
        return lb.auto_ingest(out_dir)
    except Exception as ex:                                 # noqa: BLE001
        print(f"[library] ⚠️ 自動入庫無法啟動(不影響生成結果)："
              f"{type(ex).__name__}: {ex}")
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}


# ------------------------------------------------------------------------------ standalone
def _load_from_director(scene: str, obj_id: str):
    manifest_path = BI_ROOT / "output" / scene / "2_director" / "generation_list.json"
    if not manifest_path.is_file():
        p = Path(scene)
        manifest_path = p if p.is_file() else p / "2_director" / "generation_list.json"
    if not manifest_path.is_file():
        raise SystemExit(f"generation_list.json not found for scene '{scene}'")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next((o for o in manifest.get("objects", []) if o["id"] == obj_id), None)
    if entry is None:
        ids = ", ".join(o["id"] for o in manifest.get("objects", []))
        raise SystemExit(f"object id '{obj_id}' not in manifest (have: {ids})")
    if entry.get("generator") != "ProceduralGenerator":
        print(f"[warn] '{obj_id}' generator is {entry.get('generator')}, not ProceduralGenerator")
    scene_root = manifest_path.parent.parent
    target = None
    if entry.get("refs"):                                   # single-view target ref
        cand = scene_root / entry["refs"]["single"]
        target = cand if cand.is_file() else None
    factory = entry.get("factory")
    if factory is not None:
        factory = str(factory).strip() or None
    return (entry.get("instance_prompt") or entry.get("prompt")
            or entry.get("name") or obj_id, factory,
            target, asset_store_dir("ProceduralGenerator", obj_id, "run"),
            str(entry.get("color_variation") or "fixed"), str(entry.get("color_reason") or ""))


def main():
    ap = argparse.ArgumentParser(description="Procedural generator: rewrite an Infinigen factory to match a request")
    ap.add_argument("--scene", help="scene name (or path) to load the object from Agent 2's manifest")
    ap.add_argument("--id", help="object id within that scene's manifest")
    ap.add_argument("--factory", help="Infinigen starter factory name, or 'auto' to pick the closest one")
    ap.add_argument("--prompt", help="object text description (when not using --scene/--id)")
    ap.add_argument("--name", help="object slug (default: --id)")
    ap.add_argument("--target", help="path to the single-view target image")
    ap.add_argument("--out-dir", help="output directory")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--depth", type=int, default=2, help="import-follow depth (fresh-build only)")
    ap.add_argument("--match-threshold", type=int, default=DEFAULT_MATCH_THRESHOLD)
    ap.add_argument("--no-turntable", action="store_true", help="skip the 360 turntable GIF")
    ap.add_argument("--turntable-frames-per-view", type=int, default=36,
                    help="frames for each of level/top-down views (default 36 = 72 total)")
    ap.add_argument("--turntable-views", choices=("level", "both"), default="both",
                    help="level keeps still PNGs only; both also renders top-down GIF")
    ap.add_argument("--no-seed-check", action="store_true",
                    help=f"skip the {SEED_CHECK_N}-seed diversity render "
                         "(Blender time only, no API cost)")
    ap.add_argument("--no-plan", action="store_true",
                    help="A4 ablation：跳過 planner，coder 直接從 request 出發")
    ap.add_argument("--no-ref", action="store_true",
                    help="不要自動生 reference 圖(預設會生；--target 已給時本來就不生)")
    ap.add_argument("--require-reference", action="store_true",
                    help="沒有 reference 圖就直接失敗，不要退回純文字模式"
                         "(paper 批次用：確保每一筆都是 ref-guided)")
    ap.add_argument("--seed-color", choices=("auto", "on", "off"), default="auto",
                    help="每個 seed 是否換顏色：on=per_seed、off=fixed、"
                         "auto=不提(走 --scene/--id 時由 Director 的 color_variation 決定)")
    ap.add_argument("--base-overlay", default=None,
                    help="ADAPT：從這棵已 materialize 的 overlay 出發改，而不是官方 infinigen")
    args = ap.parse_args()
    if args.turntable_frames_per_view < 1:
        ap.error("--turntable-frames-per-view must be >= 1")

    if args.base_overlay:
        global BASE_OVERLAY
        BASE_OVERLAY = Path(args.base_overlay).resolve()
        if not (BASE_OVERLAY / "infinigen").is_dir():
            ap.error(f"--base-overlay 不是 overlay import root(缺 infinigen/): {BASE_OVERLAY}")
        n = len(_base_overlay_changed())
        print(f"[adapt] 基底 = {BASE_OVERLAY}（相對官方改寫 {n} 個檔）")

    if args.scene and args.id:
        (prompt, factory, target, out_dir,
         color_variation, color_reason) = _load_from_director(args.scene, args.id)
        name = args.name or args.id
        if args.factory is not None:
            factory = args.factory
        target = Path(args.target) if args.target else target
        out_dir = Path(args.out_dir) if args.out_dir else out_dir
    else:
        if not (args.prompt and args.out_dir):
            sys.exit("provide either --scene/--id, or --prompt/--out-dir [--factory]")
        name = args.name or (args.factory if args.factory and args.factory != "auto"
                             else "auto_factory")
        prompt, factory, out_dir = args.prompt, args.factory, Path(args.out_dir)
        target = Path(args.target) if args.target else None
        color_variation, color_reason = "auto", ""

    if args.seed_color != "auto":                 # 明確指定就蓋過 Director
        color_variation = "per_seed" if args.seed_color == "on" else "fixed"
        color_reason = "--seed-color " + args.seed_color

    res = generate_procedural(name, prompt, factory, out_dir, target_image=target,
                              rounds=args.rounds, seed=args.seed, depth=args.depth,
                              match_threshold=args.match_threshold,
                              turntable=not args.no_turntable,
                              auto_ref=not args.no_ref,
                              require_reference=args.require_reference,
                              no_seed_check=args.no_seed_check,
                              turntable_frames_per_view=args.turntable_frames_per_view,
                              turntable_views=args.turntable_views,
                              color_variation=color_variation,
                              color_reason=color_reason,
                              no_plan=args.no_plan)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

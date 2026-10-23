"""把一句 input 摘要成專案名稱(slug)。

    「一棵樹幹彎曲、葉子會發藍光的樹」 -> silver_tree

流程的第一步：之後每個 stage 都用這個 slug 當專案名(見 paths.py)。
`paths.slugify()` 只是機械截斷的 fallback(會生出 a_tree_with_curved 這種)，
這支才是規範上該用的那一步。

用法:
    python project_namer.py "一棵樹幹彎曲、葉子會發藍光的樹"
    python project_namer.py "..." --init      # 順便建立 output/<slug>/project.json

    from project_namer import name_project
    slug = name_project(prompt)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from paths import init_project, slugify  # noqa: E402

# 沿用 2_director.py / 3_*_generator.py 的模型常數，讓整條 pipeline 的
# agent 都跑同一個模型；要換模型時只有這幾處，不會有一支偷偷用別的。
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

SYSTEM = """\
You name projects. Given a description of a scene or an object, reply with a short
snake_case identifier that will be used as a directory name for everything this project
produces.

Rules:
- 1 to 3 English words joined by underscores, lowercase, ASCII letters and digits only.
- Name the SUBJECT, not the sentence: keep what makes this scene distinctive and drop
  articles, filler and generic modifiers. "a tree with a curved trunk and glowing blue
  leaves" -> `silver_tree`, not `a_tree_with_curved`.
- It has to survive being read on its own months later, next to a dozen sibling
  directories, so `scene_01` or `test` are useless — but so is a name so specific it
  stops matching once the scene is iterated on.
- Reply with the identifier ONLY. No quotes, no explanation, no trailing punctuation.
"""

_VALID = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def _client():
    """跟其他 agent 共用同一組 .env 搜尋順序 —— 搬家時只有一處要改。"""
    import anthropic
    from dotenv import dotenv_values

    env: dict = {}
    for f in (HERE / ".env",):
        if f.is_file():
            env.update({k: v for k, v in dotenv_values(f).items() if v})
    for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    key = env.get("CLAUDE_API_KEY")
    if not key:
        raise SystemExit("CLAUDE_API_KEY not found")
    base = (env.get("CLAUDE_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):                      # SDK 自己會補 /v1
        base = base[:-3]
    return anthropic.Anthropic(api_key=key, base_url=base or None)


def name_project(prompt: str, model: str = MODEL, max_words: int = 3) -> str:
    """一句 input -> slug。

    ★取名失敗不該擋住整條 pipeline★，所以任何錯誤(沒 key、API 掛掉、回了一坨
    不能當目錄名的東西)都退回 paths.slugify() 的機械截斷，並印出原因。
    """
    try:
        resp = _client().messages.create(
            model=model, max_tokens=32, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        slug = re.sub(r"[^a-z0-9_]", "", raw.lower().replace("-", "_").replace(" ", "_"))
        slug = re.sub(r"_+", "_", slug).strip("_")
        if _VALID.match(slug) and len(slug.split("_")) <= max_words:
            return slug
        print(f"[namer] ⚠️ 回覆不能當目錄名({raw[:60]!r}) ⇒ 退回機械截斷", flush=True)
    except Exception as ex:
        print(f"[namer] ⚠️ {type(ex).__name__}: {str(ex)[:120]} ⇒ 退回機械截斷", flush=True)
    return slugify(prompt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", help="一句話的場景/物體描述")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--init", action="store_true",
                    help="順便建立 output/<slug>/ 並寫 project.json(記下原句)")
    a = ap.parse_args()
    slug = name_project(a.prompt, model=a.model)
    print(slug)
    if a.init:
        print(f"[namer] {init_project(slug, a.prompt)}")


if __name__ == "__main__":
    main()

"""輸出路徑的唯一來源 —— 專案優先。

一句 input 先被摘要成一個 slug(例:「彎曲樹幹、葉子發藍光的樹」-> `silver_tree`)，
之後每一個 stage 都用同一個 slug 當專案名，該專案需要的所有東西都收在它底下：

    output/<project>/
        project.json          原句、slug、建立時間
        1_director/
        2_scene_picker/
        3_asset_generator/<object>/{procedural,blender}/   (抽樣工廠直接放 <object>/)
        4_composer/

沒有前段流程、我們自己指定要生什麼的測試(例:「就是想看它生書架」)不屬於任何專案，
一律放 `output/_test/` 底下，免得跟真的專案產物混在一起：

    output/_test/
        3_asset_generator/<object>/...
        4_composer/<case>/
        ablation/                比較實驗

用法:
    from paths import project_dir, stage_dir, asset_dir, TEST_ROOT
    stage_dir("silver_tree", 1)                  -> output/silver_tree/1_director
    asset_dir("silver_tree", "bookshelf", "blender")
                                                 -> output/silver_tree/3_asset_generator/bookshelf/blender
    asset_dir(None, "bookshelf", "blender")      -> output/_test/3_asset_generator/bookshelf/blender
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root (this file lives in core/)
OUTPUT_ROOT = ROOT / "output"
TEST_ROOT = OUTPUT_ROOT / "_test"
ASSET_ROOT = OUTPUT_ROOT / "asset"

STAGE_DIR = {
    1: "1_director",
    2: "2_scene_picker",
    3: "3_asset_generator",
    4: "4_composer",
}

# 抽樣工廠沒有程式碼產物(只是用不同 seed 抽官方 factory)，所以不另開一層。
GENERATOR_SUBDIR = {
    "blender": "blender",
    "procedural": "procedural",
    "sampler": "",
}

ASSET_KIND = {
    "BlenderGenerator": "blender",
    "ProceduralGenerator": "procedural",
    "3_blender": "blender",
    "3_procedural": "procedural",
    "blender": "blender",
    "procedural": "procedural",
}


def slugify(text: str, max_words: int = 4) -> str:
    """把一段描述壓成檔名安全的 slug。★這只是 fallback★。

    正式流程應該由一個 agent 讀完整句 input 給出有意義的短名(silver_tree)；
    這裡只做機械化的截斷，用在沒有 agent 的場合(手動測試)。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    words = [w for w in re.split(r"[^0-9a-z]+", text) if w]
    return "_".join(words[:max_words]) or "untitled"


def project_dir(project: str | None) -> Path:
    """專案根目錄；`project` 為 None 代表「沒有前段流程的測試」。"""
    return TEST_ROOT if project is None else OUTPUT_ROOT / project


def stage_dir(project: str | None, stage: int | str) -> Path:
    name = STAGE_DIR.get(stage, stage) if not isinstance(stage, str) else stage
    return project_dir(project) / name


def asset_dir(project: str | None, obj: str, generator: str = "blender") -> Path:
    """單一物件在某個 generator 底下的產物目錄。"""
    sub = GENERATOR_SUBDIR.get(generator, generator)
    base = stage_dir(project, 3) / obj
    return base / sub if sub else base


def asset_store_kind(generator_or_kind: str | None) -> str:
    """Normalize a pipeline generator name to output/asset/{kind}."""
    if not generator_or_kind:
        return "unknown"
    return ASSET_KIND.get(generator_or_kind, str(generator_or_kind).lower())


def asset_store_dir(
    generator_or_kind: str | None,
    obj: str,
    slot: str | None = "run",
) -> Path:
    """Central asset store path: output/asset/{procedural|blender}/{object}/{slot}."""
    root = ASSET_ROOT / asset_store_kind(generator_or_kind) / obj
    return root if not slot else root / slot


def init_project(project: str, source_input: str) -> Path:
    """建立專案目錄並記下它是從哪一句話來的。

    ★這份紀錄很重要★：slug 是被摘要過的，看到 `silver_tree` 沒辦法反推原本那句
    「彎曲的樹幹、發藍光的葉子……」，事後想重跑或比對就沒有依據。
    """
    d = project_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "project.json"
    if not meta.exists():
        meta.write_text(json.dumps(
            {"project": project, "input": source_input,
             "created": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return d

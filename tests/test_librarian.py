"""Regression for the expanded per-Factory asset library.

★這裡守的是三件會安靜壞掉的事★
  1. 不夠好的資產不准入庫(否則庫會被垃圾污染，而且是安靜的)
  2. materialize 出來的 overlay 必須★只有改寫檔是實體檔★，其餘全是指向當下基底的
     symlink —— 這正是 placer.verify_overlay_not_stale 的那把尺
  3. 基底更新後找不到改寫檔時要★明確報 stale★，不可以硬疊出一棵
     「看起來對、import 卻走官方版」的 overlay(placer.py 記的 0728 11-E 坑)
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_generator.library import librarian as lb  # noqa: E402
from pipeline import glue as glue_mod  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """把庫指到 tmp，絕不碰真的 asset_generator/library/。"""
    monkeypatch.setattr(lb, "LIB", tmp_path / "library")
    monkeypatch.setattr(lb, "INDEX", tmp_path / "library" / "index.json")
    monkeypatch.setattr(lb, "VARIANTS", tmp_path / "library" / "variants")
    monkeypatch.setattr(lb, "EXPAND_TXT", tmp_path / "library" / "Expand_Factory.txt")
    monkeypatch.setattr(lb, "ASSET_ROOT", tmp_path / "asset")
    monkeypatch.setattr(lb, "ASSET_MANIFESTS", tmp_path / "asset" / "_manifests")
    monkeypatch.setattr(lb, "GOOD_LIST_JSONL", tmp_path / "asset" / "_manifests" / "good_list.jsonl")
    monkeypatch.setattr(lb, "GOOD_LIST_TXT", tmp_path / "asset" / "_manifests" / "good_list.txt")
    return tmp_path / "library"


def _fake_base(tmp_path, monkeypatch):
    """一棵最小的假基底套件，取代真的 infinigen 樹。"""
    base = tmp_path / "base_infinigen"
    (base / "assets" / "objects").mkdir(parents=True)
    (base / "__init__.py").write_text("", encoding="utf-8")
    (base / "assets" / "objects" / "creatures.py").write_text(
        "class BirdFactory:\n    colour = 'brown'\n", encoding="utf-8")
    (base / "assets" / "objects" / "untouched.py").write_text(
        "SHARED = 1\n", encoding="utf-8")
    # ★patch 在 glue 上★ —— changed_files() 與 librarian.base_pkg() 共用它。
    monkeypatch.setattr(glue_mod, "BASE_PKG", base)
    return base


def _fake_run(tmp_path, base, *, pct=88, gate=True, colour="peacock-green",
              name="colorful_peacock"):
    """一份看起來像 3_procedural_generator.py 產物的 run 目錄。"""
    run = tmp_path / "run" / name
    overlay = run / "code" / "overlay"
    (overlay / "infinigen" / "assets" / "objects").mkdir(parents=True)
    # 未改寫的檔：與基底逐位元組相同 ⇒ changed_files() 必須忽略它
    (overlay / "infinigen" / "assets" / "objects" / "untouched.py").write_text(
        "SHARED = 1\n", encoding="utf-8")
    # 真正被改寫的檔
    (overlay / "infinigen" / "assets" / "objects" / "creatures.py").write_text(
        f"class BirdFactory:\n    colour = {colour!r}\n", encoding="utf-8")
    (overlay / "factory.txt").write_text(
        "factory: BirdFactory\nrole: ENTRY\n"
        "entry: assets/objects/creatures.py\n"
        "fqn: infinigen.assets.objects.creatures.BirdFactory\n"
        "anchoring: conform\n", encoding="utf-8")
    run.joinpath("request.txt").write_text(
        "a colorful peacock with full tail display", encoding="utf-8")
    run.joinpath("best.md").write_text("best_round: r1\n", encoding="utf-8")
    (run / "r1").mkdir()
    (run / "r1" / "verify.md").write_text(f"match_pct: {pct}\nmissing: none\n",
                                          encoding="utf-8")
    run.joinpath("stage_c_gate.json").write_text(
        json.dumps({"ok": gate}), encoding="utf-8")
    run.joinpath("final.png").write_bytes(b"\x89PNG fake")
    return run


def test_add_stores_only_the_rewritten_file(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base)

    r = lb.add(run, min_score=80, log=lambda *_: None)

    assert r["ok"], r["reason"]
    meta = r["meta"]
    # ★未改寫的 untouched.py 不可以被存進庫★ —— 存了就會在基底更新後 stale
    assert meta["entry_files"] == ["assets/objects/creatures.py"]
    assert meta["cross_file"] is False
    assert meta["factory"] == "BirdFactory"
    stored = lib / "variants" / "BirdFactory" / meta["variant_id"] / "changed"
    assert (stored / "assets" / "objects" / "creatures.py").is_file()
    assert not (stored / "assets" / "objects" / "untouched.py").exists()


def test_add_rejects_below_threshold_and_failed_gate(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)

    low = lb.add(_fake_run(tmp_path, base, pct=45), min_score=80, log=lambda *_: None)
    assert not low["ok"] and "45" in low["reason"]

    bad_gate = _fake_run(tmp_path, base, gate=False, name="gate_fail_peacock")
    r = lb.add(bad_gate, min_score=80, log=lambda *_: None)
    assert not r["ok"] and "stage_c_gate" in r["reason"]

    assert lb.load_index()["variants"] == {}


def test_materialize_rebuilds_a_mostly_symlinked_overlay(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    vid = lb.add(_fake_run(tmp_path, base), min_score=80,
                 log=lambda *_: None)["variant_id"]

    out = lb.materialize(vid, tmp_path / "mat", log=lambda *_: None)
    overlay = out["overlay"]

    rewritten = overlay / "infinigen" / "assets" / "objects" / "creatures.py"
    mirrored = overlay / "infinigen" / "assets" / "objects" / "untouched.py"
    # 改寫檔是實體檔，且內容就是庫裡那一份
    assert not rewritten.is_symlink()
    assert "peacock-green" in rewritten.read_text(encoding="utf-8")
    # 其餘一律是指向★當下基底★的 symlink ⇒ verify_overlay_not_stale 必然通過
    assert mirrored.is_symlink()
    assert mirrored.resolve() == (base / "assets" / "objects" / "untouched.py").resolve()
    assert (overlay / "factory.txt").is_file()


def test_materialize_refuses_when_base_no_longer_has_the_file(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    vid = lb.add(_fake_run(tmp_path, base), min_score=80,
                 log=lambda *_: None)["variant_id"]

    # 模擬 infinigen 基底改版：被改寫的檔搬走了
    (base / "assets" / "objects" / "creatures.py").unlink()

    with pytest.raises(RuntimeError, match="stale"):
        lb.materialize(vid, tmp_path / "mat2", log=lambda *_: None)


def test_lookup_ranks_the_matching_variant_first(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    lb.add(_fake_run(tmp_path, base), min_score=80,
           keywords=["peacock", "colorful", "bird"], log=lambda *_: None)

    hits = lb.lookup("a peacock with a colorful tail", factory="BirdFactory")
    assert hits and hits[0]["keyword_overlap"] >= 2

    assert lb.lookup("a peacock", factory="FishFactory") == []


def test_lookup_uses_good_list_before_legacy_index(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base, name="good_list_peacock")
    lb._write_good_list([{
        "variant_id": "good_list_peacock",
        "asset_id": "good_list_peacock",
        "kind": "procedural",
        "generator": "ProceduralGenerator",
        "factory": "BirdFactory",
        "fqn": "infinigen.assets.objects.creatures.BirdFactory",
        "request": "a colorful peacock with full tail display",
        "keywords": ["peacock", "colorful", "tail"],
        "match_pct": 91,
        "score": 91,
        "run_dir": str(run),
        "score_path": str(run / "score.json"),
        "overlay_root": str(run / "code" / "overlay"),
        "overlay_pkg": str(run / "code" / "overlay" / "infinigen"),
        "entry_files": ["assets/objects/creatures.py"],
        "new_files": [],
        "path": str(run),
    }])

    hits = lb.lookup("a peacock with a colorful tail", factory="BirdFactory")
    assert hits and hits[0]["variant_id"] == "good_list_peacock"


def test_materialize_uses_good_list_entries(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base, name="good_list_materialize")
    lb._write_good_list([{
        "variant_id": "good_list_materialize",
        "asset_id": "good_list_materialize",
        "kind": "procedural",
        "generator": "ProceduralGenerator",
        "factory": "BirdFactory",
        "fqn": "infinigen.assets.objects.creatures.BirdFactory",
        "request": "a colorful peacock with full tail display",
        "keywords": ["peacock", "colorful", "tail"],
        "match_pct": 91,
        "score": 91,
        "run_dir": str(run),
        "score_path": str(run / "score.json"),
        "overlay_root": str(run / "code" / "overlay"),
        "overlay_pkg": str(run / "code" / "overlay" / "infinigen"),
        "entry_files": ["assets/objects/creatures.py"],
        "new_files": [],
        "path": str(run),
    }])

    out = lb.materialize("good_list_materialize", tmp_path / "mat_good_list",
                         log=lambda *_: None)
    overlay = out["overlay"]
    rewritten = overlay / "infinigen" / "assets" / "objects" / "creatures.py"
    mirrored = overlay / "infinigen" / "assets" / "objects" / "untouched.py"
    assert not rewritten.is_symlink()
    assert "peacock-green" in rewritten.read_text(encoding="utf-8")
    assert mirrored.is_symlink()
    assert mirrored.resolve() == (base / "assets" / "objects" / "untouched.py").resolve()


def test_expand_factory_txt_is_generated_from_the_index(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    lb.add(_fake_run(tmp_path, base), min_score=80, log=lambda *_: None)

    text = lb.EXPAND_TXT.read_text(encoding="utf-8")
    assert "## BirdFactory" in text
    assert "唯讀視圖" in text

    # 手改會被下一次 reindex 覆蓋 —— 這是刻意的單一來源設計
    lb.EXPAND_TXT.write_text("人手改的內容\n", encoding="utf-8")
    lb.reindex()
    assert "## BirdFactory" in lb.EXPAND_TXT.read_text(encoding="utf-8")


# ─────────────────────────────────────────── 自動入庫(生成結束後自己判斷分數)
def test_auto_ingest_stores_when_the_score_clears_the_bar(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base)
    monkeypatch.setattr(lb, "score_run",
                        lambda rd, **kw: {"ok": True, "match_pct": 88})
    monkeypatch.setenv("BI_LIBRARY_MIN_SCORE", "60")

    r = lb.auto_ingest(run, log=lambda *_: None)

    assert r["ok"], r.get("reason")
    assert lb.load_index()["variants"][r["variant_id"]]["factory"] == "BirdFactory"


def test_auto_ingest_respects_the_threshold_and_the_off_switch(lib, tmp_path, monkeypatch):
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base, pct=30)
    monkeypatch.setattr(lb, "score_run",
                        lambda rd, **kw: {"ok": True, "match_pct": 30})
    monkeypatch.setenv("BI_LIBRARY_MIN_SCORE", "60")
    assert not lb.auto_ingest(run, log=lambda *_: None)["ok"]

    monkeypatch.setenv("BI_LIBRARY_AUTOADD", "0")
    monkeypatch.setattr(lb, "score_run",
                        lambda rd, **kw: {"ok": True, "match_pct": 99})
    r = lb.auto_ingest(run, log=lambda *_: None)
    assert not r["ok"] and r["reason"] == "disabled"
    assert lb.load_index()["variants"] == {}


def test_auto_ingest_never_raises_into_the_generator(lib, tmp_path, monkeypatch):
    """★生成花了半小時★ —— 庫壞掉不可以讓它回報成失敗。"""
    base = _fake_base(tmp_path, monkeypatch)
    run = _fake_run(tmp_path, base)

    def boom(*a, **k):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(lb, "score_run", boom)
    r = lb.auto_ingest(run, log=lambda *_: None)
    assert not r["ok"] and "scorer exploded" in r["reason"]


def test_auto_ingest_does_not_overwrite_an_earlier_variant(lib, tmp_path, monkeypatch):
    """同名再跑一次要並存 —— 舊的可能才是好的那一個。"""
    base = _fake_base(tmp_path, monkeypatch)
    monkeypatch.setattr(lb, "score_run", lambda rd, **kw: {"ok": True, "match_pct": 88})
    monkeypatch.setenv("BI_LIBRARY_MIN_SCORE", "60")

    # 兩次生成各自有 run 目錄，但 slug 相同 —— 這正是 variant_id 會撞的情境
    first = lb.auto_ingest(_fake_run(tmp_path, base, name="peacock_run"),
                           log=lambda *_: None)
    second_run = _fake_run(tmp_path, base, colour="teal", name="second/peacock_run")
    second = lb.auto_ingest(second_run, log=lambda *_: None)

    assert first["ok"] and second["ok"]
    assert first["variant_id"] != second["variant_id"]
    assert len(lb.load_index()["variants"]) == 2


def test_lookup_matches_chinese_and_multi_word_keywords(lib, tmp_path, monkeypatch):
    """★中文查詢曾經完全失效★：`[一-鿿]{2,}` 是貪婪的，「一隻孔雀」變成一個
    token，永遠對不上入庫時存的「孔雀」。片語 keyword 也一樣要能被拆開比對。"""
    base = _fake_base(tmp_path, monkeypatch)
    lb.add(_fake_run(tmp_path, base), min_score=80,
           keywords=["孔雀", "peacock", "tail display"], log=lambda *_: None)

    assert lb.lookup("一隻孔雀", factory="BirdFactory")[0]["keyword_overlap"] >= 1
    assert lb.lookup("我想要一隻孔雀開屏", factory="BirdFactory")[0]["keyword_overlap"] >= 1
    # 片語 keyword：查詢只提到其中一個字也要算命中
    assert lb.lookup("a bird with a big tail", factory="BirdFactory")[0]["keyword_overlap"] >= 1

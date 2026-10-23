from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace, ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stage_exec"))

from pipeline import glue as GLUE  # noqa: E402
import placement_contract as plc  # noqa: E402
import overlay_imports  # noqa: E402


def _write_factory_overlay(
        root: Path, *, marker: str, use_gin: bool = False,
        base_pkg: Path | None = None) -> Path:
    pkg = root / "infinigen" / "assets" / "objects" / "creatures"
    pkg.mkdir(parents=True, exist_ok=True)
    for path in [
        root / "infinigen",
        root / "infinigen" / "assets",
        root / "infinigen" / "assets" / "objects",
        pkg,
    ]:
        (path / "__init__.py").write_text("", encoding="utf-8")
    if use_gin:
        (pkg / "carnivore.py").write_text(
            "import gin\n\n"
            "@gin.configurable\n"
            "class CarnivoreFactory:\n"
            f"    MARKER = {marker!r}\n",
            encoding="utf-8",
        )
    else:
        (pkg / "carnivore.py").write_text(
            f"class CarnivoreFactory:\n    MARKER = {marker!r}\n",
            encoding="utf-8",
        )
    (root / "factory.txt").write_text(
        "fqn: infinigen.assets.objects.creatures.carnivore.CarnivoreFactory\n"
        "factory: CarnivoreFactory\n"
        "entry: assets/objects/creatures/carnivore.py\n"
        "role: ENTRY\n",
        encoding="utf-8",
    )
    if base_pkg is not None:
        (root / "overlay_source.json").write_text(
            json.dumps({"base_pkg": str(base_pkg)}, indent=2) + "\n",
            encoding="utf-8",
        )
    return root


def _install_fake_gin(monkeypatch):
    state = {
        "enter_calls": 0,
        "clear_calls": 0,
        "interactive": False,
        "registered": set(),
    }
    gin = ModuleType("gin")

    def enter_interactive_mode():
        state["enter_calls"] += 1
        state["interactive"] = True

    def clear_config(*, clear_registered=False):
        state["clear_calls"] += 1
        _ = clear_registered

    def configurable(obj=None, **_kwargs):
        def decorate(target):
            key = f"{target.__module__}.{target.__name__}"
            if key in state["registered"] and not state["interactive"]:
                raise ValueError(
                    "A different configurable matching "
                    "'infinigen.core.util.math.FixedSeed' already exists.")
            state["registered"].add(key)
            state["interactive"] = False
            return target

        return decorate if obj is None else decorate(obj)

    gin.enter_interactive_mode = enter_interactive_mode
    gin.clear_config = clear_config
    gin.configurable = configurable
    monkeypatch.setitem(sys.modules, "gin", gin)
    return state


def test_load_overlay_factory_isolated_per_root(tmp_path):
    overlay_a = _write_factory_overlay(tmp_path / "brown_bear", marker="bear")
    overlay_b = _write_factory_overlay(tmp_path / "red_fox", marker="fox")

    cls_a, fqn_a = overlay_imports.load_overlay_factory(
        overlay_a, "CarnivoreFactory")
    cls_b, fqn_b = overlay_imports.load_overlay_factory(
        overlay_b, "CarnivoreFactory")

    assert cls_a.MARKER == "bear"
    assert cls_b.MARKER == "fox"
    assert cls_a is not cls_b
    assert fqn_a == fqn_b == (
        "infinigen.assets.objects.creatures.carnivore.CarnivoreFactory")


def test_load_overlay_factory_resets_gin_before_each_root(tmp_path, monkeypatch):
    state = _install_fake_gin(monkeypatch)
    overlay_a = _write_factory_overlay(
        tmp_path / "brown_bear", marker="bear", use_gin=True)
    overlay_b = _write_factory_overlay(
        tmp_path / "red_fox", marker="fox", use_gin=True)

    cls_a, _ = overlay_imports.load_overlay_factory(overlay_a, "CarnivoreFactory")
    cls_b, _ = overlay_imports.load_overlay_factory(overlay_b, "CarnivoreFactory")

    assert cls_a.MARKER == "bear"
    assert cls_b.MARKER == "fox"
    assert state["enter_calls"] == 2
    assert state["clear_calls"] == 2


def test_load_overlay_factory_prunes_previous_overlay_roots(tmp_path):
    overlay_a = _write_factory_overlay(tmp_path / "brown_bear", marker="bear")
    overlay_b = _write_factory_overlay(tmp_path / "red_fox", marker="fox")

    original_sys_path = list(sys.path)
    try:
        cls_a, _ = overlay_imports.load_overlay_factory(
            overlay_a, "CarnivoreFactory")
        cls_b, _ = overlay_imports.load_overlay_factory(
            overlay_b, "CarnivoreFactory")

        assert cls_a.MARKER == "bear"
        assert cls_b.MARKER == "fox"
        assert str(overlay_a) not in sys.path
        assert str(overlay_b) in sys.path[:2]
    finally:
        sys.path[:] = original_sys_path


def test_cmd_fillcode_routes_by_code_from(tmp_path):
    table_path = tmp_path / "overlay_table.json"
    manifest_path = tmp_path / "manifest.json"
    table_path.write_text(json.dumps({
        "brown_bear": {
            "overlay": "/overlays/brown_bear",
            "id": "brown_bear",
            "factory": "CarnivoreFactory",
        },
        "red_fox": {
            "overlay": "/overlays/red_fox",
            "id": "red_fox",
            "factory": "CarnivoreFactory",
        },
    }, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "explicit_code_routing": True,
        "placements": [
            {"factory": "CarnivoreFactory", "code_from": "red_fox", "code": None},
            {"factory": "CarnivoreFactory", "code_from": "brown_bear", "code": None},
            {"factory": "HerbivoreFactory", "code_from": "wild_boar_herd", "code": None},
        ],
    }, indent=2), encoding="utf-8")

    rc = GLUE.cmd_fillcode(SimpleNamespace(
        manifest=manifest_path,
        table=table_path,
    ))

    assert rc == 0
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [p["code"] for p in updated["placements"]] == [
        "/overlays/red_fox",
        "/overlays/brown_bear",
        None,
    ]


def test_cmd_overlays_stages_separate_roots_for_same_factory(tmp_path, monkeypatch):
    base_pkg = tmp_path / "base_infinigen"
    base_file = base_pkg / "infinigen" / "assets" / "objects" / "creatures" / "carnivore.py"
    base_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_text("class CarnivoreFactory:\n    MARKER = 'base'\n", encoding="utf-8")
    monkeypatch.setattr(GLUE, "BASE_PKG", base_pkg / "infinigen")

    bear_base = tmp_path / "bear_base_infinigen"
    fox_base = tmp_path / "fox_base_infinigen"
    (bear_base / "infinigen").mkdir(parents=True)
    (fox_base / "infinigen").mkdir(parents=True)

    bear_overlay = _write_factory_overlay(
        tmp_path / "bear_overlay", marker="bear", base_pkg=bear_base / "infinigen")
    fox_overlay = _write_factory_overlay(
        tmp_path / "fox_overlay", marker="fox", base_pkg=fox_base / "infinigen")

    genlist = tmp_path / "generation_list.json"
    genlist.write_text(json.dumps({
        "objects": [],
        "reused": [
            {
                "id": "brown_bear",
                "factory": "CarnivoreFactory",
                "route_factory": "CarnivoreFactory",
                "library_variant": "brown_bear",
            },
            {
                "id": "red_fox",
                "factory": "CarnivoreFactory",
                "route_factory": "CarnivoreFactory",
                "library_variant": "red_fox_v2",
            },
        ],
    }, indent=2), encoding="utf-8")

    def fake_materialize(variant_id: str, out_dir: Path, *, log=None):
        overlay = bear_overlay if variant_id == "brown_bear" else fox_overlay
        return {"overlay": overlay}

    monkeypatch.setattr(
        "asset_generator.library.librarian.materialize",
        fake_materialize,
    )

    out_dir = tmp_path / "overlay_merged"
    table_path = tmp_path / "overlay_table.json"
    rc = GLUE.cmd_overlays(SimpleNamespace(
        genlist=genlist,
        scene_root=tmp_path,
        out=out_dir,
        table=table_path,
        reuse_existing=False,
    ))

    assert rc == 0
    table = json.loads(table_path.read_text(encoding="utf-8"))
    assert set(table) == {"brown_bear", "red_fox"}
    assert table["brown_bear"]["base_pkg"] == str(bear_base / "infinigen")
    assert table["red_fox"]["base_pkg"] == str(fox_base / "infinigen")
    assert (out_dir / "brown_bear").is_symlink()
    assert (out_dir / "red_fox").is_symlink()
    assert (out_dir / "brown_bear").resolve() == bear_overlay.resolve()
    assert (out_dir / "red_fox").resolve() == fox_overlay.resolve()


def test_overlay_base_pkg_uses_recorded_table_over_sys_path(tmp_path, monkeypatch):
    wrong_base = tmp_path / "wrong_checkout"
    good_base = tmp_path / "good_checkout"
    for base, marker in ((wrong_base, "wrong"), (good_base, "good")):
        file = base / "infinigen" / "assets" / "objects" / "creatures" / "carnivore.py"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"MARKER = {marker!r}\n", encoding="utf-8")

    overlay_root = tmp_path / "overlay_merged" / "brown_bear"
    overlay_root.mkdir(parents=True)
    table_path = tmp_path / "overlay_table.json"
    table_path.write_text(json.dumps({
        "brown_bear": {
            "overlay": str(overlay_root),
            "staged_overlay": str(overlay_root.resolve()),
            "id": "brown_bear",
            "factory": "CarnivoreFactory",
            "base_pkg": str(good_base / "infinigen"),
        },
    }, indent=2), encoding="utf-8")

    monkeypatch.setattr(sys, "path", [
        str(wrong_base / "infinigen"),
        str(good_base / "infinigen"),
    ])

    assert plc.overlay_base_pkg(overlay_root) == good_base / "infinigen"

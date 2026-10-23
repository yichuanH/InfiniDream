import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pipeline_glue", ROOT / "pipeline" / "glue.py")
GLUE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GLUE)


def _round(path: Path, tag: str) -> Path:
    script = path / f"{tag}.py"
    script.write_text(f"# {tag}\n", encoding="utf-8")
    return script


def test_best_script_ignores_failed_round_log_before_authoritative_best(tmp_path):
    _round(tmp_path, "round_00")
    expected = _round(tmp_path, "round_01")
    tmp_path.joinpath("summary.md").write_text(
        "- round_00: script error -> AttributeError\n"
        "- round_00: repair attempt\n\n"
        "**Best (LLM judge): round_01**\n",
        encoding="utf-8",
    )

    assert GLUE._best_script(tmp_path) == expected


def test_best_script_preserves_selected_repair_tag(tmp_path):
    _round(tmp_path, "round_00")
    expected = _round(tmp_path, "round_00_fix01")
    tmp_path.joinpath("summary.md").write_text(
        "- round_00: script error\n"
        "- round_00_fix01: OK\n\n"
        "**Best: round_00_fix01**\n",
        encoding="utf-8",
    )

    assert GLUE._best_script(tmp_path) == expected


def test_genlist_routes_replace_to_existing_placeholder_factory(tmp_path):
    plan = tmp_path / "edit_plan.json"
    out = tmp_path / "generation_list.json"
    plan.write_text(json.dumps({
        "edits": [{
            "id": "replace_trees_palm",
            "name": "palm trees",
            "action": "replace",
            "target_factory": "TreeFactory",
            "factory": "PalmTreeFactory",
            "generator": "ProceduralGenerator",
            "feasible": True,
        }]
    }), encoding="utf-8")

    GLUE.cmd_genlist(SimpleNamespace(plan=plan, out=out))

    item = json.loads(out.read_text(encoding="utf-8"))["objects"][0]
    assert item["factory"] == "PalmTreeFactory"
    assert item["route_factory"] == "TreeFactory"


def test_factory_meta_for_route_writes_class_name_shim(tmp_path):
    ov = tmp_path / "overlay"
    (ov / "infinigen").mkdir(parents=True)
    (ov / "factory.txt").write_text(
        "fqn: infinigen.assets.objects.tropic_plants.palm_tree.PalmTreeFactory\n"
        "factory: PalmTreeFactory\n"
        "entry: assets/objects/tropic_plants/palm_tree.py\n"
        "role: ENTRY\n"
        "kind: hero\n",
        encoding="utf-8")

    routed = GLUE._factory_meta_for_route(
        ov, requested_id="replace_trees_palm",
        source_factory="PalmTreeFactory", route_factory="TreeFactory")

    assert "factory: TreeFactory" in routed
    assert "source_factory: PalmTreeFactory" in routed
    assert "fqn: infinigen.assets.objects.generated._bi_replace_trees_palm_treefactory_shim.TreeFactory" in routed
    shim = (ov / "infinigen" / "assets" / "objects" / "generated"
            / "_bi_replace_trees_palm_treefactory_shim.py")
    assert shim.is_file()
    assert "class TreeFactory(_SourceFactory):" in shim.read_text(encoding="utf-8")

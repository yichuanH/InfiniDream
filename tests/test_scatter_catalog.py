"""scatter_catalog 與 ground_scatter 路由的回歸測試。

鎖住 2026-09-07 接上 infinigen 原生 scatter 這條路的關鍵不變式：
  - catalog 指到的模組/類別/factory 符號在 infinigen 裡真的存在
  - domain tag 照抄 base_nature.gin，且預設排除畫面外地形
  - 選不到 key 的 tag 要能事前判出來(否則是撒 0 個的靜默失敗)
  - director 不再把 ground_scatter 的三個動作一律判死
"""
import argparse
import ast
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ⚠️ ★不要把 ROOT/"director" 也塞進 sys.path★ —— 那會讓 `director` 解析成
#    director/director.py 這個「模組」而不是 director/ 這個 namespace package，
#    於是同一輪 discover 裡其他測試的 `from director.director import ...` 全部
#    炸成「'director' is not a package」。走 namespace package 匯入即可。
import scatter_adapters as sa                      # noqa: E402
import scatter_catalog as sc                       # noqa: E402
from director import placement_classes as pc       # noqa: E402

INFINIGEN_SCATTERS = Path(
    Path(os.environ.get("INFINIGEN_ROOT", Path(__file__).resolve().parents[1] / "third_party" / "infinigen")) / "infinigen" / "assets" / "scatters")


class ScatterCatalogTest(unittest.TestCase):
    def test_every_entry_resolves_to_a_real_infinigen_module(self):
        """catalog 不能指到不存在的模組 —— 那會在 step 8 才 ImportError。"""
        if not INFINIGEN_SCATTERS.is_dir():
            self.skipTest("infinigen scatters directory not available")
        for name, entry in sc.SCATTERS.items():
            path = INFINIGEN_SCATTERS / f"{entry['module']}.py"
            self.assertTrue(path.is_file(), f"{name}: 缺 {path}")

    def test_class_and_factory_symbol_exist_in_that_module(self):
        """cls 要真的是個 class；factory_symbol 要真的是模組全域名。

        外觀替換是 setattr(module, factory_symbol, cls)，符號拼錯的話
        ★patch 會安靜地新增一個沒人用的屬性★，外觀替換靜默失效。
        """
        if not INFINIGEN_SCATTERS.is_dir():
            self.skipTest("infinigen scatters directory not available")
        for name, entry in sc.SCATTERS.items():
            tree = ast.parse((INFINIGEN_SCATTERS / f"{entry['module']}.py").read_text())
            classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
            self.assertIn(entry["cls"], classes, f"{name}: 類別 {entry['cls']} 不存在")
            symbol = entry["factory_symbol"]
            if symbol is None:
                continue
            imported = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            self.assertIn(symbol, imported,
                          f"{name}: factory 符號 {symbol} 不是該模組的全域名")

    def test_apply_accepts_selection(self):
        """撒在哪全靠 selection；沒有這個參數的系統不能進 catalog。"""
        if not INFINIGEN_SCATTERS.is_dir():
            self.skipTest("infinigen scatters directory not available")
        for name, entry in sc.SCATTERS.items():
            tree = ast.parse((INFINIGEN_SCATTERS / f"{entry['module']}.py").read_text())
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == entry["cls"]:
                    apply_fn = next(
                        (m for m in node.body
                         if isinstance(m, ast.FunctionDef) and m.name == "apply"), None)
                    self.assertIsNotNone(apply_fn, f"{name}: 沒有 apply()")
                    args = [a.arg for a in apply_fn.args.args]
                    self.assertIn("selection", args, f"{name}: apply() 不收 selection")

    def test_swappable_factories_take_a_single_seed(self):
        """★可換外觀的門檻不只是符號存在★，還要看它被怎麼呼叫。

        生成端的 overlay factory 一律是 (factory_seed, coarse=False)。若 scatter
        用 Factory(seed, season) 或 Factory(seed, grass=...) 建構，patch 上去會在
        step 8 才 TypeError。這條測試把「單一 seed 位置參數」鎖成入場條件。
        """
        if not INFINIGEN_SCATTERS.is_dir():
            self.skipTest("infinigen scatters directory not available")
        for name, entry in sc.SCATTERS.items():
            symbol = entry["factory_symbol"]
            if symbol is None:
                continue
            tree = ast.parse((INFINIGEN_SCATTERS / f"{entry['module']}.py").read_text())
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == symbol
            ]
            self.assertTrue(calls, f"{name}: 找不到 {symbol} 的建構呼叫")
            for call in calls:
                self.assertLessEqual(len(call.args), 1,
                                     f"{name}: {symbol} 用了額外的位置參數")
                self.assertEqual(len(call.keywords), 0,
                                 f"{name}: {symbol} 用了關鍵字參數")

    def test_known_incompatible_signatures_stay_official(self):
        """這四支實測簽章不相容，不能被誤加回可換外觀。"""
        for name in ("chopped_trees", "monocots", "pebbles", "ivy"):
            self.assertIsNone(sc.SCATTERS[name]["factory_symbol"],
                              f"{name} 的母本建構帶額外參數，不能換外觀")

    def test_snow_layer_stays_out(self):
        """snow_layer 是唯一沒有 selection 的系統，不能被加回來。"""
        self.assertNotIn("snow_layer", sc.SCATTERS)
        self.assertIn("snow_layer", sc.UNSUPPORTED)

    def test_domain_tags_match_infinigen_config(self):
        """★照抄 base_nature.gin★ —— 自己編 tag 會選到空集合。"""
        self.assertEqual(sc.DOMAIN_TAGS["land"],
                         "landscape,-liquid_covered,-cave,-beach")
        self.assertEqual(sc.DOMAIN_TAGS["nonliving"], "landscape,-cave")
        self.assertEqual(sc.DOMAIN_TAGS["underwater"],
                         "landscape,liquid_covered,-cave")

    def test_camera_visible_filter_is_on_by_default(self):
        self.assertTrue(sc.domain_tag("land").endswith(",-out_of_view"))
        self.assertNotIn("out_of_view",
                         sc.domain_tag("land", camera_visible_only=False))

    def test_tag_keys_filters_like_infinigen(self):
        """用真實 forest MaskTag 驗證：相機過濾要真的砍掉 out_of_view 那幾組。"""
        mask = ROOT / "scenes/forest/seed0/fine_cam/MaskTag.json"
        if not mask.is_file():
            self.skipTest("forest MaskTag.json not available")
        tag_dict = json.loads(mask.read_text())
        visible = sc.tag_keys(tag_dict, sc.domain_tag("land"))
        everything = sc.tag_keys(
            tag_dict, sc.domain_tag("land", camera_visible_only=False))
        self.assertTrue(visible)
        self.assertLess(len(visible), len(everything))
        self.assertFalse([k for k in visible if "out_of_view" in k])
        # forest 沒有水體 ⇒ underwater 選不到任何 key，執行層必須靠這個擋下來
        self.assertEqual(sc.tag_keys(tag_dict, sc.domain_tag("underwater")), [])

    def test_menu_is_filtered_by_scene_surfaces(self):
        forest = sc.systems_for_surfaces(["rock", "soil", "grass"])
        self.assertIn("flowers", forest)
        self.assertIn("grass", forest)
        self.assertNotIn("coral_reef", forest)
        reef = sc.systems_for_surfaces(["underwater_ground"])
        self.assertIn("coral_reef", reef)
        self.assertNotIn("flowers", reef)

    def test_flower_field_uses_the_scatter_master_factory(self):
        """花海母本是 FlowerPlantFactory，不是 hero 的 FlowerFactory。"""
        self.assertEqual(sc.get("flowers")["factory_symbol"], "FlowerPlantFactory")


class SemanticScatterIdentityTest(unittest.TestCase):
    def test_legacy_native_names_map_to_semantic_ids(self):
        cases = {
            "scatter:GrassTuftFactory(161001)": "grass",
            "scatter:FlowerPlantFactory(45250)": "flowers",
            "scatter:GenericTreeFactory(77320)": "ground_twigs",
            "scatter:chopped_tree": "chopped_trees",
            "scatter:coral.002": "coral_reef",
            "scatter:mollusk": "seashells",
        }
        for object_name, semantic_id in cases.items():
            self.assertEqual(
                sc.semantic_id_from_carrier_name(object_name), semantic_id
            )

    def test_substrings_do_not_match(self):
        self.assertIsNone(
            sc.semantic_id_from_carrier_name("scatter:not_really_grass")
        )
        self.assertIsNone(
            sc.semantic_id_from_carrier_name("scatter:mollusk_extra")
        )


class OfficialScatterAdapterTest(unittest.TestCase):
    def test_every_ground_scatter_has_an_adapter(self):
        for name in sc.systems_for_surfaces(None):
            description = sa.describe(name)
            passes = sa.passes_for(
                name,
                sc.domain_tag(sc.SCATTERS[name]["domain"]),
                "normal",
            )
            self.assertEqual(len(passes), description["pass_count"], name)

    def test_coral_runs_vertical_and_horizontal_official_passes(self):
        passes = sa.passes_for(
            "coral_reef", sc.domain_tag("underwater"), "normal"
        )
        self.assertEqual([row["name"] for row in passes],
                         ["vertical", "horizontal"])
        self.assertEqual(passes[0]["kwargs"]["density"], 2.5)
        self.assertEqual(passes[1]["kwargs"]["density"], 2.5)
        self.assertTrue(passes[1]["kwargs"]["horizontal"])
        self.assertEqual(passes[1]["kwargs"]["n"], 5)
        self.assertEqual(
            passes[1]["mask"]["normal_thresh_high"], 0.4
        )

    def test_monocots_runs_both_official_forms(self):
        passes = sa.passes_for(
            "monocots", sc.domain_tag("land"), "normal"
        )
        self.assertEqual([row["kwargs"]["grass"] for row in passes],
                         [True, False])
        self.assertEqual([row["call"] for row in passes],
                         ["class", "module"])

    def test_official_stage_density_baselines(self):
        self.assertEqual(sc.density_for("coral_reef", "normal"), 2.5)
        self.assertEqual(sc.density_for("mushroom", "normal"), 2.0)

    def test_official_nonliving_stage_tags(self):
        for name in ("ground_twigs", "chopped_trees"):
            passes = sa.passes_for(name, sc.domain_tag("land"), "normal")
            self.assertEqual(
                passes[0]["mask"]["tag"], sc.domain_tag("nonliving")
            )


class FillExtraCodeTest(unittest.TestCase):
    """extra_edits 的 code 解析 —— 0907 e2e 實測炸過的那一段。

    ★病因★：composer 自己造 slug（schema 就是這樣要求它的），它★不保證等於
    director 的 edit id★。實測 director 的 `sunflower_field` 被 composer 寫成
    `generated/yellow_sunflower_field`，名字比對落空 ⇒ 花海降級成官方外觀。
    HERO 那條線沒事，是因為它 join 的是 manifest 的 `code_from`（一路從 director
    帶下來的身分），從不依賴 composer 取的名字。
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "glue_under_test", ROOT / "pipeline" / "glue.py")
        self.glue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.glue)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _overlay(self, name):
        """建一個看起來像 generator 產物的 overlay 目錄(要有 factory.txt)。"""
        d = Path(self.tmp) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "factory.txt").write_text(
            "fqn: x.Y\nfactory: Y\n", encoding="utf-8")
        return str(d)

    def _run(self, extra_edits, table):
        plan = Path(self.tmp) / "plan.json"
        tbl = Path(self.tmp) / "table.json"
        plan.write_text(json.dumps({"hero_edits": [], "extra_edits": extra_edits}),
                        encoding="utf-8")
        tbl.write_text(json.dumps(table), encoding="utf-8")
        args = argparse.Namespace(plan=str(plan), table=str(tbl))
        self.glue.cmd_fillextracode(args)
        return json.loads(plan.read_text(encoding="utf-8"))["extra_edits"]

    def test_slug_match_resolves(self):
        overlay = self._overlay("sunflower_field")
        out = self._run(
            [{"op": "create", "target": "flowers", "code": "generated/sunflower_field"}],
            {"sunflower_field": {"id": "sunflower_field",
                                 "factory": "FlowerPlantFactory", "overlay": overlay}})
        self.assertEqual(out[0]["code"], overlay)

    def test_two_assets_on_one_factory_are_told_apart_by_slug_suffix(self):
        """0912 coral_reef：composer 造的 slug `pink_blue_coral_blossom_bush` 對不上
        任何 id，退回用 factory 比對；但 orange_sea_anemone(hero) 與
        coral_blossom_bush(scatter) ★都是 CoralFactory★ ⇒ 兩筆命中 ⇒ 以前直接放棄、
        默默改用官方珊瑚 —— 使用者看到的珊瑚「一直沒被換」就是這樣。
        slug 的尾碼就是資產 id，拿它決勝。"""
        bush = self._overlay("coral_blossom_bush")
        anemone = self._overlay("orange_sea_anemone")
        out = self._run(
            [{"op": "create", "target": "coral_reef",
              "code": "generated/pink_blue_coral_blossom_bush"}],
            {"orange_sea_anemone": {"id": "orange_sea_anemone",
                                    "factory": "CoralFactory", "overlay": anemone},
             "coral_blossom_bush": {"id": "coral_blossom_bush",
                                    "factory": "CoralFactory", "overlay": bush}})
        self.assertEqual(out[0]["code"], bush)
        self.assertEqual(out[0]["code_from"], "coral_blossom_bush")

    def test_ambiguous_factory_with_no_suffix_hint_still_falls_back(self):
        """兩筆命中、slug 也沒指向任何一筆 ⇒ 維持原本的保守行為(不亂猜)。"""
        a = self._overlay("coral_a")
        b = self._overlay("coral_b")
        out = self._run(
            [{"op": "create", "target": "coral_reef", "code": "generated/mystery"}],
            {"coral_a": {"id": "coral_a", "factory": "CoralFactory", "overlay": a},
             "coral_b": {"id": "coral_b", "factory": "CoralFactory", "overlay": b}})
        self.assertNotIn("code", out[0])
        self.assertEqual(out[0]["code_unresolved"], "generated/mystery")

    def test_composer_invented_slug_falls_back_to_factory(self):
        """★這就是 e2e 炸掉的那一格★：slug 對不上，必須用 factory 接住。"""
        overlay = self._overlay("sunflower_field")
        out = self._run(
            [{"op": "create", "target": "flowers",
              "code": "generated/yellow_sunflower_field"}],
            {"sunflower_field": {"id": "sunflower_field",
                                 "factory": "FlowerPlantFactory", "overlay": overlay}})
        self.assertEqual(out[0]["code"], overlay)
        self.assertEqual(out[0]["code_from"], "sunflower_field")

    def test_unmatched_drops_code_rather_than_keeping_a_broken_path(self):
        """留壞路徑會讓 load_overlay_factory 掛掉整個 step 8；降級才是對的。"""
        out = self._run(
            [{"op": "create", "target": "flowers", "code": "generated/nothing_matches"}],
            {"other": {"id": "other", "factory": "GrassTuftFactory",
                       "overlay": "/definitely/not/here"}})
        self.assertNotIn("code", out[0])
        self.assertEqual(out[0]["target"], "flowers")

    def test_ambiguous_factory_refuses_rather_than_guessing(self):
        """同 factory 有兩筆時不能亂配 —— 寧可用官方外觀。"""
        a, b = self._overlay("field_a"), self._overlay("field_b")
        out = self._run(
            [{"op": "create", "target": "flowers", "code": "generated/whatever"}],
            {"a": {"id": "a", "factory": "FlowerPlantFactory", "overlay": a},
             "b": {"id": "b", "factory": "FlowerPlantFactory", "overlay": b}})
        self.assertNotIn("code", out[0])

    def test_is_reentrant_after_a_failed_resolve(self):
        """★對不上之後必須還能重試★(0907 實測踩到)。

        第一版對不上時直接 pop("code")，slug 從 plan 裡永久消失；之後就算修好
        比對邏輯，從 step 8 續跑也會在開頭 continue 掉，外觀再也接不回來。
        """
        overlay = self._overlay("sunflower_field")
        # 第一輪：table 裡沒有對得上的東西 → 降級
        plan = Path(self.tmp) / "plan.json"
        tbl = Path(self.tmp) / "table.json"
        plan.write_text(json.dumps({"hero_edits": [], "extra_edits": [
            {"op": "create", "target": "flowers",
             "code": "generated/yellow_sunflower_field"}]}), encoding="utf-8")
        tbl.write_text(json.dumps({}), encoding="utf-8")
        self.glue.cmd_fillextracode(
            argparse.Namespace(plan=str(plan), table=str(tbl)))
        first = json.loads(plan.read_text(encoding="utf-8"))["extra_edits"][0]
        self.assertNotIn("code", first)
        self.assertEqual(first["code_unresolved"], "generated/yellow_sunflower_field")

        # 第二輪：資產這次在 table 裡了 → 必須接得回來
        tbl.write_text(json.dumps({"sunflower_field": {
            "id": "sunflower_field", "factory": "FlowerPlantFactory",
            "overlay": overlay}}), encoding="utf-8")
        self.glue.cmd_fillextracode(
            argparse.Namespace(plan=str(plan), table=str(tbl)))
        second = json.loads(plan.read_text(encoding="utf-8"))["extra_edits"][0]
        self.assertEqual(second["code"], overlay)
        self.assertNotIn("code_unresolved", second)

    def test_resolves_even_when_plan_never_had_code(self):
        """composer 沒寫 code、或前一輪被清掉 —— create/replace 仍應查得到資產。"""
        overlay = self._overlay("sunflower_field")
        out = self._run(
            [{"op": "create", "target": "flowers"}],
            {"sunflower_field": {"id": "sunflower_field",
                                 "factory": "FlowerPlantFactory", "overlay": overlay}})
        self.assertEqual(out[0]["code"], overlay)

    def test_absolute_path_is_left_alone(self):
        overlay = self._overlay("already_resolved")
        out = self._run([{"op": "create", "target": "flowers", "code": overlay}], {})
        self.assertEqual(out[0]["code"], overlay)


    def test_director_surface_policy_is_preserved(self):
        plan = Path(self.tmp) / "plan.json"
        table = Path(self.tmp) / "table.json"
        director = Path(self.tmp) / "director.json"
        plan.write_text(json.dumps({
            "hero_edits": [],
            "extra_edits": [{"op": "create", "target": "flowers"}],
        }), encoding="utf-8")
        table.write_text("{}", encoding="utf-8")
        placement_spec = {
            "surface_policy": {
                "required_surface_types": ["grass"],
            },
        }
        director.write_text(json.dumps({"edits": [{
            "id": "flower_patch",
            "scatter_system": "flowers",
            "scatter_density": "dense",
            "placement_spec": placement_spec,
        }]}), encoding="utf-8")
        self.glue.cmd_fillextracode(argparse.Namespace(
            plan=str(plan),
            table=str(table),
            director_plan=str(director),
        ))
        edit = json.loads(plan.read_text(encoding="utf-8"))["extra_edits"][0]
        self.assertEqual(edit["density_level"], "dense")
        self.assertEqual(edit["placement_spec"], placement_spec)
        self.assertEqual(edit["director_edit_id"], "flower_patch")


class GroundScatterRoutingTest(unittest.TestCase):
    def test_ground_scatter_actions_are_actionable(self):
        """0904 之前這三個一律 False，讓花海連 plan 都不成立。"""
        for action in ("add", "replace", "delete"):
            supported, why = pc.action_ok("ground_scatter", action)
            self.assertTrue(supported, f"ground_scatter {action} 應該可行: {why}")
            self.assertTrue(why)



class DensityLevels(unittest.TestCase):
    """密度語意分級：值必須來自各系統自己的值域，且 normal == 官方預設。"""

    def test_normal_equals_official_default(self):
        """normal ★就是官方預設★ —— 不選等於不改變行為。"""
        for name, entry in sc.SCATTERS.items():
            if entry["density"] is None:
                continue
            self.assertEqual(sc.density_for(name, "normal"), entry["density"],
                             f"{name}: normal 應該完全等於官方預設")

    def test_untunable_returns_none(self):
        """apply() 不收 density(或收了是死參數)的系統一律回 None ⇒ 呼叫端不傳。"""
        for name, entry in sc.SCATTERS.items():
            if entry["density"] is not None:
                continue
            for level in sc.DENSITY_LEVELS:
                self.assertIsNone(sc.density_for(name, level), f"{name}/{level}")

    def test_monotonic(self):
        """四個等級必須單調遞增，否則語意就反了。"""
        order = ["sparse", "normal", "dense", "very_dense"]
        for name in sc.SCATTERS:
            if not sc.density_is_tunable(name):
                continue
            vals = [sc.density_for(name, l) for l in order]
            nums = [v[1] if isinstance(v, tuple) else v for v in vals]
            self.assertEqual(nums, sorted(nums), f"{name}: {dict(zip(order, vals))}")

    def test_dead_density_params_are_not_tunable(self):
        """★pine_needle / seaweed 的 density 是死參數★(apply() 收下但沒用到)。

        對照官方原始碼：函式體裡完全沒有出現 density，覆蓋率寫死在 vol_density。
        標成可調會讓 agent 以為調得動，實際上是靜默無效。
        """
        for name in ("pine_needle", "seaweed"):
            self.assertFalse(sc.density_is_tunable(name),
                             f"{name} 的 density 是死參數，不可標成可調")

    def test_semantics_tagged_for_every_tunable(self):
        """可調的每一支都要標明 density 實際被怎麼用，否則倍率沒有意義。"""
        for name in sc.SCATTERS:
            if not sc.density_is_tunable(name):
                continue
            self.assertIn(sc.density_semantics(name),
                          ("per_area", "per_volume", "clipped"), name)

    def test_invalid_level_falls_back(self):
        for name in ("flowers", "coral_reef"):
            self.assertEqual(sc.density_for(name, "banana"),
                             sc.density_for(name, sc.DEFAULT_DENSITY_LEVEL))



class OfficialMaskParams(unittest.TestCase):
    """遮罩參數必須★逐支對得上官方 generate_nature.py★。

    我們只負責兩件事：讓東西出現在鏡頭前、以及提供高階語意選項。
    scale / select_thresh / normal_thresh / return_scalar / target 一律照官方。
    """

    OFFICIAL = {   # 逐支從 infinigen_examples/generate_nature.py 抄，uniform 取中點
        "flowers":       (0.01,  0.6,   None, True,  "inview"),
        "ferns":         (0.1,   0.6,   None, True,  "inview"),
        "grass":         (0.1,   0.375, None, True,  "inview"),
        "pebbles":       (0.15,  0.5,   0.7,  True,  "inview"),
        "chopped_trees": (0.15,  0.575, 0.7,  True,  "inview"),
        "monocots":      (0.2,   0.55,  None, False, "inview"),
        "coral_reef":    (0.15,  0.46,  None, False, "inview"),
        "seaweed":       (0.05,  0.5,   0.4,  False, "inview"),
        "urchin":        (0.05,  0.5,   None, False, "inview"),
        "jellyfish":     (0.05,  0.5,   None, False, "inview"),
        "ground_leaves": (0.1,   0.52,  0.7,  True,  "near"),
        "ground_twigs":  (0.15,  0.55,  0.7,  True,  "near"),
        "mushroom":      (0.1,   0.65,  None, True,  "near"),
        "seashells":     (0.05,  0.5,   None, True,  "near"),
        "pinecone":      (0.1,   0.63,  None, False, "near"),
        "pine_needle":   (0.125, 0.475, None, True,  "near"),
        "decorative_plants": (0.125, 0.575, None, True, "near"),
    }

    def test_matches_official(self):
        for name, (scale, thr, nt, rs, tgt) in self.OFFICIAL.items():
            got = sc.official_mask_params(name)
            self.assertEqual(got["mask_scale"], scale, f"{name} scale")
            self.assertEqual(got["select_thresh"], thr, f"{name} select_thresh")
            self.assertEqual(got["normal_thresh"], nt, f"{name} normal_thresh")
            self.assertEqual(bool(got["return_scalar"]), rs, f"{name} return_scalar")
            self.assertEqual(got["terrain_target"], tgt, f"{name} target")

    def test_normal_level_is_exactly_official(self):
        """★normal 必須完全等於官方值★ —— 不選等級 = 不改變行為。"""
        for name, (_s, thr, *_r) in self.OFFICIAL.items():
            self.assertAlmostEqual(sc.select_thresh_for(name, "normal"), thr,
                                   places=6, msg=name)
            self.assertAlmostEqual(sc.select_thresh_for(name, None), thr,
                                   places=6, msg=name)

    def test_levels_monotonic_and_clamped(self):
        order = ["sparse", "normal", "dense", "very_dense"]
        for name in self.OFFICIAL:
            vals = [sc.select_thresh_for(name, lv) for lv in order]
            self.assertEqual(vals, sorted(vals, reverse=True),
                             f"{name}: 門檻要隨密度遞減 {dict(zip(order, vals))}")
            for v in vals:
                self.assertGreaterEqual(v, sc.SELECT_THRESH_MIN)
                self.assertLessEqual(v, sc.SELECT_THRESH_MAX)

    def test_unknown_scatter_returns_none_not_a_guess(self):
        """catalog 沒記到官方值時要回 None(呼叫端不傳)，★不要瞎猜一個★。"""
        self.assertIsNone(sc.select_thresh_for("ivy", "very_dense"))
        self.assertIsNone(sc.official_mask_params("ivy")["mask_scale"])

    def test_grime_not_offered_as_ground_scatter(self):
        """★官方當 grime 的不該出現在 ground_scatter 選單裡★。

        infinigen 把 slime_mold/lichen/ivy/moss 貼在 populated 的資產表面，
        不是撒在地形上。留在選單裡等於邀請 director 選一個錯的用法。
        mushroom 兩邊都算(官方既有 run_stage 撒 terrain_near)，所以保留。
        """
        menu = sc.systems_for_surfaces(["rock", "soil", "grass"])
        for name in ("ivy", "moss", "lichen", "slime_mold"):
            self.assertNotIn(name, menu, name)
        self.assertIn("mushroom", menu)
        # 仍然查得到(舊 plan 要能被辨識並擋下，而不是 KeyError)
        self.assertIsNotNone(sc.get("ivy"))

    def test_official_grime_flagged(self):
        """官方把這幾支當 grime 貼在資產表面，不是撒在地形上。"""
        for name in ("ivy", "moss", "lichen", "slime_mold"):
            self.assertTrue(sc.is_official_grime(name), name)
        self.assertFalse(sc.is_official_grime("flowers"))


class CoverageContract(unittest.TestCase):
    """0909 量測：「撒得不明顯」的旋鈕是 coverage，不是 density。

    ferns 的官方有效密度已經是 ~20 株/m²，而 52 個相機可見 anchor 蓋住基底
    13.94%(≈260m²)卻只長出 2 株 ⇒ 瓶頸★完全在遮罩通過率★。所以自動補救
    只准動 select_thresh(官方自己的旋鈕)，而且每一階都要留下紀錄。
    """

    def test_ladder_starts_at_the_directors_level(self):
        """第 0 階必須★等於 director 選的等級★，安全網才是「疊在上面」。"""
        for name in ("ferns", "flowers", "grass", "pebbles"):
            for level in sorted(sc.DENSITY_LEVELS):
                ladder = sc.coverage_escalation_thresholds(name, level)
                self.assertEqual(ladder[0], sc.select_thresh_for(name, level),
                                 f"{name}/{level}")

    def test_ladder_is_monotonically_looser(self):
        """每一階只能放寬，否則「升級」會反而撒得更少。"""
        for name in sc.SCATTERS:
            ladder = sc.coverage_escalation_thresholds(name, "normal")
            self.assertEqual(ladder, sorted(ladder, reverse=True), name)
            for value in ladder:
                self.assertGreaterEqual(value, sc.SELECT_THRESH_MIN, name)

    def test_no_ladder_when_official_thresh_unknown(self):
        """catalog 沒記到官方 select_thresh 的就★不要自己編一個★。"""
        for name in ("mollusk", "ivy", "moss"):
            self.assertIsNone(sc.select_thresh_for(name, "normal"), name)
            self.assertEqual(sc.coverage_escalation_thresholds(name, "normal"),
                             [], name)

    def test_min_visible_is_a_real_threshold(self):
        """contract 曾經寫死 1 —— 那讓 ferns 的 2 株(effective=1)判成 PASS。"""
        for name in sc.SCATTERS:
            self.assertGreaterEqual(sc.min_effectively_visible(name), 5, name)

    def test_min_visible_scales_down_for_sparse(self):
        """sparse 是 director 從「a few scattered」選的 —— ★撒得少本來就對★。

        用同一個門檻去判會把正確行為判成失敗(contract 定義錯誤，不是 scatter
        有問題)。
        """
        for name in ("flowers", "grass", "mushroom", "jellyfish"):
            sparse = sc.min_effectively_visible(name, "sparse")
            normal = sc.min_effectively_visible(name, "normal")
            self.assertLess(sparse, normal, name)
            self.assertGreaterEqual(sparse, 1, name)

    def test_min_visible_never_scales_up(self):
        """★只往下縮不往上加★ —— 這是「有沒有撒出來」的地板，不是藝術目標。

        往上加會讓 0909 的 18 支與 0910 的 12 支(都跑 very_dense)重新變成
        失敗，平白製造要分流的雜訊。
        """
        for name in sc.SCATTERS:
            base = sc.min_effectively_visible(name, "normal")
            for level in ("dense", "very_dense"):
                self.assertLessEqual(
                    sc.min_effectively_visible(name, level), base,
                    f"{name}/{level}")

    def test_min_visible_default_level_matches_normal(self):
        """不給等級時要等於 normal，舊呼叫端行為不變。"""
        for name in sc.SCATTERS:
            self.assertEqual(sc.min_effectively_visible(name),
                             sc.min_effectively_visible(name, "normal"), name)

    def test_small_ground_cover_needs_more_than_single_plants(self):
        """小型地被要成片才讀得出來，門檻必須高於中型單株。"""
        for small in ("grass", "flowers", "ground_leaves", "pine_needle"):
            for medium in ("ferns", "mushroom", "monocots"):
                self.assertGreater(sc.min_effectively_visible(small),
                                   sc.min_effectively_visible(medium),
                                   f"{small} vs {medium}")


if __name__ == "__main__":
    unittest.main()

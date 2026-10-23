"""Director decides WHETHER an asset animates in place; the catalog decides HOW.

Two mechanisms live behind one field:
  constructor_mode  Factory takes animation_mode and builds its own rig
  gn_scene_time     no rig, but the Factory's geometry nodes animate from Scene Time
                    (generated variants ship with the amplitude zeroed)
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import local_animation as la  # noqa: E402


class LocalAnimationPolicyTest(unittest.TestCase):
    def test_catalog_documents_both_kinds(self):
        catalog = la.load_catalog()
        self.assertEqual(set(catalog["local_animation_kinds"]),
                         {"constructor_mode", "gn_scene_time"})
        self.assertEqual(
            catalog["factories"]["FishFactory"]["local_animation_kind"],
            "constructor_mode")
        self.assertEqual(
            catalog["factories"]["DragonflyFactory"]["local_animation_kind"],
            "gn_scene_time")

    def test_available_request_picks_the_mechanism_the_factory_has(self):
        self.assertEqual(la.available_request("FishFactory"),
                         {"policy": "inherit", "mode": "idle"})
        self.assertEqual(la.available_request("DragonflyFactory"),
                         {"policy": "gn_scene_time", "mode": None})
        # BirdFactory's modes are recorded as user_reported_failure
        self.assertEqual(la.available_request("BirdFactory"),
                         {"policy": "none", "mode": None})
        self.assertEqual(la.available_request("TreeFactory"),
                         {"policy": "none", "mode": None})

    def test_director_only_chooses_whether(self):
        for factory in ("FishFactory", "DragonflyFactory", "TreeFactory"):
            self.assertEqual(la.director_request(factory, enabled=False),
                             {"policy": "none", "mode": None})
        self.assertEqual(la.director_request("DragonflyFactory", enabled=True),
                         {"policy": "gn_scene_time", "mode": None})
        # asking for animation an asset cannot do yields none, never an error
        self.assertEqual(la.director_request("TreeFactory", enabled=True),
                         {"policy": "none", "mode": None})

    def test_gn_scene_time_needs_no_constructor_kwarg(self):
        request = {"policy": "gn_scene_time", "mode": None}
        self.assertEqual(
            la.normalize_request(request, factory="DragonflyFactory"), request)
        self.assertEqual(
            la.constructor_kwargs(request, factory="DragonflyFactory"), {})
        self.assertEqual(la.gn_scene_time_params("DragonflyFactory"),
                         {"frequency_hz": 1.0, "flap_magnitude": 0.22})

    def test_gn_scene_time_rejected_for_factories_without_the_driver(self):
        with self.assertRaisesRegex(ValueError, "Scene-Time"):
            la.normalize_request({"policy": "gn_scene_time"}, factory="TreeFactory")
        with self.assertRaises(ValueError):
            la.gn_scene_time_params("TreeFactory")

    def test_master_pool_kept_for_gn_scene_time_disabled_for_rigs(self):
        import master_pool
        self.assertEqual(master_pool.effective_pool_size(
            5, {"policy": "gn_scene_time", "mode": None}), 5)
        self.assertEqual(master_pool.effective_pool_size(
            5, {"policy": "inherit", "mode": "idle"}), 0)


class DirectorToExecutorTest(unittest.TestCase):
    """The choice must survive the whole plan chain, not just the catalog."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        sys.path.insert(0, str(ROOT / "director"))

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        cls.director = load("bi_director_anim_test", ROOT / "director/director.py")
        cls.composer = load("bi_composer_anim_test", ROOT / "planner/composer.py")
        cls.catalog = cls.director.load_catalog()
        shadowing = {(ROOT / "director").resolve(), (ROOT / "planner").resolve()}
        sys.path[:] = [p for p in sys.path
                       if Path(p or ".").resolve() not in shadowing]

    def finalize(self, **extra):
        raw = {"scene_reading": "meadow", "edits": [{
            "id": "bee", "name": "bee", "action": "add", "placement": "hero",
            "anchoring": "airborne", "factory": "DragonflyFactory",
            "has_feature": True, "is_organic": True, "spec": "a honey bee",
            "count": 4, "n_masters": 3,
            "world_motion": {"preset": "air_hover", "intensity": "subtle"},
            **extra}]}
        manifest = {"factories": [], "collections": [], "scatters": [], "swarms": [],
                    "raycast_surfaces": [], "available_surface_types": []}
        return self.director.finalize(raw, manifest, self.catalog, "meadow")["edits"][0]

    def test_director_choice_reaches_the_edit(self):
        self.assertEqual(self.finalize(animate_in_place=True)["local_animation"],
                         {"policy": "gn_scene_time", "mode": None})
        self.assertEqual(self.finalize(animate_in_place=False)["local_animation"],
                         {"policy": "none", "mode": None})
        # omitted -> inherit whatever the asset can do (previous behaviour)
        self.assertEqual(self.finalize()["local_animation"],
                         {"policy": "gn_scene_time", "mode": None})

    def test_composer_carries_it_into_the_hero_add(self):
        edit = self.finalize(animate_in_place=True)
        out = self.composer.normalize_execution_contract(
            {"edit_plan": {"hero_edits": [], "extra_edits": []}},
            {"edits": [edit]},
            {"collections": [], "scatters": [], "raycast_surfaces": [],
             "available_surface_types": []})
        adds = [e for e in out["edit_plan"]["hero_edits"] if e.get("op") == "add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["local_animation"],
                         {"policy": "gn_scene_time", "mode": None})


if __name__ == "__main__":
    unittest.main()

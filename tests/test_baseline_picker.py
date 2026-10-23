from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "baseline_picker", ROOT / "tools" / "scene_picker" / "pick_baseline.py")
BASELINE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BASELINE)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


class BaselinePickerTest(unittest.TestCase):
    def test_outdoor_seed_candidates_use_camera_manifest_selected_angle(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            scenes = Path(tmp) / "scenes"
            manifest = scenes / "CAMERA_MANIFEST.tsv"
            manifest.parent.mkdir(parents=True)
            rows = [
                "biome\tseed\tangle\taerial_m\tpitch_deg\tback_m\tside_m\tyaw_deg\t"
                "cam_x\tcam_y\tcam_z\tcam_match\tblend\tsize_gb\tstatus"
            ]
            for seed, angle in enumerate(["ground", "wide", "eye", "raised", "closeup"]):
                seed_dir = scenes / "forest" / f"seed{seed}"
                _touch(seed_dir / "coarse" / "scene.blend")
                _touch(seed_dir / "fine" / "scene.blend")
                _touch(seed_dir / "preview" / f"angle_{angle}.png")
                if angle != "ground":
                    _touch(seed_dir / "camera_choice.json")
                rows.append(
                    f"forest\t{seed}\t{angle}\t0\t0\t0\t0\t0\t0\t0\t0\tOK\t"
                    f"forest/seed{seed}/fine/scene.blend\t1.0\tOK"
                )
            manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")

            candidates = BASELINE.outdoor_seed_candidates(
                "forest", scenes_dir=scenes, manifest_path=manifest)

        self.assertEqual([c["seed"] for c in candidates], [0, 1, 2, 3, 4])
        self.assertEqual(candidates[1]["angle"], "wide")
        self.assertTrue(candidates[1]["preview_image"].endswith(
            "forest/seed1/preview/angle_wide.png"))
        self.assertTrue(candidates[1]["camera_choice"].endswith(
            "forest/seed1/camera_choice.json"))

    def test_indoor_seed_candidates_use_semantics_directory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            scenes_inside = Path(tmp) / "scenes_inside"
            for seed in range(5):
                seed_dir = scenes_inside / "Kitchen" / f"seed{seed}"
                _touch(seed_dir / "coarse" / "scene.blend")
                _touch(seed_dir / "preview" / "angle_corner.png")

            candidates = BASELINE.indoor_seed_candidates(
                "kitchen", scenes_inside_dir=scenes_inside)

        self.assertEqual([c["candidate_id"] for c in candidates], [
            "kitchen_seed0",
            "kitchen_seed1",
            "kitchen_seed2",
            "kitchen_seed3",
            "kitchen_seed4",
        ])
        self.assertTrue(all(c["room_type"] == "Kitchen" for c in candidates))
        self.assertTrue(candidates[0]["baseline_blend"].endswith(
            "Kitchen/seed0/coarse/scene.blend"))

    def test_validate_seed_pick_maps_invalid_id_back_by_seed(self):
        candidates = [
            {"candidate_id": "forest_seed0", "seed": 0},
            {"candidate_id": "forest_seed3", "seed": 3},
        ]
        got = BASELINE.validate_seed_pick({
            "candidate_id": "not_real",
            "seed": 3,
            "score": 88,
            "reason": "best open foreground",
            "visual_gap": "needs the requested additions",
            "ranked": [{"candidate_id": "not_real", "seed": 3, "score": 88}],
        }, candidates)

        self.assertEqual(got["candidate_id"], "forest_seed3")
        self.assertEqual(got["seed"], 3)
        self.assertEqual(got["score"], 88)

    def test_validate_seed_pick_maps_invalid_id_by_scene_and_seed(self):
        candidates = [
            {"candidate_id": "forest_seed3", "scene": "forest", "seed": 3},
            {"candidate_id": "mountain_seed3", "scene": "mountain", "seed": 3},
        ]
        got = BASELINE.validate_seed_pick({
            "candidate_id": "not_real",
            "scene": "mountain",
            "seed": 3,
            "score": 79,
            "reason": "mountain seed has the better open slope",
            "visual_gap": "needs animals",
        }, candidates)

        self.assertEqual(got["candidate_id"], "mountain_seed3")
        self.assertEqual(got["scene"], "mountain")

    def test_validate_seed_pick_does_not_use_ambiguous_seed_alone(self):
        candidates = [
            {"candidate_id": "forest_seed3", "scene": "forest", "seed": 3},
            {"candidate_id": "mountain_seed3", "scene": "mountain", "seed": 3},
        ]
        got = BASELINE.validate_seed_pick({
            "candidate_id": "not_real",
            "seed": 3,
            "score": 79,
        }, candidates)

        self.assertEqual(got["candidate_id"], "forest_seed3")
        self.assertIn("fallback_reason", got)

    def test_validate_seed_pick_falls_back_to_first_available_candidate(self):
        candidates = [
            {"candidate_id": "forest_seed0", "seed": 0},
            {"candidate_id": "forest_seed1", "seed": 1},
        ]
        got = BASELINE.validate_seed_pick(
            {"candidate_id": "missing", "seed": 99}, candidates)

        self.assertEqual(got["candidate_id"], "forest_seed0")
        self.assertIn("fallback_reason", got)

    def test_validate_scene_candidates_keeps_two_distinct_outdoor_scenes(self):
        got = BASELINE.validate_scene_candidates({
            "location": "outdoor",
            "ranked_scenes": [
                {
                    "scene": "forest",
                    "rank": 1,
                    "reason": "tree cover matches the request",
                    "match": 72,
                    "gap": "needs added animals",
                    "caveat": "none",
                },
                {
                    "scene": "mountain",
                    "rank": 2,
                    "reason": "some mountain seeds also contain trees",
                    "match": 65,
                    "gap": "less dense forest",
                    "caveat": "none",
                },
            ],
        })

        self.assertEqual([p["scene"] for p in got], ["forest", "mountain"])
        self.assertEqual([p["rank"] for p in got], [1, 2])

    def test_validate_scene_candidates_fills_duplicate_with_fallback(self):
        got = BASELINE.validate_scene_candidates({
            "location": "outdoor",
            "ranked_scenes": [
                {"scene": "forest", "rank": 1, "match": 80},
                {"scene": "forest", "rank": 2, "match": 70},
            ],
        })

        self.assertEqual(got[0]["scene"], "forest")
        self.assertEqual(len(got), 2)
        self.assertNotEqual(got[1]["scene"], "forest")

    def test_seed_candidates_for_scene_picks_fills_skipped_unrendered_group(self):
        original = BASELINE.seed_candidates

        def fake_seed_candidates(scene_pick):
            if scene_pick["scene"] == "utility":
                return []
            return [{
                "candidate_id": f"{scene_pick['scene']}_seed0",
                "location": scene_pick["location"],
                "scene": scene_pick["scene"],
                "seed": 0,
            }]

        try:
            BASELINE.seed_candidates = fake_seed_candidates
            scene_picks, candidates = BASELINE.seed_candidates_for_scene_picks([
                {"rank": 1, "location": "indoor", "scene": "bathroom", "match": 95},
                {"rank": 2, "location": "indoor", "scene": "utility", "match": 30},
            ])
        finally:
            BASELINE.seed_candidates = original

        self.assertEqual(len(scene_picks), 2)
        self.assertEqual(scene_picks[0]["scene"], "bathroom")
        self.assertNotEqual(scene_picks[1]["scene"], "utility")
        self.assertEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()

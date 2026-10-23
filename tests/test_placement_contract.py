from __future__ import annotations

from copy import deepcopy
import json
import math
import sys
import tempfile
import os
import unittest
import unittest.mock

import placement_region_contract as contract_region
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stage_exec"))

import placement_contract as contract
import placement_modes
import surface_registry
import camera_visibility as visibility
import world_motion
import local_animation
import placement_region_contract
from asset_generator.extraworld.instance_contract import (
    logical_instance_contract,
    single_instance_prompt,
)
from planner.placement_critic import (
    apply_candidate_request_bounds,
    compact_candidate_feedback,
    extract_json,
    normalize_decision_kind,
    validate_and_apply,
    validate_candidate_requests,
)
from planner.placement_candidate_critic import apply_candidate
from planner.composer import check as check_composer_contract
from planner.composer import normalize_execution_contract
from planner.composer_machine_reviser import (
    build_revision_history,
    collect_allowed_updates,
    oscillation_evidence,
    validate_revision,
)
from planner.composer_image_placement_selector import (
    apply_image_selection,
    image_selection_from_plan_screen_positions,
    normalize_image_selection,
    snap_selection_to_maps,
)
from stage_exec.prepare_placement_candidates import build_candidate_plan, candidate_points
from stage_exec.filter_placement_candidates import filter_candidates
from stage_exec.prepare_nonconverged_preview import prepare_diagnostic_plan
from stage_exec.check_world_motion_frames import frame_range_failures
from stage_exec.annotate_placement_region_map import active_selection_state
from placement_region_contract import (
    _boxes_overlap,
    _collision_half_extents,
    _collision_uses_3d,
    _map_cell_collision_position,
    aabb_overlap_fraction,
    all_map_managed_instances,
    all_conform_instances,
    apply_region_selection,
    automatic_region_assignments,
    conform_failed_instances,
    connected_feasible_regions,
    cross_edit_reserved_cells,
    estimate_region_capacity,
    grid_points,
    is_large_ground_proxy,
    is_map_managed_anchoring,
    is_outdoor_ultra_loose_map,
    is_soft_scene_obstacle,
    large_ground_relaxation_attempts,
    map_managed_failed_instances,
    placeholder_collection_claimed_by_plan,
    placement_population_intent,
    placement_should_materialize,
    requires_full_footprint_structure_support,
    rotated_aabb_bounds,
    validate_region_selection,
)


SUNFLOWER_FIELD_REGION_CELLS = [
    {
        "cell_id": f"C{col}_{row}",
        "world_position": [col * 0.5, row * 0.5, 0.0],
    }
    for row in range(5)
    for col in range(5)
]


class PlacementContractTest(unittest.TestCase):
    def test_blender_safe_placeholder_name_preserves_short_identity(self):
        self.assertEqual(
            contract.blender_safe_placeholder_name("FishFactory", 123, 4),
            "FishFactory(123).spawn_placeholder(4)",
        )

    def test_blender_safe_placeholder_name_compacts_long_factory(self):
        factory = "SmallEnclosedResearchSubmarineFactory"
        canonical = f"{factory}(56448163).spawn_placeholder(0)"
        compact = contract.blender_safe_placeholder_name(factory, 56448163, 0)
        self.assertGreater(len(canonical.encode("utf-8")), 63)
        self.assertLessEqual(len(compact.encode("utf-8")), 63)
        self.assertRegex(compact, r"^F[0-9a-f]{10}\(56448163\)\.spawn_placeholder\(0\)$")

    def test_asset_generator_prompt_owns_one_logical_instance(self):
        source = "Five seagulls at varied heights in a loose flock."
        prompt = single_instance_prompt(source, 5)
        self.assertIn("exactly ONE logical object", prompt)
        self.assertIn("requested total count (5)", prompt)
        self.assertIn(source, prompt)
        self.assertIn("Do not build a flock", prompt)
        self.assertEqual(logical_instance_contract(5), {
            "version": 1,
            "logical_instances_per_spawn": 1,
            "scene_instance_count": 5,
            "scene_count_owner": "composer_placer",
        })

    def test_asset_generator_instance_count_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "scene_count"):
            logical_instance_contract(0)

    def test_uniform_proxy_fit_requires_evaluated_bounds_to_change(self):
        before = [5.0, 10.0, 4.0]
        proxy = [0.5, 0.5, 0.5]
        factor = contract.uniform_proxy_fit_scale(before, proxy)
        expected = [value * factor for value in before]
        self.assertEqual(
            contract.uniform_proxy_fit_failures(before, expected, proxy, factor), [])
        smaller_than_expected = [value * 0.5 for value in expected]
        self.assertEqual(
            contract.uniform_proxy_fit_failures(
                before, smaller_than_expected, proxy, factor), [])
        failures = contract.uniform_proxy_fit_failures(
            before, before, proxy, factor)
        self.assertTrue(
            any("expected upper-bound" in failure for failure in failures))
        self.assertTrue(any("exceeds" in failure for failure in failures))

    def test_world_motion_smoke_frame_preflight(self):
        # minimum frame counts come from world_motion.MINIMUM_FRAME_COUNTS (one per preset)
        for preset in ("aquatic_swim", "ground_wander", "orbit"):
            self.assertEqual(world_motion.required_frame_count(preset),
                             world_motion.MINIMUM_FRAME_COUNTS[preset])
        with self.assertRaisesRegex(ValueError, "provides 1"):
            world_motion.validate_frame_range("aquatic_swim", 1, 1)
        need = world_motion.required_frame_count("ground_wander")
        world_motion.validate_frame_range("ground_wander", 1, need)

        plan = {"edits": [{
            "id": "fish",
            "feasible": True,
            "world_motion": {"preset": "aquatic_swim", "intensity": "subtle"},
        }]}
        self.assertTrue(frame_range_failures(plan, 1, 1))
        self.assertEqual(frame_range_failures(plan, 1, 5), [])

    def test_empty_placeholder_collection_uses_neutral_proxy(self):
        self.assertEqual(contract.placeholder_template_choice([0]), {
            "collection_index": 0,
            "use_neutral_proxy": True,
            "reason": "empty_existing_collection",
        })
        self.assertEqual(
            contract.placeholder_template_choice([0, 3])["collection_index"], 1)
        self.assertFalse(
            contract.placeholder_template_choice([0, 3])["use_neutral_proxy"])

    def test_overlay_membership_resolves_symlink_aliases(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            physical = tmp_path / "physical_overlay"
            module = physical / "infinigen" / "generated.py"
            module.parent.mkdir(parents=True)
            module.write_text("# generated overlay\n", encoding="utf-8")
            alias = tmp_path / "run_overlay"
            alias.symlink_to(physical, target_is_directory=True)

            self.assertTrue(contract.import_path_matches_overlay(
                alias / "infinigen" / "generated.py", physical, [alias]))
            self.assertTrue(contract.import_path_matches_overlay(
                module, physical, [alias]))
            self.assertFalse(contract.import_path_matches_overlay(
                tmp_path / "official" / "generated.py", physical, [alias]))

    def test_overlay_membership_keeps_internal_mirror_symlink_lexical(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime = tmp_path / "runtime"
            runtime.mkdir()
            runtime_module = runtime / "shared.py"
            runtime_module.write_text("# runtime mirror\n", encoding="utf-8")
            overlay = tmp_path / "overlay"
            overlay.mkdir()
            (overlay / "shared.py").symlink_to(runtime_module)

            self.assertTrue(contract.import_path_matches_overlay(
                overlay / "shared.py", overlay, [overlay]))
            self.assertFalse(contract.import_path_matches_overlay(
                runtime_module, overlay, [overlay]))

    def test_region_grid_uses_bottom_left_origin(self):
        cells = grid_points([0.2, 0.1, 0.6, 0.5], columns=2, rows=2)
        self.assertEqual(
            [(row["cell_id"], row["screen_position"]) for row in cells],
            [
                ("C00_00", [0.3, 0.2]),
                ("C01_00", [0.5, 0.2]),
                ("C00_01", [0.3, 0.4]),
                ("C01_01", [0.5, 0.4]),
            ],
        )

    def test_region_components_and_clearance_are_deterministic(self):
        cells = []
        for row in range(3):
            for column in range(3):
                cells.append({
                    "cell_id": f"C{column:02d}_{row:02d}",
                    "column": column,
                    "row": row,
                    "screen_position": [column / 10, row / 10],
                    "world_position": [float(column), float(row), 0.0],
                    "status": "feasible",
                })
        cells.append({
            "cell_id": "C09_09", "column": 9, "row": 9,
            "screen_position": [0.9, 0.9],
            "world_position": [9.0, 9.0, 0.0], "status": "feasible",
        })
        labeled, regions = connected_feasible_regions(cells)
        self.assertEqual(
            [(row["region_id"], row["cell_count"]) for row in regions],
            [("R01", 9), ("R02", 1)],
        )
        center = next(row for row in labeled if row["cell_id"] == "C01_01")
        corner = next(row for row in labeled if row["cell_id"] == "C00_00")
        self.assertEqual(center["clearance_steps"], 2)
        self.assertEqual(corner["clearance_steps"], 1)

    def test_region_map_rejects_nonconform_failed_edits(self):
        plan = {"hero_edits": [{
            "op": "add", "code_from": "bird", "anchoring": "airborne",
        }]}
        review = {"failed_instances": [{
            "edit_id": "bird", "instance_index": 0,
        }]}
        with self.assertRaisesRegex(ValueError, "only failed conform"):
            conform_failed_instances(plan, review)

    def test_map_first_scope_contains_only_conform_instances(self):
        plan = {"hero_edits": [
            {"op": "add", "code_from": "bear", "anchoring": "conform", "n": 2},
            {"op": "add", "code_from": "bird", "anchoring": "airborne", "n": 3},
        ]}
        self.assertEqual(all_conform_instances(plan), {"bear": {0, 1}})

    def test_map_first_scope_has_surface_and_3d_volume_branches(self):
        plan = {"hero_edits": [
            {"op": "add", "code_from": "bear", "anchoring": "conform", "n": 2},
            {"op": "add", "code_from": "penguins", "anchoring": "upright", "n": 5},
            {"op": "add", "code_from": "igloo", "anchoring": "tilted", "n": 1},
            {"op": "add", "code_from": "boat", "anchoring": "afloat", "n": 1},
            {"op": "add", "code_from": "tree", "anchoring": "rooted", "n": 3},
            {"op": "add", "code_from": "fish", "anchoring": "submerged", "n": 6},
            {"op": "add", "code_from": "bird", "anchoring": "airborne", "n": 3},
            {"op": "attrs", "code_from": "replace_tree", "anchoring": "rooted", "n": 1},
        ]}
        self.assertEqual(all_map_managed_instances(plan), {
            "bear": {0, 1},
            "penguins": set(range(5)),
            "igloo": {0},
            "boat": {0},
            "tree": set(range(3)),
            "fish": set(range(6)),
            "bird": set(range(3)),
        })
        for anchoring in (
                "conform", "upright", "tilted", "rooted", "afloat",
                "submerged", "airborne"):
            self.assertTrue(is_map_managed_anchoring(anchoring))

    def test_surface_map_retry_accepts_failed_upright_instances(self):
        plan = {"hero_edits": [
            {"op": "add", "code_from": "penguins", "anchoring": "upright", "n": 2},
        ]}
        review = {"failed_instances": [
            {"edit_id": "penguins", "instance_index": 1},
        ]}
        self.assertEqual(map_managed_failed_instances(plan, review), {
            "penguins": {1},
        })

    def test_submerged_retry_moves_only_environment_collision(self):
        plan = {"hero_edits": [{
            "op": "add", "code_from": "fish", "anchoring": "submerged", "n": 2,
        }]}
        review = {
            "placements": [{
                "code_from": "fish",
                "placement_spec": {"surface_policy": {
                    "require_full_footprint_support": False}},
                "anchors": [
                    {"object": "fish0", "instance_index": 0,
                     "all_frames_fully_visible": True,
                     "surface_policy_issue": None,
                     "footprint_support": {"supported": False}},
                    {"object": "fish1", "instance_index": 1,
                     "all_frames_fully_visible": True,
                     "surface_policy_issue": None,
                     "footprint_support": {"supported": False}},
                ],
            }],
            "violations": [{
                "type": "submerged_environment_aabb_collision",
                "objects": ["fish1", "CoralFactory(1)"],
            }],
        }
        self.assertEqual(
            map_managed_failed_instances(plan, review), {"fish": {1}})

    def test_submerged_aabb_uses_height_not_only_top_view_overlap(self):
        coral = [-1, -1, -1, 1, 1, 1]
        high_fish = rotated_aabb_bounds([0, 0, 5], [1, 1, 1])
        low_fish = rotated_aabb_bounds([0, 0, 1.5], [1, 1, 1])
        self.assertEqual(aabb_overlap_fraction(high_fish, coral), 0.0)
        self.assertGreater(aabb_overlap_fraction(low_fish, coral), 0.0)

    def test_submerged_capacity_can_use_separate_depth_layers(self):
        cells = [
            {"cell_id": "near", "world_position": [0.0, 0.0, 0.0],
             "candidate_proxy_world_bounds": [-1, -1, -1, 1, 1, 1]},
            {"cell_id": "far", "world_position": [0.0, 0.0, 4.0],
             "candidate_proxy_world_bounds": [-1, -1, 3, 1, 1, 5]},
        ]
        self.assertEqual(estimate_region_capacity(
            cells, half_extents=[1, 1, 1], min_sep=2.5, use_3d=True), 2)
        self.assertEqual(estimate_region_capacity(
            cells, half_extents=[1, 1], min_sep=2.5), 1)

    @staticmethod
    def _region_map_fixture(capacity=2):
        return {"bear": {
            "footprint_half_extents": [0.5, 0.5],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{
                "region_id": "R01", "estimated_capacity": capacity,
            }],
            "cells": [
                {"cell_id": "C00_00", "status": "feasible",
                 "region_id": "R01", "clearance_steps": 1,
                 "screen_position": [0.2, 0.2],
                 "world_position": [0.0, 0.0, 0.0]},
                {"cell_id": "C04_00", "status": "feasible",
                 "region_id": "R01", "clearance_steps": 2,
                 "screen_position": [0.6, 0.2],
                 "world_position": [4.0, 0.0, 0.0]},
            ],
        }}

    def test_region_selection_covers_exact_failed_instances_and_capacity(self):
        maps = self._region_map_fixture(capacity=1)
        expected = {"bear": {0, 1}}
        selection = {"decision": "select_regions", "assignments": [
            {"edit_id": "bear", "instance_index": 0, "region_id": "R01"},
            {"edit_id": "bear", "instance_index": 1, "region_id": "R01"},
        ]}
        with self.assertRaisesRegex(ValueError, "capacity=1"):
            validate_region_selection(selection, maps=maps, expected=expected)
        selection["assignments"].pop()
        with self.assertRaisesRegex(ValueError, "cover exactly"):
            validate_region_selection(selection, maps=maps, expected=expected)

    def test_automatic_region_assignment_prefers_one_sufficient_region(self):
        plan = {"hero_edits": [{
            "op": "add", "code_from": "bear", "anchoring": "conform", "n": 2,
            "placement_spec": {"screen_region": "lower_center"},
        }]}
        maps = {"bear": {
            "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            "regions": [
                {"region_id": "R01", "estimated_capacity": 3,
                 "screen_centroid": [0.6, 0.3], "max_clearance_steps": 2,
                 "cell_count": 20},
                {"region_id": "R02", "estimated_capacity": 1,
                 "screen_centroid": [0.2, 0.2], "max_clearance_steps": 3,
                 "cell_count": 5},
            ],
        }}
        assignments = automatic_region_assignments(
            plan, maps=maps, expected={"bear": {0, 1}})
        self.assertEqual([row["region_id"] for row in assignments],
                         ["R01", "R01"])

    def test_extended_grid_fills_the_frame_before_spilling_off_screen(self):
        """In-frame first is decided when regions are ASSIGNED, not when cells are picked.

        The extended grid searches 2x2 frames, so off-screen regions are large and
        whole while in-frame ones are small and broken up. The "capacity >= n"
        shortcut then discards every in-frame region and all instances land outside
        the frame (0911 canyon: obelisk 0/2, hoodoo 0/3, pillar 0/3 in frame, while
        14, 3 and 20 in-frame cells were feasible).
        """
        plan = {"hero_edits": [{
            "op": "add", "code_from": "bear", "anchoring": "conform", "n": 3,
            "placement_spec": {"screen_region": "lower_center"},
        }]}
        maps = {"bear": {
            "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            "regions": [
                {"region_id": "IN", "estimated_capacity": 2,
                 "screen_centroid": [0.5, 0.4], "max_clearance_steps": 1,
                 "cell_count": 4},
                {"region_id": "OUT", "estimated_capacity": 9,
                 "screen_centroid": [0.5, -0.4], "max_clearance_steps": 4,
                 "cell_count": 40},
            ],
            "footprint_half_extents": [0.5, 0.5],
            "min_sep": 2.5,
            "cells": (
                # in-frame: exactly two usable spots once min_sep is honoured
                [{"cell_id": f"I{i}", "status": "feasible", "region_id": "IN",
                  "screen_position": [0.5, 0.4],
                  "world_position": [3.0 * i, 0.0, 0.0]} for i in range(2)]
                + [{"cell_id": f"O{i}", "status": "feasible", "region_id": "OUT",
                    "screen_position": [0.5, -0.4],
                    "world_position": [3.0 * i, 40.0, 0.0]} for i in range(9)]),
        }}
        with unittest.mock.patch.dict(
                os.environ, {"BI_PLACEMENT_EXTENDED_GRID": "2"}):
            assignments = automatic_region_assignments(
                plan, maps=maps, expected={"bear": {0, 1, 2}})
        regions = [row["region_id"] for row in assignments]
        self.assertEqual(regions.count("IN"), 2)   # in-frame capacity used first
        self.assertEqual(regions.count("OUT"), 1)  # only the overflow spills out

        # with the extended grid off, the old "one sufficient region" rule stands
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BI_PLACEMENT_EXTENDED_GRID", None)
            assignments = automatic_region_assignments(
                plan, maps=maps, expected={"bear": {0, 1, 2}})
        self.assertEqual({row["region_id"] for row in assignments}, {"OUT"})

    def test_best_effort_assigns_past_capacity_instead_of_failing(self):
        """One cell short must not kill the case (0911 arctic: 10 penguins, 9 cells).

        The count was pinned by the scene config, so the retry could not lower it
        either -- that scene could never finish. In best-effort every instance is
        still assigned; the ones with nowhere to go are dropped when cells are
        picked, and the edit's count is trimmed there.
        """
        plan = {"hero_edits": [{
            "op": "add", "code_from": "bear", "anchoring": "conform", "n": 3,
            "placement_spec": {"screen_region": "lower_center"},
        }]}
        maps = {"bear": {
            "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            "footprint_half_extents": [0.5, 0.5],
            "min_sep": 2.5,
            "regions": [{"region_id": "R01", "estimated_capacity": 2,
                         "screen_centroid": [0.5, 0.4],
                         "max_clearance_steps": 1, "cell_count": 2}],
            "cells": [{"cell_id": f"C{i}", "status": "feasible", "region_id": "R01",
                       "screen_position": [0.5, 0.4],
                       "world_position": [3.0 * i, 0.0, 0.0]} for i in range(2)],
        }}
        expected = {"bear": {0, 1, 2}}
        with unittest.mock.patch.dict(os.environ, {"BI_PLACEMENT_BEST_EFFORT": "1"}):
            assignments = automatic_region_assignments(
                plan, maps=maps, expected=expected)
            self.assertEqual(len(assignments), 3)      # every instance assigned
            # the selection contract sees a complete cover and tolerates the
            # over-subscribed region; the surplus is dropped when cells are picked
            validate_region_selection(
                {"decision": "select_regions", "assignments": assignments},
                maps=maps, expected=expected)

        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BI_PLACEMENT_BEST_EFFORT", None)
            with self.assertRaisesRegex(ValueError, "insufficient feasible capacity"):
                automatic_region_assignments(plan, maps=maps, expected=expected)

    def test_projected_size_prefers_readable_and_avoids_blocking_the_camera(self):
        """0911: lanterns at 45-52m covered 0.3% of frame, the sailboat 32%.

        Being inside the frame was the only requirement, so a cell that renders the
        asset as a speck scored the same as one that reads, and a cell that fills
        the lens scored best of all when it was closest to the target point.
        """
        speck = {"projected_area_fraction": 0.0003}
        readable = {"projected_area_fraction": 0.08}
        blocking = {"projected_area_fraction": 0.32}
        # 0912: 1.2% 曾經算「讀得出來」，但滿分是從那裡開始的平台 ⇒ 物體停在
        #   剛好達標的★最遠處★(coast 帆船 73.3m/1.77%)。門檻提到 5% 之後
        #   這種大小只拿部分分數，會再往鏡頭走一點。
        barely = {"projected_area_fraction": 0.0177}
        self.assertLess(contract_region.projected_size_score(barely), 0.5)
        self.assertGreater(contract_region.projected_size_score(barely),
                           contract_region.projected_size_score(speck))
        # a cell that reads wins outright
        self.assertEqual(contract_region.projected_size_score(readable), 1.0)
        # both extremes lose to it, and both score below the neutral 0.5
        for bad in (speck, blocking):
            self.assertLess(contract_region.projected_size_score(bad),
                            contract_region.projected_size_score(readable))
            self.assertLess(contract_region.projected_size_score(bad), 0.5)
        # an over-sized asset still beats an invisible speck: it is bad framing,
        # not a missing object
        self.assertGreater(contract_region.projected_size_score(blocking),
                           contract_region.projected_size_score(speck))
        # a cell with no measurement is neutral
        self.assertEqual(contract_region.projected_size_score({}), 0.5)

    def test_measured_projected_size_outranks_being_nearest(self):
        """0911: the coast sailboat landed 13.7m out, filling 32% of the frame.

        Both scoring paths asked "how near is it?" as a stand-in for "how big
        does it read?"  Projected size is the real measure and it is two-sided:
        a firefly only reads up close, a sailboat stops reading once it fills
        the lens.  Where the map measured it, it replaces the depth preference.
        """
        def cell(cell_id, distance, fraction):
            return {
                "cell_id": cell_id, "region_id": "r0",
                "world_position": [float(distance), 0.0, 0.0],
                "screen_position": [0.5, 0.5],
                "distance": float(distance),
                "candidate_depth_m": float(distance),
                "clearance_steps": 3.0,
                "projected_area_fraction": fraction,
            }

        # the near cell blocks the lens, the far one reads at a sane size
        blocking_near = cell("near", 13.7, 0.32)
        readable_far = cell("far", 48.0, 0.012)
        edit = {"op": "add", "code_from": "sailboat", "n": 1}
        map_data = {"screen_bounds": [0.0, 0.0, 1.0, 1.0], "regions": []}

        def choose(cells):
            chosen, _reason, _scores = contract_region._choose_arrangement_cell(
                compatible=cells, edit=edit, edit_id="e0", instance_index=0,
                map_data=map_data, occupied=[], use_3d=True, history_key="h")
            return chosen["cell_id"]

        self.assertEqual(choose([blocking_near, readable_far]), "far")

        # without a measurement nothing changes: the nearest cell still wins
        for entry in (blocking_near, readable_far):
            entry.pop("projected_area_fraction")
        self.assertEqual(choose([blocking_near, readable_far]), "near")

    def test_scatter_keeps_instances_inside_the_frame_before_spreading_them(self):
        """0912 coast: five flamingos were placed, three landed at x=0.014, x=0.986
        and y=0.025 -- scatter's first key is "farthest from siblings", which pushes
        instances to the extreme corners of the available cells, and being inside
        the frame weighed only 4% of the score.  The user saw one flamingo.
        """
        def cell(cell_id, x, y, screen):
            return {
                "cell_id": cell_id, "region_id": "r0",
                "world_position": [x, y, 0.0],
                "screen_position": list(screen),
                "candidate_screen_bounds": [screen[0] - 0.02, screen[1] - 0.02,
                                            screen[0] + 0.02, screen[1] + 0.02],
                "distance": 20.0, "clearance_steps": 3.0,
                "projected_area_fraction": 0.03,
            }
        sibling = {"edit_id": "e0", "world_position": [0.0, 0.0, 0.0],
                   "screen_position": [0.5, 0.3]}
        edge_far = cell("edge", 40.0, 0.0, (0.01, 0.3))     # 離兄弟遠，壓在左緣
        inside_near = cell("inside", 8.0, 0.0, (0.65, 0.3))  # 離兄弟近，在畫面內
        edit = {"op": "add", "code_from": "flamingo", "n": 5,
                "placement_spec": {"arrangement": "scatter"}}
        map_data = {"screen_bounds": [0.0, 0.0, 1.0, 1.0], "regions": []}
        chosen, _reason, _scores = contract_region._choose_arrangement_cell(
            compatible=[edge_far, inside_near], edit=edit, edit_id="e0",
            instance_index=1, map_data=map_data, occupied=[sibling],
            use_3d=False, history_key="h")
        self.assertEqual(chosen["cell_id"], "inside")
        # 沒有畫面內的格子時，盡量模式仍然保底：邊緣的還是會被選
        chosen, _reason, _scores = contract_region._choose_arrangement_cell(
            compatible=[edge_far], edit=edit, edit_id="e0",
            instance_index=1, map_data=map_data, occupied=[sibling],
            use_3d=False, history_key="h")
        self.assertEqual(chosen["cell_id"], "edge")
        # try_3 抓到的洞：地面/水面格子沒有投影盒，只有點 ⇒ x=0.01 曾被算成 tier 0。
        point_edge = cell("pt_edge", 40.0, 0.0, (0.01, 0.3)); point_edge.pop("candidate_screen_bounds")
        point_in = cell("pt_in", 8.0, 0.0, (0.65, 0.3)); point_in.pop("candidate_screen_bounds")
        self.assertEqual(contract_region.frame_tier(point_edge), 1)
        self.assertEqual(contract_region.frame_tier(point_in), 0)
        chosen, _reason, _scores = contract_region._choose_arrangement_cell(
            compatible=[point_edge, point_in], edit=edit, edit_id="e0",
            instance_index=1, map_data=map_data, occupied=[sibling],
            use_3d=False, history_key="h")
        self.assertEqual(chosen["cell_id"], "pt_in")

    def test_ideal_depth_pulls_small_things_in_and_pushes_big_things_out(self):
        """0912: every airborne object sat at exactly 8.0m -- the depth ladder started
        at a fixed 8m and stopped at the first feasible rung, so the size score never
        got a say.  A 16cm butterfly at 8m covered 0.167% of the frame; the crystal
        pillar at 4.8m covered 216%.  Projected area goes as 1/d^2, so one probe
        projection gives the distance at which the object reads at the target size.
        """
        ideal = contract_region.ideal_depth_for_readable_size
        target = contract_region.READABLE_FRAME_FRACTION
        # 小東西往前：蝴蝶 8m/0.167% → 遠低於 8m(會被 2m 下限接住)
        self.assertLess(ideal(8.0, 0.001671), contract_region.AIRBORNE_MIN_DEPTH_M)
        # 大東西往後：老鷹 8m/19.9% → 約 16m；冰晶 4.8m/216% → 30m 開外
        self.assertAlmostEqual(ideal(8.0, 0.199), 8.0 * (0.199 / target) ** 0.5, places=6)
        self.assertGreater(ideal(4.8, 2.16), 30.0)
        # 剛好達標的不動
        self.assertAlmostEqual(ideal(20.0, target), 20.0, places=9)
        # 壞輸入回 None，不要炸掉地圖
        for bad in [(0.0, 0.05), (8.0, 0.0), (None, 0.05), ("x", 0.05)]:
            self.assertIsNone(ideal(*bad))
        # 下限本身要小到讓昆蟲能靠近
        self.assertLess(contract_region.AIRBORNE_MIN_DEPTH_M, 8.0)

    def test_automatic_region_assignment_prefers_camera_target_region(self):
        plan = {"hero_edits": [{
            "op": "add", "code_from": "boat", "anchoring": "afloat", "n": 1,
            "screen_positions": [[0.5, 0.35]],
            "placement_spec": {
                "screen_region": "lower_center",
                "screen_bounds": [0.1, 0.1, 0.9, 0.6],
            },
        }]}
        maps = {"boat": {
            "screen_bounds": [0.1, 0.1, 0.9, 0.6],
            "regions": [
                {"region_id": "left_patch", "estimated_capacity": 3,
                 "screen_centroid": [0.2, 0.34],
                 "max_clearance_steps": 3, "cell_count": 40},
                {"region_id": "camera_patch", "estimated_capacity": 3,
                 "screen_centroid": [0.52, 0.36],
                 "max_clearance_steps": 3, "cell_count": 40},
            ],
        }}
        assignments = automatic_region_assignments(
            plan, maps=maps, expected={"boat": {0}})
        self.assertEqual(assignments[0]["region_id"], "camera_patch")

    def test_region_application_preserves_sibling_and_avoids_history(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 2, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1], [0.8, 0.8]],
        }]}
        maps = self._region_map_fixture()
        revised, metadata = apply_region_selection(
            plan,
            maps=maps,
            assignments=[{
                "edit_id": "bear", "instance_index": 0, "region_id": "R01",
            }],
            rejected_positions={("bear", 0): [[0.6, 0.2]]},
        )
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_positions"], [[0.2, 0.2], [0.8, 0.8]])
        self.assertEqual(edit["placement_yaws"], [0.0, None])
        self.assertEqual(edit["screen_position_projection"], "visible_surface")
        self.assertEqual(metadata["applied"][0]["cell_id"], "C00_00")
        self.assertFalse(any("candidate" in key.lower() for key in edit))

    def test_submerged_region_application_owns_explicit_3d_position(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "FishFactory", "code_from": "fish",
            "n": 1, "anchoring": "submerged", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1]],
        }]}
        maps = {"fish": {
            "anchoring": "submerged",
            "footprint_half_extents": [1.0, 1.0],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{"region_id": "R01", "estimated_capacity": 1}],
            "cells": [{
                "cell_id": "C00_00", "status": "feasible", "region_id": "R01",
                "screen_position": [0.3, 0.4],
                "world_position": [-30.0, -15.0, -16.0],
                "candidate_world_position": [-30.0, -15.0, -14.0],
                "candidate_depth_m": 38.0,
                "feasible_yaws": [0.0],
            }],
        }}
        revised, metadata = apply_region_selection(
            plan, maps=maps,
            assignments=[{
                "edit_id": "fish", "instance_index": 0, "region_id": "R01",
            }],
        )
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["positions"], [[-30.0, -15.0, -16.0]])
        self.assertEqual(edit["screen_position_projection"],
                         "explicit_3d_region_map")
        self.assertEqual(metadata["applied"][0]["candidate_depth_m"], 38.0)

    def test_airborne_region_application_owns_explicit_3d_position(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BalloonFactory", "code_from": "balloon",
            "n": 1, "anchoring": "airborne", "min_sep": 7.0,
            "screen_positions": [[0.5, 0.6]],
        }]}
        maps = {"balloon": {
            "anchoring": "airborne",
            "footprint_half_extents": [3.0, 3.0],
            "proxy_half_extents": [3.0, 3.0, 4.0],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{"region_id": "R01", "estimated_capacity": 1}],
            "cells": [{
                "cell_id": "C00_00", "status": "feasible", "region_id": "R01",
                "screen_position": [0.45, 0.62],
                "world_position": [8.0, 12.0, 17.0],
                "candidate_world_position": [8.0, 12.0, 20.0],
                "candidate_depth_m": 24.0,
                "feasible_yaws": [0.0],
            }],
        }}
        revised, metadata = apply_region_selection(
            plan, maps=maps,
            assignments=[{
                "edit_id": "balloon", "instance_index": 0,
                "region_id": "R01",
            }],
        )
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["positions"], [[8.0, 12.0, 17.0]])
        self.assertEqual(edit["screen_position_projection"],
                         "explicit_3d_region_map")
        self.assertTrue(metadata["applied"][0]["use_3d"])

    def test_region_application_never_reuses_only_rejected_cell(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 1, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1]],
        }]}
        maps = self._region_map_fixture(capacity=1)
        maps["bear"]["cells"] = maps["bear"]["cells"][:1]
        with self.assertRaisesRegex(ValueError, "no remaining"):
            apply_region_selection(
                plan, maps=maps,
                assignments=[{
                    "edit_id": "bear", "instance_index": 0,
                    "region_id": "R01",
                }],
                rejected_positions={("bear", 0): [[0.2, 0.2]]},
            )

    def test_off_screen_cell_is_handed_over_as_a_world_position(self):
        """Extended grid seeds land outside the frame; they cannot be re-projected.

        apply_plan rejects a screen position outside the frame, and a point outside
        the frame has no "visible surface" to project onto (that cache is built from
        in-frame samples only). The map already knows the cell's world position, so
        it hands that over instead (0911 canyon died on [0.486, -0.425]).
        """
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 1, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.3, -0.4]],
        }]}
        maps = self._region_map_fixture(capacity=2)
        maps["bear"]["cells"] = [{
            "cell_id": "C21_00", "status": "feasible", "region_id": "R01",
            "clearance_steps": 2, "screen_position": [0.31, -0.42],
            "world_position": [29.3, 9.9, 2.0], "hit_surface": "OpaqueTerrain",
            "surface_normal": [-0.15, 0.23, 0.95],
        }]
        revised, _ = apply_region_selection(
            plan, maps=maps,
            assignments=[{"edit_id": "bear", "instance_index": 0, "region_id": "R01"}])
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_position_projection"], "explicit_3d_region_map")
        self.assertEqual(edit["positions"], [[29.3, 9.9, 2.0]])
        support = edit["explicit_position_supports"][0]
        self.assertEqual(support["surface"], "OpaqueTerrain")
        self.assertTrue(support["off_screen"])

    def test_in_frame_cell_still_projects_onto_the_visible_surface(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 1, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.2, 0.2]],
        }]}
        maps = self._region_map_fixture(capacity=2)
        revised, _ = apply_region_selection(
            plan, maps=maps,
            assignments=[{"edit_id": "bear", "instance_index": 0, "region_id": "R01"}])
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_position_projection"], "visible_surface")
        self.assertNotIn("positions", edit)

    def test_best_effort_places_fewer_instead_of_failing(self):
        """BI_PLACEMENT_BEST_EFFORT=1: a frame with room for one keeps one, not zero."""
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 2, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1], [0.8, 0.8]],
        }]}
        maps = self._region_map_fixture(capacity=1)
        maps["bear"]["cells"] = maps["bear"]["cells"][:1]
        assignments = [
            {"edit_id": "bear", "instance_index": 0, "region_id": "R01"},
            {"edit_id": "bear", "instance_index": 1, "region_id": "R01"},
        ]
        with unittest.mock.patch.dict(
                os.environ, {"BI_PLACEMENT_BEST_EFFORT": "1"}):
            revised, application = apply_region_selection(
                plan, maps=maps, assignments=assignments)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["n"], 1)
        self.assertEqual(len(edit["screen_positions"]), 1)
        self.assertEqual(application["best_effort_reductions"],
                         [{"edit_id": "bear", "requested": 2, "placed": 1,
                           "unplaced_instances": [1]}])
        # default (off) still fails loudly rather than silently placing fewer
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BI_PLACEMENT_BEST_EFFORT", None)
            with self.assertRaisesRegex(ValueError, "no remaining"):
                apply_region_selection(plan, maps=maps, assignments=assignments)

    def test_region_application_excludes_cells_tried_by_a_sibling(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 2, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1], [0.8, 0.8]],
        }]}
        maps = self._region_map_fixture(capacity=1)
        maps["bear"]["cells"] = maps["bear"]["cells"][:1]
        with self.assertRaisesRegex(ValueError, "no remaining"):
            apply_region_selection(
                plan, maps=maps,
                assignments=[{
                    "edit_id": "bear", "instance_index": 0,
                    "region_id": "R01",
                }],
                rejected_positions={("bear", 1): [[0.2, 0.2]]},
            )

    @staticmethod
    def _arrangement_map_fixture(*, submerged=False):
        anchoring = "submerged" if submerged else "conform"
        cells = [
            {"cell_id": "origin", "status": "feasible", "region_id": "R01",
             "screen_position": [0.2, 0.2],
             "world_position": [0.0, 0.0, 0.0]},
            {"cell_id": "near", "status": "feasible", "region_id": "R01",
             "screen_position": [0.4, 0.2],
             "world_position": [3.0, 0.0, 0.0]},
            {"cell_id": "far", "status": "feasible", "region_id": "R01",
             "screen_position": [0.75, 0.65],
             "world_position": [9.0, 4.0, 2.0 if submerged else 0.0]},
        ]
        if submerged:
            for cell in cells:
                cell["candidate_world_position"] = list(cell["world_position"])
                cell["feasible_yaws"] = [0.0]
        return {
            "edit": {
                "edit_id": "edit", "anchoring": anchoring,
                "footprint_half_extents": [0.4, 0.4],
                "proxy_half_extents": [0.4, 0.4, 0.4],
                "grid": {"screen_step": [0.1, 0.1]},
                "regions": [{"region_id": "R01", "estimated_capacity": 3}],
                "current_instances": [{
                    "instance_index": 0,
                    "world_position": [0.0, 0.0, 0.0],
                    "candidate_world_position": [0.0, 0.0, 0.0],
                }],
                "cells": cells,
            },
        }

    def test_scatter_and_school_use_distinct_cell_strategies(self):
        maps = self._arrangement_map_fixture(submerged=True)
        assignment = [{
            "edit_id": "edit", "instance_index": 1, "region_id": "R01",
        }]
        base = {
            "hero_edits": [{
                "op": "add", "factory": "FishFactory", "code_from": "edit",
                "n": 2, "anchoring": "submerged", "min_sep": 1.0,
                "screen_positions": [[0.2, 0.2], [0.9, 0.8]],
            }],
        }
        scatter_plan = deepcopy(base)
        scatter_plan["hero_edits"][0]["placement_spec"] = {
            "arrangement": "scatter"}
        _revised, scatter = apply_region_selection(
            scatter_plan, maps=deepcopy(maps), assignments=assignment)
        school_plan = deepcopy(base)
        school_plan["hero_edits"][0]["placement_spec"] = {
            "arrangement": "school"}
        _revised, school = apply_region_selection(
            school_plan, maps=deepcopy(maps), assignments=assignment)
        self.assertEqual(scatter["applied"][0]["cell_id"], "far")
        self.assertEqual(school["applied"][0]["cell_id"], "near")
        self.assertIn("farthest", scatter["applied"][0]["selection_policy"])
        self.assertIn("compact_3d", school["applied"][0]["selection_policy"])

    def test_field_uses_compact_cell_strategy(self):
        maps = self._arrangement_map_fixture(submerged=False)
        assignment = [{
            "edit_id": "edit", "instance_index": 1, "region_id": "R01",
        }]
        field_plan = {
            "hero_edits": [{
                "op": "add", "factory": "FlowerFactory", "code_from": "edit",
                "n": 2, "anchoring": "conform", "min_sep": 0.7,
                "screen_positions": [[0.2, 0.2], [0.9, 0.8]],
                "placement_spec": {"arrangement": "field"},
            }],
        }
        _revised, field = apply_region_selection(
            field_plan, maps=deepcopy(maps), assignments=assignment)
        self.assertEqual(field["applied"][0]["cell_id"], "near")
        self.assertIn("field_compact", field["applied"][0]["selection_policy"])

    def test_group_origin_prefers_near_camera_depth_after_hard_filters(self):
        cells = []
        for index, distance in enumerate([10.0, 20.0, 30.0, 40.0, 50.0]):
            cells.append({
                "cell_id": f"depth_{int(distance)}",
                "status": "feasible",
                "region_id": "R01",
                "screen_position": [0.1 + index * 0.18, 0.4],
                "world_position": [index * 5.0, 0.0, 0.0],
                "distance": distance,
                "clearance_steps": 4,
            })
        maps = {"bear": {
            "edit_id": "bear", "anchoring": "conform",
            "footprint_half_extents": [0.4, 0.4],
            "proxy_half_extents": [0.4, 0.4, 0.4],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{"region_id": "R01", "estimated_capacity": 5}],
            "current_instances": [],
            "cells": cells,
        }}
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 1, "anchoring": "conform", "min_sep": 1.0,
            "screen_positions": [[0.5, 0.5]],
            "placement_spec": {"arrangement": "single"},
        }]}
        _revised, metadata = apply_region_selection(
            plan, maps=maps, assignments=[{
                "edit_id": "bear", "instance_index": 0,
                "region_id": "R01",
            }])
        selected = metadata["applied"][0]
        self.assertEqual(selected["cell_id"], "depth_10")
        self.assertNotEqual(selected["cell_id"], "depth_20")
        self.assertEqual(selected["depth_target_quantile"], 0.04)
        self.assertIn("depth_staged", selected["selection_policy"])
        self.assertIn("near_camera", selected["depth_policy"])
        self.assertEqual(
            selected["screen_target_source"], "composer_screen_position")
        self.assertIn("camera_composition_score", selected)
        self.assertIn("camera_proximity_score", selected)
        self.assertGreater(selected["camera_depth_score"], 0.99)

    def test_single_cell_prefers_camera_target_after_hard_filters(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 2, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.5, 0.5], [0.5, 0.5]],
            "placement_spec": {"arrangement": "single"},
        }]}
        maps = {"bear": {
            "edit_id": "bear", "anchoring": "conform",
            "screen_bounds": [0.0, 0.0, 1.0, 1.0],
            "footprint_half_extents": [0.2, 0.2],
            "proxy_half_extents": [0.2, 0.2, 0.2],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{"region_id": "R01", "estimated_capacity": 3,
                         "cell_count": 3}],
            "cells": [
                {"cell_id": "occupied_center", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.5, 0.5],
                 "world_position": [0.0, 0.0, 0.0], "distance": 20.0,
                 "clearance_steps": 2},
                {"cell_id": "near_target", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.56, 0.5],
                 "world_position": [3.0, 0.0, 0.0], "distance": 20.0,
                 "clearance_steps": 2},
                {"cell_id": "far_screen", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.9, 0.1],
                 "world_position": [8.0, 0.0, 0.0], "distance": 20.0,
                 "clearance_steps": 2},
            ],
        }}
        _revised, metadata = apply_region_selection(
            plan, maps=maps, assignments=[{
                "edit_id": "bear", "instance_index": 1, "region_id": "R01",
            }])
        selected = metadata["applied"][0]
        self.assertEqual(selected["cell_id"], "near_target")
        self.assertNotEqual(selected["cell_id"], "occupied_center")
        self.assertGreater(selected["screen_target_score"], 0.9)

    def test_volume_selection_rejects_overlapping_candidate_proxy_bounds(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "OrcaFactory", "code_from": "orca_pod",
            "n": 2, "anchoring": "submerged", "min_sep": 0.1,
            "screen_positions": [[0.5, 0.5], [0.5, 0.5]],
            "placement_spec": {"arrangement": "single"},
        }]}
        maps = {"orca_pod": {
            "edit_id": "orca_pod", "anchoring": "submerged",
            "screen_bounds": [0.0, 0.0, 1.0, 1.0],
            "footprint_half_extents": [0.2, 0.2],
            "proxy_half_extents": [0.2, 0.2, 0.2],
            "grid": {"screen_step": [0.1, 0.1]},
            "regions": [{"region_id": "R01", "estimated_capacity": 3,
                         "cell_count": 3}],
            "cells": [
                {"cell_id": "occupied_volume", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.5, 0.5],
                 "world_position": [0.0, 0.0, 0.0],
                 "candidate_world_position": [0.0, 0.0, 0.0],
                 "candidate_depth_m": 20.0, "clearance_steps": 2,
                 "candidate_proxy_world_bounds": [-2, -1, -1, 2, 1, 1],
                 "feasible_yaws": [0.0]},
                {"cell_id": "aabb_overlap", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.52, 0.5],
                 "world_position": [3.0, 0.0, 0.0],
                 "candidate_world_position": [3.0, 0.0, 0.0],
                 "candidate_depth_m": 20.0, "clearance_steps": 2,
                 "candidate_proxy_world_bounds": [1.5, -1, -1, 4.5, 1, 1],
                 "feasible_yaws": [0.0]},
                {"cell_id": "safe_volume", "status": "feasible",
                 "region_id": "R01", "screen_position": [0.7, 0.5],
                 "world_position": [8.0, 0.0, 0.0],
                 "candidate_world_position": [8.0, 0.0, 0.0],
                 "candidate_depth_m": 20.0, "clearance_steps": 2,
                 "candidate_proxy_world_bounds": [7, -1, -1, 9, 1, 1],
                 "feasible_yaws": [0.0]},
            ],
        }}
        _revised, metadata = apply_region_selection(
            plan, maps=maps, assignments=[{
                "edit_id": "orca_pod", "instance_index": 1,
                "region_id": "R01",
            }])
        selected = metadata["applied"][0]
        self.assertEqual(selected["cell_id"], "safe_volume")
        self.assertIsNotNone(selected["candidate_proxy_world_bounds"])

    def test_row_strategy_aligns_with_passing_sibling(self):
        maps = self._arrangement_map_fixture(submerged=False)
        plan = {"hero_edits": [{
            "op": "add", "factory": "CabinFactory", "code_from": "edit",
            "n": 2, "anchoring": "conform", "min_sep": 1.0,
            "screen_positions": [[0.2, 0.2], [0.9, 0.8]],
            "placement_spec": {
                "arrangement": "row", "row_axis": "horizontal",
                "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            },
        }]}
        _revised, metadata = apply_region_selection(
            plan, maps=maps, assignments=[{
                "edit_id": "edit", "instance_index": 1, "region_id": "R01",
            }])
        self.assertEqual(metadata["applied"][0]["cell_id"], "near")
        self.assertIn("row_axis", metadata["applied"][0]["selection_policy"])

    def test_mixed_edits_allocate_large_proxy_first(self):
        def map_for(edit_id, half, cells):
            return {
                "edit_id": edit_id, "anchoring": "conform",
                "footprint_half_extents": [half, half],
                "proxy_half_extents": [half, half, half],
                "grid": {"screen_step": [0.1, 0.1]},
                "regions": [{"region_id": "R01", "estimated_capacity": len(cells)}],
                "cells": cells,
            }
        origin = {"cell_id": "origin", "status": "feasible",
                  "region_id": "R01", "screen_position": [0.3, 0.3],
                  "world_position": [0.0, 0.0, 0.0]}
        far = {"cell_id": "far", "status": "feasible",
               "region_id": "R01", "screen_position": [0.7, 0.3],
               "world_position": [7.0, 0.0, 0.0]}
        maps = {
            "bear": map_for("bear", 0.5, [deepcopy(origin), deepcopy(far)]),
            "igloo": map_for("igloo", 2.0, [deepcopy(origin)]),
        }
        maps["bear"]["cells"][1]["region_id"] = "R02"
        maps["bear"]["regions"].append({
            "region_id": "R02", "estimated_capacity": 1})
        plan = {"hero_edits": [
            {"op": "add", "factory": "BearFactory", "code_from": "bear",
             "n": 1, "anchoring": "conform", "screen_positions": [[0.5, 0.5]],
             "placement_spec": {"arrangement": "single"}},
            {"op": "add", "factory": "IglooFactory", "code_from": "igloo",
             "n": 1, "anchoring": "conform", "screen_positions": [[0.5, 0.5]],
             "placement_spec": {"arrangement": "single"}},
        ]}
        _revised, metadata = apply_region_selection(
            plan, maps=maps, assignments=[
                {"edit_id": "bear", "instance_index": 0, "region_id": "R01"},
                {"edit_id": "igloo", "instance_index": 0, "region_id": "R01"},
            ])
        self.assertEqual(metadata["applied"][0]["edit_id"], "igloo")
        bear = next(row for row in metadata["applied"]
                    if row["edit_id"] == "bear")
        self.assertEqual(bear["cell_id"], "far")
        self.assertEqual(bear["requested_region_id"], "R01")
        self.assertEqual(bear["region_id"], "R02")
        self.assertTrue(bear["cross_region_fallback"])

    def test_other_edit_reservations_gray_conflicting_cells(self):
        map_data = self._arrangement_map_fixture(submerged=False)["edit"]
        map_data["edit_id"] = "bear"
        reserved = cross_edit_reserved_cells(map_data, [{
            "edit_id": "igloo", "instance_index": 0,
            "collision_world_position": [0.0, 0.0, 0.0],
            "half_extents": [1.0, 1.0], "use_3d": False,
            "min_sep": 2.5,
        }])
        self.assertEqual([row["cell_id"] for row in reserved], ["origin"])

    def test_outdoor_loose_metadata_does_not_disable_reservations(self):
        map_data = self._arrangement_map_fixture(submerged=False)["edit"]
        map_data["edit_id"] = "bear"
        map_data["outdoor_ultra_loose_relaxation"] = {
            "applied": True,
            "scope": "outdoor_composer_only",
        }
        reserved = cross_edit_reserved_cells(map_data, [{
            "edit_id": "igloo", "instance_index": 0,
            "collision_world_position": [0.0, 0.0, 0.0],
            "half_extents": [1.0, 1.0], "use_3d": False,
            "min_sep": 2.5,
        }])
        self.assertFalse(is_outdoor_ultra_loose_map(map_data))
        self.assertEqual([row["cell_id"] for row in reserved], ["origin"])

    def test_outdoor_loose_metadata_still_requires_distinct_sibling_cells(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BirdFactory", "code_from": "penguins",
            "n": 3, "anchoring": "conform", "min_sep": 1.0,
            "screen_positions": [[0.1, 0.1], [0.1, 0.1], [0.1, 0.1]],
            "placement_spec": {"arrangement": "cluster"},
        }]}
        maps = {"penguins": {
            "edit_id": "penguins",
            "anchoring": "conform",
            "footprint_half_extents": [0.05, 0.05],
            "proxy_half_extents": [0.05, 0.05, 0.05],
            "grid": {"screen_step": [0.05, 0.05]},
            "regions": [{"region_id": "R01", "estimated_capacity": 5}],
            "outdoor_ultra_loose_relaxation": {
                "applied": True,
                "scope": "outdoor_composer_only",
                "actual_proxy_dimensions_m": [0.4, 0.4, 1.0],
            },
            "cells": [
                {"cell_id": "C00", "status": "feasible", "region_id": "R01",
                 "screen_position": [0.20, 0.20],
                 "world_position": [0.0, 0.0, 0.0]},
                {"cell_id": "C01", "status": "feasible", "region_id": "R01",
                 "screen_position": [0.28, 0.20],
                 "world_position": [2.0, 0.0, 0.0]},
                {"cell_id": "C02", "status": "feasible", "region_id": "R01",
                 "screen_position": [0.20, 0.28],
                 "world_position": [0.0, 2.0, 0.0]},
                {"cell_id": "C03", "status": "feasible", "region_id": "R01",
                 "screen_position": [0.36, 0.20],
                 "world_position": [4.0, 0.0, 0.0]},
                {"cell_id": "C04", "status": "feasible", "region_id": "R01",
                 "screen_position": [0.20, 0.36],
                 "world_position": [0.0, 4.0, 0.0]},
            ],
        }}
        revised, metadata = apply_region_selection(
            plan,
            maps=maps,
            assignments=[{
                "edit_id": "penguins", "instance_index": index,
                "region_id": "R01",
            } for index in range(3)],
        )
        positions = revised["hero_edits"][0]["screen_positions"]
        self.assertEqual(len({tuple(point) for point in positions}), 3)
        self.assertFalse(any(row["outdoor_ultra_loose"]
                             for row in metadata["applied"]))

    def test_outdoor_loose_metadata_still_requires_distinct_cross_edit_cells(self):
        def map_for(edit_id):
            return {
                "edit_id": edit_id,
                "anchoring": "conform",
                "footprint_half_extents": [0.05, 0.05],
                "proxy_half_extents": [0.05, 0.05, 0.05],
                "grid": {"screen_step": [0.05, 0.05]},
                "regions": [{"region_id": "R01", "estimated_capacity": 3}],
                "outdoor_ultra_loose_relaxation": {
                    "applied": True,
                    "scope": "outdoor_composer_only",
                },
                "cells": [
                    {"cell_id": "near", "status": "feasible",
                     "region_id": "R01", "screen_position": [0.20, 0.20],
                     "world_position": [0.0, 0.0, 0.0]},
                    {"cell_id": "mid", "status": "feasible",
                     "region_id": "R01", "screen_position": [0.34, 0.20],
                     "world_position": [3.0, 0.0, 0.0]},
                    {"cell_id": "far", "status": "feasible",
                     "region_id": "R01", "screen_position": [0.50, 0.20],
                     "world_position": [6.0, 0.0, 0.0]},
                ],
            }
        plan = {"hero_edits": [
            {"op": "add", "factory": "BearFactory", "code_from": "bear",
             "n": 1, "anchoring": "conform", "min_sep": 1.0,
             "screen_positions": [[0.1, 0.1]],
             "placement_spec": {"arrangement": "single"}},
            {"op": "add", "factory": "IglooFactory", "code_from": "igloo",
             "n": 1, "anchoring": "conform", "min_sep": 1.0,
             "screen_positions": [[0.1, 0.1]],
             "placement_spec": {"arrangement": "single"}},
        ]}
        revised, _metadata = apply_region_selection(
            plan,
            maps={
                "bear": map_for("bear"),
                "igloo": map_for("igloo"),
            },
            assignments=[
                {"edit_id": "bear", "instance_index": 0, "region_id": "R01"},
                {"edit_id": "igloo", "instance_index": 0, "region_id": "R01"},
            ],
        )
        bear = revised["hero_edits"][0]["screen_positions"][0]
        igloo = revised["hero_edits"][1]["screen_positions"][0]
        self.assertGreaterEqual(math.dist(bear, igloo), 0.10)

    def test_outdoor_loose_metadata_does_not_ignore_occupied_collision(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "BearFactory", "code_from": "bear",
            "n": 2, "anchoring": "conform", "min_sep": 2.5,
            "screen_positions": [[0.1, 0.1], [0.1, 0.1]],
        }]}
        maps = self._region_map_fixture(capacity=1)
        maps["bear"]["edit_id"] = "bear"
        maps["bear"]["outdoor_ultra_loose_relaxation"] = {
            "applied": True,
            "scope": "outdoor_composer_only",
        }
        maps["bear"]["cells"] = maps["bear"]["cells"][:1]
        with self.assertRaisesRegex(ValueError, "no remaining"):
            apply_region_selection(
                plan,
                maps=maps,
                assignments=[{
                    "edit_id": "bear", "instance_index": 0,
                    "region_id": "R01",
                }],
            )

    def test_indoor_planar_cross_edit_collision_uses_vertical_separation(self):
        floor_map = {
            "surface_policy": {"required_surface_types": ["floor"]},
            "proxy_half_extents": [0.4, 0.4, 0.3],
            "footprint_half_extents": [0.4, 0.4],
        }
        table_map = {
            "surface_policy": {"required_surface_types": ["tabletop"]},
            "proxy_half_extents": [0.4, 0.4, 0.3],
            "footprint_half_extents": [0.4, 0.4],
        }
        floor_box = {
            "world_position": _map_cell_collision_position(
                {"world_position": [0.0, 0.0, 0.0]}, floor_map, False),
            "half_extents": _collision_half_extents(floor_map, False),
            "collision_use_3d": True,
        }
        table_box = {
            "world_position": _map_cell_collision_position(
                {"world_position": [0.0, 0.0, 1.2]}, table_map, False),
            "half_extents": _collision_half_extents(table_map, False),
            "collision_use_3d": True,
        }
        self.assertTrue(_collision_uses_3d(floor_map, False))
        self.assertEqual(table_box["half_extents"], [0.4, 0.4, 0.3])
        self.assertFalse(_boxes_overlap(floor_box, table_box))

    def test_outdoor_surface_collision_remains_two_dimensional(self):
        outdoor = {
            "surface_policy": {"required_surface_types": ["snow"]},
            "proxy_half_extents": [0.4, 0.4, 0.3],
            "footprint_half_extents": [0.4, 0.4],
        }
        self.assertFalse(_collision_uses_3d(outdoor, False))
        self.assertEqual(_collision_half_extents(outdoor, False), [0.4, 0.4])

    def test_region_annotation_keeps_latest_active_and_grays_replaced(self):
        initial = [
            {"edit_id": "bear", "instance_index": 0, "cell_id": "C00"},
            {"edit_id": "bear", "instance_index": 1, "cell_id": "C01"},
        ]
        current = [
            {"edit_id": "bear", "instance_index": 0, "cell_id": "C02"},
        ]
        selected, excluded = active_selection_state(initial, current)
        self.assertEqual(
            [(row["instance_index"], row["cell_id"]) for row in selected],
            [(0, "C02"), (1, "C01")],
        )
        self.assertEqual([row["cell_id"] for row in excluded], ["C00"])

    def test_region_capacity_handles_long_component(self):
        cells = [{
            "cell_id": f"C{index:02d}_00", "clearance_steps": 1,
            "world_position": [float(index), 0.0, 0.0],
        } for index in range(6)]
        self.assertGreaterEqual(estimate_region_capacity(
            cells, half_extents=[0.4, 0.4], min_sep=2.0), 3)

    def test_fish_inherits_only_verified_idle_mode(self):
        request = local_animation.inherited_request("FishFactory")
        self.assertEqual(request, {"policy": "inherit", "mode": "idle"})
        self.assertEqual(
            local_animation.constructor_kwargs(request, factory="FishFactory"),
            {"animation_mode": "idle"})

    def test_unverified_factory_falls_back_to_no_local_animation(self):
        self.assertEqual(
            local_animation.inherited_request("BirdFactory"),
            {"policy": "none", "mode": None})
        self.assertEqual(
            local_animation.constructor_kwargs(None, factory="BirdFactory"), {})

    def test_agent_cannot_invent_local_animation_mode(self):
        with self.assertRaisesRegex(ValueError, "inherit only"):
            local_animation.normalize_request(
                {"policy": "inherit", "mode": "roam"},
                factory="FishFactory")

    def test_composer_preserves_catalog_gated_fish_inheritance(self):
        spec = {
            "arrangement": "single", "semantic_target": "open water",
            "screen_region": "center", "screen_bounds": [0.2, 0.2, 0.8, 0.8],
            "row_axis": "horizontal", "orientation": "random",
            "surface_policy": {},
        }
        plan = {"edits": [{
            "id": "add_fish", "action": "add", "placement": "hero",
            "feasible": True, "factory": "FishFactory", "count": 1,
            "anchoring": "submerged", "n_masters": 1,
            "local_animation": {"policy": "inherit", "mode": "idle"},
            "world_motion": {"preset": "aquatic_swim", "intensity": "subtle"},
            "placement_spec": spec,
        }]}
        out = {"edit_plan": {"hero_edits": [{
            "op": "add", "factory": "FishFactory", "n": 1,
            "screen_positions": [[0.5, 0.5]],
        }], "extra_edits": []}, "appearance": {"placements": []}}
        result = normalize_execution_contract(
            out, plan, {"raycast_surfaces": [], "available_surface_types": []})
        add = result["edit_plan"]["hero_edits"][0]
        self.assertEqual(add["local_animation"],
                         {"policy": "inherit", "mode": "idle"})
        self.assertEqual(add["world_motion"]["preset"], "aquatic_swim")

    @staticmethod
    def _composer_add_contract(points, *, anchoring="conform"):
        spec = {
            "arrangement": "scatter",
            "semantic_target": "visible ground",
            "screen_region": "full_frame",
            "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            "row_axis": "horizontal",
            "orientation": "random",
            "surface_policy": {},
        }
        plan = {"edits": [{
            "id": "add_test", "action": "add", "placement": "hero",
            "feasible": True, "factory": "TestFactory", "count": 2,
            "anchoring": anchoring, "n_masters": 2,
            "world_motion": {"preset": "none", "intensity": "subtle", "params": {}},
            "placement_spec": spec,
        }]}
        out = {"edit_plan": {"hero_edits": [{
            "op": "add", "factory": "TestFactory", "n": 2,
            "screen_positions": points,
        }], "extra_edits": []}, "appearance": {"placements": []}}
        manifest = {"raycast_surfaces": [], "available_surface_types": []}
        return normalize_execution_contract(out, plan, manifest)

    def test_composer_valid_screen_positions_are_preserved(self):
        result = self._composer_add_contract([[0.22, 0.31], [0.73, 0.54]])
        add = result["edit_plan"]["hero_edits"][0]
        self.assertEqual(add["screen_positions"], [[0.22, 0.31], [0.73, 0.54]])
        self.assertEqual(add["screen_positions_source"], "composer")

    def test_composer_invalid_screen_positions_are_rejected_for_retry(self):
        with self.assertRaises(ValueError):
            self._composer_add_contract([[0.22, 0.31]])

    def test_map_first_composer_coordinates_are_seeded_by_code(self):
        result = self._composer_add_contract(None)
        add = result["edit_plan"]["hero_edits"][0]
        self.assertEqual(len(add["screen_positions"]), 2)
        self.assertEqual(
            add["screen_positions_source"], "deterministic_map_first_seed")

    def test_surface_anchorings_are_seeded_for_map_first_composer(self):
        result = self._composer_add_contract(None, anchoring="upright")
        add = result["edit_plan"]["hero_edits"][0]
        self.assertEqual(len(add["screen_positions"]), 2)
        self.assertEqual(add["anchoring"], "upright")
        self.assertEqual(
            add["screen_positions_source"], "deterministic_map_first_seed")

    def test_composer_synthesizes_null_factory_add_and_prunes_orphan(self):
        spec = {
            "arrangement": "scatter",
            "semantic_target": "cliff rock ledges",
            "screen_region": "full_frame",
            "screen_bounds": [0.12, 0.15, 0.88, 0.8],
            "row_axis": "horizontal",
            "orientation": "random",
            "surface_policy": {},
        }
        plan = {"edits": [{
            "id": "beehives",
            "name": "beehives",
            "action": "add",
            "placement": "hero",
            "feasible": True,
            "factory": None,
            "host_factory": None,
            "count": 3,
            "anchoring": "tilted",
            "n_masters": 1,
            "world_motion": {"preset": "none", "intensity": "subtle"},
            "placement_spec": spec,
        }]}
        out = {"edit_plan": {"hero_edits": [
            {
                "op": "add",
                "factory": "BeehiveHost",
                "n": 3,
                "anchoring": "tilted",
                "placement_spec": deepcopy(spec),
            },
            {
                "op": "add",
                "factory": None,
                "code_from": "beehives",
                "n": 3,
                "anchoring": "tilted",
                "placement_spec": deepcopy(spec),
            },
        ], "extra_edits": []}, "appearance": {"placements": []}}

        result = normalize_execution_contract(
            out, plan, {"raycast_surfaces": [], "available_surface_types": []})
        adds = [
            edit for edit in result["edit_plan"]["hero_edits"]
            if edit.get("op") == "add"
        ]

        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["factory"], "BeehivesFactory")
        self.assertEqual(adds[0]["code_from"], "beehives")
        self.assertEqual(
            all_map_managed_instances(result["edit_plan"]),
            {"beehives": {0, 1, 2}},
        )
        self.assertTrue(any("removed orphan map-managed add" in warning
                            for warning in result["warnings"]))

    def test_composer_check_rejects_map_add_without_code_from(self):
        warnings = check_composer_contract({
            "edit_plan": {"hero_edits": [{
                "op": "add",
                "factory": "BeehiveHost",
                "n": 3,
                "anchoring": "tilted",
                "placement_spec": {
                    "arrangement": "scatter",
                    "screen_bounds": [0.12, 0.15, 0.88, 0.8],
                    "surface_policy": {},
                },
            }], "extra_edits": []},
            "appearance": {"placements": []},
        }, {"collections": [], "scatters": [],
            "raycast_surfaces": [], "available_surface_types": []})
        self.assertTrue(any("code_from" in warning for warning in warnings))

    def test_mesh_level_required_surface_wins_over_indistinguishable_forbidden(self):
        spec = {
            "arrangement": "single", "semantic_target": "open sand",
            "screen_region": "center", "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            "row_axis": "horizontal", "orientation": "random",
            "surface_policy": {
                "required_surface_types": ["sand"],
                "forbidden_surface_types": ["rock"],
            },
        }
        plan = {"edits": [{
            "id": "camel", "action": "add", "placement": "hero",
            "feasible": True, "factory": "HerbivoreFactory", "count": 1,
            "anchoring": "conform", "n_masters": 1,
            "world_motion": {"preset": "none", "intensity": "subtle"},
            "placement_spec": spec,
        }]}
        out = {"edit_plan": {"hero_edits": [{
            "op": "add", "factory": "HerbivoreFactory", "n": 1,
            "screen_positions": [[0.5, 0.4]],
        }], "extra_edits": []}, "appearance": {"placements": []}}
        manifest = {
            "available_surface_types": [
                {"type": "sand", "mesh_names": ["OpaqueTerrain"]},
                {"type": "rock", "mesh_names": ["OpaqueTerrain"]},
            ],
            "raycast_surfaces": [{"name": "OpaqueTerrain"}],
        }
        result = normalize_execution_contract(out, plan, manifest)
        policy = result["edit_plan"]["hero_edits"][0]["placement_spec"][
            "surface_policy"]
        self.assertEqual(policy["required_surface_names"], ["OpaqueTerrain"])
        self.assertEqual(policy["forbidden_surface_names"], [])
        self.assertTrue(any("required wins" in warning
                            for warning in result["warnings"]))

    def test_projection_failures_allow_only_listed_instances(self):
        review = {"failed_instances": [{
            "edit_id": "goats", "instance_index": 0,
            "failure": "raycast_miss",
        }]}
        self.assertEqual(collect_allowed_updates(review), {"goats": {0}})
        plan = {"hero_edits": [{
            "op": "add", "factory": "GoatFactory", "code_from": "goats",
            "n": 2, "anchoring": "conform",
            "placement_spec": {"screen_bounds": [0.1, 0.1, 0.9, 0.8]},
            "screen_positions": [[0.2, 0.3], [0.7, 0.3]],
        }], "extra_edits": []}
        revised = json.loads(json.dumps(plan))
        revised["hero_edits"][0]["screen_positions"][0] = [0.3, 0.4]
        self.assertEqual(validate_revision(plan, revised, {"goats": {0}}), revised)
        illegal = json.loads(json.dumps(revised))
        illegal["hero_edits"][0]["screen_positions"][1] = [0.6, 0.4]
        with self.assertRaisesRegex(ValueError, "passing instances"):
            validate_revision(plan, illegal, {"goats": {0}})

    def test_revision_restores_machine_owned_placement_metadata(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "GoatFactory", "code_from": "goats",
            "n": 1, "anchoring": "conform",
            "placement_spec": {"screen_bounds": [0.1, 0.1, 0.9, 0.8]},
            "screen_positions": [[0.2, 0.3]],
            "positions": [[1.0, 2.0, 0.0]],
            "positions_source": "composer_image_semantic_map_snap",
            "explicit_position_supports": [{"surface": "floor"}],
        }], "extra_edits": []}
        revised = json.loads(json.dumps(plan))
        revised["hero_edits"][0]["screen_positions"] = [[0.3, 0.4]]
        revised["hero_edits"][0].pop("positions")
        revised["hero_edits"][0].pop("positions_source")
        revised["hero_edits"][0].pop("explicit_position_supports")
        accepted = validate_revision(plan, revised, {"goats": {0}})
        self.assertEqual(accepted["hero_edits"][0]["positions"],
                         [[1.0, 2.0, 0.0]])
        self.assertEqual(
            accepted["hero_edits"][0]["explicit_position_supports"],
            [{"surface": "floor"}])

    def test_revision_must_change_every_failed_instance(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "GoatFactory", "code_from": "goats",
            "n": 1, "anchoring": "conform",
            "placement_spec": {"screen_bounds": [0.1, 0.1, 0.9, 0.8]},
            "screen_positions": [[0.2, 0.3]],
        }], "extra_edits": []}
        with self.assertRaisesRegex(ValueError, "did not change"):
            validate_revision(plan, json.loads(json.dumps(plan)), {"goats": {0}})

    def test_nonconverged_preview_uses_a_derived_plan_only(self):
        plan = {"hero_edits": [{
            "op": "add", "screen_positions": [[0.2, 0.3]],
        }, {"op": "scale", "value": 2}], "extra_edits": []}
        derived = prepare_diagnostic_plan(plan)
        self.assertNotIn(
            "diagnostic_preview_allow_rejected_projection",
            plan["hero_edits"][0])
        self.assertTrue(derived["hero_edits"][0][
            "diagnostic_preview_allow_rejected_projection"])
        self.assertNotIn(
            "diagnostic_preview_allow_rejected_projection",
            derived["hero_edits"][1])

    def test_revision_history_records_rejected_points_and_oscillation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in (1, 2):
                round_dir = root / f"round_{number:02d}"
                round_dir.mkdir()
                (round_dir / "edit_plan_before.json").write_text(json.dumps({
                    "hero_edits": [{
                        "code_from": "camels",
                        "screen_positions": [[0.4, 0.2], [0.7, 0.3]],
                    }],
                }), encoding="utf-8")
                (round_dir / "machine_review.json").write_text(json.dumps({
                    "failed_instances": [{
                        "edit_id": "camels", "instance_index": 0,
                        "failure": "projection_rejected",
                    }],
                    "failures": ["camels instance 0 rejected"],
                }), encoding="utf-8")
            history = build_revision_history(root)
        self.assertEqual(len(history), 2)
        self.assertEqual(
            history[0]["rejected_positions"][0]["screen_position"],
            [0.4, 0.2])
        evidence = oscillation_evidence(history)
        self.assertTrue(evidence["coordinated_failed_set_relayout_required"])
        self.assertEqual(len(evidence["exact_rejected_positions_reused"]), 1)

    def test_surface_policy_for_bear_forbids_liquid(self):
        spec = contract.normalize_placement_spec({
            "surface_policy": {
                "forbidden_surface_names": ["liquid"],
                "max_slope_deg": 20,
                "require_full_footprint_support": True,
            }}, count=1, anchoring="conform")
        policy = spec["surface_policy"]
        self.assertIn("liquid", policy["forbidden_surface_names"])
        self.assertIsNotNone(contract.surface_policy_issue(
            policy, surface_name="liquid.001", slope_deg=0))
        self.assertIsNone(contract.surface_policy_issue(
            policy, surface_name="CollectiveTransparentTerrain", slope_deg=10))

    def test_surface_policy_for_afloat_boat_requires_liquid(self):
        spec = contract.normalize_placement_spec({
            "surface_policy": {"required_surface_names": ["liquid"]},
        }, count=1, anchoring="afloat")
        self.assertIsNone(contract.surface_policy_issue(
            spec["surface_policy"], surface_name="liquid", slope_deg=0))
        self.assertIsNotNone(contract.surface_policy_issue(
            spec["surface_policy"], surface_name="OpaqueTerrain", slope_deg=0))

    def test_surface_type_policy_requires_resolved_indoor_type(self):
        policy = contract.normalize_surface_policy({
            "required_surface_types": ["tabletop"],
        })
        self.assertIsNone(contract.surface_type_policy_issue(
            policy, ["tabletop"]))
        self.assertIsNotNone(contract.surface_type_policy_issue(
            policy, ["floor"]))
        self.assertIsNotNone(contract.surface_type_policy_issue(policy, []))

    def test_surface_type_policy_rejects_forbidden_resolved_type(self):
        policy = contract.normalize_surface_policy({
            "forbidden_surface_types": ["shelf"],
        })
        self.assertIsNone(contract.surface_type_policy_issue(policy, ["floor"]))
        self.assertIsNotNone(contract.surface_type_policy_issue(
            policy, ["shelf"]))

    def test_surface_policy_rejects_required_forbidden_conflict(self):
        with self.assertRaisesRegex(ValueError, "disjoint"):
            contract.normalize_surface_policy({
                "required_surface_names": ["liquid"],
                "forbidden_surface_names": ["LIQUID"],
            })

    def test_surface_policy_names_must_come_from_scene_inventory(self):
        policy = contract.normalize_surface_policy({
            "forbidden_surface_names": ["liquid"],
        })
        self.assertEqual(
            contract.validate_surface_policy_names(
                policy, ["OpaqueTerrain", "liquid.001"]), policy)
        with self.assertRaisesRegex(ValueError, "absent from raycast_surfaces"):
            contract.validate_surface_policy_names(
                {"required_surface_names": ["imaginary_ice"]},
                ["OpaqueTerrain", "liquid"])

    def test_surface_policy_types_must_come_from_camera_inventory(self):
        policy = contract.normalize_surface_policy({
            "required_surface_types": ["ice"],
            "forbidden_surface_types": ["liquid"],
        })
        self.assertEqual(
            contract.validate_surface_policy_types(
                policy, ["ice", "liquid"])["required_surface_types"],
            ["ice"])
        with self.assertRaises(ValueError):
            contract.validate_surface_policy_types(policy, ["liquid"])

    def test_unknown_global_surface_type_is_rejected(self):
        with self.assertRaises(ValueError):
            contract.normalize_surface_policy({
                "required_surface_types": ["marshmallow"]})

    def test_indoor_planar_policy_never_relaxes_to_center_only(self):
        rigid = contract.harden_indoor_surface_policy({
            "required_surface_types": ["shelf"],
            "max_slope_deg": 20,
            "require_full_footprint_support": False,
        })
        self.assertTrue(rigid["require_full_footprint_support"])
        self.assertEqual(rigid["max_slope_deg"], 20.0)
        soft = contract.harden_indoor_surface_policy({
            "required_surface_types": ["bed_top"],
            "max_slope_deg": 30,
            "require_full_footprint_support": False,
        })
        self.assertTrue(soft["require_full_footprint_support"])
        self.assertEqual(soft["max_slope_deg"], 45.0)
        outdoor = contract.harden_indoor_surface_policy({
            "required_surface_types": ["snow"],
            "max_slope_deg": 30,
            "require_full_footprint_support": False,
        })
        self.assertFalse(outdoor["require_full_footprint_support"])
        self.assertEqual(outdoor["max_slope_deg"], 30.0)

    def test_indoor_plush_proxy_uses_four_times_larger_minimum(self):
        self.assertEqual(
            contract.indoor_proxy_dimensions(
                "PlushToyFactory", [0.32, 0.24, 0.24]),
            [1.28, 0.96, 0.60],
        )
        self.assertEqual(
            contract.indoor_proxy_dimensions(
                "PlushToyFactory", [1.5, 1.1, 0.8]),
            [1.5, 1.1, 0.8],
        )
        self.assertEqual(
            contract.indoor_proxy_dimensions(
                "BookStackFactory", [0.32, 0.18, 0.22]),
            [0.32, 0.18, 0.22],
        )

    def test_indoor_visibility_scaling_uses_coarse_surface_caps(self):
        self.assertEqual(
            contract.indoor_visibility_scale_factor(0.03, ["bed_top"]),
            4.0,
        )
        self.assertEqual(
            contract.indoor_visibility_scale_factor(0.03, ["floor"]),
            3.0,
        )
        self.assertEqual(
            contract.indoor_visibility_scale_factor(0.03, ["tabletop"]),
            2.5,
        )
        self.assertEqual(
            contract.indoor_visibility_scale_factor(0.03, ["shelf"]),
            2.0,
        )

    def test_indoor_visibility_scaling_uses_smallest_sufficient_step(self):
        dimensions, scale = contract.indoor_visibility_proxy_dimensions(
            [0.4, 0.2, 0.3], 0.08, ["tabletop"])
        self.assertEqual(scale, 1.5)
        for actual, expected in zip(dimensions, [0.6, 0.3, 0.45]):
            self.assertAlmostEqual(actual, expected)
        unchanged, scale = contract.indoor_visibility_proxy_dimensions(
            [0.4, 0.2, 0.3], 0.12, ["tabletop"])
        self.assertEqual(scale, 1.0)
        self.assertEqual(unchanged, [0.4, 0.2, 0.3])

    def test_indoor_visibility_scaling_does_not_touch_outdoor_types(self):
        dimensions, scale = contract.indoor_visibility_proxy_dimensions(
            [0.4, 0.2, 0.3], 0.01, ["snow"])
        self.assertEqual(scale, 1.0)
        self.assertEqual(dimensions, [0.4, 0.2, 0.3])

    def test_director_infers_missing_indoor_surface_from_edit_text(self):
        from director.director import _infer_missing_indoor_surface_type

        edit = {
            "id": "cabinet_vase",
            "name": "flower vase",
            "spec": "Small vase on the visible cabinet top.",
            "placement_reason": "Placed on a cabinet/countertop surface.",
        }
        self.assertEqual(
            _infer_missing_indoor_surface_type(
                edit, ["floor", "countertop", "bed_top"]),
            "countertop")
        self.assertIsNone(
            _infer_missing_indoor_surface_type(edit, ["floor", "bed_top"]))

    def test_director_infers_missing_indoor_proxy_dimensions(self):
        from director.director import _infer_missing_indoor_proxy_dimensions

        self.assertEqual(
            _infer_missing_indoor_proxy_dimensions({
                "factory": "VaseFactory",
                "spec": "Small compact flower vase.",
            }),
            [0.12, 0.12, 0.28])
        self.assertEqual(
            _infer_missing_indoor_proxy_dimensions({
                "factory": "BoxFactory",
                "spec": "Compact audio speaker with front grille.",
            }),
            [0.25, 0.12, 0.16])

    def test_director_infers_back_to_wall_mode_from_edit_text(self):
        from director.director import _infer_back_to_wall_mode

        self.assertTrue(_infer_back_to_wall_mode({
            "id": "wall_chairs",
            "spec": "Two chairs placed back-to-wall in a neat row.",
        }))
        self.assertTrue(_infer_back_to_wall_mode({
            "id": "wall_chairs",
            "placement_mode_reason": "Their backs set close to the wall.",
        }))
        self.assertFalse(_infer_back_to_wall_mode({
            "id": "floor_rug",
            "spec": "A rug centered on the open floor.",
        }))

    def test_connected_regions_split_adjacent_3d_support_planes(self):
        cells = [{
            "cell_id": f"C{column:02d}_00",
            "column": column,
            "row": 0,
            "screen_position": [0.2 + 0.1 * column, 0.3],
            "world_position": [float(column), 0.0, float(column)],
            "status": "feasible",
            "support_plane_id": f"P{column + 1:02d}",
        } for column in range(2)]
        labeled, regions = connected_feasible_regions(cells)
        self.assertEqual(len(regions), 2)
        self.assertEqual(
            {cell["region_id"] for cell in labeled}, {"R01", "R02"})

    def test_indoor_surface_types_are_supported_registry_vocabulary(self):
        for surface_type in (
                "floor", "tabletop", "countertop", "shelf", "bed_top", "seat"):
            self.assertIn(surface_type, surface_registry.SURFACE_TYPES)

    def test_indoor_scene_aliases_resolve_room_surface_rules(self):
        self.assertEqual(
            surface_registry.canonical_scene_name("DiningRoom"), "dining-room")
        self.assertIn("tabletop", surface_registry.scene_rules("DiningRoom"))
        self.assertEqual(
            surface_registry.types_for_mesh(
                "DiningRoom",
                "TableDiningFactory(879779).spawn_asset(2013057)"),
            ["tabletop"])

    def test_indoor_floor_regex_does_not_match_floor_lamp(self):
        self.assertEqual(
            surface_registry.types_for_mesh(
                "Bedroom", "bedroom_0/0.floor"),
            ["floor"])
        self.assertEqual(
            surface_registry.types_for_mesh(
                "Bedroom",
                "FloorLampFactory(190593).spawn_asset(5140734)"),
            [])

    def test_indoor_support_furniture_types_are_named_conservatively(self):
        examples = {
            "KitchenCabinetFactory(1740162).spawn_asset(9117157)": ["countertop"],
            "LargeShelfFactory(7373488).bbox_placeholder(2226035)": ["shelf"],
            "ComforterFactory(8799795).spawn_asset(6359590)": ["bed_top"],
            "SofaFactory(6304742).spawn_asset(9369380)": ["seat"],
            "DeskLampFactory(903702).spawn_asset(7640962)": [],
        }
        for mesh_name, expected in examples.items():
            self.assertEqual(
                surface_registry.types_for_mesh("living-room", mesh_name),
                expected)

    def test_afloat_is_surface_anchored_not_submerged(self):
        self.assertIn("afloat", placement_modes.ANCHORINGS)
        self.assertNotIn("afloat", placement_modes.AIRBORNE_MODES)
        normal_fac, altitude = placement_modes.params_for("afloat")
        self.assertEqual(normal_fac, 0.0)
        self.assertLess(altitude, 0.0)
        spec = contract.normalize_placement_spec(None, count=1, anchoring="afloat")
        self.assertEqual(spec["semantic_target"], "open water surface")
        self.assertEqual(spec["screen_region"], "lower_center")

    def test_airborne_default_is_upper_frame(self):
        spec = contract.normalize_placement_spec(
            None, count=1, anchoring="airborne")
        self.assertEqual(spec["arrangement"], "single")
        self.assertEqual(spec["screen_region"], "upper_center")
        self.assertEqual(contract.initial_screen_positions(spec, 1), [[0.5, 0.625]])
        self.assertFalse(spec["surface_policy"]["require_full_footprint_support"])

    def test_row_coordinates_are_ordered_and_parallel(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "row",
            "semantic_target": "river bank",
            "screen_region": "lower_center",
            "row_axis": "horizontal",
            "orientation": "parallel",
        }, count=5, anchoring="conform")
        points = contract.initial_screen_positions(spec, 5)
        self.assertEqual(len(points), 5)
        self.assertEqual(len({point[1] for point in points}), 1)
        self.assertEqual(points, sorted(points))
        self.assertAlmostEqual(
            contract.parallel_row_yaw([[1.0, 2.0, 0.0], [4.0, 2.0, 0.0]]), 0.0)
        self.assertAlmostEqual(
            contract.parallel_row_yaw([[1.0, 2.0, 0.0], [1.0, 5.0, 0.0]]),
            math.pi / 2.0)

    def test_scatter_gets_stable_spread_for_initial_visual_review(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "scatter",
            "screen_bounds": [0.1, 0.2, 0.9, 0.8],
        }, count=4, anchoring="conform")
        first = contract.initial_screen_positions(spec, 4)
        second = contract.initial_screen_positions(spec, 4)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 4)
        self.assertEqual(len({tuple(point) for point in first}), 4)
        for x, y in first:
            self.assertTrue(0.1 <= x <= 0.9)
            self.assertTrue(0.2 <= y <= 0.8)

    def test_dense_flower_requests_become_field_with_tighter_spacing(self):
        scatter = contract.normalize_placement_spec({
            "arrangement": "scatter",
            "screen_bounds": [0.1, 0.2, 0.9, 0.8],
        }, count=4, anchoring="conform")
        scatter_points = contract.initial_screen_positions(scatter, 4)
        field = contract.normalize_placement_spec({
            "semantic_target": "sunflower field",
            "proxy_dimensions_m": [0.5, 0.5, 1.8],
        }, count=24, anchoring="upright", factory="FlowerFactory",
            code_from="add_sunflowers")
        self.assertEqual(field["arrangement"], "field")
        self.assertEqual(field["proxy_dimensions_m"], [0.22, 0.22, 1.8])
        self.assertEqual(contract.dense_field_instance_count(
            count=24, factory="FlowerFactory", code_from="add_sunflowers",
            semantic_target="sunflower field", arrangement="field"), 60)
        self.assertEqual(contract.default_min_sep(
            factory="FlowerFactory", code_from="add_sunflowers",
            semantic_target="sunflower field", arrangement="field",
            count=24), 0.30)
        self.assertEqual(contract.dense_field_visual_fit_dimensions(
            actual_dimensions=[0.22, 0.22, 1.8],
            factory="FlowerFactory", code_from="add_sunflowers",
            semantic_target="sunflower field", arrangement="field",
            count=24), [1.2, 1.2, 1.8])
        self.assertGreaterEqual(estimate_region_capacity(
            SUNFLOWER_FIELD_REGION_CELLS,
            half_extents=[0.11, 0.11],
            min_sep=0.30,
            arrangement="field"), 25)
        dense_field_cells = [
            {
                "cell_id": f"D{col:02d}_{row:02d}",
                "world_position": [col * 0.28, row * 0.28, 0.0],
            }
            for row in range(15)
            for col in range(15)
        ]
        self.assertGreaterEqual(estimate_region_capacity(
            dense_field_cells,
            half_extents=[0.11, 0.11],
            min_sep=0.30,
            arrangement="field"), 60)
        field_points = contract.initial_screen_positions(field, 4)
        scatter_x_span = max(point[0] for point in scatter_points) - min(
            point[0] for point in scatter_points)
        scatter_y_span = max(point[1] for point in scatter_points) - min(
            point[1] for point in scatter_points)
        field_x_span = max(point[0] for point in field_points) - min(
            point[0] for point in field_points)
        field_y_span = max(point[1] for point in field_points) - min(
            point[1] for point in field_points)
        self.assertLess(field_x_span, scatter_x_span)
        self.assertLess(field_y_span, scatter_y_span)

        legacy = contract.normalize_placement_spec({
            "arrangement": "scatter",
            "semantic_target": "flat open grass plain",
            "screen_region": "full_frame",
            "screen_bounds": [0.05, 0.1, 0.95, 0.55],
        }, count=24, anchoring="upright", factory="FlowerFactory",
            code_from="add_sunflowers")
        self.assertEqual(legacy["arrangement"], "field")
        self.assertEqual(legacy["screen_region"], "lower_center")
        self.assertEqual(legacy["screen_bounds"], [0.35, 0.2, 0.65, 0.43])

        broad = contract.normalize_placement_spec({
            "arrangement": "field",
            "semantic_target": "flat grass plain foreground and midground",
            "screen_region": "full_frame",
            "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            "outdoor_broad_search_relaxation": {"applied": True},
        }, count=60, anchoring="upright", factory="FlowerFactory",
            code_from="add_sunflowers")
        self.assertEqual(broad["arrangement"], "field")
        self.assertEqual(broad["screen_region"], "full_frame")
        self.assertEqual(broad["screen_bounds"], [0.05, 0.05, 0.95, 0.9])

    def test_creatures_are_never_a_dense_botanical_field(self):
        """0911 forest: a butterfly over blossoms became 60 hero instances.

        The dense-field floor and the 0.22x0.22x1.8 proxy describe a flower stem.
        A butterfly matched only because its semantic_target mentioned blossoms,
        and 60 then broke the fixed-asset hero cap, failing the case four times.
        """
        self.assertEqual(contract.dense_field_instance_count(
            count=8, factory="DragonflyFactory", code_from="colorful_butterfly",
            semantic_target="blossom canopy", arrangement="field"), 8)
        self.assertFalse(contract.uses_dense_field_request(
            factory="DragonflyFactory", code_from="colorful_butterfly",
            semantic_target="blossom canopy", arrangement="field", count=8))
        self.assertTrue(contract.is_creature("FlyingBirdFactory"))
        self.assertTrue(contract.is_creature(None, "monarch_butterfly"))
        self.assertFalse(contract.is_creature("FlowerFactory", "add_sunflowers"))
        # the botanical case is untouched
        self.assertEqual(contract.dense_field_instance_count(
            count=24, factory="FlowerFactory", code_from="add_sunflowers",
            semantic_target="sunflower field", arrangement="field"), 60)

    def test_dense_field_visual_fit_prefers_population_target_count(self):
        self.assertEqual(contract.dense_field_visual_fit_count(
            population_target_count=60, asset_count=1), 60)
        self.assertEqual(contract.dense_field_visual_fit_count(
            manifest_population_targets=24, asset_count=1), 24)
        self.assertEqual(contract.dense_field_visual_fit_count(
            asset_count=1), 1)

    def test_composer_dense_flower_add_respects_field_floor(self):
        plan = {"edits": [{
            "id": "add_sunflowers",
            "action": "add",
            "placement": "hero",
            "feasible": True,
            "factory": "FlowerFactory",
            "count": 25,
            "anchoring": "upright",
            "n_masters": 6,
            "world_motion": {"preset": "none", "intensity": "subtle", "params": {}},
            "placement_spec": {
                "arrangement": "field",
                "semantic_target": "flat grass plain foreground and midground",
                "screen_region": "lower_center",
                "screen_bounds": [0.05, 0.1, 0.95, 0.55],
                "row_axis": "horizontal",
                "orientation": "random",
                "surface_policy": {},
            },
        }]}
        out = {"edit_plan": {"hero_edits": [{
            "op": "add",
            "factory": "FlowerFactory",
            "n": 25,
            "code_from": "add_sunflowers",
            "screen_positions": [[0.5, 0.5]] * 25,
        }], "extra_edits": []}, "appearance": {"placements": []}}
        result = normalize_execution_contract(
            out, plan, {"raycast_surfaces": [], "available_surface_types": []})
        add = result["edit_plan"]["hero_edits"][0]
        self.assertEqual(add["n"], 60)
        self.assertEqual(add["placement_spec"]["arrangement"], "field")
        self.assertEqual(add["placement_spec"]["proxy_dimensions_m"], [0.22, 0.22, 1.8])
        self.assertEqual(len(add["screen_positions"]), 60)
        self.assertEqual(add["screen_positions_source"],
                         "deterministic_map_first_seed")

    def test_field_broad_search_does_not_inflate_slack_target(self):
        self.assertEqual(
            placement_region_contract.outdoor_broad_search_target_capacity(
                required_capacity=60, arrangement="field"),
            60,
        )
        self.assertEqual(
            placement_region_contract.outdoor_broad_search_target_capacity(
                required_capacity=60, arrangement="scatter"),
            90,
        )

    def test_proxy_dimensions_are_validated_and_preserved(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "single",
            "proxy_dimensions_m": [8.0, 3.0, 2.5],
        }, count=1, anchoring="submerged")
        self.assertEqual(spec["proxy_dimensions_m"], [8.0, 3.0, 2.5])
        with self.assertRaisesRegex(ValueError, "proxy_dimensions_m"):
            contract.normalize_placement_spec({
                "arrangement": "single",
                "proxy_dimensions_m": [8.0, -3.0, 2.5],
            }, count=1, anchoring="submerged")

    def test_outdoor_loose_metadata_is_preserved_but_not_advisory(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "single",
            "outdoor_ultra_loose_relaxation": {
                "applied": True,
                "scope": "outdoor_composer_only",
            },
        }, count=1, anchoring="conform")
        self.assertFalse(contract.is_outdoor_ultra_loose_placement_spec(spec))
        self.assertEqual(
            spec["outdoor_ultra_loose_relaxation"]["scope"],
            "outdoor_composer_only")

    def test_outdoor_loose_advisory_flag_is_ignored(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "single",
            "outdoor_ultra_loose_relaxation": {
                "applied": True,
                "scope": "outdoor_composer_only",
                "review_constraints_advisory": True,
            },
        }, count=1, anchoring="conform")
        self.assertFalse(contract.is_outdoor_ultra_loose_placement_spec(spec))
        map_data = {
            "outdoor_ultra_loose_relaxation": {
                "applied": True,
                "scope": "outdoor_composer_only",
                "review_constraints_advisory": True,
            },
        }
        self.assertFalse(is_outdoor_ultra_loose_map(map_data))

    def test_legacy_outdoor_ultra_loose_recovers_hard_policy(self):
        edit = {
            "op": "add",
            "factory": "IglooFactory",
            "code_from": "igloo",
            "n": 1,
            "anchoring": "conform",
            "min_sep": 0.25,
            "placement_spec": {
                "arrangement": "single",
                "screen_bounds": [0.05, 0.05, 0.95, 0.90],
                "proxy_dimensions_m": [0.1, 0.1, 0.1],
                "surface_policy": {
                    "required_surface_types": [],
                    "forbidden_surface_types": [],
                    "required_surface_names": [],
                    "forbidden_surface_names": [],
                    "max_slope_deg": 90.0,
                    "require_full_footprint_support": False,
                },
                "outdoor_ultra_loose_relaxation": {
                    "applied": True,
                    "mode": "ultra_loose",
                    "original_surface_policy": {
                        "required_surface_types": ["ice", "snow"],
                        "forbidden_surface_types": ["liquid"],
                        "required_surface_names": ["CollectiveTransparentTerrain"],
                        "forbidden_surface_names": ["liquid"],
                        "max_slope_deg": 15.0,
                        "require_full_footprint_support": True,
                    },
                    "original_screen_bounds": [0.3, 0.42, 0.7, 0.62],
                    "original_proxy_dimensions_m": [3.0, 3.0, 2.0],
                    "original_min_sep": 2.5,
                    "planning_proxy_dimensions_m": [0.1, 0.1, 0.1],
                    "strict_rules_relaxed": ["surface_policy", "min_sep"],
                },
            },
        }
        recovery = contract.recover_legacy_outdoor_ultra_loose_edit(edit)
        self.assertTrue(recovery["applied"])
        self.assertEqual(edit["min_sep"], 2.5)
        self.assertEqual(
            edit["placement_spec"]["screen_bounds"], [0.3, 0.42, 0.7, 0.62])
        self.assertEqual(
            edit["placement_spec"]["proxy_dimensions_m"], [3.0, 3.0, 2.0])
        policy = edit["placement_spec"]["surface_policy"]
        self.assertEqual(policy["forbidden_surface_names"], ["liquid"])
        self.assertTrue(policy["require_full_footprint_support"])
        self.assertNotIn(
            "outdoor_ultra_loose_relaxation", edit["placement_spec"])

    def test_missing_proxy_dimensions_infer_small_insect_defaults(self):
        self.assertEqual(
            contract.inferred_proxy_dimensions(
                factory="BeetleFactory",
                code_from="add_bees",
                anchoring="airborne",
                semantic_target="open air",
            ),
            [0.5, 0.5, 0.35],
        )
        self.assertEqual(
            contract.inferred_proxy_dimension_source(
                factory="BeetleFactory",
                code_from="add_bees",
                anchoring="airborne",
                semantic_target="open air",
            ),
            "semantic_small_insect_fallback",
        )
        self.assertEqual(
            contract.default_min_sep(
                factory="BeetleFactory",
                code_from="add_bees",
                anchoring="airborne",
                semantic_target="open air",
            ),
            0.8,
        )
        self.assertIsNone(contract.inferred_proxy_dimensions(
            factory="HerbivoreFactory",
            code_from="add_giraffes",
            anchoring="conform",
            semantic_target="forest floor",
        ))
        self.assertEqual(
            contract.default_min_sep(
                factory="HerbivoreFactory",
                code_from="add_giraffes",
                anchoring="conform",
                semantic_target="forest floor",
            ),
            2.5,
        )

    def test_large_ground_relaxation_targets_only_big_grounded_edits(self):
        attempts = large_ground_relaxation_attempts(
            factory="HerbivoreFactory",
            code_from="add_giraffes",
            anchoring="conform",
            semantic_target="forest floor between trees",
            proxy_dimensions_m=[3.0, 1.5, 5.0],
            surface_policy={
                "max_slope_deg": 20,
                "require_full_footprint_support": True,
            },
        )
        self.assertGreaterEqual(len(attempts), 5)
        self.assertEqual(attempts[0]["max_slope_deg"], 25.0)
        self.assertTrue(attempts[0]["require_full_footprint_support"])
        self.assertTrue(any(
            attempt["obstacle_policy"] == "hard_scene_obstacles"
            for attempt in attempts))
        self.assertTrue(any(
            not attempt["require_full_footprint_support"]
            for attempt in attempts))

        self.assertFalse(is_large_ground_proxy(
            factory="BeeFactory",
            code_from="add_bees",
            anchoring="airborne",
            semantic_target="open air",
            proxy_dimensions_m=[0.5, 0.5, 0.35],
        ))
        self.assertFalse(is_large_ground_proxy(
            factory="FlowerFactory",
            code_from="cliffside_flowers",
            anchoring="tilted",
            semantic_target="cliff wall flowers",
            proxy_dimensions_m=[2.2, 2.2, 1.2],
        ))
        self.assertFalse(is_large_ground_proxy(
            factory="CarnivoreFactory",
            code_from="monkeys",
            anchoring="conform",
            semantic_target="rocky cliff ledges",
            proxy_dimensions_m=[0.8, 0.4, 0.9],
        ))

    def test_large_ground_relaxation_keeps_structures_on_full_support(self):
        self.assertTrue(requires_full_footprint_structure_support(
            factory="TentFactory",
            code_from="tent",
            semantic_target="snowy ground",
        ))
        attempts = large_ground_relaxation_attempts(
            factory="TentFactory",
            code_from="tent",
            anchoring="conform",
            semantic_target="flat campsite ground",
            proxy_dimensions_m=[2.6, 2.4, 1.6],
            surface_policy={
                "max_slope_deg": 38,
                "require_full_footprint_support": True,
            },
        )
        self.assertTrue(attempts)
        self.assertTrue(all(
            attempt["require_full_footprint_support"]
            for attempt in attempts))
        self.assertTrue(all(
            attempt["max_slope_deg"] >= 38.0
            for attempt in attempts))

    def test_large_ground_relaxation_restores_structure_full_support(self):
        attempts = large_ground_relaxation_attempts(
            factory="IglooFactory",
            code_from="igloo",
            anchoring="conform",
            semantic_target="ice floe",
            proxy_dimensions_m=[3.0, 3.0, 2.0],
            surface_policy={
                "max_slope_deg": 15,
                "require_full_footprint_support": False,
            },
        )
        self.assertTrue(attempts)
        self.assertTrue(all(
            attempt["require_full_footprint_support"]
            for attempt in attempts))

    def test_soft_scene_obstacle_classification_keeps_hard_obstacles(self):
        self.assertTrue(is_soft_scene_obstacle(
            label="PineTreeFactory",
            source="evaluated_instance",
            object_name="PineTreeFactory.001",
        ))
        self.assertTrue(is_soft_scene_obstacle(
            label="orchid_cluster",
            source="evaluated_scene_object",
            object_name="colorful_orchid_patch",
        ))
        self.assertFalse(is_soft_scene_obstacle(
            label="BlenderRockFactory",
            source="evaluated_instance",
            object_name="BlenderRockFactory.004",
        ))
        self.assertTrue(is_soft_scene_obstacle(
            label="TreeFactory",
            source="placeholders:TreeFactory(123)",
            object_name="TreeFactory(123).spawn_placeholder(0)",
        ))

    def test_placeholder_collection_claimed_by_replacement_attrs(self):
        plan = {"hero_edits": [{
            "op": "attrs",
            "target_factory": "TreeFactory(3040046)",
            "code_from": "replace_trees_palm",
        }]}
        self.assertTrue(placeholder_collection_claimed_by_plan(
            plan,
            collection="placeholders:TreeFactory(3040046)",
            factory="TreeFactory",
            seed=3040046,
        ))
        self.assertFalse(placeholder_collection_claimed_by_plan(
            plan,
            collection="placeholders:TreeFactory(99)",
            factory="TreeFactory",
            seed=99,
        ))

    def test_placeholder_population_intent_keeps_replace_and_skips_latent(self):
        self.assertEqual(
            placement_population_intent({
                "realized_at": "populate",
                "added_this_plan": 2,
                "population_target_count": 2,
            }),
            "requested_add",
        )
        self.assertEqual(
            placement_population_intent({
                "realized_at": "populate",
                "added_this_plan": 0,
                "code_from": "replace_trees_palm",
                "population_target_count": 271,
            }),
            "requested_replace",
        )
        self.assertTrue(placement_should_materialize({
            "realized_at": "populate",
            "added_this_plan": 0,
            "population_target_count": 3,
        }))
        self.assertFalse(placement_should_materialize({
            "realized_at": "populate",
            "added_this_plan": 0,
            "population_target_count": 0,
        }))
        self.assertEqual(
            placement_population_intent({
                "realized_at": "populate",
                "population_targets": [
                    {"object": "TreeFactory(1).spawn_placeholder(0)",
                     "will_populate": False},
                ],
            }),
            "latent_unmodified_skip",
        )
        self.assertTrue(placement_should_materialize({
            "realized_at": "populate",
            "population_targets": [
                {"object": "TreeFactory(1).spawn_placeholder(0)",
                 "will_populate": False},
                {"object": "TreeFactory(1).spawn_placeholder(1)",
                 "will_populate": True},
            ],
        }))

    def test_placeholder_population_intent_preserves_legacy_manifests(self):
        self.assertTrue(placement_should_materialize({
            "realized_at": "populate",
            "added_this_plan": 0,
            "code": None,
        }))
        self.assertTrue(placement_should_materialize({
            "realized_at": "populate",
            "population_targets": ["legacy_placeholder_name"],
        }))
        self.assertEqual(
            placement_population_intent({
                "realized_at": "coarse",
                "added_this_plan": 0,
            }),
            "coarse_realized",
        )

    def test_real_asset_uses_one_uniform_fit_inside_proxy(self):
        factor = contract.uniform_proxy_fit_scale(
            [10.0, 2.0, 2.0], [7.5, 2.6, 2.8])
        self.assertAlmostEqual(factor, 0.75)
        fitted = [dimension * factor for dimension in [10.0, 2.0, 2.0]]
        self.assertEqual(fitted, [7.5, 1.5, 1.5])
        self.assertTrue(all(
            actual <= target
            for actual, target in zip(fitted, [7.5, 2.6, 2.8])))
        with self.assertRaisesRegex(ValueError, "actual_dimensions"):
            contract.uniform_proxy_fit_scale(
                [10.0, 0.0, 2.0], [7.5, 2.6, 2.8])

    def test_blender_asset_aligns_long_axis_before_filling_proxy(self):
        actual = [1.0088, 2.6650, 1.0513]
        proxy = [4.0, 1.8, 2.0]
        self.assertEqual(
            contract.front_axis_to_proxy_heading_quarter_turns("-Y"), 1)
        self.assertEqual(
            contract.front_axis_to_proxy_heading_quarter_turns("+X"), 0)
        aligned = [actual[1], actual[0], actual[2]]
        factor = contract.uniform_proxy_fit_scale(aligned, proxy)
        self.assertAlmostEqual(aligned[0] * factor, proxy[0])
        self.assertLessEqual(aligned[1] * factor, proxy[1])
        self.assertLessEqual(aligned[2] * factor, proxy[2])

    def test_heading_fit_does_not_rotate_torii_width_into_proxy_depth(self):
        actual = [3.891, 0.641, 3.872]
        proxy = [4.0, 1.5, 5.0]
        turns = contract.best_horizontal_proxy_fit_quarter_turns(
            actual, proxy, "-Y")
        self.assertEqual(turns, 0)
        factor = contract.uniform_proxy_fit_scale(actual, proxy)
        self.assertGreater(factor, 1.0)
        self.assertAlmostEqual(actual[0] * factor, proxy[0], places=6)

    def test_heading_fit_still_rotates_submarine_long_axis_into_proxy_length(self):
        actual = [1.0088, 2.6650, 1.0513]
        proxy = [4.0, 1.8, 2.0]
        self.assertEqual(
            contract.best_horizontal_proxy_fit_quarter_turns(
                actual, proxy, "-Y"),
            1)

    def test_school_requires_multiple_submerged_instances(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "school",
        }, count=6, anchoring="submerged")
        self.assertEqual(spec["arrangement"], "school")
        with self.assertRaisesRegex(ValueError, "requires anchoring='submerged'"):
            contract.normalize_placement_spec({
                "arrangement": "school",
            }, count=6, anchoring="conform")

    def test_agent_point_must_stay_in_declared_bounds(self):
        spec = contract.normalize_placement_spec({
            "arrangement": "single",
            "screen_bounds": [0.2, 0.2, 0.4, 0.5],
        }, count=1, anchoring="conform")
        # out-of-bounds agent points are clamped into screen_bounds (warn-and-continue),
        # they no longer abort the run
        pts = contract.normalize_screen_positions([[0.8, 0.3]], spec=spec, count=1)
        x, y = pts[0]
        self.assertTrue(0.2 <= x <= 0.4 and 0.2 <= y <= 0.5, pts)

    def test_single_rejects_multiple_instances(self):
        with self.assertRaisesRegex(ValueError, "requires count=1"):
            contract.normalize_placement_spec(
                {"arrangement": "single"}, count=2, anchoring="conform")

    def test_only_agent_patch_changes_composer_position(self):
        plan = {"hero_edits": [{
            "op": "add",
            "factory": "BearFactory",
            "n": 1,
            "anchoring": "conform",
            "code_from": "polar_bear",
            "world_motion": {"preset": "ground_wander", "intensity": "subtle"},
            "placement_spec": {
                "arrangement": "single",
                "semantic_target": "ice floe",
                "screen_region": "lower_center",
                "screen_bounds": [0.3, 0.2, 0.7, 0.55],
            },
            "screen_positions": [[0.5, 0.375]],
        }]}
        revised, _applied = validate_and_apply({
            "decision": "revise",
            "placements": [{
                "edit_id": "polar_bear",
                "screen_positions": [[0.38, 0.31]],
            }],
        }, plan, apply_revision=True)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_positions"], [[0.38, 0.31]])
        self.assertEqual(
            edit["world_motion"],
            {"preset": "ground_wander", "intensity": "subtle"},
        )

    def test_agent_can_revise_bounded_world_motion_without_moving_start(self):
        plan = {"hero_edits": [{
            "op": "add",
            "factory": "CarnivoreFactory",
            "n": 1,
            "anchoring": "conform",
            "code_from": "polar_bear",
            "world_motion": {
                "preset": "ground_forward", "intensity": "subtle", "params": {}},
            "placement_spec": {
                "arrangement": "single",
                "semantic_target": "visible ice floe",
                "screen_bounds": [0.1, 0.1, 0.9, 0.8],
            },
            "screen_positions": [[0.2, 0.35]],
        }]}
        revised, applied = validate_and_apply({
            "decision": "revise",
            "placements": [{
                "edit_id": "polar_bear",
                "world_motion_params": {
                    "heading_deg": 180,
                    "speed_scale": 0.5,
                },
            }],
        }, plan, apply_revision=True)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_positions"], [[0.2, 0.35]])
        self.assertEqual(edit["world_motion"]["params"], {
            "heading_deg": 180.0,
            "speed_scale": 0.5,
        })
        self.assertIsNone(applied[0]["screen_positions"])

    def test_agent_reposition_marks_visible_surface_projection(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "CarnivoreFactory", "n": 1,
            "anchoring": "conform", "code_from": "polar_bear",
            "world_motion": {"preset": "none", "intensity": "subtle", "params": {}},
            "placement_spec": {"arrangement": "single"},
            "positions": [[1, 2, 3]],
        }]}
        revised, _ = validate_and_apply({
            "decision": "revise",
            "placements": [{
                "edit_id": "polar_bear", "screen_positions": [[0.5, 0.4]],
            }],
        }, plan, apply_revision=True)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_position_projection"], "visible_surface")
        self.assertNotIn("positions", edit)

    def test_candidate_request_can_expand_search_bounds_without_moving_objects(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "CarnivoreFactory", "n": 2,
            "anchoring": "conform", "code_from": "polar_bears",
            "placement_spec": {
                "arrangement": "scatter",
                "screen_bounds": [0.2, 0.1, 0.6, 0.4],
            },
            "screen_positions": [[0.3, 0.2], [0.5, 0.2]],
        }]}
        requests = validate_candidate_requests({
            "decision": "revise",
            "placements": [{
                "edit_id": "polar_bears",
                "instance_indices": [1],
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            }],
        }, plan)
        revised, applied = apply_candidate_request_bounds(plan, requests)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["screen_positions"], [[0.3, 0.2], [0.5, 0.2]])
        self.assertEqual(
            edit["placement_spec"]["screen_bounds"],
            [0.05, 0.05, 0.95, 0.9],
        )
        self.assertEqual(len(applied), 1)

    def test_prior_none_feedback_is_compact(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate_decision.json"
            path.write_text(json.dumps({
                "decision": "none",
                "reason": "all candidates were crowded",
                "candidate_payload": {
                    "edit_id": "polar_bears", "instance_index": 1,
                    "candidates": [{"large": "omitted"}],
                },
            }))
            self.assertEqual(compact_candidate_feedback([path]), [{
                "edit_id": "polar_bears",
                "instance_index": 1,
                "decision": "none",
                "candidate_id": None,
                "reason": "all candidates were crowded",
            }])


class CriticReplyParsingTest(unittest.TestCase):
    """A benchmark case must not die because a reply carried stray text.

    Each case here reproduces a real 2026-08-13 10-case benchmark failure or
    guards a near miss found while fixing them.
    """

    def test_plain_object_still_parses(self):
        self.assertEqual(
            extract_json('{"decision":"none","candidate_id":null,"reason":"r"}'),
            {"decision": "none", "candidate_id": None, "reason": "r"})

    def test_second_object_wins_instead_of_extra_data_crash(self):
        # Case 05 desert died here: the old greedy match spanned both objects.
        self.assertEqual(
            extract_json(
                '{"decision":"select","candidate_id":"B3-C1","reason":"draft"}\n\n'
                '{"decision":"select","candidate_id":"B3-C4","reason":"final"}'
            )["candidate_id"],
            "B3-C4")

    def test_unbalanced_prose_brace_does_not_swallow_the_decision(self):
        self.assertEqual(
            extract_json(
                "Reading the sheet {note that B0-C2 is closest\n"
                '{"decision":"none","candidate_id":null,"reason":"all steep"}'
            )["reason"],
            "all steep")

    def test_fenced_and_nested_objects_parse(self):
        self.assertEqual(
            extract_json(
                '```json\n{"decision":"revise","placements":'
                '[{"edit_id":"goats","instance_indices":[0]}]}\n```'
            )["placements"][0]["edit_id"],
            "goats")

    def test_brace_inside_reason_string_is_not_structural(self):
        self.assertEqual(
            extract_json(
                '{"decision":"none","candidate_id":null,'
                '"reason":"box {B1-C2} spans a \\" gap"}'
            )["decision"],
            "none")

    def test_truncated_trailing_object_is_ignored(self):
        self.assertEqual(
            extract_json(
                '{"decision":"none","candidate_id":null,"reason":"z"}\n{"deci'
            )["reason"],
            "z")

    def test_reply_without_json_still_raises(self):
        with self.assertRaises(ValueError):
            extract_json("I cannot pick any of these.")

    def test_decision_verb_is_case_and_space_tolerant(self):
        self.assertEqual(
            normalize_decision_kind(" Select ", {"select", "none"}), "select")
        with self.assertRaises(ValueError):
            normalize_decision_kind("maybe", {"select", "none"})
    def test_agent_can_move_one_instance_without_touching_its_sibling(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "CarnivoreFactory", "n": 2,
            "anchoring": "conform", "code_from": "polar_bear",
            "placement_spec": {
                "arrangement": "scatter",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            },
            "screen_positions": [[0.24, 0.30], [0.66, 0.36]],
        }]}
        revised, applied = validate_and_apply({
            "decision": "revise",
            "placements": [{
                "edit_id": "polar_bear",
                "instance_positions": [{
                    "instance_index": 0,
                    "screen_position": [0.20, 0.20],
                }],
            }],
        }, plan, apply_revision=True)
        self.assertEqual(
            revised["hero_edits"][0]["screen_positions"],
            [[0.20, 0.20], [0.66, 0.36]],
        )
        self.assertEqual(applied[0]["instance_positions"][0]["instance_index"], 0)

    def test_image_guided_selection_updates_every_instance(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "ChairFactory", "n": 2,
            "anchoring": "upright", "code_from": "wall_chairs",
            "placement_spec": {
                "arrangement": "row",
                "semantic_target": "three chairs against the living room wall",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            },
            "screen_positions": [[0.20, 0.20], [0.80, 0.20]],
        }]}
        rows = normalize_image_selection({
            "decision": "place_from_image",
            "placements": [
                {
                    "edit_id": "wall_chairs", "instance_index": 0,
                    "screen_position": [0.22, 0.62],
                    "placement_mode": "back_to_wall",
                    "support_kind": "floor",
                    "reason": "left chair near the back wall",
                },
                {
                    "edit_id": "wall_chairs", "instance_index": 1,
                    "screen_position": [0.72, 0.62],
                    "placement_mode": "back_to_wall",
                    "support_kind": "floor",
                    "reason": "right chair near the back wall",
                },
            ],
        }, plan)
        revised, application = apply_image_selection(plan, rows)
        edit = revised["hero_edits"][0]
        self.assertEqual(
            edit["screen_positions"], [[0.22, 0.62], [0.72, 0.62]])
        self.assertEqual(edit["screen_positions_source"],
                         "composer_image_semantic")
        self.assertEqual(edit["screen_position_projection"], "visible_surface")
        self.assertEqual(edit["placement_spec"]["placement_mode"],
                         "back_to_wall")
        self.assertEqual(len(application["applied"]), 2)

    def test_image_guided_selection_keeps_plan_back_to_wall_over_row(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "ChairFactory", "n": 1,
            "anchoring": "conform", "code_from": "wall_chairs",
            "placement_spec": {
                "arrangement": "row",
                "semantic_target": "chairs against the wall",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
                "placement_mode": "back_to_wall",
            },
            "screen_positions": [[0.20, 0.20]],
        }]}
        rows = normalize_image_selection({
            "decision": "place_from_image",
            "placements": [{
                "edit_id": "wall_chairs",
                "instance_index": 0,
                "screen_position": [0.22, 0.62],
                "placement_mode": "row",
                "support_kind": "floor",
            }],
        }, plan)
        revised, _application = apply_image_selection(plan, rows)
        self.assertEqual(rows[0]["placement_mode"], "back_to_wall")
        self.assertEqual(
            revised["hero_edits"][0]["placement_spec"]["placement_mode"],
            "back_to_wall")

    def test_image_guided_back_to_wall_snaps_to_feasible_support_plane(self):
        rows = [
            {
                "edit_id": "wall_chairs", "instance_index": 0,
                "screen_position": [0.22, 0.62],
                "placement_mode": "back_to_wall",
                "support_kind": "floor",
            },
            {
                "edit_id": "wall_chairs", "instance_index": 1,
                "screen_position": [0.72, 0.62],
                "placement_mode": "back_to_wall",
                "support_kind": "floor",
            },
        ]
        maps = {"wall_chairs": {"cells": [
            {
                "status": "feasible", "cell_id": "front_left",
                "screen_position": [0.20, 0.30], "support_plane_id": "P01",
                "region_id": "R01", "surface_types": ["floor"],
                "hit_surface": "living-room_0/0.floor",
                "world_position": [1.0, 0.0, 0.0],
                "surface_normal": [0.0, 0.0, 1.0],
                "slope_deg": 0.0,
                "clearance_steps": 3, "feasible_yaws": [0.0],
            },
            {
                "status": "feasible", "cell_id": "back_left",
                "screen_position": [0.25, 0.75], "support_plane_id": "P01",
                "region_id": "R01", "surface_types": ["floor"],
                "hit_surface": "living-room_0/0.floor",
                "world_position": [2.0, 0.0, 0.0],
                "surface_normal": [0.0, 0.0, 1.0],
                "slope_deg": 0.0,
                "clearance_steps": 2, "feasible_yaws": [0.0],
            },
            {
                "status": "feasible", "cell_id": "back_right",
                "screen_position": [0.72, 0.76], "support_plane_id": "P01",
                "region_id": "R01", "surface_types": ["floor"],
                "hit_surface": "living-room_0/0.floor",
                "world_position": [4.0, 0.0, 0.0],
                "surface_normal": [0.0, 0.0, 1.0],
                "slope_deg": 0.0,
                "clearance_steps": 2, "feasible_yaws": [0.0],
            },
        ]}}
        snapped, notes = snap_selection_to_maps(rows, maps)
        self.assertEqual(
            [row["cell_id"] for row in snapped],
            ["back_left", "back_right"],
        )
        self.assertEqual(snapped[0]["support_patch_id"], "P01")
        self.assertIn("support_kind=floor", notes[0]["filters"])
        self.assertIn("unused_global_cell", notes[0]["filters"])
        self.assertIn("cross_edit_non_overlapping", notes[0]["filters"])
        plan = {"hero_edits": [{
            "op": "add", "factory": "ChairFactory", "n": 2,
            "anchoring": "upright", "code_from": "wall_chairs",
            "placement_spec": {
                "arrangement": "row",
                "semantic_target": "living room wall",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            },
            "screen_positions": [[0.20, 0.20], [0.80, 0.20]],
        }]}
        revised, _application = apply_image_selection(plan, snapped)
        edit = revised["hero_edits"][0]
        self.assertEqual(edit["positions_source"],
                         "composer_image_semantic_map_snap")
        self.assertEqual(edit["positions"], [[2.0, 0.0, 0.0],
                                             [4.0, 0.0, 0.0]])
        self.assertEqual(
            edit["explicit_position_supports"][0]["surface"],
            "living-room_0/0.floor")
        resnap_rows = image_selection_from_plan_screen_positions(revised)
        self.assertEqual(
            [(row["edit_id"], row["instance_index"])
             for row in resnap_rows],
            [("wall_chairs", 0), ("wall_chairs", 1)])
        self.assertEqual(resnap_rows[0]["screen_position"], [0.25, 0.75])

    def test_image_guided_snap_uses_camera_facing_quarter_turn_yaw(self):
        rows = [{
            "edit_id": "chair", "instance_index": 0,
            "screen_position": [0.5, 0.5],
            "placement_mode": "center",
            "support_kind": "floor",
        }]
        maps = {"chair": {
            "camera": {"world_position": [0.0, 5.0, 2.0]},
            "cells": [{
                "status": "feasible", "cell_id": "center",
                "screen_position": [0.5, 0.5], "support_plane_id": "P01",
                "region_id": "R01", "surface_types": ["floor"],
                "hit_surface": "living-room_0/0.floor",
                "world_position": [0.0, 0.0, 0.0],
                "surface_normal": [0.0, 0.0, 1.0],
                "slope_deg": 0.0,
                "clearance_steps": 3,
                "feasible_yaws": [0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0],
            }],
        }}
        snapped, notes = snap_selection_to_maps(rows, maps)
        self.assertAlmostEqual(snapped[0]["placement_yaw"], math.pi / 2.0)
        self.assertEqual(snapped[0]["placement_yaw_degrees"], 90.0)
        self.assertEqual(notes[0]["placement_yaw_degrees"], 90.0)

    def test_image_guided_selection_writes_selected_indoor_surface_policy(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "MugFactory", "n": 1,
            "anchoring": "upright", "code_from": "mug",
            "placement_spec": {
                "arrangement": "single",
                "semantic_target": "visible support",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
                "surface_policy": {
                    "required_surface_types": ["floor", "tabletop"],
                    "forbidden_surface_names": [
                        "TableDiningFactory(1).spawn_asset(2)"
                    ],
                },
            },
            "screen_positions": [[0.40, 0.55]],
        }]}
        rows = [{
            "edit_id": "mug", "instance_index": 0,
            "screen_position": [0.42, 0.58],
            "placement_mode": "center",
            "support_kind": "tabletop",
            "surface_types": ["tabletop"],
            "hit_surface": "TableDiningFactory(1).spawn_asset(2)",
            "candidate_world_position": [1.0, 2.0, 0.75],
            "surface_normal": [0.0, 0.0, 1.0],
            "slope_deg": 0.0,
            "clearance_steps": 2,
            "feasible_yaws": [0.0, math.pi / 2.0],
        }]
        revised, _application = apply_image_selection(plan, rows)
        policy = revised["hero_edits"][0]["placement_spec"]["surface_policy"]
        self.assertEqual(policy["required_surface_types"], ["tabletop"])
        self.assertEqual(
            policy["required_surface_names"],
            ["TableDiningFactory(1).spawn_asset(2)"])
        self.assertEqual(policy["forbidden_surface_types"], [])
        self.assertEqual(policy["forbidden_surface_names"], [])
        self.assertEqual(policy["max_slope_deg"], 20.0)
        self.assertTrue(policy["require_full_footprint_support"])

    def test_region_map_yaw_candidates_are_quarter_turns(self):
        from stage_exec import build_placement_region_map as region_map

        self.assertEqual(region_map.YAW_CANDIDATES, (
            0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0))
        self.assertEqual(region_map.VOLUME_YAW_CANDIDATES,
                         region_map.YAW_CANDIDATES)

    def test_indoor_semantic_choice_keeps_explicit_non_floor_surface(self):
        from stage_exec import build_placement_region_map as region_map

        original_surface_type_names = region_map._surface_type_names
        region_map._surface_type_names = lambda _scene_name: {
            "floor": ["bedroom_0/0.floor"],
            "tabletop": ["SimpleDeskFactory(1).spawn_asset(2)"],
            "countertop": ["KitchenCabinetFactory(1).spawn_asset(2)"],
            "shelf": [],
            "bed_top": ["BedFactory(1).spawn_asset(2)"],
            "seat": [],
        }
        try:
            edit = {"placement_spec": {"surface_policy": {
                "required_surface_types": ["bed_top"],
                "forbidden_surface_types": ["floor"],
                "required_surface_names": ["BedFactory(1).spawn_asset(2)"],
                "forbidden_surface_names": ["bedroom_0/0.floor"],
            }}}
            metadata = region_map._enable_indoor_semantic_surface_choice(
                edit, "Bedroom")
        finally:
            region_map._surface_type_names = original_surface_type_names

        self.assertIsNone(metadata)
        policy = edit["placement_spec"]["surface_policy"]
        self.assertEqual(policy["required_surface_types"], ["bed_top"])
        self.assertEqual(policy["forbidden_surface_types"], ["floor"])

    def test_indoor_surface_fallback_preserves_forbidden_surface(self):
        from stage_exec.build_placement_region_map import _clone_with_surface_policy

        edit = {"placement_spec": {"surface_policy": {
            "required_surface_types": ["countertop"],
            "forbidden_surface_types": ["floor"],
            "required_surface_names": ["counter"],
            "forbidden_surface_names": ["kitchen_0/0.floor"],
        }}}
        cloned = _clone_with_surface_policy(
            edit,
            surface_type="floor",
            surface_names=["kitchen_0/0.floor"],
            require_full_footprint_support=True,
            full_frame=False,
        )
        policy = cloned["placement_spec"]["surface_policy"]
        self.assertEqual(policy["required_surface_types"], ["floor"])
        self.assertEqual(policy["forbidden_surface_types"], ["floor"])
        self.assertEqual(policy["forbidden_surface_names"], ["kitchen_0/0.floor"])

    def test_candidate_plan_keeps_other_instance_fixed_and_selection_updates_one(self):
        plan = {"hero_edits": [{
            "op": "add", "factory": "CarnivoreFactory", "n": 2,
            "n_masters": 2, "anchoring": "conform", "code_from": "polar_bear",
            "placement_spec": {
                "arrangement": "scatter",
                "semantic_target": "visible ice floe",
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
            },
            "screen_positions": [[0.24, 0.30], [0.66, 0.36]],
        }]}
        candidate_plan, metadata = build_candidate_plan(
            plan, edit_id="polar_bear", instance_index=0, count=10, batch=0)
        temporary = candidate_plan["hero_edits"][0]
        self.assertEqual(temporary["n"], 11)
        self.assertEqual(temporary["screen_positions"][0], [0.66, 0.36])
        self.assertEqual(len(temporary["placement_yaws"]), 11)
        self.assertIn("#1 - FIXED", temporary["placement_preview_labels"][0])
        chosen = metadata["candidates"][0]
        revised, applied = apply_candidate(plan, metadata, chosen["candidate_id"])
        final_points = revised["hero_edits"][0]["screen_positions"]
        self.assertEqual(final_points[0], chosen["screen_position"])
        self.assertEqual(final_points[1], [0.66, 0.36])
        self.assertEqual(applied["untouched_instances"], [1])

    def test_semantic_candidates_restore_requested_surface_before_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "region_map_index.json").write_text(json.dumps({
                "contract_version": 1,
                "scene_name": "LivingRoom",
                "scene_location": "indoor",
                "semantic_support_grid": {
                    "edit_id": "__semantic_support_grid",
                    "map_json": "semantic_support_grid.json",
                },
                "maps": [],
            }), encoding="utf-8")
            (root / "semantic_support_grid.json").write_text(json.dumps({
                "contract_version": 1,
                "grid": {"columns": 2, "rows": 2},
                "screen_bounds": [0.05, 0.05, 0.95, 0.9],
                "cells": [
                    {
                        "status": "feasible",
                        "cell_id": "C00_00",
                        "semantic_cell_id": "S001",
                        "support_plane_id": "P01",
                        "region_id": "R01",
                        "screen_position": [0.2, 0.2],
                        "hit_surface": "living-room_0/0.floor",
                        "world_position": [0.0, 0.0, 0.0],
                        "surface_normal": [0.0, 0.0, 1.0],
                        "slope_deg": 0.0,
                        "plane_quality_score": 1.0,
                        "clearance_steps": 3,
                    },
                    {
                        "status": "feasible",
                        "cell_id": "T001_00_00",
                        "semantic_cell_id": "S010",
                        "support_plane_id": "P02",
                        "region_id": "R02",
                        "screen_position": [0.45, 0.34],
                        "screen_polygon": [
                            [0.42, 0.32], [0.48, 0.32],
                            [0.48, 0.36], [0.42, 0.36],
                        ],
                        "hit_surface": "SofaFactory(123).spawn_asset(456)",
                        "world_position": [1.0, 2.0, 0.5],
                        "surface_normal": [0.0, 0.0, 1.0],
                        "slope_deg": 0.0,
                        "plane_quality_score": 0.8,
                        "clearance_steps": 1,
                    },
                ],
            }), encoding="utf-8")
            plan = {"hero_edits": [{
                "op": "add", "factory": "CushionFactory", "n": 1,
                "n_masters": 1, "anchoring": "upright",
                "code_from": "sofa_cushion",
                "placement_spec": {
                    "arrangement": "single",
                    "semantic_target": "sofa seat",
                    "screen_bounds": [0.05, 0.05, 0.95, 0.9],
                    "surface_policy": {
                        "required_surface_types": ["floor"],
                        "required_surface_names": ["living-room_0/0.floor"],
                        "forbidden_surface_types": [],
                        "forbidden_surface_names": [],
                        "max_slope_deg": 20,
                        "require_full_footprint_support": False,
                    },
                },
                "surface_fallback": {
                    "original_surface_policy": {
                        "required_surface_types": ["seat"],
                        "required_surface_names": [
                            "SofaFactory(123).spawn_asset(456)"],
                        "forbidden_surface_types": [],
                        "forbidden_surface_names": [],
                        "max_slope_deg": 20,
                        "require_full_footprint_support": False,
                    },
                },
                "screen_positions": [[0.2, 0.2]],
            }]}
            candidate_plan, metadata = build_candidate_plan(
                plan, edit_id="sofa_cushion", instance_index=0,
                count=2, batch=0,
                semantic_map_index=root / "region_map_index.json")
            self.assertEqual(metadata["candidates"][0]["surface_role"],
                             "matched_surface")
            self.assertEqual(metadata["candidates"][0]["surface_types"],
                             ["seat"])
            self.assertEqual(len(metadata["candidates"]), 1)
            self.assertTrue(metadata["uses_explicit_world_positions"])
            temporary = candidate_plan["hero_edits"][0]
            self.assertEqual(temporary["positions"][0], [1.0, 2.0, 0.5])
            self.assertEqual(len(temporary["placement_yaws"]), 1)
            self.assertEqual(
                temporary["explicit_position_supports"][0]["surface"],
                "SofaFactory(123).spawn_asset(456)")
            _floor_plan, floor_metadata = build_candidate_plan(
                plan, edit_id="sofa_cushion", instance_index=0,
                count=2, batch=1,
                semantic_map_index=root / "region_map_index.json")
            self.assertEqual(floor_metadata["candidates"][0]["surface_role"],
                             "floor_fallback")

            revised, applied = apply_candidate(
                plan, metadata, metadata["candidates"][0]["candidate_id"])
            edit = revised["hero_edits"][0]
            self.assertEqual(edit["positions"], [[1.0, 2.0, 0.5]])
            self.assertEqual(applied["world_position"], [1.0, 2.0, 0.5])
            self.assertEqual(
                edit["placement_spec"]["surface_policy"]["required_surface_types"],
                ["seat"])
            self.assertEqual(
                edit["placement_spec"]["surface_policy"]["required_surface_names"],
                ["SofaFactory(123).spawn_asset(456)"])

    def test_candidate_batches_do_not_repeat_points(self):
        bounds = [0.05, 0.05, 0.95, 0.9]
        first = candidate_points(bounds, count=10, batch=0, anchoring="conform")
        second = candidate_points(bounds, count=10, batch=1, anchoring="conform")
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertTrue(set(map(tuple, first)).isdisjoint(map(tuple, second)))

    def test_candidate_machine_filter_is_hard_and_dispersed(self):
        candidates = [{
            "candidate_id": f"B0-C{i}", "screen_position": point,
            "temporary_instance_index": i,
        } for i, point in enumerate(([0.1, 0.1], [0.2, 0.2], [0.8, 0.8]))]
        metadata = {"edit_id": "bear", "instance_index": 0,
                    "fixed_instances": [], "candidates": candidates}
        def audit(index, surface, supported=True):
            x, y = candidates[index]["screen_position"]
            return {"instance_index": index, "selected_visible_surface": surface,
                    "selected_surface_slope_deg": 0,
                    "all_frames_fully_visible": True,
                    "footprint_support": {"supported": supported},
                    "samples": [{"bounds_ndc": [x, y, x + .05, y + .05]}]}
        review = {"placements": [{"code_from": "bear", "anchors": [
            audit(0, "liquid"), audit(1, "OpaqueTerrain", False),
            audit(2, "OpaqueTerrain", True),
        ]}]}
        selected, rejected = filter_candidates(metadata, review, {
            "required_surface_names": [], "forbidden_surface_names": ["liquid"],
            "max_slope_deg": 20, "require_full_footprint_support": True,
        }, limit=10)
        self.assertEqual([x["candidate_id"] for x in selected], ["B0-C2"])
        self.assertEqual(len(rejected), 2)
        self.assertEqual(len(selected), 1)  # never pad with invalid points

    def test_world_motion_params_are_bounded(self):
        request = world_motion.validate_runtime_request({
            "preset": "ground_forward",
            "intensity": "normal",
            "params": {"heading_deg": 359.5, "speed_scale": 0.25},
        })
        self.assertEqual(request["params"]["heading_deg"], 359.5)
        self.assertEqual(request["params"]["speed_scale"], 0.25)
        with self.assertRaisesRegex(ValueError, "heading_deg"):
            world_motion.validate_runtime_request({
                "preset": "ground_forward", "params": {"heading_deg": 360}})
        with self.assertRaisesRegex(ValueError, "speed_scale"):
            world_motion.validate_runtime_request({
                "preset": "ground_forward", "params": {"speed_scale": 0.1}})


class SavedRoundOverlapTest(unittest.TestCase):
    def _violations(self, round_name):
        path = (ROOT / "output/_test/coarse_check/desert_giraffe_eagle_motorcycle"
                / "placement_review/rounds" / round_name / "placement_review.json")
        if not path.is_file():
            self.skipTest(f"archived calibration data missing: {path}")
        review = json.loads(path.read_text(encoding="utf-8"))
        items = [
            {"edit_id": row.get("code_from"), "object": audit.get("object"),
             "audit": audit}
            for row in review.get("placements", [])
            for audit in row.get("anchors", [])
        ]
        return visibility.projected_overlap_violations(items)

    def test_round_one_moderate_projection_is_not_occlusion_warning(self):
        violations = self._violations("round_01")
        self.assertEqual(violations, [])

    def test_round_two_has_no_requested_box_overlap(self):
        self.assertEqual(self._violations("round_02"), [])

    def test_world_box_overlap_metric(self):
        left = {"min": [0, 0, 0], "max": [2, 2, 2]}
        right = {"min": [1, 1, 1], "max": [3, 3, 3]}
        separate = {"min": [3, 3, 3], "max": [4, 4, 4]}
        self.assertAlmostEqual(
            visibility.box_overlap_of_smaller(left, right), 1.0 / 8.0)
        self.assertEqual(visibility.box_overlap_of_smaller(left, separate), 0.0)

    def test_projected_overlap_only_warns_above_seventy_percent(self):
        def item(name, bounds):
            return {"edit_id": name, "object": name, "audit": {"samples": [
                {"frame": 1, "bounds_ndc": bounds}]}}
        moderate = visibility.projected_overlap_violations([
            item("a", [0, 0, 1, 1]), item("b", [0.4, 0, 1.4, 1])])
        severe = visibility.projected_overlap_violations([
            item("a", [0, 0, 1, 1]), item("b", [0.2, 0, 1.2, 1])])
        self.assertEqual(moderate, [])
        self.assertEqual(len(severe), 1)
        self.assertAlmostEqual(severe[0]["max_overlap_of_smaller"], 0.8)

    def test_snapshot_requested_visibility_is_loose_by_default(self):
        class Anchor(dict):
            def __init__(self, name, index):
                super().__init__(bi_requested_placement_index=index)
                self.name = name

        scene = SimpleNamespace(
            frame_start=1,
            frame_end=6,
            objects=[Anchor("a", 0), Anchor("b", 1)],
        )

        original = visibility.audit_anchor

        def fake_audit(_scene, anchor, frames=None, visible_only=True):
            visible_frames = 1 if anchor.name == "a" else 2
            return {
                "object": anchor.name,
                "frame_count": 6,
                "visible_frames": visible_frames,
                "all_frames_visible": False,
                "fully_visible_frames": visible_frames,
                "all_frames_fully_visible": False,
                "samples": [],
            }

        try:
            visibility.audit_anchor = fake_audit
            report = visibility.snapshot_requested_visibility(scene)
        finally:
            visibility.audit_anchor = original

        self.assertEqual(report["minimum_visible_frames"], 2)
        self.assertEqual(
            report["failures"],
            ["placement #0 a: visible 1/6 frames (need >= 2)"],
        )

    def test_same_factory_collision_audit_filters_by_factory_name(self):
        class Scene:
            frame_start = 1
            frame_end = 1
            frame_current = 1

            def frame_set(self, frame):
                self.frame_current = frame

        class Anchor(dict):
            def __init__(self, name, factory, bounds):
                super().__init__(bi_requested_factory=factory)
                self.name = name
                self._bounds = bounds

        scene = Scene()
        anchors = [
            Anchor("a", "BirdFactory", {"min": [0, 0, 0], "max": [2, 2, 2]}),
            Anchor("b", "BirdFactory", {"min": [0, 0, 0], "max": [2, 2, 2]}),
            Anchor("c", "TreeFactory", {"min": [10, 10, 10], "max": [11, 11, 11]}),
        ]

        original_world_bounds = visibility.world_bounds
        original_projected_bounds = visibility.projected_bounds
        original_bpy = sys.modules.get("bpy")
        try:
            sys.modules["bpy"] = SimpleNamespace(
                context=SimpleNamespace(
                    view_layer=SimpleNamespace(update=lambda: None)))
            visibility.world_bounds = lambda _scene, anchor, visible_only=True: anchor._bounds
            visibility.projected_bounds = lambda _scene, anchor, visible_only=True: {
                "in_view": True}
            report = visibility.audit_world_collisions(
                scene, anchors, frames=[1], threshold=0.0, same_factory_only=True)
        finally:
            visibility.world_bounds = original_world_bounds
            visibility.projected_bounds = original_projected_bounds
            if original_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = original_bpy

        self.assertFalse(report["ok"])
        self.assertEqual(len(report["violations"]), 1)
        self.assertEqual(report["violations"][0]["factory"], "BirdFactory")
        self.assertEqual(report["violations"][0]["factories"], ["BirdFactory", "BirdFactory"])

    def test_candidate_request_limit_keeps_only_highest_priority_instances(self):
        from planner.placement_critic import validate_candidate_requests
        plan = {"hero_edits": [{
            "op": "add", "code_from": "crowd", "n": 4,
        }]}
        decision = {
            "decision": "revise",
            "placements": [{
                "edit_id": "crowd", "instance_indices": [0, 1, 2, 3],
            }],
        }
        self.assertEqual(
            validate_candidate_requests(decision, plan, max_requests=3),
            [
                {"edit_id": "crowd", "instance_index": 0},
                {"edit_id": "crowd", "instance_index": 1},
                {"edit_id": "crowd", "instance_index": 2},
            ],
        )

    def test_multi_support_embed_uses_highest_foot_gap(self):
        self.assertAlmostEqual(
            placement_modes.multi_support_embed_delta(
                [-0.10, -0.08, 0.72, 0.75], embed_depth=0.10),
            -0.85,
        )

    def test_multi_support_embed_requires_multiple_supports(self):
        self.assertIsNone(
            placement_modes.multi_support_embed_delta([0.4], embed_depth=0.10))

    def test_multi_support_embed_never_turns_negative_depth_into_lift(self):
        self.assertAlmostEqual(
            placement_modes.multi_support_embed_delta(
                [-0.20, -0.15], embed_depth=-1.0),
            0.0,
        )


class ProjectionFailureReportingTest(unittest.TestCase):
    """apply_plan must check every hero edit before reporting failures.

    apply_plan.py imports bpy at module scope, so this reads its AST instead of
    running it.  The regression it guards is behavioural, not cosmetic: while
    the handler re-raised, the hero loop stopped at the first rejected edit and
    machine_review.json listed only that edit's instances.  The 0824 arctic run
    rejected `penguins` first, so `igloo` was never projected at all and the
    composer — which may only move instances named in `failed_instances` — left
    the igloo sitting on liquid for all three rounds.
    """

    @staticmethod
    def hero_loop():
        import ast

        source = (ROOT / "stage_exec" / "apply_plan.py").read_text(
            encoding="utf-8")
        module = ast.parse(source)
        for index, node in enumerate(module.body):
            if (isinstance(node, ast.For)
                    and isinstance(node.target, ast.Tuple)
                    and ast.unparse(node.iter) == "enumerate(hero_edits)"):
                return module, index, node
        raise AssertionError("apply_plan.py no longer has a hero_edits loop")

    @staticmethod
    def projection_handlers(loop):
        import ast

        return [handler for handler in ast.walk(loop)
                if isinstance(handler, ast.ExceptHandler)
                and handler.type is not None
                and ast.unparse(handler.type).endswith(
                    "PlacementProjectionFailure")]

    def test_rejected_edit_does_not_abort_the_remaining_hero_edits(self):
        import ast

        _module, _index, loop = self.hero_loop()
        handlers = self.projection_handlers(loop)
        self.assertTrue(handlers, "hero loop no longer handles projection failures")
        for handler in handlers:
            self.assertFalse(
                [node for node in ast.walk(handler) if isinstance(node, ast.Raise)],
                "a rejected hero edit must be collected, not re-raised: "
                "re-raising skips the projection check for every later edit")
            self.assertIsInstance(
                handler.body[-1], ast.Continue,
                "the handler must fall through to the next hero edit")

    def test_collected_failures_are_raised_after_every_edit_is_checked(self):
        import ast

        module, loop_index, _loop = self.hero_loop()
        raised = [
            node for statement in module.body[loop_index + 1:]
            for node in ast.walk(statement)
            if isinstance(node, ast.Raise) and node.exc is not None
            and ast.unparse(node.exc).startswith(
                ("plc.PlacementProjectionFailure", "PlacementProjectionFailure"))
        ]
        self.assertTrue(
            raised,
            "collected projection failures must still fail the run, after the "
            "hero loop has checked every edit")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from stage_exec import validate_final_scene as contract


def mesh_object(*, hide_render=False, render_visible=True):
    return {
        "type": "MESH",
        "hide_render": hide_render,
        "render_visible": render_visible,
        "mesh": {"vertices": 10, "polygons": 8},
    }


class FinalSceneTerrainContractTest(unittest.TestCase):
    def test_collective_transparent_terrain_is_a_primary_fine_terrain(self):
        populated = {
            "objects": {
                "CollectiveTransparentTerrain": mesh_object(
                    hide_render=False, render_visible=True),
                "liquid": mesh_object(hide_render=False, render_visible=True),
            },
        }
        fine = {
            "objects": {
                "CollectiveTransparentTerrain": mesh_object(
                    hide_render=True, render_visible=False),
                "CollectiveTransparentTerrain_fine": mesh_object(
                    hide_render=False, render_visible=True),
                "liquid": mesh_object(hide_render=True, render_visible=False),
                "liquid_fine": mesh_object(hide_render=False, render_visible=True),
            },
        }

        self.assertEqual(
            contract.primary_terrain_bases(populated, fine),
            ["CollectiveTransparentTerrain"],
        )
        ok, detail = contract.nonempty_visible_mesh(
            fine["objects"], "CollectiveTransparentTerrain_fine")
        self.assertTrue(ok, detail)
        ok, detail = contract.coarse_terrain_hidden(
            fine["objects"], "CollectiveTransparentTerrain")
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()


class ParticleMasterInvarianceTest(unittest.TestCase):
    def test_particle_masters_are_not_scene_geometry(self):
        master = {"collections": [
            "assets:swapped:unique_assets:FishSchoolFactory(4755707)",
            "particleassets:swapped:unique_assets:FishSchoolFactory(4755707)"]}
        self.assertTrue(contract.is_particle_master(master))
        self.assertFalse(contract.is_particle_master(
            {"collections": ["unique_assets:TreeFactory(1)"]}))
        self.assertFalse(contract.is_particle_master({}))

    def test_placeholder_scaffolding_is_not_scene_geometry(self):
        skeleton = {"collections": ["placeholders:TreeFactory(9449143)"],
                    "parent": "TreeFactory(9449143).spawn_placeholder(5)"}
        self.assertTrue(contract.is_scaffolding(skeleton))
        self.assertFalse(contract.is_scaffolding(
            {"collections": ["unique_assets:TreeFactory(9449143)"]}))
        self.assertFalse(contract.is_scaffolding({}))


if __name__ == "__main__":
    unittest.main()

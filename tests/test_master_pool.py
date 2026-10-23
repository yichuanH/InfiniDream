from types import SimpleNamespace
import unittest

import master_pool


class MasterPoolCloneReferenceTest(unittest.TestCase):
    def test_animated_rigs_bypass_linked_clone_pool(self):
        self.assertEqual(master_pool.effective_pool_size(
            3, {"policy": "inherit", "mode": "run"}), 0)
        self.assertEqual(master_pool.effective_pool_size(
            3, {"policy": "none", "mode": None}), 3)

    def test_retargets_rig_constraints_and_driver_targets(self):
        source_armature = object()
        cloned_armature = object()
        source_target = object()
        cloned_target = object()

        modifier = SimpleNamespace(object=source_armature)
        object_constraint = SimpleNamespace(target=source_target)
        pose_constraint = SimpleNamespace(
            target=source_target, pole_target=source_target)
        driver_target = SimpleNamespace(id=source_target)
        cloned_mesh = SimpleNamespace(
            modifiers=[modifier],
            constraints=[object_constraint],
            pose=SimpleNamespace(bones=[SimpleNamespace(
                constraints=[pose_constraint])]),
            animation_data=SimpleNamespace(drivers=[SimpleNamespace(
                driver=SimpleNamespace(variables=[SimpleNamespace(
                    targets=[driver_target])]))]),
        )

        repaired = master_pool._retarget_clone_references({
            source_armature: cloned_armature,
            source_target: cloned_target,
            object(): cloned_mesh,
        })

        self.assertEqual(modifier.object, cloned_armature)
        self.assertEqual(object_constraint.target, cloned_target)
        self.assertEqual(pose_constraint.target, cloned_target)
        self.assertEqual(pose_constraint.pole_target, cloned_target)
        self.assertEqual(driver_target.id, cloned_target)
        self.assertEqual(repaired, 5)

    def test_external_references_are_preserved(self):
        external = object()
        modifier = SimpleNamespace(object=external)
        cloned_mesh = SimpleNamespace(
            modifiers=[modifier], constraints=[], pose=None,
            animation_data=None)

        repaired = master_pool._retarget_clone_references({object(): cloned_mesh})

        self.assertIs(modifier.object, external)
        self.assertEqual(repaired, 0)


if __name__ == "__main__":
    unittest.main()

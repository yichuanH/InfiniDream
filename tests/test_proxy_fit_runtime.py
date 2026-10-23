from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stage_exec"))

import proxy_fit_runtime


class _Matrix:
    def __init__(self, token="basis"):
        self.token = token
        self.was_reset = False

    def identity(self):
        self.was_reset = True

    def copy(self):
        return _Matrix(self.token)


class _Object(dict):
    def __init__(self, name, parent=None):
        super().__init__()
        self.name = name
        self.parent = parent
        self.matrix_parent_inverse = _Matrix("parent_inverse")
        self.matrix_basis = _Matrix("intended_basis")


class GeneratedRootParentingTest(unittest.TestCase):
    def test_identity_root_is_reestablished_under_placeholder(self):
        placeholder = _Object("requested_box")
        root = _Object("GeneratedFactory(7).spawn_asset(0)", placeholder)

        rows = proxy_fit_runtime.restore_identity_root_placeholder_inheritance(
            [(7, root)])

        self.assertTrue(root.matrix_parent_inverse.was_reset)
        self.assertEqual(root.matrix_basis.token, "intended_basis")
        self.assertFalse(root.matrix_basis.was_reset)
        self.assertIs(root.parent, placeholder)
        self.assertTrue(root["bi_identity_root_parenting_repaired"])
        self.assertEqual(rows, [{
            "asset": "GeneratedFactory(7).spawn_asset(0)",
            "placeholder": "requested_box",
            "contract": "generated_adapter_identity_root",
        }])

    def test_parentless_root_is_not_modified(self):
        root = _Object("unparented")

        rows = proxy_fit_runtime.restore_identity_root_placeholder_inheritance(
            [(0, root)])

        self.assertEqual(rows, [])
        self.assertFalse(root.matrix_parent_inverse.was_reset)
        self.assertFalse(root.matrix_basis.was_reset)


if __name__ == "__main__":
    unittest.main()

"""Blender runtime helpers for measuring generated geometry after proxy fit."""

from __future__ import annotations


def object_tree(root):
    """Yield one Blender object hierarchy without version-specific helpers."""
    yield root
    for child in root.children:
        yield from object_tree(child)


def unhide_object_tree_collections(anchors) -> dict:
    """Make populated object trees evaluable and return viewport state by name."""
    state = {}
    for anchor in anchors:
        for obj in object_tree(anchor):
            for collection in obj.users_collection:
                if collection.name not in state:
                    state[collection.name] = {
                        "collection": collection,
                        "hide_viewport": bool(collection.hide_viewport),
                    }
                collection.hide_viewport = False
    force_evaluation(anchors)
    return state


def restore_collection_visibility(state: dict) -> None:
    import bpy

    for row in state.values():
        row["collection"].hide_viewport = bool(row["hide_viewport"])
    bpy.context.view_layer.update()


def restore_identity_root_placeholder_inheritance(asset_rows) -> list[dict]:
    """Repair generated roots after Infinigen's no-inverse parent operator.

    BeyondInfinigen's generated-asset adapters return a parentless root with an
    identity basis.  ``AssetFactory.spawn_asset`` then parents that root with
    ``parent_no_inverse_set(keep_transform=False)``.  In Blender this can leave
    the serialized child basis equal to the inverse placeholder transform even
    though the current depsgraph still displays the child at the placeholder.
    After save/reopen the two transforms cancel and the asset moves to world
    origin.

    This helper deliberately applies only when the caller has already proved
    that the roots come from the generated-adapter identity-root contract.  It
    must not be used for arbitrary official factories, whose roots may carry a
    meaningful local transform.
    """
    repaired = []
    for _seed, root in asset_rows:
        placeholder = root.parent
        if placeholder is None:
            continue
        # Merely assigning Identity to fields that already compare equal does
        # not invalidate Blender's stale matrix_local/matrix_world cache.  A
        # real detach/reattach is required; preserve the adapter-provided basis
        # across that operation (identity here, with fit rotation/scale applied
        # later by placer).
        basis = root.matrix_basis.copy()
        root.parent = None
        root.matrix_parent_inverse.identity()
        root.matrix_basis = basis
        root.parent = placeholder
        root.matrix_parent_inverse.identity()
        root.matrix_basis = basis
        root["bi_identity_root_parenting_repaired"] = True
        repaired.append({
            "asset": root.name,
            "placeholder": placeholder.name,
            "contract": "generated_adapter_identity_root",
        })
    return repaired


def force_evaluation(roots=()) -> None:
    """Tag transform hierarchies and synchronously refresh the depsgraph."""
    import bpy

    for root in roots:
        for obj in object_tree(root):
            try:
                obj.update_tag(refresh={"OBJECT"})
            except (TypeError, ValueError):
                obj.update_tag()
    bpy.context.view_layer.update()
    bpy.context.evaluated_depsgraph_get().update()


def rotate_roots_about_parent_z(roots, quarter_turns: int) -> None:
    """Rotate root geometry in its placeholder frame without moving its pivot."""
    import math
    from mathutils import Matrix

    turns = int(quarter_turns) % 4
    if not turns:
        return
    rotation = Matrix.Rotation(turns * math.pi / 2.0, 4, "Z")
    for root in roots:
        basis = root.matrix_basis.copy()
        translation = basis.translation.copy()
        basis = rotation @ basis
        basis.translation = translation
        root.matrix_basis = basis
        root["bi_proxy_heading_quarter_turns"] = turns
    force_evaluation(roots)


def dimensions_in_anchor_space(anchor, asset_roots):
    """Return evaluated XYZ dimensions relative to an unscaled proxy anchor."""
    import bpy
    from mathutils import Vector

    depsgraph = bpy.context.evaluated_depsgraph_get()
    depsgraph.update()
    evaluated_anchor = anchor.evaluated_get(depsgraph)
    anchor_inverse = evaluated_anchor.matrix_world.inverted_safe()
    points = []
    for root in asset_roots:
        for obj in object_tree(root):
            if obj.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
                continue
            evaluated = obj.evaluated_get(depsgraph)
            try:
                points.extend(
                    anchor_inverse @ (evaluated.matrix_world @ Vector(corner))
                    for corner in evaluated.bound_box
                )
            except (AttributeError, TypeError, ValueError):
                continue
    if not points:
        return None
    return [
        max(point[axis] for point in points)
        - min(point[axis] for point in points)
        for axis in range(3)
    ]

"""Shared visible-surface projection checks for placement and planning maps.

This module is Blender-only.  It deliberately contains no search policy: one
call evaluates one normalized camera coordinate against the exact same center,
footprint, surface, slope, height-gap, and proxy-boundary contract used by
``apply_plan.py``.  Planning-map code may call it over a deterministic grid;
the canonical executor calls it only for submitted coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os

from mathutils import Vector

import placement_contract as plc
import surface_raycast_cache as srcache
import surface_registry as sr


@dataclass
class ProjectionEvaluation:
    screen_position: list[float]
    direction: Vector
    hit: tuple | None
    status: str
    reason: str | None = None
    footprint_samples: list[dict] = field(default_factory=list)
    support_summary: dict = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return self.hit is not None and self.status == "feasible"

    def as_json(self) -> dict:
        hit = self.hit
        return {
            "screen_position": list(self.screen_position),
            "status": self.status,
            "reason": self.reason,
            "hit_surface": None if hit is None else str(hit[2]),
            "world_position": None if hit is None else [float(v) for v in hit[1]],
            "distance": None if hit is None else float(hit[0]),
            "surface_normal": None if hit is None else [float(v) for v in hit[3]],
            "slope_deg": None if hit is None else slope_degrees(hit[3]),
            "footprint_samples": self.footprint_samples,
            "support_summary": self.support_summary,
            "plane_quality_score": self.support_summary.get(
                "plane_quality_score"),
        }


def ndc_box_touches_frame(corners) -> bool:
    """True when the projected box (camera-view NDC corners) overlaps the frame at all."""
    front = [corner for corner in corners if corner.z > 0.0]
    if not front:
        return False
    xs = [corner.x for corner in front]
    ys = [corner.y for corner in front]
    return max(xs) >= 0.0 and min(xs) <= 1.0 and max(ys) >= 0.0 and min(ys) <= 1.0


def slope_degrees(normal) -> float | None:
    if normal is None:
        return None
    return float(math.degrees(math.acos(
        max(0.0, min(1.0, abs(float(normal.z)))))))


class VisibleSurfaceEvaluator:
    """Evaluate normalized bottom-left-origin camera points without mutation."""

    def __init__(self, *, scene, camera, origin, forward, right, up,
                 tan_x: float, tan_y: float, surface_policy: dict,
                 footprint_half_extents=(1.0, 1.0), box_half_height=1.0,
                 max_support_height_gap=None, scene_name: str = ""):
        self.scene = scene
        self.camera = camera
        self.origin = Vector(origin).copy()
        self.forward = Vector(forward).normalized()
        self.right = Vector(right).normalized()
        self.up = Vector(up).normalized()
        self.tan_x = float(tan_x)
        self.tan_y = float(tan_y)
        self.surface_policy = plc.normalize_surface_policy(surface_policy)
        self.scene_name = str(scene_name or "")
        self.footprint_half_extents = tuple(
            map(float, footprint_half_extents))
        self.box_half_height = float(box_half_height)
        self.planar_surface_types = (
            set(self.surface_policy.get("required_surface_types") or [])
            & plc.INDOOR_PLANAR_SURFACE_TYPES)
        policy_surface_types = (
            set(self.surface_policy.get("required_surface_types") or [])
            | set(self.surface_policy.get("forbidden_surface_types") or []))
        # Enforce semantic surface types only when this scene has deterministic
        # registry evidence. Unknown outdoor scenes still rely on resolved mesh
        # names; registered scenes such as arctic can hard-reject liquid.
        self.enforce_registered_surface_types = bool(
            policy_surface_types and sr.scene_rules(self.scene_name))
        self.strict_planar_support = bool(
            self.planar_surface_types
            and self.surface_policy.get("require_full_footprint_support", True))
        self.soft_planar_support = bool(
            self.planar_surface_types & plc.INDOOR_SOFT_PLANAR_SURFACE_TYPES)
        if max_support_height_gap is None:
            max_support_height_gap = (
                0.35 if self.soft_planar_support
                else 0.20 if self.strict_planar_support
                else 0.75)
        self.max_support_height_gap = float(max_support_height_gap)
        self.minimum_edge_clearance = 0.0

    @classmethod
    def from_camera(cls, *, scene, camera, surface_policy,
                    footprint_half_extents=(1.0, 1.0), box_half_height=1.0,
                    scene_name: str = ""):
        deps = __import__("bpy").context.evaluated_depsgraph_get()
        matrix = camera.evaluated_get(deps).matrix_world.copy()
        forward = -Vector((matrix[0][2], matrix[1][2], matrix[2][2]))
        right = Vector((matrix[0][0], matrix[1][0], matrix[2][0]))
        up = Vector((matrix[0][1], matrix[1][1], matrix[2][1]))
        return cls(
            scene=scene,
            camera=camera,
            origin=matrix.translation,
            forward=forward,
            right=right,
            up=up,
            tan_x=math.tan(camera.data.angle_x / 2.0),
            tan_y=math.tan(camera.data.angle_y / 2.0),
            surface_policy=surface_policy,
            footprint_half_extents=footprint_half_extents,
            box_half_height=box_half_height,
            scene_name=scene_name,
        )

    def _surface_types(self, surface_name) -> list[str]:
        if not self.enforce_registered_surface_types:
            return []
        return sr.types_for_mesh(self.scene_name, str(surface_name))

    def _surface_policy_issue(self, surface_name, slope_deg) -> str | None:
        issue = plc.surface_policy_issue(
            self.surface_policy,
            surface_name=surface_name,
            slope_deg=slope_deg,
        )
        if issue is not None or not self.enforce_registered_surface_types:
            return issue
        return plc.surface_type_policy_issue(
            self.surface_policy, self._surface_types(surface_name))

    def camera_direction(self, nx: float, ny: float) -> Vector:
        """Return a ray for normalized render coordinates, bottom-left origin."""
        return (
            self.right * ((float(nx) * 2.0 - 1.0) * self.tan_x)
            + self.up * ((float(ny) * 2.0 - 1.0) * self.tan_y)
            + self.forward
        ).normalized()

    def visible_surface_below(self, point):
        point = Vector(point)
        # A 10m indoor ray starts above the ceiling and hits the room shell
        # before the local table/shelf under test.  Strict planar sampling owns
        # a local height band, so begin just above that band.  Outdoor/legacy
        # evaluation retains the original long ray exactly.
        rise = (max(0.08, self.max_support_height_gap + 0.03)
                if self.strict_planar_support else 10.0)
        origin = Vector((point.x, point.y, point.z + rise))
        return srcache.raycast(
            self.scene, origin, Vector((0.0, 0.0, -1.0)),
            distance=(2.0 * rise if self.strict_planar_support else 1e5))

    def _footprint_offsets(self, yaw: float = 0.0):
        """Return the proxy rectangle corners at one exact planned yaw."""
        half_x, half_y = self.footprint_half_extents
        cosine, sine = math.cos(float(yaw)), math.sin(float(yaw))
        corners = (
            (-half_x, -half_y), (-half_x, half_y),
            (half_x, -half_y), (half_x, half_y),
        )
        return tuple((
            dx * cosine - dy * sine,
            dx * sine + dy * cosine,
        ) for dx, dy in corners)

    def _support_offsets(self, yaw: float = 0.0):
        """Return a coarse 3x3 grid over an indoor support footprint."""
        if not self.strict_planar_support:
            return self._footprint_offsets(yaw)
        half_x, half_y = self.footprint_half_extents
        half_x += self.minimum_edge_clearance
        half_y += self.minimum_edge_clearance
        grid_n = 3
        cosine, sine = math.cos(float(yaw)), math.sin(float(yaw))
        result = []
        for row in range(grid_n):
            fy = -1.0 + 2.0 * row / (grid_n - 1)
            for column in range(grid_n):
                fx = -1.0 + 2.0 * column / (grid_n - 1)
                dx, dy = fx * half_x, fy * half_y
                result.append((
                    dx * cosine - dy * sine,
                    dx * sine + dy * cosine,
                ))
        return tuple(result)

    @staticmethod
    def _same_support_surface(actual, expected) -> bool:
        return (plc.surface_name_matches(str(actual), str(expected))
                or plc.surface_name_matches(str(expected), str(actual)))

    def _support_summary(self, samples: list[dict], expected_count: int) -> dict:
        heights = [float(sample["world_position"][2]) for sample in samples
                   if sample.get("world_position") is not None]
        slopes = [float(sample["slope_deg"]) for sample in samples
                  if sample.get("slope_deg") is not None]
        height_range = max(heights) - min(heights) if heights else None
        maximum_slope = max(slopes) if slopes else None
        coverage = len(heights) / max(1, int(expected_count))
        allowed_slope = self.surface_policy.get("max_slope_deg")
        slope_ratio = (
            0.0 if maximum_slope is None or allowed_slope in (None, 0)
            else min(1.0, maximum_slope / max(float(allowed_slope), 1e-6)))
        height_ratio = (
            1.0 if height_range is None else
            min(1.0, height_range / max(self.max_support_height_gap, 1e-6)))
        quality = max(0.0, min(
            1.0,
            0.55 * coverage + 0.25 * (1.0 - height_ratio)
            + 0.20 * (1.0 - slope_ratio),
        ))
        return {
            "strict_planar_support": self.strict_planar_support,
            "sample_grid": (
                3 if self.strict_planar_support else 2),
            "sample_count": len(heights),
            "expected_sample_count": int(expected_count),
            "support_coverage_ratio": float(coverage),
            "support_height_range_m": height_range,
            "max_support_height_gap_m": self.max_support_height_gap,
            "maximum_sample_slope_deg": maximum_slope,
            "minimum_edge_clearance_m": self.minimum_edge_clearance,
            "plane_quality_score": float(quality),
        }

    def _footprint(self, hit, *, yaw: float = 0.0
                   ) -> tuple[str | None, list[dict], dict]:
        if not self.surface_policy.get("require_full_footprint_support", True):
            return None, [], {}
        center = Vector(hit[1])
        samples = []
        offsets = self._support_offsets(yaw)
        for sample_index, (dx, dy) in enumerate(offsets):
            support = (hit if abs(dx) < 1e-9 and abs(dy) < 1e-9 else
                       self.visible_surface_below(
                           Vector((center.x + dx, center.y + dy, center.z))))
            sample = {
                "sample_index": sample_index,
                "offset_xy": [dx, dy],
                "surface": None,
                "surface_types": [],
                "world_position": None,
                "slope_deg": None,
                "height_gap": None,
                "issue": None,
            }
            if support is None:
                sample["issue"] = "no support"
                samples.append(sample)
                summary = self._support_summary(samples, len(offsets))
                return (f"footprint sample {sample_index} has no support",
                        samples, summary)
            sample.update({
                "surface": str(support[2]),
                "surface_types": self._surface_types(support[2]),
                "world_position": [float(v) for v in support[1]],
                "slope_deg": slope_degrees(support[3]),
                "height_gap": abs(float(support[1].z) - float(center.z)),
            })
            issue = self._surface_policy_issue(
                support[2], sample["slope_deg"])
            if issue:
                sample["issue"] = issue
                samples.append(sample)
                return (f"footprint sample {sample_index}: {issue}", samples,
                        self._support_summary(samples, len(offsets)))
            if sample["height_gap"] > self.max_support_height_gap:
                issue = (
                    f"footprint sample {sample_index}: support height differs "
                    f"by {sample['height_gap']:.3f}m")
                sample["issue"] = issue
                samples.append(sample)
                return issue, samples, self._support_summary(samples, len(offsets))
            samples.append(sample)
        summary = self._support_summary(samples, len(offsets))
        height_range = summary.get("support_height_range_m")
        if (height_range is not None
                and height_range > self.max_support_height_gap):
            issue = (
                "footprint support is not one plane: height range "
                f"{height_range:.3f}m exceeds "
                f"{self.max_support_height_gap:.3f}m")
            return issue, samples, summary
        return None, samples, summary

    def _volume_clearance_issue(self, hit, samples: list[dict], *, yaw: float
                                ) -> str | None:
        """Reject an indoor proxy whose above-support volume is occupied."""
        if not self.strict_planar_support:
            return None
        height = max(0.05, 2.0 * self.box_half_height)
        epsilon = 0.006
        # Check overhead at the center and the four footprint corners.  Start
        # each ray above its own sampled support height so a soft bed surface
        # cannot collide with itself merely because it is locally undulating.
        candidates = [samples[len(samples) // 2]] if samples else []
        for sample in candidates:
            point = sample.get("world_position")
            if point is None:
                continue
            obstruction = srcache.raycast(
                self.scene,
                Vector((point[0], point[1], point[2] + epsilon)),
                Vector((0.0, 0.0, 1.0)),
                distance=height + 0.02,
            )
            if obstruction is not None:
                return (
                    "proxy volume above support is blocked by "
                    f"{obstruction[2]} at {obstruction[0]:.3f}m")

        return None

    def _boundary_issue(self, hit, *, yaw: float = 0.0) -> str | None:
        from bpy_extras.object_utils import world_to_camera_view

        center = Vector(hit[1])
        vertical_offsets = ((0.0, 2.0 * self.box_half_height)
                            if self.strict_planar_support else
                            (-self.box_half_height, self.box_half_height))
        corners = [
            world_to_camera_view(
                self.scene, self.camera, center + Vector((dx, dy, dz)))
            for dx, dy in self._footprint_offsets(yaw)
            for dz in vertical_offsets
        ]
        # Extended grid: off-screen placement is allowed, only "in front of the camera".
        if plc.extended_search_bounds() is not None:
            if all(ndc.z > 0.0 for ndc in corners):
                return None
            return "placeholder box is behind the camera"
        # BI_PARTIAL_IN_FRAME_OK=1 (batch opt-in): being partly in frame is enough.
        if os.environ.get("BI_PARTIAL_IN_FRAME_OK") == "1":
            if ndc_box_touches_frame(corners):
                return None
            return "placeholder box is entirely outside the camera frame"
        for ndc in corners:
            if not (
                0.0 <= ndc.x <= 1.0
                and 0.0 <= ndc.y <= 1.0
                and ndc.z > 0.0
            ):
                return "full placeholder box would cross the camera boundary"
        return None

    def evaluate(self, nx: float, ny: float, *, yaw: float = 0.0
                 ) -> ProjectionEvaluation:
        point = [float(nx), float(ny)]
        direction = self.camera_direction(*point)
        hit = srcache.raycast(self.scene, self.origin, direction)
        if hit is None:
            return ProjectionEvaluation(
                point, direction, None, "no_hit",
                "camera ray did not hit a visible surface")

        center_issue = self._surface_policy_issue(
            hit[2], slope_degrees(hit[3]))
        if center_issue:
            status = "slope" if "slope" in center_issue else "surface"
            return ProjectionEvaluation(
                point, direction, hit, status, str(center_issue))

        footprint_issue, footprint_samples, support_summary = self._footprint(
            hit, yaw=yaw)
        if footprint_issue:
            return ProjectionEvaluation(
                point, direction, hit, "footprint", footprint_issue,
                footprint_samples, support_summary)

        clearance_issue = self._volume_clearance_issue(
            hit, footprint_samples, yaw=yaw)
        if clearance_issue:
            return ProjectionEvaluation(
                point, direction, hit, "clearance", clearance_issue,
                footprint_samples, support_summary)

        boundary_issue = self._boundary_issue(hit, yaw=yaw)
        if boundary_issue:
            return ProjectionEvaluation(
                point, direction, hit, "boundary", boundary_issue,
                footprint_samples, support_summary)

        return ProjectionEvaluation(
            point, direction, hit, "feasible", None, footprint_samples,
            support_summary)

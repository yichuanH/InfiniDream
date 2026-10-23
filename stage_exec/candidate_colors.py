"""Pure color contract shared by Blender and contact-sheet processes."""

from __future__ import annotations

import re


CANDIDATE_PALETTE = (
    (0.96, 0.22, 0.20),  # C0 red
    (1.00, 0.48, 0.05),  # C1 orange
    (0.95, 0.78, 0.08),  # C2 yellow
    (0.48, 0.86, 0.14),  # C3 lime
    (0.08, 0.72, 0.34),  # C4 green
    (0.00, 0.76, 0.82),  # C5 cyan
    (0.10, 0.46, 0.96),  # C6 blue
    (0.43, 0.30, 0.95),  # C7 violet
    (0.76, 0.22, 0.90),  # C8 purple
    (0.96, 0.24, 0.62),  # C9 pink
)


def candidate_color(label: str):
    """Return the stable RGB assigned to a candidate id such as B0-C7."""
    match = re.fullmatch(r"[A-Z]\d+-C(\d+)", str(label))
    if not match:
        raise ValueError(f"invalid candidate label {label!r}")
    return CANDIDATE_PALETTE[int(match.group(1)) % len(CANDIDATE_PALETTE)]

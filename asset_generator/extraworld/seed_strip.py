"""Shared helper: lay a directory of seed renders out as one 1xN strip.

Imported by BOTH generators. Kept in its own module deliberately — the same logic living
in two files is how the .env path bug survived a move (0730 3-A / 0731 3-B).
"""
from pathlib import Path

import numpy as np
from PIL import Image


def build_seed_strip(seed_dir: Path, out_png: Path, pad: int = 24, gap: int = 16,
                     slide_height: int = 420) -> Path | None:
    """Lay the seed renders out in a single row -> <out_png> (+ a _slide.png at slide_height).

    All panels are cut with ONE shared bounding box. Cropping each render to its own
    content would normalise away the size differences between seeds, and those differences
    are part of what the strip exists to show.
    """
    paths = sorted(Path(seed_dir).glob("seed_*.png"))
    if not paths:
        return None
    ims = [Image.open(p).convert("RGB") for p in paths]
    x0 = y0 = 10 ** 9
    x1 = y1 = 0
    for im in ims:
        ys, xs = np.where(np.asarray(im.convert("L")) < 245)   # anything not the backdrop
        if not len(xs):
            continue
        x0, x1 = min(x0, int(xs.min())), max(x1, int(xs.max()))
        y0, y1 = min(y0, int(ys.min())), max(y1, int(ys.max()))
    w, h = ims[0].size
    if x1 <= x0 or y1 <= y0:                                   # every render came out blank
        box = (0, 0, w, h)
    else:
        box = (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))
    tiles = [im.crop(box) for im in ims]
    tw, th = tiles[0].size
    strip = Image.new("RGB", (tw * len(tiles) + gap * (len(tiles) - 1), th), (255, 255, 255))
    for i, t in enumerate(tiles):
        strip.paste(t, (i * (tw + gap), 0))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    strip.save(out_png)
    if slide_height and strip.height > slide_height:            # slide-sized copy
        sw = round(strip.width * slide_height / strip.height)
        strip.resize((sw, slide_height), Image.LANCZOS).save(
            out_png.with_name(out_png.stem + "_slide" + out_png.suffix))
    return out_png

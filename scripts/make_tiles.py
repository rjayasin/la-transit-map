"""Slice hi-res rasterizations of the map PDF into a WebP tile pyramid.

Usage: make_tiles.py <map8k.png> <map16k.png>
Emits web/tiles/{2,4}/{col}_{row}.webp (512px tiles; level = scale vs 4096 base).
"""
import os, sys
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
TILE = 512

for path, level in [(sys.argv[1], 2), (sys.argv[2], 4)]:
    im = Image.open(path).convert("RGB")
    out = f"web/tiles/{level}"
    os.makedirs(out, exist_ok=True)
    cols = (im.width + TILE - 1) // TILE
    rows = (im.height + TILE - 1) // TILE
    for r in range(rows):
        for c in range(cols):
            box = (c * TILE, r * TILE, min((c + 1) * TILE, im.width), min((r + 1) * TILE, im.height))
            im.crop(box).save(f"{out}/{c}_{r}.webp", quality=85, method=4)
    print(f"level {level}: {cols}x{rows} = {cols*rows} tiles")

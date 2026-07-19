"""Render a WebP tile pyramid for the map background directly from the PDF.

Each 512px tile is rasterized from the PDF vectors at exact target resolution
(no intermediate giant bitmap), so tiles are as crisp as the PDF itself.
Levels are scale factors relative to the 4096px base image: 2, 4, 8
(8192 / 16384 / 32768 px equivalent; level 8 ~ 700 dpi of the 47" map).

Usage: make_tiles.py
"""
import os
import fitz
from PIL import Image

PDF = "26-1720_blt_system_map_47x47.5-2.pdf"
BASE_W = 4096
TILE = 512
LEVELS = [2, 4, 8]

doc = fitz.open(PDF)
page = doc[0]
dl = page.get_displaylist()
pw, ph = page.rect.width, page.rect.height
base_scale = BASE_W / pw          # map px per pt at level 1

for lvl in LEVELS:
    s = base_scale * lvl          # output px per pt
    w, h = int(pw * s + 0.5), int(ph * s + 0.5)
    cols, rows = (w + TILE - 1) // TILE, (h + TILE - 1) // TILE
    out = f"web/tiles/{lvl}"
    os.makedirs(out, exist_ok=True)
    total = 0
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * TILE, r * TILE
            x1, y1 = min(x0 + TILE, w), min(y0 + TILE, h)
            clip = fitz.Rect(x0 / s, y0 / s, x1 / s, y1 / s)
            pix = dl.get_pixmap(matrix=fitz.Matrix(s, s), clip=clip, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            im.save(f"{out}/{c}_{r}.webp", quality=85, method=4)
            total += 1
    print(f"level {lvl}: {cols}x{rows} = {total} tiles ({w}x{h}px)")

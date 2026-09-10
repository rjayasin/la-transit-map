"""Render the map's WebP tile pyramid.

Detail tiles are rasterized from PDF vectors at their target resolution.
Overview levels 0.25, 0.5 and 1 are reduced from map.png.
Detail levels are scale factors relative to the 4096px base image: 2, 4, 8
(8192 / 16384 / 32768 px equivalent; level 8 ~ 700 dpi of the 47" map).

Usage: make_tiles.py [--overview-only]
"""
import argparse
import os
import fitz
from PIL import Image

PDF = "26-1720_blt_system_map_47x47.5-2.pdf"
BASE_W = 4096
TILE = 512
LEVELS = [2, 4, 8]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--overview-only", action="store_true", help="skip the PDF detail tiles")
args = parser.parse_args()

source = Image.open("map.png").convert("RGBA")
# Match the PDF renderer's white background in transparent margins.
base = Image.new("RGBA", source.size, "white")
base = Image.alpha_composite(base, source).convert("RGB")
for level in (0.25, 0.5, 1):
    im = base.resize((round(base.width * level), round(base.height * level)),
                     Image.Resampling.LANCZOS) if level != 1 else base
    out = f"tiles/{level}"
    os.makedirs(out, exist_ok=True)
    total = 0
    for y in range(0, im.height, TILE):
        for x in range(0, im.width, TILE):
            im.crop((x, y, min(x + TILE, im.width), min(y + TILE, im.height))).save(
                f"{out}/{x // TILE}_{y // TILE}.webp", quality=85, method=4)
            total += 1
    print(f"level {level}: {total} tiles ({im.width}x{im.height}px)")

if args.overview_only:
    raise SystemExit(0)

doc = fitz.open(PDF)
page = doc[0]
dl = page.get_displaylist()
pw, ph = page.rect.width, page.rect.height
base_scale = BASE_W / pw          # map px per pt at level 1

for lvl in LEVELS:
    s = base_scale * lvl          # output px per pt
    w, h = int(pw * s + 0.5), int(ph * s + 0.5)
    cols, rows = (w + TILE - 1) // TILE, (h + TILE - 1) // TILE
    out = f"tiles/{lvl}"
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

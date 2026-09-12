#!/usr/bin/env python3
"""Export the shared vector mark to the macOS app icon family.

Run on macOS with CairoSVG and Pillow installed. The committed exports are
used by normal app/release builds; these are design-time dependencies only.
"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import cairosvg
from PIL import Image


ASSETS = Path(__file__).resolve().parent
MARK = ASSETS.parent / "static" / "brand-mark.svg"
SVG_NS = "http://www.w3.org/2000/svg"
SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def main():
    if not shutil.which("iconutil"):
        raise SystemExit("macOS iconutil is required to export AppIcon.icns")

    ET.register_namespace("", SVG_NS)
    icon = ET.Element(f"{{{SVG_NS}}}svg", {
        "viewBox": "0 0 1024 1024", "width": "1024", "height": "1024",
    })
    ET.SubElement(icon, f"{{{SVG_NS}}}title").text = "Astro Digest"
    ET.SubElement(icon, f"{{{SVG_NS}}}desc").text = (
        "A warm-white paper and a five-star gold star on midnight blue."
    )
    ET.SubElement(icon, f"{{{SVG_NS}}}path", {
        "fill": "#18263D",
        "d": "M300 100H724C864 100 924 160 924 300V724"
             "C924 864 864 924 724 924H300C160 924 100 864 100 724"
             "V300C100 160 160 100 300 100Z",
    })
    # Optical centering: the solid paper carries more weight than the star.
    # Shift 40px right of the combined bounds' center to balance that mass.
    glyph = ET.SubElement(icon, f"{{{SVG_NS}}}g", {
        "transform": "translate(246 236) scale(8.5)",
    })
    for child in ET.parse(MARK).getroot():
        glyph.append(deepcopy(child))

    ET.indent(icon)
    svg = ET.tostring(icon, encoding="unicode") + "\n"
    (ASSETS / "icon.svg").write_text(svg, encoding="utf-8")

    # Supersample once, then downsample with premultiplied alpha to keep the
    # transparent outer corners and small glyph edges clean on any background.
    rendered = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"), output_width=4096, output_height=4096,
    )
    with Image.open(BytesIO(rendered)) as source:
        source = source.convert("RGBa")
        iconset = ASSETS / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for filename, size in SIZES.items():
            raster = source.resize((size, size), Image.Resampling.LANCZOS)
            raster.convert("RGBA").save(iconset / filename)

    shutil.copyfile(iconset / "icon_512x512@2x.png", ASSETS / "icon_1024.png")
    subprocess.run([
        "iconutil", "-c", "icns", str(iconset),
        "-o", str(ASSETS / "AppIcon.icns"),
    ], check=True)
    print("Generated icon.svg, icon_1024.png, 10 iconset PNGs, and AppIcon.icns")


if __name__ == "__main__":
    main()

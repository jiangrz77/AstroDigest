# App identity

Astro Digest helps researchers find the astronomy papers worth reading.
The mark pairs one paper with one star. Two short cutouts suggest a digest;
the open corner gives the star space. The app icon uses midnight blue
(`#18263D`), warm white (`#F6F5F0`), and a gold star (`#F4B400`) matching
the app's five-star paper ratings, with transparent outer corners and no border.

`../static/brand-mark.svg` is the editable vector master, also served in the
app's headers. `icon.svg` is its generated macOS tile composition.
The tile places the mark 40px right of geometric center on the 1024px
canvas, balancing the paper's greater visual weight against the star.

## Regenerate the icons

On macOS, from the repository root, in a separate Python environment:

```sh
brew install cairo
python3 -m pip install CairoSVG Pillow
DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib" python3 AstroDigest/assets/generate_icons.py
```

The library path lets CairoSVG find Homebrew's Cairo library. The generator
exports `icon.svg`, the 1024px PNG, all ten standard
macOS iconset files, and `AppIcon.icns`. Commit the exports together with
the source. Normal app and DMG builds use these exports directly and do
not require the design tools.

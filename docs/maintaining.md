# Maintaining Astro Digest

[Home](../README.md)

## Releasing a New Version (maintainers)

1. Bump `AstroDigest/version.txt` (e.g. `2.4.0`), stage the reviewed release changes, then commit and push:
   ```bash
   git commit -m "v2.4.0"
   git push origin main
   git tag v2.4.0 && git push origin v2.4.0
   ```
2. Run `cd AstroDigest && ./release.sh` — builds the self-contained app, then generates three artifacts under `dist/` (with SHA-256): `AstroDigest-v2.4.0.source.zip` (source channel), `AstroDigest-v2.4.0.app.zip` (update package), `AstroDigest-2.4.0.dmg` (installer), plus `version.json`.
3. On GitHub: **Releases → Draft a new release** → pick tag `v2.4.0` → write release notes → upload all three artifacts → **Publish release** (do NOT mark it Pre-release). Alternatively push the tag and let the GitHub Actions workflow build and attach everything automatically.
4. dmg users install the new version from the dmg; existing app users see the banner and update in one click after launching.

## Rename compatibility

The product name is **Astro Digest**; repository, source directory, executable, and package prefix use **AstroDigest**. Register `astrodigest` and the previous `astropaperdigest` URL schemes, and retain `com.arxivdailydigest.app` as the stable macOS bundle identifier.

The Finder-visible bundle is `Astro Digest.app`. Its internal executable remains `AstroDigest`, and release asset filenames continue using the technical `AstroDigest` prefix for update compatibility.

Publish the `AstroPaperDigest-v<V>.app.zip` and `.source.zip` compatibility aliases together with the new `AstroDigest` packages. Each alias must be byte-for-byte identical to its corresponding new package. Older desktop clients only recognize the old asset names. Keep the App ZIP checksum first in release notes for those clients, then list checksums by complete asset filename. Current clients associate checksums with the selected asset.

Older source clients also have archive-layout and checksum-selection limitations; the first transition may require downloading the source package manually. Preserve personal configuration and output when replacing source files. Historical release notes and patches retain the project name used at publication.

For isolated checks use `ASTRODIGEST_DATA_DIR`; the legacy `APD_DATA_DIR` remains accepted. Existing packaged installations continue using their old data directory, while fresh installs use `~/Library/Application Support/AstroDigest`.

# Maintaining AstroPaperDigest

[Home](../README.md)

## Releasing a New Version (maintainers)

1. Bump `AstroPaperDigest/version.txt` (e.g. `2.3.5`), stage the reviewed release changes, then commit and push:
   ```bash
   git commit -m "v2.3.5"
   git push origin main
   git tag v2.3.5 && git push origin v2.3.5
   ```
2. Run `cd AstroPaperDigest && ./release.sh` — builds the self-contained app, then generates three artifacts under `dist/` (with SHA-256): `AstroPaperDigest-v2.3.5.source.zip` (source channel), `AstroPaperDigest-v2.3.5.app.zip` (update package), `AstroPaperDigest-2.3.5.dmg` (installer), plus `version.json`.
3. On GitHub: **Releases → Draft a new release** → pick tag `v2.3.5` → write release notes → upload all three artifacts → **Publish release** (do NOT mark it Pre-release). Alternatively push the tag and let the GitHub Actions workflow build and attach everything automatically.
4. dmg users install the new version from the dmg; existing app users see the banner and update in one click after launching.

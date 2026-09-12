#!/bin/bash
# Build release artifacts for a GitHub Release.
# Usage: ./release.sh   (run AFTER: git tag vX.Y.Z && git push origin vX.Y.Z)
#
# Produces (all under dist/):
#   AstroDigest-v<v>.source.zip  source package (Install.command / dev channel)
#   AstroDigest-v<v>.app.zip     self-contained update package (dmg channel)
#   AstroDigest-<v>.dmg          drag-to-install disk image
#   AstroPaperDigest-v<v>.*.zip  byte-identical aliases for installed clients
#   version.json                manifest used by self-hosted update mirrors
set -euo pipefail
cd "$(dirname "$0")"
APP_ROOT="$(pwd)"

VERSION="$(cat version.txt | tr -d '[:space:]')"
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "ERROR: version.txt contains an invalid version (expected x.y.z, got: '$VERSION')" >&2; exit 1 ;;
esac
TAG="v$VERSION"

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: git tag $TAG does not exist."
    echo "  Run first:"
    echo "    git tag $TAG && git push origin $TAG"
    exit 1
fi

# Source and binary artifacts must describe the same immutable commit.
if [ "$(git rev-parse "$TAG^{commit}")" != "$(git rev-parse HEAD)" ]; then
    echo "ERROR: HEAD must match $TAG. Use a new version for fixes; never move a release tag." >&2
    exit 1
fi
if [ -n "${GITHUB_REF_NAME:-}" ] && [ "$GITHUB_REF_NAME" != "$TAG" ]; then
    echo "ERROR: triggering tag differs from version.txt." >&2
    exit 1
fi
if ! git diff --quiet HEAD -- . ../.github; then
    echo "ERROR: commit tracked changes before building a release." >&2
    exit 1
fi

echo "==> Building self-contained app + dmg (build_dmg.sh) ..."
ASTRODIGEST_RELEASE_BUILD=1 ./build_dmg.sh

echo "==> Building source package ..."
SOURCE_ZIP="dist/AstroDigest-$TAG.source.zip"
rm -f "$SOURCE_ZIP"
# The source updater expects one project directory at the archive root. The
# tree-ish path keeps root-level release notes, patches, and other workspace
# files out of the install archive.
REPO_ROOT="$(git rev-parse --show-toplevel)"
git -C "$REPO_ROOT" archive --format=zip --prefix=AstroDigest/ \
  -o "$APP_ROOT/$SOURCE_ZIP" "$TAG:AstroDigest"

APP_ZIP="dist/AstroDigest-$TAG.app.zip"
DMG="dist/AstroDigest-$VERSION.dmg"

# Older installed clients select the exact historical asset names. These are
# byte-identical aliases, so the published app checksum also validates them.
LEGACY_SOURCE_ZIP="dist/AstroPaperDigest-$TAG.source.zip"
LEGACY_APP_ZIP="dist/AstroPaperDigest-$TAG.app.zip"
cp "$SOURCE_ZIP" "$LEGACY_SOURCE_ZIP"
cp "$APP_ZIP" "$LEGACY_APP_ZIP"

SHA_SOURCE="$(shasum -a 256 "$SOURCE_ZIP" | awk '{print $1}')"
SHA_APP="$(shasum -a 256 "$APP_ZIP" | awk '{print $1}')"
SHA_DMG="$(shasum -a 256 "$DMG" | awk '{print $1}')"

cat > dist/version.json <<EOF
{
  "version": "$VERSION",
  "tag": "$TAG",
  "url": "https://github.com/jiangrz77/AstroDigest/releases/download/$TAG/AstroDigest-$TAG.app.zip",
  "sha256": "$SHA_APP",
  "min_system_version": "10.15"
}
EOF

shasum -a 256 "$APP_ZIP" "$SOURCE_ZIP" "$DMG" "$LEGACY_APP_ZIP" "$LEGACY_SOURCE_ZIP" > dist/SHA256SUMS

echo ""
echo "Done! Artifacts in dist/:"
echo "  $SOURCE_ZIP   SHA256: $SHA_SOURCE"
echo "  $APP_ZIP      SHA256: $SHA_APP"
echo "  $DMG          SHA256: $SHA_DMG"
echo ""
echo "Next steps (GitHub web UI):"
echo "  1. Open https://github.com/jiangrz77/AstroDigest/releases/new"
echo "  2. Select tag $TAG, set the title to $TAG"
echo "  3. Include the app checksum first for older clients, then each asset checksum:"
echo "     - $(basename "$APP_ZIP")"
echo "       SHA256: $SHA_APP"
echo "     - $(basename "$SOURCE_ZIP")"
echo "       SHA256: $SHA_SOURCE"
echo "  4. Attach both app/source zip names, $DMG, version.json and SHA256SUMS"
echo "  5. Click Publish release (do NOT mark as Pre-release)"

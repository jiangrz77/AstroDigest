#!/bin/bash
# Build the self-contained Astro Digest.app + dmg (dmg channel).
#
# Produces:
#   dist/Astro Digest.app           self-contained app (no Python required)
#   dist/AstroDigest-<v>.dmg        drag-to-install disk image
#   dist/AstroDigest-v<v>.app.zip   update package (whole-bundle replace)
#
# Usage: ./build_dmg.sh
#   PYTHON=... ./build_dmg.sh        override the python that has PyInstaller
#
# Requires: macOS, a python3 with PyInstaller (e.g. pip install pyinstaller).
# Recommend Python 3.12+ (OpenSSL 3.x): older CPythons bundle LibreSSL 2.8.x
# which intermittently fails TLS against modern hosts.
set -euo pipefail
cd "$(dirname "$0")"
APP_ROOT="$(pwd)"

VERSION="$(cat version.txt 2>/dev/null | tr -d '[:space:]')"
if [ -z "$VERSION" ]; then echo "ERROR: version.txt missing or empty." >&2; exit 1; fi

PYTHON="${PYTHON:-python3}"
if [ "${ASTRODIGEST_RELEASE_BUILD:-${APD_RELEASE_BUILD:-0}}" = "1" ]; then
    "$PYTHON" -c 'import sys, platform; assert sys.version_info[:2] == (3, 12) and platform.system() == "Darwin" and platform.machine() == "arm64", "Release lock requires macOS arm64 / Python 3.12"'
    "$PYTHON" -m pip install --require-hashes -r requirements-release.lock -q
    "$PYTHON" -m pip check
fi

APP_BINARY_NAME="AstroDigest"
APP_DISPLAY_NAME="Astro Digest"
APP_BINARY_BUNDLE="$APP_BINARY_NAME.app"
APP_DISPLAY_BUNDLE="$APP_DISPLAY_NAME.app"
# Keep the historical identifier so macOS retains the existing application identity.
BUNDLE_ID="com.arxivdailydigest.app"

echo "==> Building bundled CLI (astrodigest-cli) ..."
rm -rf build/cli dist
mkdir -p build dist
"$PYTHON" -m PyInstaller --noconfirm --clean --console \
  --name astrodigest-cli \
  --paths "$APP_ROOT" \
  --collect-data latex2mathml \
  --distpath "$APP_ROOT/build/cli" --workpath "$APP_ROOT/build/pyi-cli" --specpath "$APP_ROOT/build" \
  "$APP_ROOT/cli_entry.py" > "$APP_ROOT/build/cli-build.log" 2>&1

echo "==> Building self-contained .app ..."
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed \
  --argv-emulation \
  --name "$APP_BINARY_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --icon "$APP_ROOT/assets/AppIcon.icns" \
  --paths "$APP_ROOT" \
  --collect-all webview \
  --collect-data latex2mathml \
  --add-data "$APP_ROOT/static:static" \
  --add-binary "$APP_ROOT/build/cli/astrodigest-cli:." \
  --distpath "$APP_ROOT/dist" --workpath "$APP_ROOT/build/pyi-app" --specpath "$APP_ROOT/build" \
  "$APP_ROOT/src/gui.py" > "$APP_ROOT/build/app-build.log" 2>&1

echo "==> Setting bundle version in Info.plist ..."
PLIST="$APP_ROOT/dist/$APP_BINARY_BUNDLE/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Astro Digest" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Astro Digest" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Astro Digest" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleName string Astro Digest" "$PLIST"

# Register the deep link used by daily emails.
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string Astro Digest" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string astrodigest" "$PLIST" 2>/dev/null || true
# Keep links in previously sent daily emails working.
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:1 string astropaperdigest" "$PLIST" 2>/dev/null || true

echo "==> Ad-hoc signing the .app (no Developer ID required) ..."
codesign --force --deep -s - "$APP_ROOT/dist/$APP_BINARY_BUNDLE"
mv "$APP_ROOT/dist/$APP_BINARY_BUNDLE" "$APP_ROOT/dist/$APP_DISPLAY_BUNDLE"

echo "==> Verifying packaged startup (isolated from user data) ..."
"$PYTHON" "$APP_ROOT/verify_bundle.py" "$APP_ROOT/dist/$APP_DISPLAY_BUNDLE"

echo "==> Creating dmg ..."
rm -rf "$APP_ROOT/build/dmgroot"
mkdir -p "$APP_ROOT/build/dmgroot"
cp -R "$APP_ROOT/dist/$APP_DISPLAY_BUNDLE" "$APP_ROOT/build/dmgroot/"
ln -s /Applications "$APP_ROOT/build/dmgroot/Applications"
DMG="$APP_ROOT/dist/$APP_BINARY_NAME-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "Astro Digest" -srcfolder "$APP_ROOT/build/dmgroot" -ov -format UDZO "$DMG" > "$APP_ROOT/build/dmg.log" 2>&1
codesign -s - "$DMG" 2>/dev/null || true

echo "==> Creating update package (.app zip) ..."
APPZIP="$APP_ROOT/dist/$APP_BINARY_NAME-v$VERSION.app.zip"
rm -f "$APPZIP"
ditto -c -k --keepParent "$APP_ROOT/dist/$APP_DISPLAY_BUNDLE" "$APPZIP"

echo ""
echo "Done:"
echo "  dist/$APP_DISPLAY_BUNDLE"
echo "  $DMG"
echo "  $APPZIP"

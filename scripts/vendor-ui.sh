#!/usr/bin/env sh
set -e
# Pinned to the robotsix-ui PR #78 merge commit (ConfigPanel: foldable group
# headers, advanced-settings mechanism removed, smart schema-driven grouping).
# No release tag contains this commit yet; re-pin to the tag once released.
VERSION="${ROBOTSIX_UI_VERSION:-0a655e5b7be6a6f16b5ec67a28cd1c7255c35f06}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${REPO_ROOT}/src/robotsix_chat/ui/static/vendor"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
npm install --no-save "github:damien-robotsix/robotsix-ui#${VERSION}"
mkdir -p "$DEST"
cp node_modules/@robotsix/ui/dist/vanilla.js "$DEST/vanilla.js"
cp node_modules/@robotsix/ui/dist/style.css  "$DEST/style.css"
echo "Vendored @robotsix/ui@${VERSION} → $DEST"

#!/usr/bin/env sh
set -e
VERSION="${ROBOTSIX_UI_VERSION:-v0.1.40}"
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

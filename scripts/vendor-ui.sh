#!/usr/bin/env sh
set -e
# Pinned to the robotsix-ui main commit after PR #190 (ConfigPanel: field
# descriptions only in the hover tooltip (#189); object-typed settings edited
# as JSON instead of "[object Object]" (#190)).
# No release tag contains this commit yet; re-pin to the tag once released.
VERSION="${ROBOTSIX_UI_VERSION:-f62ec92324a1a28b61b37d7a439f1f79270ee2a1}"
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

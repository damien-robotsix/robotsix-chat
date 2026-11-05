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
# The robotsix-ui dist is chunked (vanilla.js re-exports from content-hashed
# chunks like appshell-<hash>.js), so vendor the whole dist/ directory and
# clear the destination first so stale hashed chunks don't accumulate.
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R node_modules/@robotsix/ui/dist/. "$DEST/"
sh "${REPO_ROOT}/scripts/check-ui-vendor.sh" "$DEST"
echo "Vendored @robotsix/ui@${VERSION} → $DEST"

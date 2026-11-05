#!/usr/bin/env sh
# Regression guard for the vendored @robotsix/ui build.
#
# Usage: check-ui-vendor.sh <dir>
#
# <dir> is either the @robotsix/ui `dist/` directory or the vendored copy
# (`src/robotsix_chat/ui/static/vendor/`). The robotsix-ui build is chunked:
# `vanilla.js` is a thin shim re-exporting from content-hashed sibling chunks
# (e.g. `./appshell-<hash>.js`). Fails when the entry files are missing or
# when any relative `./*.js` import referenced from a vendored `.js` file
# (scanned transitively, i.e. across every `.js` in <dir>) does not exist.
set -e
DIR="${1:?usage: check-ui-vendor.sh <dir>}"
status=0
for f in vanilla.js style.css; do
  if [ ! -f "$DIR/$f" ]; then
    echo "check-ui-vendor: missing $DIR/$f" >&2
    status=1
  fi
done
for js in "$DIR"/*.js; do
  [ -f "$js" ] || continue
  refs="$(grep -oE "[\"']\./[A-Za-z0-9_.-]+\.js[\"']" "$js" | tr -d "\"'" | sort -u || true)"
  for ref in $refs; do
    if [ ! -f "$DIR/${ref#./}" ]; then
      echo "check-ui-vendor: $(basename "$js") imports $ref but $DIR/${ref#./} is missing" >&2
      status=1
    fi
  done
done
if [ "$status" -eq 0 ]; then
  echo "check-ui-vendor: OK ($DIR)"
fi
exit "$status"

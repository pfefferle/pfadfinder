#!/bin/sh
# Refresh the bundled copy of RocketGod's site clients (MIT) at a given commit:
#   tools/update-reference.sh <commit>
# The files are copied unchanged; vendor/REFERENCE records the commit and their sha256.
set -eu
cd "$(dirname "$0")/.."
COMMIT=${1:?usage: tools/update-reference.sh <commit>}
BASE="https://raw.githubusercontent.com/RocketGod-git/wdgwars-uploader/$COMMIT"
DEST=vendor/wdgwars_uploader
mkdir -p "$DEST"
for f in __init__.py config.py wigle.py uploader.py wardrift.py mesh.py mesh_capture.py; do
    curl -fsSL "$BASE/wdgwars_uploader/$f" -o "$DEST/$f"
done
curl -fsSL "$BASE/LICENSE" -o "$DEST/LICENSE"
{
    echo "RocketGod-git/wdgwars-uploader $COMMIT"
    if command -v sha256sum >/dev/null 2>&1; then SUM="sha256sum"; else SUM="shasum -a 256"; fi
    for f in __init__.py config.py wigle.py uploader.py wardrift.py mesh.py mesh_capture.py LICENSE; do
        $SUM "$DEST/$f" | awk -v n="$f" '{print $1, n}'
    done
} > vendor/REFERENCE
echo "vendored $COMMIT"

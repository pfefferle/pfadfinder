#!/bin/sh
# Build build/pfadfinder_<version>_all.deb. Runs in the dev container: ./dev packaging/build-deb.sh
set -eu
cd "$(dirname "$0")/.."
: "${PFADFINDER_MAINTAINER:?set PFADFINDER_MAINTAINER to "Name <email>"}"

VERSION=$(python3 -c 'import pfadfinder; print(pfadfinder.__version__)')
STAGE=build/stage
APP=$STAGE/usr/share/APPLaunch/apps/pfadfinder
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$APP/vendor" "$STAGE/usr/share/APPLaunch/bin" \
         "$STAGE/usr/share/APPLaunch/applications" "$STAGE/usr/share/APPLaunch/share/images" \
         "$STAGE/lib/systemd/system" "$STAGE/usr/lib/udev/rules.d" "$STAGE/usr/lib/tmpfiles.d"

# application code (no caches)
tar -c --exclude=__pycache__ pfadfinder | tar -x -C "$APP"

# dbus-fast, pure Python only (compiled modules are tied to one Python version)
DBUS_FAST=$(python3 -c 'import dbus_fast, os; print(os.path.dirname(dbus_fast.__file__))')
tar -C "$(dirname "$DBUS_FAST")" -c --exclude=__pycache__ --exclude='*.so' --exclude='*.c' \
    --exclude='*.pyx' --exclude='*.pxd' dbus_fast | tar -x -C "$APP/vendor"
DBUS_FAST_LICENSE=$(python3 -c 'from importlib.metadata import distribution as d; f = [p for p in d("dbus-fast").files if p.name.upper().startswith("LICENSE")][0]; print(f.locate())')
install -m 0644 "$DBUS_FAST_LICENSE" "$APP/vendor/dbus_fast/LICENSE"

# RocketGod's site clients (MIT), unchanged copy pinned in vendor/REFERENCE
tar -c --exclude=__pycache__ -C vendor wdgwars_uploader | tar -x -C "$APP/vendor"

# Meshtastic and MeshCore libraries: pinned pure-Python wheels, licences under vendor/licenses/<name>/
python3 -m pip install -q --no-deps --require-hashes --only-binary=:all: --platform any --implementation py \
    --python-version 3.11 --target "$APP/vendor" -r packaging/mesh-requirements.txt
rm -rf "$APP/vendor/bin" "$APP/vendor/meshtastic/tests"  # command-line tools and the library's own tests
for dist in "$APP"/vendor/*.dist-info; do
    name=$(sed -n 's/^Name: //p' "$dist/METADATA" | head -n 1 | tr -d '\r' | tr 'A-Z_.' 'a-z--')
    mkdir -p "$APP/vendor/licenses/$name"
    find "$dist" -type f \( -iname 'LICEN[CS]E*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) \
        -exec install -m 0644 {} "$APP/vendor/licenses/$name/" \;
done
# meshcore imports pycryptodome as Crypto; Debian ships it as Cryptodome
install -D -m 0644 packaging/Crypto/__init__.py "$APP/vendor/Crypto/__init__.py"

install -m 0644 LICENSE "$APP/LICENSE"
install -m 0755 packaging/pfadfinder-setup "$APP/pfadfinder-setup"
install -m 0755 packaging/launcher.sh "$STAGE/usr/share/APPLaunch/bin/pfadfinder"
install -m 0644 packaging/pfadfinder.desktop "$STAGE/usr/share/APPLaunch/applications/pfadfinder.desktop"
install -m 0644 packaging/icon.png "$STAGE/usr/share/APPLaunch/share/images/pfadfinder.png"
install -m 0644 packaging/pfadfinder-core.service "$STAGE/lib/systemd/system/pfadfinder-core.service"
install -m 0644 packaging/70-pfadfinder.rules "$STAGE/usr/lib/udev/rules.d/70-pfadfinder.rules"
install -m 0644 packaging/pfadfinder.tmpfiles "$STAGE/usr/lib/tmpfiles.d/pfadfinder.conf"
install -m 0755 packaging/postinst packaging/prerm "$STAGE/DEBIAN/"
sed -e "s/@VERSION@/$VERSION/" -e "s|@MAINTAINER@|$PFADFINDER_MAINTAINER|" packaging/control.in > "$STAGE/DEBIAN/control"

python3 -m compileall -q "$APP" >/dev/null
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "build/pfadfinder_${VERSION}_all.deb" >/dev/null
echo "build/pfadfinder_${VERSION}_all.deb"

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "wdgwars_uploader"
FILES = ["__init__.py", "config.py", "wigle.py", "uploader.py", "wardrift.py", "mesh.py", "mesh_capture.py", "LICENSE"]


def _reference():
    lines = (ROOT / "vendor" / "REFERENCE").read_text().splitlines()
    commit = lines[0].split()[-1]
    hashes = dict(reversed(line.split()) for line in lines[1:] if line.strip())
    return commit, hashes


def test_reference_pinned():
    commit, _ = _reference()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)


def test_vendored_files_unmodified():
    _, hashes = _reference()
    assert sorted(hashes) == sorted(FILES)
    for name in FILES:
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == hashes[name], name


def test_reference_modules_import_on_linux():
    from wdgwars_uploader import mesh, mesh_capture, uploader, wardrift, wigle  # noqa: F401
    windows = [m for m in sys.modules if m.startswith(("winrt", "pystray", "winreg", "pywintypes"))]
    assert windows == []
    assert callable(wigle.upload) and callable(wardrift.sign_in) and hasattr(uploader, "WDGWarsClient")
    # the radio libraries are imported only when a worker connects
    assert "meshtastic" not in sys.modules and "meshcore" not in sys.modules
    assert callable(mesh.contact_to_node) and hasattr(mesh_capture, "MeshStore")

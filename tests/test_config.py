import os
import stat
from pathlib import Path

import pytest

from pfadfinder.config import (Config, ConfigError, ConfigWatcher, Paths, ensure_config, load_config,
                               update_config_file)


def test_template_created_with_comments_and_defaults(tmp_path):
    path = tmp_path / "c" / "config.toml"
    ensure_config(path)
    text = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert text.startswith("# Pfadfinder configuration")
    assert "https://wigle.net/account" in text
    assert load_config(path) == Config()


def test_ensure_keeps_existing_lines(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[wigle]\napi_name = "mine"\n')
    ensure_config(path)
    assert path.read_text().startswith('[wigle]\napi_name = "mine"\n')


MANUAL = """# my own notes
[drive]
throttle_meters = 40.0   # wider on the motorway
start_on_boot = true

[wigle]
# api_token = "old commented token"
api_token = "abc"
api_name = "AID1"
"""


def test_update_replaces_only_that_line(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(MANUAL)
    update_config_file(path, "wigle_api_token", 'n"ew\\tok')
    lines = path.read_text().splitlines()
    assert lines == MANUAL.replace('api_token = "abc"', 'api_token = "n\\"ew\\\\tok"').splitlines()
    assert load_config(path).wigle_api_token == 'n"ew\\tok'


def test_update_ignores_commented_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(MANUAL)
    update_config_file(path, "wigle_api_token", "x")
    assert '# api_token = "old commented token"' in path.read_text()


def test_update_adds_missing_key_to_its_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(MANUAL)
    update_config_file(path, "throttle_seconds", 15.0)
    text = path.read_text()
    assert "throttle_seconds = 15.0" in text
    assert text.index("throttle_seconds") < text.index("[wigle]")  # inside [drive]
    cfg = load_config(path)
    assert cfg.throttle_seconds == 15.0 and cfg.throttle_meters == 40.0 and cfg.start_on_boot is True


def test_update_adds_missing_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(MANUAL)
    update_config_file(path, "screen_mode", "mount")
    assert path.read_text().startswith(MANUAL)
    assert load_config(path).screen_mode == "mount"


def test_update_bool_and_keeps_mode(tmp_path):
    path = tmp_path / "config.toml"
    ensure_config(path)
    update_config_file(path, "start_on_boot", True)
    assert load_config(path).start_on_boot is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_update_creates_template_when_missing(tmp_path):
    path = tmp_path / "config.toml"
    update_config_file(path, "wigle_api_name", "AID")
    assert path.read_text().startswith("# Pfadfinder configuration")
    assert load_config(path).wigle_api_name == "AID"


def test_update_unknown_key_rejected(tmp_path):
    with pytest.raises(KeyError):
        update_config_file(tmp_path / "config.toml", "nope", 1)


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.toml") == Config()


def test_unknown_keys_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[wigle]\napi_name = "x"\nfoo = 1\n[other]\nbar = true\n')
    assert load_config(path) == Config(wigle_api_name="x")


def test_invalid_toml_raises_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[wigle]\napi_name = "unterminated\n')
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "line 2" in str(e.value)


def test_watcher_detects_change(tmp_path):
    path = tmp_path / "config.toml"
    ensure_config(path)
    w = ConfigWatcher(path)
    assert w.changed() is False
    path.write_text(path.read_text() + "\n# edited\n")
    os.utime(path, ns=(1, 2))  # force a different mtime even on coarse clocks
    assert w.changed() is True
    assert w.changed() is False


def test_paths_from_env():
    p = Paths.from_env({"HOME": "/home/pi"})
    assert p.db_path == Path("/home/pi/.local/share/pfadfinder/pfadfinder.db")
    assert p.sessions_dir == Path("/home/pi/.local/share/pfadfinder/sessions")
    assert p.state_path == Path("/home/pi/.local/state/pfadfinder/state.json")
    assert p.config_path == Path("/home/pi/.config/pfadfinder/config.toml")
    assert p.socket_path == Path("/run/pfadfinder/core.sock")


def test_template_has_site_sections(tmp_path):
    path = tmp_path / "config.toml"
    ensure_config(path)
    text = path.read_text()
    assert "[wdgwars]" in text and "https://wdgwars.pl/profile" in text
    assert "[wardrift]" in text and "removed after the first sign-in" in text
    cfg = load_config(path)
    assert (cfg.wdgwars_api_key, cfg.wardrift_api_key, cfg.wardrift_username, cfg.wardrift_password) == ("", "", "", "")


def test_site_keys_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    for field, value in (("wdgwars_api_key", "a" * 64), ("wardrift_api_key", "wd_key"),
                         ("wardrift_username", "pfefferle"), ("wardrift_password", "s3cr3t")):
        update_config_file(path, field, value)
    cfg = load_config(path)
    assert (cfg.wdgwars_api_key, cfg.wardrift_api_key, cfg.wardrift_username, cfg.wardrift_password) == \
        ("a" * 64, "wd_key", "pfefferle", "s3cr3t")


OLD_FILE = """# my notes
[wigle]
api_name = "AID1"  # mine
api_token = "tok"
"""


def test_missing_sections_appended_with_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_FILE)
    ensure_config(path)
    text = path.read_text()
    assert text.startswith(OLD_FILE)
    for section in ("[wdgwars]", "[wardrift]", "[ui]", "[drive]"):
        assert text.count(section) == 1, section
    assert text.count("[wigle]") == 1
    assert "https://wdgwars.pl/profile" in text and "removed after the first sign-in" in text
    cfg = load_config(path)
    assert cfg.wigle_api_name == "AID1" and cfg.wdgwars_api_key == "" and cfg.screen_mode == "pocket"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_adding_sections_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_FILE)
    ensure_config(path)
    once = path.read_text()
    ensure_config(path)
    assert path.read_text() == once


def test_complete_file_untouched(tmp_path):
    path = tmp_path / "config.toml"
    ensure_config(path)
    path.write_text(path.read_text() + "# my own comment\n")
    before = path.stat().st_mtime_ns, path.read_text()
    ensure_config(path)
    assert (path.stat().st_mtime_ns, path.read_text()) == before


def test_concurrent_updates_keep_both(tmp_path):
    import threading
    path = tmp_path / "config.toml"
    ensure_config(path)
    fields = ["wigle_api_name", "wdgwars_api_key", "wardrift_api_key", "wardrift_username"]
    threads = [threading.Thread(target=lambda f=f: [update_config_file(path, f, f"v{i}") for i in range(40)])
               for f in fields]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    cfg = load_config(path)
    assert [getattr(cfg, f) for f in fields] == ["v39"] * 4

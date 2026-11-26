import gzip

from pfadfinder.storage.gzwriter import GzipMemberWriter


def test_members_concatenate(tmp_path):
    w = GzipMemberWriter(tmp_path / "a.gz")
    w.write("one\n"); w.flush(); w.write("two\n"); w.flush()
    assert gzip.open(tmp_path / "a.gz", "rt").read() == "one\ntwo\n"


def test_crash_after_flush_keeps_data(tmp_path):
    w = GzipMemberWriter(tmp_path / "a.gz")
    w.write("one\n"); w.flush(); w.write("lost\n")
    del w  # no close: simulates power loss
    assert gzip.open(tmp_path / "a.gz", "rt").read() == "one\n"


def test_empty_flush_writes_nothing(tmp_path):
    w = GzipMemberWriter(tmp_path / "a.gz")
    w.flush()
    assert not (tmp_path / "a.gz").exists() or (tmp_path / "a.gz").stat().st_size == 0


def test_close_flushes(tmp_path):
    w = GzipMemberWriter(tmp_path / "a.gz")
    w.write("x\n"); w.close()
    assert gzip.open(tmp_path / "a.gz", "rt").read() == "x\n"


def test_partial_write_rolled_back(tmp_path, monkeypatch):
    import os as _os
    from pfadfinder.storage import gzwriter
    w = GzipMemberWriter(tmp_path / "a.gz")
    w.write("one\n"); w.flush()
    real_write = gzwriter._write
    calls = {"n": 0}

    def half_then_fail(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(data[: len(data) // 2]))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gzwriter, "_write", half_then_fail)
    w.write("two\n")
    try:
        w.flush()
    except OSError:
        pass
    monkeypatch.setattr(gzwriter, "_write", real_write)
    w.flush()  # retried after the disk has room again
    assert gzip.open(tmp_path / "a.gz", "rt").read() == "one\ntwo\n"


def test_repair_truncates_torn_tail(tmp_path):
    from pfadfinder.storage.gzwriter import repair
    p = tmp_path / "a.gz"
    good = gzip.compress(b"one\n") + gzip.compress(b"two\n")
    p.write_bytes(good + gzip.compress(b"three\n")[:9])
    repair(p)
    assert p.read_bytes() == good and gzip.open(p, "rt").read() == "one\ntwo\n"

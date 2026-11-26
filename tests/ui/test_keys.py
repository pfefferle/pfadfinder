import os
import struct

from pfadfinder.ui.keys import KeyReader, decode

KEY_A, KEY_1, KEY_ENTER, KEY_ESC, KEY_LEFTSHIFT, KEY_UP, KEY_SEMICOLON = 30, 2, 28, 1, 42, 103, 39


def test_decode_plain():
    assert decode(KEY_A, False) == "a" and decode(KEY_1, False) == "1"
    assert decode(KEY_ENTER, False) == "ENTER" and decode(KEY_ESC, False) == "ESC" and decode(KEY_UP, False) == "UP"


def test_decode_shift():
    assert decode(KEY_A, True) == "A" and decode(KEY_1, True) == "!" and decode(KEY_SEMICOLON, True) == ":"


def test_decode_unknown():
    assert decode(250, False) is None


def ev(code, value, type_=1):
    return struct.pack("llHHi", 0, 0, type_, code, value)


def test_reader_tracks_shift():
    r, w = os.pipe()
    os.write(w, ev(KEY_LEFTSHIFT, 1) + ev(KEY_A, 1) + ev(KEY_A, 0) + ev(KEY_LEFTSHIFT, 0) + ev(KEY_A, 1)
             + ev(0, 0, type_=0))
    reader = KeyReader(fds=[r])
    assert reader.read(0.1) == ["A", "a"]


def test_reader_timeout_empty():
    r, _ = os.pipe()
    assert KeyReader(fds=[r]).read(0.01) == []

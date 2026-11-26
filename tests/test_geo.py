from pfadfinder.geo import freq_to_channel, haversine_m


def test_freq_to_channel():
    assert freq_to_channel(2412) == 1
    assert freq_to_channel(2437.0) == 6
    assert freq_to_channel(2484) == 14
    assert freq_to_channel(5180) == 36
    assert freq_to_channel(5955) == 1
    assert freq_to_channel(900) == 0


def test_haversine_m():
    assert abs(haversine_m(52.0, 13.0, 52.0, 13.001) - 68.5) < 1.0

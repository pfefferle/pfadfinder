from PIL import Image

from pfadfinder.ui.fb import Backlight, Framebuffer, to_rgb565


def test_rgb565_red():
    assert to_rgb565(Image.new("RGB", (1, 1), (255, 0, 0))) == b"\x00\xf8"


def test_rgb565_white_green_blue():
    img = Image.new("RGB", (3, 1))
    img.putpixel((0, 0), (255, 255, 255)); img.putpixel((1, 0), (0, 255, 0)); img.putpixel((2, 0), (0, 0, 255))
    assert to_rgb565(img) == b"\xff\xff\xe0\x07\x1f\x00"


def test_rgb565_shape():
    assert len(to_rgb565(Image.new("RGB", (320, 170)))) == 320 * 170 * 2


def test_framebuffer_stride_padding(tmp_path):
    dev = tmp_path / "fb0"
    dev.write_bytes(b"\xaa" * (8 * 2))
    fb = Framebuffer(dev, size=(2, 2), stride=8)
    fb.show(Image.new("RGB", (2, 2), (255, 0, 0)))
    data = dev.read_bytes()
    assert data[0:4] == b"\x00\xf8\x00\xf8" and data[8:12] == b"\x00\xf8\x00\xf8"
    assert data[4:8] == b"\xaa" * 4  # padding untouched


def test_backlight_levels(tmp_path):
    bl = tmp_path / "sys/class/backlight/panel"
    bl.mkdir(parents=True)
    (bl / "max_brightness").write_text("255\n")
    (bl / "brightness").write_text("200\n")
    b = Backlight(tmp_path)
    b.set(0); assert (bl / "brightness").read_text().strip() == "0"
    b.set(0.3); assert (bl / "brightness").read_text().strip() == "60"
    b.set(1); assert (bl / "brightness").read_text().strip() == "200"  # the user's own brightness is "full"


def test_backlight_missing_is_noop(tmp_path):
    Backlight(tmp_path).set(0)

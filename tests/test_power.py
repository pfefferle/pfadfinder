from pfadfinder.power import PowerManager


def write(root, rel, value):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")
    return p


def fake_power_tree(root):
    write(root, "sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "ondemand")
    for n in (1, 2, 3):
        write(root, f"sys/devices/system/cpu/cpu{n}/online", "1")
    write(root, "sys/bus/usb/devices/1-1.1/idVendor", "0fe6")
    write(root, "sys/bus/usb/devices/1-1.1/idProduct", "9900")
    write(root, "sys/bus/usb/devices/1-1.1/authorized", "1")
    write(root, "sys/bus/usb/devices/1-1.2/idVendor", "0bda")
    write(root, "sys/bus/usb/devices/1-1.2/idProduct", "2838")
    write(root, "sys/bus/usb/devices/1-1.2/authorized", "1")


def read(root, rel):
    return (root / rel).read_text().strip()


def test_power_enter_leave_restores(tmp_path):
    fake_power_tree(tmp_path)
    pm = PowerManager(tmp_path)
    pm.enter_wardrive(cores=1)
    assert read(tmp_path, "sys/devices/system/cpu/cpufreq/policy0/scaling_governor") == "powersave"
    assert [read(tmp_path, f"sys/devices/system/cpu/cpu{n}/online") for n in (1, 2, 3)] == ["0", "0", "0"]
    assert read(tmp_path, "sys/bus/usb/devices/1-1.1/authorized") == "0"
    assert read(tmp_path, "sys/bus/usb/devices/1-1.2/authorized") == "1"  # not the Ethernet chip
    pm.leave_wardrive()
    assert read(tmp_path, "sys/devices/system/cpu/cpufreq/policy0/scaling_governor") == "ondemand"
    assert [read(tmp_path, f"sys/devices/system/cpu/cpu{n}/online") for n in (1, 2, 3)] == ["1", "1", "1"]
    assert read(tmp_path, "sys/bus/usb/devices/1-1.1/authorized") == "1"


def test_enter_keeps_needed_cores(tmp_path):
    fake_power_tree(tmp_path)
    PowerManager(tmp_path).enter_wardrive(cores=2)
    assert [read(tmp_path, f"sys/devices/system/cpu/cpu{n}/online") for n in (1, 2, 3)] == ["1", "0", "0"]


def test_permission_errors_ignored(tmp_path):
    fake_power_tree(tmp_path)
    gov = tmp_path / "sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
    gov.unlink()
    gov.mkdir()  # writing fails with an OSError even as root (tests run as root in the container)
    pm = PowerManager(tmp_path)
    pm.enter_wardrive(cores=1)  # must not raise
    pm.leave_wardrive()


def test_battery_from_power_now(tmp_path):
    b = "sys/class/power_supply/bq27220-0"
    write(tmp_path, f"{b}/type", "Battery")
    write(tmp_path, f"{b}/capacity", "72")
    write(tmp_path, f"{b}/voltage_now", "3850000")
    write(tmp_path, f"{b}/power_now", "3200000")
    info = PowerManager(tmp_path).battery()
    assert (info.percent, info.voltage_v, info.power_w) == (72, 3.85, 3.2)


def test_battery_from_current_voltage(tmp_path):
    b = "sys/class/power_supply/bq27220-0"
    write(tmp_path, f"{b}/type", "Battery")
    write(tmp_path, f"{b}/capacity", "50")
    write(tmp_path, f"{b}/voltage_now", "4000000")
    write(tmp_path, f"{b}/current_now", "-800000")
    write(tmp_path, "sys/class/power_supply/usb/type", "USB")
    info = PowerManager(tmp_path).battery()
    assert info.percent == 50 and abs(info.power_w - 3.2) < 1e-9


def test_no_battery(tmp_path):
    assert PowerManager(tmp_path).battery() is None

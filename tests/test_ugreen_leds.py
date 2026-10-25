"""Unit tests for ugreen_leds.py against a simulated LED controller.

FakeController stands in for I2CDevice: it decodes the 12-byte command frames, keeps per-LED
state, answers register 0x80 and the 0x81 + id status reads, and can be told to drop commands
the way the real MCU does under load. Stdlib only:  python3 -m unittest discover -s tests
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ugreen_leds as u  # noqa: E402


class FakeController:
    """The UGREEN LED MCU as seen through i2c-dev."""

    MODE_CODES = {"off": 0, "on": 1, "blink": 2, "breath": 3}

    def __init__(self):
        self.state = {}       # led id → dict(mode, brightness, rgb, t_cycle, t_on)
        self.frames = []      # every frame written, accepted or not
        self.drop = 0         # reject this many upcoming frames
        self.last_ok = 0

    def led(self, led):
        return self.state.setdefault(led, {"mode": "off", "brightness": 0, "rgb": (0, 0, 0),
                                           "t_cycle": 0, "t_on": 0})

    def write_i2c_block(self, register, payload):
        frame = list(payload)
        self.frames.append((register, frame))
        self.last_ok = 0
        if self.drop:
            self.drop -= 1
            return
        if len(frame) != 12 or frame[0] != register or frame[1:5] != [0xA0, 0x01, 0, 0]:
            return
        if sum(frame[1:10]) != (frame[10] << 8 | frame[11]):
            return
        led, cmd, p = self.led(register), frame[5], frame[6:10]
        if cmd == u.Controller.CMD_BRIGHTNESS:
            led["brightness"] = p[0]
        elif cmd == u.Controller.CMD_COLOR:
            led["rgb"] = tuple(p[:3])
        elif cmd == u.Controller.CMD_ONOFF:
            led["mode"] = "on" if p[0] else "off"
        elif cmd in (u.Controller.CMD_BLINK, u.Controller.CMD_BREATH):
            led["mode"] = "blink" if cmd == u.Controller.CMD_BLINK else "breath"
            led["t_cycle"], led["t_on"] = p[0] << 8 | p[1], p[2] << 8 | p[3]
        else:
            return
        self.last_ok = 1

    def read_byte(self, register):
        assert register == 0x80, f"unexpected byte read from 0x{register:02x}"
        return self.last_ok

    def read_i2c_block(self, register, length):
        led = self.led(register - 0x81)
        raw = [self.MODE_CODES[led["mode"]], led["brightness"], *led["rgb"],
               led["t_cycle"] >> 8, led["t_cycle"] & 0xFF, led["t_on"] >> 8, led["t_on"] & 0xFF]
        cksum = sum(raw)
        return bytes(raw + [cksum >> 8, cksum & 0xFF])[:length]

    def commands(self, led=None):
        """Command bytes of the frames sent, optionally for one LED."""
        return [f[5] for r, f in self.frames if led is None or r == led]


class SimTestCase(unittest.TestCase):
    def setUp(self):
        # The controller's pacing sleeps add up to seconds over a suite; they are not under test.
        patcher = mock.patch.object(u.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dev = FakeController()
        self.ctl = u.Controller(self.dev)


class ControllerTest(SimTestCase):
    def test_frame_layout_and_checksum(self):
        self.assertTrue(self.ctl.color(2, (255, 128, 1)))
        register, frame = self.dev.frames[0]
        self.assertEqual(register, 2)
        body = [0xA0, 0x01, 0x00, 0x00, 0x02, 255, 128, 1, 0]
        self.assertEqual(frame, [2] + body + [sum(body) >> 8, sum(body) & 0xFF])

    def test_blink_encodes_cycle_and_on_time(self):
        self.ctl.blink(3, 100, 150)
        self.assertEqual(self.dev.frames[0][1][6:10], [0, 250, 0, 100])
        self.assertEqual(self.ctl.status(3)["t_off"], 150)

    def test_retries_until_accepted(self):
        self.dev.drop = 4
        self.assertTrue(self.ctl.on(2))
        self.assertEqual(len(self.dev.frames), 5)
        self.assertEqual(self.dev.led(2)["mode"], "on")

    def test_gives_up_after_retry_budget_without_logging(self):
        self.dev.drop = 99
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(self.ctl.on(2))
        self.assertEqual(len(self.dev.frames), u.Controller.CMD_RETRIES)
        self.assertEqual(out.getvalue(), "")

    def test_status_round_trip(self):
        self.ctl.brightness(0, 64)
        self.ctl.color(0, (10, 20, 30))
        self.ctl.on(0)
        self.assertEqual(self.ctl.status(0), {"mode": "on", "brightness": 64,
                                              "rgb": (10, 20, 30), "t_on": 0, "t_off": 0})

    def test_status_rejects_bad_checksum(self):
        self.dev.read_i2c_block = lambda register, length: bytes([1] * 9 + [0, 0])
        self.assertIsNone(self.ctl.status(0))


class LedTest(SimTestCase):
    def setUp(self):
        super().setUp()
        self.led = u.Led(self.ctl, "disk1")

    def test_first_apply_sets_brightness_colour_and_mode(self):
        self.led.apply("on", (1, 2, 3))
        self.assertEqual(self.dev.led(2), {"mode": "on", "brightness": u.BRIGHTNESS,
                                           "rgb": (1, 2, 3), "t_cycle": 0, "t_on": 0})

    def test_unchanged_state_sends_nothing(self):
        self.led.apply("on", (1, 2, 3))
        sent = len(self.dev.frames)
        self.led.apply("on", (1, 2, 3))
        self.assertEqual(len(self.dev.frames), sent)

    def test_mode_change_sends_only_the_mode(self):
        self.led.apply("on", (1, 2, 3))
        self.dev.frames.clear()
        self.led.apply("blink", (1, 2, 3))
        self.assertEqual(self.dev.commands(), [u.Controller.CMD_BLINK])
        self.assertEqual(self.dev.led(2)["mode"], "blink")

    def test_force_resends_everything(self):
        self.led.apply("on", (1, 2, 3))
        self.dev.frames.clear()
        self.led.apply("on", (1, 2, 3), force=True)
        self.assertEqual(self.dev.commands(), [u.Controller.CMD_BRIGHTNESS,
                                               u.Controller.CMD_COLOR, u.Controller.CMD_ONOFF])

    def test_off_skips_colour(self):
        self.led.apply("off", (1, 2, 3))
        self.assertNotIn(u.Controller.CMD_COLOR, self.dev.commands())

    def test_rejected_command_is_resent_next_tick(self):
        self.led.apply("on", (1, 2, 3))
        self.dev.drop = u.Controller.CMD_RETRIES
        self.led.apply("blink", (1, 2, 3))
        self.assertEqual(self.dev.led(2)["mode"], "on")
        self.led.apply("blink", (1, 2, 3))
        self.assertEqual(self.dev.led(2)["mode"], "blink")

    def test_isolated_rejection_is_not_logged(self):
        self.led.apply("on", (1, 2, 3))
        self.dev.drop = u.Controller.CMD_RETRIES
        out = io.StringIO()
        with redirect_stdout(out):
            self.led.apply("blink", (1, 2, 3))
            self.led.apply("blink", (1, 2, 3))
        self.assertEqual(out.getvalue(), "")

    def test_stuck_led_logs_once_then_recovery(self):
        self.led.apply("on", (1, 2, 3))
        stuck = u.Led.STUCK_TICKS
        self.dev.drop = (stuck + 2) * u.Controller.CMD_RETRIES
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(stuck + 3):
                self.led.apply("blink", (1, 2, 3))
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(f"disk1: controller has rejected blink {stuck} ticks running", lines[0])
        self.assertIn("disk1: controller accepting commands again", lines[1])
        self.assertEqual(self.dev.led(2)["mode"], "blink")


class DaemonTest(SimTestCase):
    """Drive Daemon.tick() against a fixture /sys and /proc."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sys, self.proc = os.path.join(tmp.name, "sys"), os.path.join(tmp.name, "proc")
        for name, value in (("SYSFS", self.sys), ("PROCFS", self.proc), ("NET_IFACES", [])):
            patcher = mock.patch.object(u, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.add_disk("sda", "ata1")
        self.add_disk("sdb", "ata3")
        self.add_nic("eno1", carrier="1")
        self.set_pool("tank", "ONLINE")
        with redirect_stdout(io.StringIO()):
            self.daemon = u.Daemon(self.ctl)
            self.daemon.rescan()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def add_disk(self, dev, port):
        target = os.path.join(self.sys, f"devices/pci0000:00/{port}/host0/block/{dev}")
        self.write(os.path.join(target, "stat"), "0 0 0 0")
        os.makedirs(os.path.join(self.sys, "block"), exist_ok=True)
        os.symlink(target, os.path.join(self.sys, "block", dev))

    def disk_io(self, dev):
        path = os.path.join(self.sys, "block", dev, "stat")
        with open(path) as fh:
            reads = int(fh.read().split()[0])
        self.write(path, f"{reads + 1} 0 0 0")

    def add_nic(self, name, carrier):
        base = os.path.join(self.sys, "class/net", name)
        os.makedirs(os.path.join(base, "device"))
        self.write(os.path.join(base, "carrier"), carrier)
        for d in ("rx", "tx"):
            self.write(os.path.join(base, f"statistics/{d}_bytes"), "0")

    def set_pool(self, pool, state):
        self.write(os.path.join(self.proc, f"spl/kstat/zfs/{pool}/state"), state)

    def mode(self, name):
        return self.dev.led(u.LED_IDS[name])["mode"]

    def test_discovery_maps_ata_ports_to_bays(self):
        self.assertEqual(self.daemon.bays, {1: "sda", 3: "sdb"})
        self.assertEqual(self.daemon.nics, ["eno1"])

    def test_steady_state(self):
        self.daemon.tick(0.0, force=True)
        self.assertEqual([self.mode(n) for n in ("power", "netdev", "disk1", "disk2", "disk3",
                                                 "disk4")],
                         ["on", "on", "on", "off", "on", "off"])
        self.assertEqual(self.dev.led(u.LED_IDS["disk1"])["rgb"], u.COLOR_DISK)

    def test_disk_io_blinks_then_settles_after_hold(self):
        self.daemon.tick(0.0, force=True)
        self.disk_io("sda")
        self.daemon.tick(1.0)
        self.assertEqual(self.mode("disk1"), "blink")
        self.assertEqual(self.mode("disk3"), "on")
        self.daemon.tick(1.0 + u.HOLD + 0.01)
        self.assertEqual(self.mode("disk1"), "on")

    def test_network_traffic_blinks_and_no_carrier_is_off(self):
        self.daemon.tick(0.0, force=True)
        self.write(os.path.join(self.sys, "class/net/eno1/statistics/rx_bytes"), "1500")
        self.daemon.tick(1.0)
        self.assertEqual(self.mode("netdev"), "blink")
        self.write(os.path.join(self.sys, "class/net/eno1/carrier"), "0")
        self.daemon.tick(2.0)
        self.assertEqual(self.mode("netdev"), "off")

    def test_degraded_pool_turns_disks_red(self):
        self.daemon.tick(0.0, force=True)
        self.set_pool("tank", "DEGRADED")
        with redirect_stdout(io.StringIO()):
            self.daemon.rescan()
        self.daemon.tick(1.0)
        self.assertEqual(self.dev.led(u.LED_IDS["disk1"])["rgb"], u.COLOR_FAIL)

    def test_settle_leaves_blinking_leds_steady(self):
        self.daemon.tick(0.0, force=True)
        self.disk_io("sda")
        self.daemon.tick(1.0)
        self.daemon.settle()
        self.assertEqual(self.mode("disk1"), "on")


if __name__ == "__main__":
    unittest.main()

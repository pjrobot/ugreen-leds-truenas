# ugreen-leds-truenas

Front-panel LEDs for UGREEN DX/DXP NAS units running TrueNAS SCALE, with **no kernel module**:
power and network LEDs, plus one per drive bay, behaving the way UGOS drives them.

| LED | Behaviour |
| --- | --- |
| Power | steady |
| Network | steady with link, blinks on traffic like a switch port LED, off without link |
| Bay 1–n | steady while a SATA disk is present, blinks on disk I/O, **red while any ZFS pool is not `ONLINE`**, off when the bay is empty |

It installs as a **TrueNAS Custom App** from a single YAML paste. It survives TrueNAS updates,
because it never touches the read-only root filesystem.

**Tested:** UGREEN DXP4800 Plus on TrueNAS SCALE 25.04.2.6 (see the compatibility table below). Other
DX/DXP models use the same controller and should work; please report back.

## Why not the existing options

The LED controller was reverse-engineered by
[miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller), which every
other guide builds on. Its main path is an out-of-tree kernel module (`led-ugreen`). On TrueNAS
that module has to be rebuilt against the exact kernel and re-installed after **every** update,
because the root filesystem is read-only and replaced wholesale — the single most-reported
problem in [upstream issue #13](https://github.com/miskcoo/ugreen_leds_controller/issues/13).
The cron-job alternative using the upstream CLI survives updates but only shows pool health every
few minutes, with no activity blinking.

The controller is just an I2C device (address `0x3a` on the Intel SMBus). This project speaks
the same wire protocol from userspace through the stock `i2c-dev` interface, in ~300 lines of
standard-library Python, packaged as a container. Nothing depends on the kernel version.

## Install on TrueNAS SCALE

1. **Load `i2c-dev` at boot.** The TrueNAS kernel ships it but doesn't load it, and a container
   can't `modprobe` on the host. **System → Advanced → Init/Shutdown Scripts → Add:**
   type *Command*, command `modprobe i2c-dev`, when *Pre Init*, enabled. Init scripts live in
   the TrueNAS config database, so this survives updates and is in your config backup.

   Load it now without rebooting, from **System → Shell**:

   ```bash
   sudo modprobe i2c-dev
   grep -l 'SMBus I801' /sys/class/i2c-dev/i2c-*/device/name   # the bus the daemon will pick
   ```

2. **Install the app.** **Apps → Discover Apps → Custom App → Install via YAML.** Name it
   `ugreen-leds` and paste [`ugreen-leds.yml`](ugreen-leds.yml) unchanged. The daemon is
   embedded in the YAML under `configs:`, so there is nothing to copy onto the NAS.

   Within a few seconds the power and network LEDs come on and the bay LEDs light, then flicker
   as the disks are read.

3. **Verify.**

   ```bash
   sudo docker logs ugreen-leds                                      # bus found; bay1=sda …
   sudo docker exec ugreen-leds python3 /app/ugreen_leds.py status    # per-LED controller state
   sudo dd if=/dev/sda of=/dev/null bs=1M count=512 iflag=direct      # bay 1 alone should blink
   ```

   If the wrong bay blinks, see *Bay mapping* below.

To change a setting later, edit the app's YAML in the UI and redeploy. The file has no
placeholders and no secrets, so pasting a newer `ugreen-leds.yml` over the running app's YAML is
safe.

## Settings

All optional, set as environment variables on the service in the YAML:

| Variable | Default | Meaning |
| --- | --- | --- |
| `UGREEN_BRIGHTNESS` | `64` | 0–255. UGOS runs the panel fairly dim. |
| `UGREEN_BAYS` | `ata1,ata2,ata3,ata4` | SATA ports in physical bay order. Add ports for 6/8-bay units. |
| `UGREEN_NET_MIN_BYTES` | `1` | Bytes per sample before the network LED counts it as traffic. `65536` (~5 Mbit/s at the default poll) ignores background chatter; `1048576` blinks only for LAN-speed transfers. |
| `UGREEN_NET_IFACES` | all NICs with a device | Comma-separated interfaces for the network LED. |
| `UGREEN_COLOR_POWER` / `_NETDEV` / `_DISK` / `_FAIL` | white, white, white, red | `R,G,B` each 0–255. |
| `UGREEN_POLL_SECONDS` | `0.1` | Activity sampling interval. |
| `UGREEN_HOLD_SECONDS` | `0.3` | Keep blinking this long after the last I/O. |
| `UGREEN_RESCAN_SECONDS` | `30` | Disk/NIC/pool re-discovery and full LED re-sync. |
| `UGREEN_I2C_DEV` | auto | Force a `/dev/i2c-N` instead of detecting the SMBus I801 adapter. |

The container runs `privileged` (for the `/dev/i2c-*` nodes and the `I2C_SMBUS` ioctl — the same
posture as Scrutiny's disk passthrough) with `network_mode: host` (so the network LED sees the
host's NICs; nothing is published). It idles around 12 MiB under a 64 MB cap.

## Bay mapping

Bays are mapped by the SATA port in each disk's sysfs path (`ata1`…), which is stable across
boots unlike `/dev/sdX`. On the DXP4800 Plus `ata1`–`ata4` are bays 1–4 in order. Other models
may wire the ports differently — the DXP6800 Pro, for example, is known upstream to be
`ata3,ata4,ata5,ata6,ata1,ata2`. The `dd` in step 3 is the test: if the wrong bay blinks, run
`status` to see which disk is on which port and set `UGREEN_BAYS` to the ports in physical order.

## Compatibility

| Model | OS | Status |
| --- | --- | --- |
| DXP4800 Plus | TrueNAS SCALE 25.04.2.6 | ✅ tested |
| other DX/DXP | any Linux with `i2c-dev` | untested — same controller per upstream; please open an issue with your `status` output |

## Running without Docker

`ugreen_leds.py` is a standalone script (Python 3.8+, no dependencies) and the same code that is
embedded in the YAML. For Debian, Proxmox or any systemd host with a writable root, see
[`ugreen-leds.service`](ugreen-leds.service). Not recommended on TrueNAS, where the root
filesystem is reset on every update.

`UGREEN_DRY_RUN=1` logs the I2C frames instead of sending them, and `UGREEN_SYSFS` /
`UGREEN_PROCFS` point the discovery at a fixture tree, which is how it is tested without
hardware. `make test` runs the unit tests in [`tests/`](tests/) against a simulated LED
controller that speaks the wire protocol below (and drops commands on request, like the real
one under load); `make check` runs them too and confirms the YAML embeds the same daemon.

## How it works

Each command is a 12-byte SMBus I2C-block write to register *led id*:
`[id, 0xA0, 0x01, 0x00, 0x00, cmd, p1, p2, p3, p4, cksum_hi, cksum_lo]`, where the checksum is
the plain sum of bytes 1–9. Commands: `0x01` brightness, `0x02` colour (R,G,B), `0x03` on/off,
`0x04` blink and `0x05` breathe (cycle and on-time in ms, big-endian). Register `0x80` reads `1`
if the last command was accepted; an 11-byte block read from `0x81 + id` returns the LED's
state. LED ids: power 0, network 1, bays 2–9.

Blinking uses the controller's own hardware blink mode (100 ms on / 100 ms off), so an I/O burst
costs two writes — blink on, steady again — rather than one per sample. Activity is detected by
watching `/sys/block/<dev>/stat` and `/sys/class/net/<if>/statistics`; pool health comes from
`/proc/spl/kstat/zfs/<pool>/state`.

## Credits and licence

The protocol, the LED ids and the model-specific port orders come from
[miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller) (MIT), which
did all the reverse-engineering. This is an independent userspace implementation of that
protocol, MIT licensed — see [LICENSE](LICENSE).

# udev rule for the Zigbee dongle

The dongle's serial node (e.g. `/dev/ttyUSB0`) is created by the kernel as
`root:dialout` with mode `0660`. If your user isn't in `dialout`, opening it
fails with `Permission denied`. A **udev rule** is the clean, persistent way to
change that ownership/permissions so the daemon can read the device.

No `apt install` is required — the `cp210x` kernel driver is already loaded.

## Install

```bash
cd udev
./install.sh --user "$USER"   # recommended: you own the device, immediate access
```

Then **re-plug the dongle** or run `sudo udevadm trigger`.

Verify:

```bash
ls -l /dev/ttyUSB0           # should show owner '$USER', mode 0600
.venv/bin/python -c "import serial; s=serial.Serial('/dev/ttyUSB0', timeout=1); print('OK'); s.close()"
```

## Options

```bash
./install.sh --user alice                      # alice owns the device (immediate)
./install.sh --group zigbee --mode 0660        # group-based access (needs re-login)
./install.sh --vendor 10c4 --product ea60      # match a different dongle
```

**Access model:**
- `--user USER` → rule sets `OWNER=USER, MODE=0600`. The user directly owns
  the device, so access is **immediate** (no re-login / `newgrp` needed).
- Without `--user` → rule sets `GROUP=GROUP, MODE=0660` and adds the current
  user to that group. Takes effect in a **new login session** (or `newgrp`).

The rule matches the dongle by its **parent USB vendor:product** id
(`10c4:ea60` for the Sonoff Zigbee 3.0 USB Dongle Plus). Find your dongle's ids
with:

```bash
lsusb | grep -i "silicon labs\|conbee\|sonoff"
```

## What the rule does

With `--user` (recommended):

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", OWNER="rschoene", MODE="0600"
```

Without `--user` (group-based):

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", GROUP="dialout", MODE="0660"
```

- `SUBSYSTEM=="tty"` — only serial nodes.
- `ATTRS{idVendor}/idProduct` — match the dongle's parent USB device.
- `OWNER="user"` / `GROUP="group"` — set the owner or group owner.
- `MODE` — `0600` (owner only) or `0660` (owner + group).

## Alternatives

- **Dedicated group** — `./install.sh --group zigbee` keeps Zigbee access
  separate from general serial access.
- **`uaccess`** — `TAG+="uaccess"` grants access to whoever plugged the device
  in, with no group needed (requires a recent systemd/udev).

## Uninstall

```bash
sudo rm /etc/udev/rules.d/99-zigbee-dongle.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

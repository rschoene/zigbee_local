"""USB device scanning and Zigbee dongle identification.

Reads ``/sys/bus/usb/devices/`` to enumerate USB devices without requiring
``lsusb``. Matches against a table of known Zigbee dongle vendor:product IDs
and resolves the corresponding serial device node (``/dev/ttyUSB*`` or
``/dev/ttyACM*``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Known Zigbee dongle IDs
# --------------------------------------------------------------------------- #
# (vendor, product) -> {name, radio, description}
#
# `radio` is the value to use in config.yaml (matches _load_radio_class keys).
KNOWN_ZIGBEE_DONGLES: dict[tuple[str, str], dict[str, str]] = {
    # Silicon Labs CP210x UART bridge (many dongles use this)
    ("10c4", "ea60"): {
        "name": "Silicon Labs CP210x",
        "radio": "zigpy-znp",
        "description": "Sonoff Zigbee 3.0 / CC2652 / CC1352 (ZNP)",
    },
    ("10c4", "8a28"): {
        "name": "Silicon Labs CP2102",
        "radio": "zigpy-deconz",
        "description": "Conbee / deCONZ (CC2531)",
    },
    ("10c4", "8bda"): {
        "name": "Silicon Labs CP2105",
        "radio": "zigpy-deconz",
        "description": "Conbee II/III / deCONZ (EM358)",
    },
    ("10c4", "8a60"): {
        "name": "Silicon Labs CP210x",
        "radio": "zigpy-znp",
        "description": "Zigbee dongle (ZNP)",
    },
    # Dresden Elektronik Conbee
    ("1366", "1051"): {
        "name": "Dresden Elektronik Conbee II",
        "radio": "zigpy-deconz",
        "description": "Conbee II (CC2531, deCONZ)",
    },
    ("1366", "1052"): {
        "name": "Dresden Elektronik Conbee III",
        "radio": "zigpy-deconz",
        "description": "Conbee III (EM358, deCONZ)",
    },
    ("1366", "1053"): {
        "name": "Dresden Elektronik Conbee 3",
        "radio": "zigpy-deconz",
        "description": "Conbee 3 (EM358, deCONZ)",
    },
    # TI CC2652 / CC1352 (ZNP)
    ("1c4f", "0042"): {
        "name": "TI CC2652P",
        "radio": "zigpy-znp",
        "description": "TI CC2652P (ZNP)",
    },
    ("1c4f", "0044"): {
        "name": "TI CC2652",
        "radio": "zigpy-znp",
        "description": "TI CC2652 (ZNP)",
    },
    ("1c4f", "0046"): {
        "name": "TI CC1352P",
        "radio": "zigpy-znp",
        "description": "TI CC1352P (ZNP)",
    },
    # WCH CH340 / CH343 (some cheap Zigbee dongles)
    ("1a86", "7523"): {
        "name": "WCH CH340",
        "radio": "zigpy-znp",
        "description": "Zigbee dongle (CH340 bridge, ZNP)",
    },
    ("1a86", "55d4"): {
        "name": "WCH CH343",
        "radio": "zigpy-znp",
        "description": "Zigbee dongle (CH343 bridge, ZNP)",
    },
}


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass
class UsbDevice:
    """A USB device as seen in /sys."""

    sysfs_path: str
    bus_id: str
    vendor_id: str
    product_id: str
    manufacturer: str
    product: str
    serial_device: str | None = None  # e.g. "/dev/ttyUSB0"
    is_zigbee: bool = False
    zigbee_info: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, PermissionError):
        return ""


def _find_serial_device(usb_sysfs: Path) -> str | None:
    """Find the serial device node for a USB device.

    Scans /sys/class/tty/ for ttyUSB* and ttyACM* entries (which are symlinks)
    and checks if their resolved path is under our USB device.
    """
    usb_real = usb_sysfs.resolve()
    tty_base = Path("/sys/class/tty")
    if not tty_base.exists():
        return None
    for entry in sorted(tty_base.iterdir()):
        if not entry.name.startswith(("ttyUSB", "ttyACM")):
            continue
        try:
            resolved = entry.resolve()
            # Walk up from the resolved tty path to find our USB device
            parent = resolved.parent
            while parent != parent.parent:
                if parent == usb_real:
                    return f"/dev/{entry.name}"
                parent = parent.parent
        except (OSError, RuntimeError):
            continue
    return None


def scan_usb_devices() -> list[UsbDevice]:
    """Scan /sys/bus/usb/devices/ and return all USB devices.

    Marks known Zigbee dongles and resolves their serial device nodes.
    """
    usb_base = Path("/sys/bus/usb/devices")
    if not usb_base.exists():
        return []

    devices: list[UsbDevice] = []
    for entry in sorted(usb_base.iterdir()):
        if not entry.is_dir():
            continue
        # Skip the root hub (bus_id like "1", "2", etc. without a dash)
        bus_id = entry.name
        if "-" not in bus_id:
            continue

        vendor_id = _read_sysfs(entry / "idVendor")
        product_id = _read_sysfs(entry / "idProduct")
        manufacturer = _read_sysfs(entry / "manufacturer")
        product = _read_sysfs(entry / "product")

        if not vendor_id or not product_id:
            continue

        # Check if it's a known Zigbee dongle
        key = (vendor_id.lower(), product_id.lower())
        zigbee_info = KNOWN_ZIGBEE_DONGLES.get(key, {})
        is_zigbee = bool(zigbee_info)

        # Find the serial device
        serial_dev = _find_serial_device(entry)

        devices.append(
            UsbDevice(
                sysfs_path=str(entry),
                bus_id=bus_id,
                vendor_id=vendor_id,
                product_id=product_id,
                manufacturer=manufacturer,
                product=product,
                serial_device=serial_dev,
                is_zigbee=is_zigbee,
                zigbee_info=zigbee_info,
            )
        )

    return devices


def find_zigbee_dongles() -> list[UsbDevice]:
    """Return only the devices identified as Zigbee dongles."""
    return [d for d in scan_usb_devices() if d.is_zigbee]


def format_device(d: UsbDevice, index: int | None = None) -> str:
    """Format a USB device for display."""
    prefix = f"  [{index}] " if index is not None else "  "
    vendor = d.manufacturer or f"0x{d.vendor_id}"
    product = d.product or f"0x{d.product_id}"
    line = f"{prefix}{vendor} {product}  ({d.vendor_id}:{d.product_id})"
    if d.serial_device:
        line += f"  ->  {d.serial_device}"
    if d.is_zigbee:
        line += "  ★ ZIGBEE"
        if d.zigbee_info.get("description"):
            line += f"  [{d.zigbee_info['description']}]"
    return line

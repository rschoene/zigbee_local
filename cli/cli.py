"""Command-line interface for the Zigbee daemon.

Subcommands
-----------
- ``devices``     list devices the daemon has seen (and their known metrics)
- ``add``         register a device AND monitor its metrics (add-device + monitor)
- ``add-device``  register a device (without monitoring metrics)
- ``monitor``     monitor metrics on a known device
- ``unmonitor``   stop monitoring a device (or specific metrics)
- ``monitored``   list the current monitored registry
- ``latest``     show the most recent reading for each monitored metric
- ``tail``       follow new readings as they arrive (like ``tail -f``)
- ``discover``   scan USB for Zigbee dongles (``-i`` to add one to config)
- ``permit``     allow new devices to join the network (via daemon socket)
- ``remove-device``  remove a device from the Zigbee network (via daemon socket)
- ``configure-reporting``  set a metric's reporting rate (via daemon socket)

``devices`` and ``monitored`` also report each source's read-status
(``OK`` / ``UNREADABLE: <reason>``) and flag devices that have no readings yet.

All commands read the same config file as the daemon to locate the SQLite
database, so they operate on the live data the daemon is writing.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime

# Allow running as a plain script (``python cli/cli.py``) as well as a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.config import load_config  # noqa: E402
from daemon.db import Database  # noqa: E402
from daemon.usb import (  # noqa: E402
    find_zigbee_dongles,
    format_device,
    scan_usb_devices,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load(args):
    """Load config and open the DB. Returns (config, db)."""
    config = load_config(args.config)
    return config, Database(config.database)


def _open_db(args) -> Database:
    """Open just the DB (for commands that don't need the config)."""
    _, db = _load(args)
    return db


def _configured_source_ids(config, source_filter: str | None) -> list[str]:
    """All configured source ids, optionally scoped to one source."""
    ids = [s.id for s in config.sources]
    if source_filter:
        ids = [i for i in ids if i == source_filter]
    return ids


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return ts


def _fmt_value(row) -> str:
    if row["value"] is not None:
        s = f"{row['value']:g}"
        if row["unit"]:
            s += f" {row['unit']}"
        return s
    return "-"


def _source_status(db: Database, source_id: str) -> str:
    """Return a short status tag for a source, e.g. 'OK' or 'UNREADABLE: ...'."""
    st = db.get_source_status(source_id)
    info = st.get(source_id)
    if not info:
        return "UNKNOWN (daemon not reporting)"
    if info["status"] == "ok":
        return "OK"
    return f"UNREADABLE: {info.get('detail') or 'error'}"


def _print_source_status(db: Database, source_ids: list[str]) -> None:
    """Print a status line for each source (only the non-OK ones are flagged)."""
    for sid in source_ids:
        tag = _source_status(db, sid)
        if tag == "OK":
            print(f"[{sid}] status: OK")
        else:
            print(f"[{sid}] status: {tag}")


def _resolve_source(args) -> str:
    """Resolve the source id, defaulting to the first configured source."""
    if args.source:
        return args.source
    config = load_config(args.config)
    if not config.sources:
        print("Error: no sources configured and no --source given.")
        sys.exit(1)
    source = config.sources[0].id
    print(f"No --source given; assuming first configured source: {source}")
    return source


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_devices(args) -> int:
    config, db = _load(args)
    try:
        # Report read-status for every configured source (so an unreadable
        # source is visible even if it has no devices yet).
        _print_source_status(db, _configured_source_ids(config, args.source))
        print()

        devices = db.list_devices(args.source)
        if not devices:
            print("No devices known yet. Is the daemon running?")
            return 0

        with_readings = db.devices_with_readings()
        for d in devices:
            metrics = ", ".join(d["metrics"]) if d["metrics"] else "(none)"
            name = d["name"] or d["device_id"]
            has_data = (d["source_id"], d["device_id"]) in with_readings
            flags = []
            if d["left_at"]:
                flags.append(f"left {d['left_at'][:19].replace('T', ' ')}")
            elif not has_data:
                flags.append("no readings yet")
            flag = "   (" + ", ".join(flags) + ")" if flags else ""
            print(f"[{d['source_id']}] {d['device_id']}  {name}{flag}")
            print(f"    metrics: {metrics}")
    finally:
        db.close()
    return 0


def cmd_add(args) -> int:
    source = _resolve_source(args)
    db = _open_db(args)
    try:
        db.upsert_device(source, args.device, name=args.name)
        db.add_monitored(source, args.device, args.metrics)
        print(f"Registered and monitoring {len(args.metrics)} metric(s) on [{source}] {args.device}: {', '.join(args.metrics)}")
    finally:
        db.close()
    return 0


def cmd_add_device(args) -> int:
    source = _resolve_source(args)
    db = _open_db(args)
    try:
        db.upsert_device(source, args.device, name=args.name)
        extra = f" as '{args.name}'" if args.name else ""
        print(f"Registered device [{source}] {args.device}{extra}")
    finally:
        db.close()
    return 0


def cmd_monitor(args) -> int:
    source = _resolve_source(args)
    db = _open_db(args)
    try:
        db.add_monitored(source, args.device, args.metrics)
        print(f"Monitoring {len(args.metrics)} metric(s) on [{source}] {args.device}: {', '.join(args.metrics)}")
    finally:
        db.close()
    return 0


def cmd_unmonitor(args) -> int:
    source = _resolve_source(args)
    db = _open_db(args)
    try:
        db.remove_monitored(source, args.device, args.metrics)
        what = ", ".join(args.metrics) if args.metrics else "all metrics"
        print(f"Stopped monitoring {what} on [{source}] {args.device}")
    finally:
        db.close()
    return 0


def cmd_monitored(args) -> int:
    config, db = _load(args)
    try:
        # Report read-status for every configured source.
        _print_source_status(db, _configured_source_ids(config, args.source))
        print()

        if getattr(args, "full", False):
            _print_monitored_full(db, args.source)
            return 0

        rows = db.list_monitored(args.source)
        if not rows:
            print("Nothing is being monitored yet. Use 'add' to register a device.")
            return 0

        with_readings = db.devices_with_readings()
        for r in rows:
            has_data = (r["source_id"], r["device_id"]) in with_readings
            flag = "" if has_data else "   (no readings yet)"
            print(f"[{r['source_id']}] {r['device_id']}  {r['metric']}{flag}")
    finally:
        db.close()
    return 0


def _print_monitored_full(db, source_id: str | None) -> None:
    """Print a table with one row per (device, metric).

    Columns: device, uid, metric, unit, monitored, in_network, rate.
    Rows are the union of every metric known from the ``devices`` table and
    every metric in the ``monitored`` table, so a metric you monitor before the
    daemon has seen the device still shows up (monitored=yes, in_network=no).
    """
    devices = db.list_devices(source_id)
    monitored = db.monitored_set()
    units = db.metric_units(source_id)
    reporting = db.get_reporting_config(source_id)

    names = {(d["source_id"], d["device_id"]): d["name"] for d in devices}
    in_database = {(d["source_id"], d["device_id"]) for d in devices}
    in_network = {
        (d["source_id"], d["device_id"]) for d in devices if d["left_at"] is None
    }
    # (source, device) -> {metric_name: [cluster_id, attr_id]}
    metric_maps = {
        (d["source_id"], d["device_id"]): d.get("metric_map") or {}
        for d in devices
    }

    # Collect the set of (source, device) we know about, and the metrics for each.
    device_metrics: dict[tuple[str, str], set[str]] = {}
    for d in devices:
        device_metrics.setdefault((d["source_id"], d["device_id"]), set()).update(d["metrics"])
    for s, dev, m in monitored:
        if source_id and s != source_id:
            continue
        device_metrics.setdefault((s, dev), set()).add(m)

    if not device_metrics:
        print("No devices or metrics known yet.")
        return

    def _rate_str(s: str, dev: str, metric: str) -> str:
        """Look up the configured reporting rate for a metric."""
        m_map = metric_maps.get((s, dev), {})
        pair = m_map.get(metric)
        if not pair:
            return ""
        cluster_id, attr_id = pair
        cfg = reporting.get((s, dev, cluster_id, attr_id))
        if not cfg:
            return ""
        return f"{cfg['min_interval']}-{cfg['max_interval']}s"

    # Each row: (source_id, uid, device_label, metric, unit, monitored, in_network, rate)
    rows = []
    for (s, dev), metrics in sorted(device_metrics.items()):
        label = names.get((s, dev)) or dev
        if not metrics:
            rows.append((s, dev, label, "(no metrics yet)", "", "no",
                         "yes" if (s, dev) in in_network else "no", ""))
            continue
        for m in sorted(metrics):
            unit = units.get((s, dev, m)) or ""
            mon = "yes" if (s, dev, m) in monitored else "no"
            net = "yes" if (s, dev) in in_network else "no"
            rate = _rate_str(s, dev, m)
            rows.append((s, dev, label, m, unit, mon, net, rate))

    headers = ("device", "uid", "metric", "unit", "monitored", "in_network", "rate")
    disp = [(r[2], r[1], r[3], r[4], r[5], r[6], r[7]) for r in rows]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in disp)) for i in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip()

    print(fmt(headers))
    print(fmt(tuple("-" * w for w in widths)))
    for r in disp:
        print(fmt(r))

    # Flag monitored devices the daemon has never seen (usually a typo in the
    # uid, or a device that hasn't joined the network yet).
    monitored_devices = {
        (s, dev) for s, dev, _m in monitored if not (source_id and s != source_id)
    }
    unseen = sorted(d for d in monitored_devices if d not in in_database)
    if unseen:
        print()
        print("⚠ monitored but not seen by the daemon (check the uid, or it hasn't joined yet):")
        for s, dev in unseen:
            print(f"    [{s}] {dev}")

    # Hint: how to monitor one metric, using the last row that has a real metric.
    real_rows = [r for r in rows if r[3] != "(no metrics yet)"]
    if real_rows:
        s, dev, _label, m, _unit, _mon, _net, _rate = real_rows[-1]
        print()
        print("To monitor a metric:  monitor -s <source> <uid> <metric>")
        print(f"e.g. for the last row:  monitor -s {s} {dev} {m}")


def cmd_latest(args) -> int:
    db = _open_db(args)
    try:
        rows = db.latest_readings(args.source)
        if not rows:
            print("No readings recorded yet.")
            return 0
        # Group by device for a tidy display.
        by_device: dict[tuple[str, str], list] = {}
        for r in rows:
            by_device.setdefault((r["source_id"], r["device_id"]), []).append(r)
        for (src, dev), items in by_device.items():
            print(f"[{src}] {dev}")
            for r in items:
                print(f"    {r['metric']:<20} {_fmt_value(r):>15}   @ {_fmt_ts(r['ts'])}")
    finally:
        db.close()
    return 0


def cmd_tail(args) -> int:
    db = _open_db(args)
    try:
        last_id = db.max_reading_id()
        print(f"Following new readings (Ctrl+C to stop)...")
        try:
            while True:
                rows = db.readings_since(last_id, limit=500)
                for r in rows:
                    last_id = r["id"]
                    print(
                        f"{_fmt_ts(r['ts'])}  [{r['source_id']}] {r['device_id']}  "
                        f"{r['metric']:<20} {_fmt_value(r)}"
                    )
                if rows:
                    sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    finally:
        db.close()
    return 0


def _send_command(config_path: str, cmd: dict) -> dict:
    """Send a JSON command to the daemon over its Unix socket."""
    config = load_config(config_path)
    db_dir = os.path.dirname(os.path.abspath(config.database)) or "."
    socket_path = os.path.join(db_dir, "daemon.sock")
    if not os.path.exists(socket_path):
        raise ConnectionError(
            f"Daemon socket not found at {socket_path}. Is the daemon running?"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(socket_path)
        s.sendall((json.dumps(cmd) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        data = s.recv(4096)
    return json.loads(data)


def _append_source_to_config(config_path: str, source_id: str, radio: str, path: str) -> None:
    """Append a zigpy source block to the config file.

    Our config layout keeps `sources:` as the last top-level key, so appending
    a list item at the end is safe and preserves existing comments.
    """
    block = (
        f"\n  # added by 'zigbee-cli discover'\n"
        f"  - id: {source_id}\n"
        f"    type: zigpy\n"
        f"    radio: {radio}\n"
        f"    path: {path}\n"
    )
    with open(config_path, "a") as f:
        f.write(block)


def cmd_permit(args) -> int:
    source = _resolve_source(args)
    try:
        result = _send_command(
            args.config,
            {"cmd": "permit_join", "source_id": source, "time_s": args.time},
        )
    except ConnectionError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1
    print(result.get("message", "OK"))
    return 0


def cmd_remove_device(args) -> int:
    source = _resolve_source(args)
    try:
        result = _send_command(
            args.config,
            {"cmd": "remove_device", "source_id": source, "device_id": args.device},
        )
    except ConnectionError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1
    print(result.get("message", "OK"))
    return 0


def cmd_configure_reporting(args) -> int:
    """Configure the reporting rate for a metric on a device.

    Resolves the metric name to its (cluster_id, attr_id) using the metric_map
    the daemon stored in the ``devices`` table, then asks the daemon to send a
    ZCL Configure Reporting command to the device.
    """
    source = _resolve_source(args)
    config, db = _load(args)
    try:
        # Resolve metric name -> [cluster_id, attr_id] from the stored map.
        device = next(
            (d for d in db.list_devices(source) if d["device_id"] == args.device),
            None,
        )
        if device is None:
            print(f"Error: device {args.device} not known to source {source}.")
            return 1
        metric_map = device.get("metric_map") or {}
        if args.metric not in metric_map:
            known = ", ".join(sorted(metric_map)) or "(none)"
            print(f"Error: metric {args.metric!r} not found for {args.device}.")
            print(f"Known metrics: {known}")
            return 1
        cluster_id, attr_id = metric_map[args.metric]

        cmd = {
            "cmd": "configure_reporting",
            "source_id": source,
            "device_id": args.device,
            "cluster_id": cluster_id,
            "attr_id": attr_id,
            "min_interval": args.min_interval,
            "max_interval": args.max_interval,
        }
        if args.change is not None:
            cmd["reportable_change"] = args.change

        try:
            result = _send_command(args.config, cmd)
        except ConnectionError as e:
            print(f"Error: {e}")
            return 1
        except Exception as e:
            print(f"Error: {e}")
            return 1

        if "error" in result:
            print(f"Error: {result['error']}")
            return 1
        print(result.get("message", "OK"))
        print(
            f"Reporting rate for {args.metric}: "
            f"every {args.min_interval}-{args.max_interval}s"
            + (f", change >= {args.change}" if args.change is not None else "")
            + "."
        )
        return 0
    finally:
        db.close()


def cmd_discover(args) -> int:
    devices = scan_usb_devices()

    print("=== USB devices ===")
    if not devices:
        print("  (none found)")
    for i, d in enumerate(devices):
        print(format_device(d, i))

    zigbee = [d for d in devices if d.is_zigbee]
    print()
    print("=== Zigbee dongles detected ===")
    if not zigbee:
        print("  No known Zigbee dongles detected.")
        print("  If your dongle is listed above but not marked, its USB id may not be")
        print("  in the known list (see daemon/usb.py KNOWN_ZIGBEE_DONGLES).")
    for i, d in enumerate(zigbee):
        print(format_device(d, i))

    if not args.interactive:
        return 0

    # --- interactive: pick a dongle to add to config.yaml ---------------- #
    if not zigbee:
        print("\nNothing to add (no Zigbee dongles detected).")
        return 0

    print("\nSelect a dongle to add to config.yaml:")
    for i, d in enumerate(zigbee):
        label = d.product or d.zigbee_info.get("name", "unknown")
        print(f"  [{i}] {label}  {d.serial_device or '(no serial device)'}")
    print("  [q] cancel")

    while True:
        choice = input("Choice: ").strip().lower()
        if choice in ("q", ""):
            print("Cancelled.")
            return 0
        if choice.isdigit() and 0 <= int(choice) < len(zigbee):
            break
        print("Invalid choice.")

    d = zigbee[int(choice)]
    if not d.serial_device:
        print("This dongle has no serial device node, so it cannot be added.")
        return 1

    config = load_config(args.config)
    existing = {s.id for s in config.sources}
    n = 1
    while f"dongle{n}" in existing:
        n += 1
    source_id = f"dongle{n}"

    radio = d.zigbee_info.get("radio", "zigpy-znp")
    _append_source_to_config(args.config, source_id, radio, d.serial_device)
    print(f"\nAdded source '{source_id}' to {args.config}:")
    print(f"  - id: {source_id}")
    print(f"    type: zigpy")
    print(f"    radio: {radio}")
    print(f"    path: {d.serial_device}")
    print("\nRestart the daemon to pick it up.")
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zigbee-cli", description="Interact with the Zigbee daemon.")
    p.add_argument("-c", "--config", default="config.yaml", help="Path to config file")
    sub = p.add_subparsers(dest="command", required=True)

    def add_source(sp):
        sp.add_argument("--source", "-s", default=None, help="Scope to a single source id")

    sp = sub.add_parser("devices", help="List known devices and their metrics")
    add_source(sp)
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("add", help="Register a device and monitor its metrics")
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("--name", default=None, help="Optional friendly name for the device")
    sp.add_argument("device", help="Device id")
    sp.add_argument("metrics", nargs="+", help="Metric name(s) to monitor")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("add-device", help="Register a device (without monitoring metrics)")
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("--name", default=None, help="Optional friendly name for the device")
    sp.add_argument("device", help="Device id")
    sp.set_defaults(func=cmd_add_device)

    sp = sub.add_parser("monitor", help="Monitor metrics on a known device")
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("device", help="Device id")
    sp.add_argument("metrics", nargs="+", help="Metric name(s) to monitor")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("unmonitor", help="Stop monitoring a device or metrics")
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("device", help="Device id")
    sp.add_argument("metrics", nargs="*", help="Metric(s); omit to unmonitor all for the device")
    sp.set_defaults(func=cmd_unmonitor)

    sp = sub.add_parser("monitored", help="List the monitored registry")
    add_source(sp)
    sp.add_argument("--full", action="store_true",
                    help="Print a table: one row per (device, metric) with uid, unit, monitored, in_network")
    sp.set_defaults(func=cmd_monitored)

    sp = sub.add_parser("latest", help="Show the latest reading per monitored metric")
    add_source(sp)
    sp.set_defaults(func=cmd_latest)

    sp = sub.add_parser("tail", help="Follow new readings (like tail -f)")
    add_source(sp)
    sp.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds")
    sp.set_defaults(func=cmd_tail)

    sp = sub.add_parser(
        "discover",
        help="Scan USB for Zigbee dongles; with -i, pick one to add to config",
    )
    sp.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Interactively select a detected dongle to add to config.yaml",
    )
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser(
        "permit",
        help="Allow new devices to join the network (daemon must be running)",
    )
    sp.add_argument("--source", "-s", default=None,
                    help="Source id (e.g. dongle1); defaults to the first configured source")
    sp.add_argument(
        "--time", "-t", type=int, default=60,
        help="How long to allow joining, in seconds (default: 60)",
    )
    sp.set_defaults(func=cmd_permit)

    sp = sub.add_parser(
        "remove-device",
        help="Remove a device from the Zigbee network (daemon must be running)",
    )
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("device", help="Device IEEE address (e.g. 00:12:4b:00:38:a8:00:21)")
    sp.set_defaults(func=cmd_remove_device)

    sp = sub.add_parser(
        "configure-reporting",
        help="Set the reporting rate for a metric (daemon must be running)",
    )
    sp.add_argument("--source", "-s", default=None, help="Source id (defaults to first configured)")
    sp.add_argument("device", help="Device IEEE address")
    sp.add_argument("metric", help="Metric name (e.g. temperature_measured_value)")
    sp.add_argument("--min", dest="min_interval", type=int, default=30,
                    help="Minimum reporting interval in seconds (default: 30)")
    sp.add_argument("--max", dest="max_interval", type=int, default=60,
                    help="Maximum reporting interval in seconds (default: 60)")
    sp.add_argument("--change", type=int, default=None,
                    help="Reportable change threshold (default: any change)")
    sp.set_defaults(func=cmd_configure_reporting)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

# Zigbee Daemon

A small Python service that:

- **lists Zigbee devices** from one or more sources,
- **records the latest readings** of *monitored* devices/metrics into a **SQLite** database,

plus a **CLI** to manage what is monitored and to watch the data.

The daemon talks to your Zigbee network **directly via [zigpy](https://github.com/zigpy/zigpy)**
(no MQTT). A **mock source** is included so the whole pipeline runs with no hardware.

## Layout

```
zigbee_daemon_website/
├── install.sh           # production installer (sudo ./install.sh)
├── uninstall.sh         # production uninstaller (sudo ./uninstall.sh)
├── config.yaml          # shared config (database path + sources) — real dongle
├── config_mock.yaml     # simulated source (no hardware needed)
├── requirements.txt
├── daemon/              # the service
│   ├── __main__.py      #   python -m daemon
│   ├── service.py       #   main loop: sources -> sqlite
│   ├── source.py        #   Source abstraction + Reading/DeviceInfo
│   ├── mock_source.py   #   simulated source (no hardware)
│   ├── zigpy_source.py  #   real coordinator via zigpy
│   ├── usb.py           #   USB scan + known Zigbee dongle ids
│   ├── db.py            #   SQLite layer (devices/monitored/readings/source_status)
│   └── config.py        #   config loader
├── cli/                 # the interaction script
│   └── cli.py           #   python cli/cli.py <command>
├── bin/
│   └── zigbee           # CLI wrapper (installed to /usr/bin/zigbee)
├── systemd/
│   └── zigbee-daemon.service  # systemd unit (installed to /etc/systemd/system/)
└── udev/                # udev rule to fix dongle ownership
    ├── install.sh       #   ./install.sh  (changes /dev/ttyUSB0 ownership)
    └── README.md
```

## Install

### Production install (systemd)

```bash
sudo ./install.sh
```

The installer is interactive — it asks for the install directory (default
`/opt/zigbee_local`) and the service user name (default `zigbee`). Use
`--batch` to skip prompts and use defaults:

```bash
sudo ./install.sh --batch
```

By default this installs to `/opt/zigbee_local`:

| Path | Purpose |
|------|---------|
| `/opt/zigbee_local/` | Project source + `.venv` |
| `/opt/zigbee_local/config.yaml` | Daemon configuration |
| `/opt/zigbee_local/data/` | SQLite DB + zigpy state |
| `/opt/zigbee_local/.install_meta` | Install metadata (for uninstall) |
| `/usr/bin/zigbee` | CLI wrapper |
| `/etc/systemd/system/zigbee-daemon.service` | systemd unit |

The daemon runs as a dedicated non-root system user (`zigbee` by default).
Your user is added to that group so the CLI can access the database.

After install:

```bash
zigbee devices          # list devices
zigbee permit 60        # allow joining for 60 s
zigbee tail             # follow readings
systemctl status zigbee-daemon
journalctl -u zigbee-daemon -f
```

### Uninstall

```bash
sudo ./uninstall.sh
```

Reads `.install_meta` to determine what to remove (including the service user
if it was created during install). Use `--batch` to skip the confirmation prompt.

### Development (manual venv)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m daemon -c config.yaml
```

`requirements.txt` already includes `zigpy` + `zigpy-znp` + `pyserial` for the
**Sonoff Zigbee 3.0 USB Dongle Plus** (ZNP protocol). For a different dongle,
swap in the matching radio package (see the comments in `requirements.txt`).

## Hardware & permissions (real dongle)

The dongle shows up as a USB-serial device, e.g. `/dev/ttyUSB0`:

```bash
lsusb | grep -i "silicon labs\|conbee\|sonoff"   # identify it
ls -l /dev/ttyUSB0                                # e.g. crw-rw---- root dialout
```

The serial node is owned by `root:dialout`. Your user must be in the `dialout`
group to open it. **No `apt install` is needed** — the `cp210x` kernel driver is
already loaded.

### Recommended: install a udev rule

A udev rule persistently changes the dongle's ownership/permissions so the
daemon can read it (and survives re-plugging). See [`udev/README.md`](udev/README.md):

```bash
cd udev
./install.sh --user "$USER"   # you own the device — immediate access
# then re-plug the dongle (or: sudo udevadm trigger)
```

### Alternative: group-based access

```bash
cd udev
./install.sh                 # group=dialout mode=0660 (needs new login)
# or manually:
sudo usermod -aG dialout "$USER"   # then log out and back in
```

Verify access either way:

```bash
.venv/bin/python -c "import serial; s=serial.Serial('/dev/ttyUSB0', timeout=1); print('OK'); s.close()"
```

> Until the dongle is readable, the daemon still runs — the CLI's `devices` and
> `monitored` commands report the source as `UNREADABLE: <reason>` (see below).

## Run the daemon

### Via systemd (production)

```bash
sudo systemctl start zigbee-daemon
sudo systemctl status zigbee-daemon
journalctl -u zigbee-daemon -f    # follow logs
```

### Manually (development)

```bash
# Real hardware (Sonoff dongle on /dev/ttyUSB0)
.venv/bin/python -m daemon -c config.yaml

# Simulated source, no hardware needed
.venv/bin/python -m daemon -c config_mock.yaml
```

`config.yaml` holds the **zigpy** source for the Sonoff dongle on `/dev/ttyUSB0`.
`config_mock.yaml` holds a **mock** source so the whole pipeline runs with no
hardware. Both write to the same `./data/zigbee.db`.

## Use the CLI

All commands take `-c <config>` (defaults to `config.yaml`) and read the same
SQLite DB the daemon writes to. The examples below use the mock source, so pass
`-c config_mock.yaml` (and run the daemon with that config first).

> **Production install:** the CLI is available as `zigbee` (e.g. `zigbee devices`).
> The examples below show the equivalent development form.

```bash
# List devices the daemon has seen (and their known metrics)
.venv/bin/python cli/cli.py -c config_mock.yaml devices

# Register a device AND monitor its metrics (add-device + monitor in one step)
.venv/bin/python cli/cli.py -c config_mock.yaml add -s mock temp-living temperature humidity

# Or do the two steps separately:
.venv/bin/python cli/cli.py -c config_mock.yaml add-device -s mock temp-living --name "Living Room"
.venv/bin/python cli/cli.py -c config_mock.yaml monitor -s mock temp-living temperature humidity

# See what is currently monitored
.venv/bin/python cli/cli.py -c config_mock.yaml monitored

# Full table: one row per (device, metric) with uid, unit, monitored, in_network
.venv/bin/python cli/cli.py -c config_mock.yaml monitored --full

# Show the latest reading for each monitored metric
.venv/bin/python cli/cli.py -c config_mock.yaml latest

# Follow new readings live (like tail -f)
.venv/bin/python cli/cli.py -c config_mock.yaml tail

# Stop monitoring (specific metrics, or all for the device)
.venv/bin/python cli/cli.py -c config_mock.yaml unmonitor -s mock temp-living temperature
.venv/bin/python cli/cli.py -c config_mock.yaml unmonitor -s mock temp-living

# Scan USB for Zigbee dongles (lists all USB devices, marks known dongles)
.venv/bin/python cli/cli.py discover

# Interactive: pick a detected dongle to add to config.yaml
.venv/bin/python cli/cli.py discover -i

# Allow new devices to join the network (daemon must be running)
# -s is optional; without it, the first configured source is used
.venv/bin/python cli/cli.py permit -s dongle1 -t 60
.venv/bin/python cli/cli.py permit -t 60

# Remove a device from the Zigbee network (daemon must be running)
.venv/bin/python cli/cli.py remove-device -s dongle1 00:12:4b:00:38:a8:00:21

# Set a metric's reporting rate (daemon must be running).
# Sleepy devices are only reachable during their poll window, so the daemon
# retries until the device wakes (up to ~2 min). The rate shows in `monitored --full`.
.venv/bin/python cli/cli.py configure-reporting -s dongle1 <ieee> temperature_measured_value --min 30 --max 60
```

`--source` / `-s` scopes a command to one source; omit it to span all sources.

> **`unmonitor` vs `remove-device`** — `unmonitor` only stops *monitoring* a
> device's metrics (it stays on the network). `remove-device` tells the
> coordinator to remove the device from the Zigbee network entirely (sends a
> ZDO leave). When a device leaves, the daemon also auto-unmonitors it (see
> "How monitoring works").

### Pairing new devices

1. Start the daemon: `.venv/bin/python -m daemon -c config.yaml`
2. Open the join window: `.venv/bin/python cli/cli.py permit -s dongle1 -t 60`
3. Put your Zigbee device in pairing mode (usually a button press).
4. The device appears in `devices` within a few seconds.
5. Register its metrics: `.venv/bin/python cli/cli.py add -s dongle1 <ieee> <metric> ...`

To remove a device from the network:
`.venv/bin/python cli/cli.py remove-device -s dongle1 <ieee>`

The `permit` and `remove-device` commands talk to the running daemon over a
Unix socket (`data/daemon.sock`), so the daemon must be started first.

### Discovering & adding dongles

`discover` scans `/sys/bus/usb/devices/` (no `lsusb` needed), matches each
device against a table of known Zigbee dongle USB ids
(`daemon/usb.py` → `KNOWN_ZIGBEE_DONGLES`), resolves the serial node
(`/dev/ttyUSB*` / `/dev/ttyACM*`), and marks matches with `★ ZIGBEE`.

With `-i` it lets you pick a detected dongle and appends a ready-to-use
`zigpy` source block to `config.yaml` (auto-named `dongleN`, with the correct
`radio` for that dongle). Restart the daemon to pick it up.

If your dongle isn't recognised, add its `vendor:product` id (from
`lsusb`) to `KNOWN_ZIGBEE_DONGLES` in `daemon/usb.py`.

### Source status & unreadable devices

`devices` and `monitored` print a status line for **every configured source**
and flag devices that have no readings yet:

```
[dongle1] status: UNREADABLE: [Errno 13] Permission denied: '/dev/ttyUSB0'

[dongle1] 00:12:4b:00:38:a8:00:21  Coordinator   (no readings yet)
    metrics: (none)
```

- `OK` — the source started and is producing data.
- `UNREADABLE: <reason>` — the source failed to start (e.g. the dongle port
  isn't readable yet). Fix it with the udev rule / `dialout` group above.
- `(no readings yet)` — the device is known but hasn't produced a reading
  (e.g. it isn't monitored yet, or it's offline).

## How monitoring works

The daemon records a reading **only** if its `(source, device, metric)` triple is in
the `monitored` table. The CLI's `add`/`unmonitor` edit that table, and the daemon
re-reads it every ~2s, so changes apply **without restarting** the daemon.

**Which attributes become metrics** — a Zigbee device exposes *hundreds* of
attributes, but most are one-time config or status (Basic `manufacturer`/`model`,
Time `time_zone`, OTA `current_file_version`, battery thresholds, alarm masks,
min/max bounds, ...). The daemon only surfaces a small allowlist of **live,
time-series** attributes (see `_MEASUREMENT_ATTRIBUTES` in
`daemon/zigpy_source.py`): temperature, humidity, illuminance, pressure, flow,
soil moisture, occupancy, mains/battery voltage & percentage, and electrical
measurements (power, current, voltage, power factor). Everything else is ignored,
so `devices` / `monitored --full` show only the readings you'd actually want to
record. To surface a different attribute, add its `(cluster_id, attr_id)` to that
table.

**Disambiguating colliding names** — attribute names are not unique across
clusters: both Temperature (0x0402) and Relative Humidity (0x0405) call their main
reading `measured_value`. When a name is shared by more than one cluster the daemon
prefixes it with a short cluster label, so you get
`temperature_measured_value` and `relative_measured_value` instead of a single
ambiguous `measured_value`.

**Auto-unmonitor on leave** — when a device leaves the network (via `remove-device`
or on its own), the daemon automatically removes its `monitored` rows so you're not
silently "monitoring" a device that's gone. Its historical `readings` and its
`devices` row are kept, so the data and the record of the device remain.

**Sleepy devices & auto re-initialization** — battery-powered end devices (e.g. a
temperature sensor) sleep most of the time and often stall mid-initialization: zigpy
only fires `device_initialized` once every cluster is discovered, but a sleeping device
won't answer the discovery requests. The daemon runs a background task that periodically
re-triggers initialization for any device that hasn't finished (every 30s, configurable
via the `reinit_interval` option on the zigpy source). Once the device wakes and answers,
its metrics appear automatically — no re-pairing needed. Until then, the device shows a
`(no metrics yet)` row in `monitored --full`.

**`in_network` state** — the `devices` table tracks whether a device is currently on
the network via a `left_at` timestamp: `NULL` means it's on the network, a timestamp
means it left (and when). When a device (re)joins, `left_at` is cleared again. The
`devices` command shows left devices with a `(left <time>)` flag:

```
[mock] old-plug  Old kitchen plug   (left 2026-10-04 12:36:06)
    metrics: power
[mock] temp-living  Living room temperature   (no readings yet)
    metrics: temperature, humidity
```

### `monitored --full`

`monitored --full` prints one row per `(device, metric)`, taking the union of every
metric the daemon has seen (the `devices` table) and every metric you've registered
in the `monitored` table:

| column        | meaning                                                        |
|---------------|----------------------------------------------------------------|
| `device`      | device name (falls back to the device id)                     |
| `uid`         | the device id — use this to tell apart identical devices      |
| `metric`      | metric name                                                    |
| `unit`        | unit from the most recent reading (blank until one is recorded)|
| `monitored`   | `yes` if this metric is in the `monitored` table              |
| `in_network`  | `yes` if the device is currently on the network (`left_at` is NULL) |

If you've monitored a device the daemon has never seen (usually a typo in the
`uid`, or a device that hasn't joined yet), it's flagged below the table:

```
⚠ monitored but not seen by the daemon (check the uid, or it hasn't joined yet):
    [mock] ghost-device
```

A hint at the bottom shows how to monitor a metric, using the last row as a
concrete example:

```
To monitor a metric:  monitor -s <source> <uid> <metric>
e.g. for the last row:  monitor -s mock temp-living temperature
```

## Multiple sources

`config.yaml` holds a **list** of sources, so you can run several at once
(e.g. two dongles, or zigpy + mock). Every table is keyed by
`(source_id, device_id)`, so identically-named devices from different sources
never collide.

## Database

SQLite file at `database` (default `./data/zigbee.db`), WAL mode so the CLI can
read while the daemon writes.

| table         | purpose                                                        |
|---------------|----------------------------------------------------------------|
| `devices`     | every device a source has reported + its known metrics + `left_at` (in-network state) |
| `monitored`   | the registry of `(source, device, metric)` we record          |
| `metrics`     | one row per (source, device, metric) with its unit             |
| `readings`    | the time series of readings, keyed by `metric_id`              |
| `source_status` | per-source read status (`ok`/`error` + reason) for the CLI  |

## Website

A PHP web frontend in `web/` provides interactive charts with per-user access
control.

### Requirements

- PHP 8.1+ with `pdo_sqlite`
- A web server (Apache, Nginx, or `php -S`)
- Read access to the SQLite database for the web server user

### Quick start (development)

```bash
cd web
php -S 0.0.0.0:8080
# open http://localhost:8080
```

### Production (Apache/Nginx)

Point your web server's document root at the `web/` directory. Make sure the
web server user (e.g. `www-data`) can read the database:

```bash
chmod o+r /opt/zigbee_local/data/zigbee.db
chmod o+rx /opt/zigbee_local /opt/zigbee_local/data
```

### Configuration

Edit `web/config.php`:

- **`database`** — absolute path to the SQLite file.
- **`users`** — map of `username => [password, metrics]`.
  - Passwords: plain-text or bcrypt hash (`php -r "echo password_hash('secret', PASSWORD_DEFAULT);"`).
  - Metrics: map of `"source_id|device_id|metric" => "Display Name"`.
    The pipe delimiter is used because IEEE addresses contain colons.

### Features

- Session-based login/logout
- Per-user metric access control (users only see what's listed for them)
- Custom display names for metrics
- Interactive Chart.js line charts (time on x-axis, value on y-axis)
- Adjustable time range (datetime pickers + preset buttons: 1h/6h/24h/7d)
- PNG and CSV download per chart
- Dark theme, responsive layout

## License

[GPL v3](LICENSE)

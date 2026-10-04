"""A real Zigbee source backed by zigpy.

This talks directly to a Zigbee coordinator (USB dongle / NCP) using the
``zigpy`` library -- no MQTT or other broker involved.

How it works
------------
1. On :meth:`start` we build a zigpy ``ControllerApplication`` for the radio
   (e.g. a ``zigpy-znp`` CC2652/CC1352 or ``zigpy-deconz`` Conbee) and start it.
2. We register a listener on the application for ``device_joined`` /
   ``device_initialized`` so we learn about devices as they appear.
3. For every (re)initialised device we attach a listener to each of its
   clusters. When a cluster fires ``attribute_updated(attrid, value, ts)`` we
   translate the attribute id to a name and emit a :class:`Reading`.

The radio is selected by the ``radio`` option (default ``zigpy-znp``) and the
serial ``path``. See the README for the exact ``pip`` extras to install.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .source import DeviceInfo, Source

log = logging.getLogger(__name__)

# Attributes that produce live, time-series readings. Everything else --
# Basic, Identify, On/Off, Level, Scenes, Time, OTA, and the config/threshold
# attributes inside measurement clusters (battery thresholds, alarm masks,
# min/max bounds, multipliers, ...) -- is one-time state and is not surfaced.
#
# Keyed by cluster id -> the set of attribute ids to keep.
_MEASUREMENT_ATTRIBUTES = {
    0x0001: frozenset({          # Power Configuration
        0x0020,  # battery_voltage
        0x0021,  # battery_percentage_remaining
    }),
    0x0400: frozenset({0x0000}),  # Illuminance Measurement -> measured_value
    0x0402: frozenset({0x0000}),  # Temperature Measurement -> measured_value
    0x0403: frozenset({0x0000}),  # Pressure Measurement    -> measured_value
    0x0404: frozenset({0x0000}),  # Flow Measurement        -> measured_value
    0x0405: frozenset({0x0000}),  # Relative Humidity       -> measured_value
    0x0406: frozenset({0x0000}),  # Occupancy Sensing       -> occupancy
    0x0408: frozenset({0x0000}),  # Soil Moisture           -> measured_value
    0x0500: frozenset({0x0000, 0x0002}),  # IAS Zone -> zone_state, zone_status
    0x0B04: frozenset({          # Electrical Measurement
        0x0505,  # rms_voltage
        0x0508,  # rms_current
        0x050B,  # active_power
        0x050E,  # reactive_power
        0x050F,  # apparent_power
        0x0510,  # power_factor
        0x0300,  # ac_frequency
        0x0303,  # neutral_current
        0x0304,  # total_active_power
    }),
}

# (cluster_id, attr_id) -> (divisor, unit)
# ZCL uses fixed-point encoding for many measurement attributes.
# divisor=1 means the raw value is already in the given unit.
_ATTR_SCALE: dict[tuple[int, int], tuple[float, str]] = {
    (0x0001, 0x0020): (10, "V"),        # battery_voltage: 0.1 V
    (0x0001, 0x0021): (1, "%"),        # battery_percentage_remaining
    (0x0400, 0x0000): (1, "lx"),       # illuminance: lux
    (0x0402, 0x0000): (100, "°C"),     # temperature: 0.01 °C
    (0x0403, 0x0000): (4, "Pa"),       # pressure: 0.25 Pa
    (0x0404, 0x0000): (10, "L/min"),   # flow: 0.1 L/min
    (0x0405, 0x0000): (100, "%"),      # relative humidity: 0.01 %
    (0x0406, 0x0000): (1, ""),         # occupancy: bitmap
    (0x0408, 0x0000): (1, "%"),        # soil moisture: %
    (0x0500, 0x0000): (1, ""),         # zone_state: bitmap
    (0x0500, 0x0002): (1, ""),         # zone_status: bitmap
    (0x0B04, 0x0505): (10, "V"),       # rms_voltage: 0.1 V
    (0x0B04, 0x0508): (1000, "A"),     # rms_current: 0.001 A
    (0x0B04, 0x050B): (1, "W"),        # active_power: 1 W
    (0x0B04, 0x050E): (1, "VAR"),      # reactive_power: 1 VAR
    (0x0B04, 0x050F): (1, "VA"),       # apparent_power: 1 VA
    (0x0B04, 0x0510): (1000, ""),      # power_factor: 0.001
    (0x0B04, 0x0300): (100, "Hz"),     # ac_frequency: 0.01 Hz
    (0x0B04, 0x0303): (1000, "A"),     # neutral_current: 0.001 A
    (0x0B04, 0x0304): (1, "W"),        # total_active_power: 1 W
}


class ZigpySource(Source):
    def __init__(self, source_id: str, options: dict[str, Any]):
        super().__init__(source_id)
        self._options = options
        self._app = None
        self._app_task: asyncio.Task | None = None
        self._reinit_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        try:
            import zigpy.application
            import zigpy.config as conf
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "zigpy is not installed. Install it with: "
                "pip install 'zigpy[deconz]'  (or the radio extra you need)"
            ) from e

        radio = self._options.get("radio", "zigpy-znp")
        path = self._options.get("path")
        if not path:
            raise ValueError(f"zigpy source '{self.source_id}' requires a 'path' option")

        # The radio is chosen by *which* ControllerApplication subclass we
        # instantiate (see _load_radio_class); zigpy's config only carries the
        # serial `path` (and optionally a database path).
        #
        # zigpy needs a state file to remember paired devices across restarts.
        # Without it, sleepy devices that don't re-announce on boot are lost.
        # Set `zigpy_state` in the source options to control the location.
        state_db = self._options.get("zigpy_state", "./data/zigpy_state.json")

        config = {
            conf.CONF_DEVICE: {
                "path": path,
            },
            conf.CONF_DATABASE: state_db,
        }

        app_cls = self._load_radio_class(radio)
        self._app = await app_cls.new(config, auto_form=False)
        self._app.add_listener(self)

        # Attach listeners to devices already present in the network.
        for dev in list(self._app.devices.values()):
            self._attach_device(dev)

        # Sleepy battery-powered devices often stall mid-initialization (they
        # don't answer discovery requests while asleep). Periodically nudge
        # unfinished devices so they eventually complete and report metrics.
        self._reinit_task = asyncio.create_task(self._reinit_loop())

    async def stop(self) -> None:
        self._running = False
        if self._reinit_task is not None:
            self._reinit_task.cancel()
            try:
                await self._reinit_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reinit_task = None
        if self._app is not None:
            try:
                # Timeout: if the dongle is unplugged, shutdown can hang
                # waiting for serial I/O. 5 s is plenty for a clean close.
                await asyncio.wait_for(self._app.shutdown(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("zigpy shutdown timed out (dongle unresponsive?)")
            except Exception:  # pragma: no cover - best effort
                log.exception("Error shutting down zigpy app")
            self._app = None

    async def _reinit_loop(self) -> None:
        """Re-trigger initialization for devices that never finished.

        Sleepy end devices frequently get stuck: zigpy's ``device_initialized``
        only fires once all clusters are discovered, but a sleeping device won't
        answer the discovery requests, so init stalls. Nudging with
        ``schedule_initialize()`` is idempotent (it skips already-initialized
        devices), so we can safely call it on every device that isn't done yet.
        """
        interval = float(self._options.get("reinit_interval", 30.0))
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._app is None:
                continue
            for dev in list(self._app.devices.values()):
                try:
                    if getattr(dev, "is_initialized", True):
                        continue
                    if getattr(dev, "initializing", False):
                        continue  # already in progress, leave it alone
                    log.info("[%s] re-initializing stuck device %s", self.source_id, dev.ieee)
                    dev.schedule_initialize()
                except Exception:  # pragma: no cover - defensive
                    log.debug("Could not re-init device %s", getattr(dev, "ieee", dev), exc_info=True)

    @staticmethod
    def _load_radio_class(radio: str):
        """Resolve a radio name to its ControllerApplication subclass."""
        import importlib

        # Map common radio names to their (module, class).
        mapping = {
            "zigpy-znp": ("zigpy_znp.zigbee.application", "ControllerApplication"),
            "zigpy-deconz": ("zigpy_deconz.zigbee", "ZigbeeControllerApplication"),
            "zigpy-xhci": ("zigpy_xhci.zigbee", "ZigbeeApplicationController"),
            "bellows": ("bellows.zigbee", "ZigbeeApplicationController"),
        }
        if radio in mapping:
            module_name, class_name = mapping[radio]
        else:
            # Fall back to treating `radio` as a module name.
            module_name, class_name = radio, "ZigbeeApplicationController"
        module = importlib.import_module(module_name)
        return getattr(module, class_name)

    # ------------------------------------------------------------------ #
    # zigpy application listener
    # ------------------------------------------------------------------ #
    def device_joined(self, device) -> None:
        log.info("[%s] device joined: %s", self.source_id, device.ieee)
        self._attach_device(device)

    def device_initialized(self, device) -> None:
        log.info("[%s] device initialized: %s", self.source_id, device.ieee)
        self._attach_device(device)

    def device_init_failure(self, device) -> None:
        # Fired when discovery times out (common for sleepy devices that don't
        # answer while asleep). We still try to attach whatever is known so far;
        # the reinit loop will keep nudging until the device wakes and answers.
        log.info("[%s] device init failure (will retry): %s", self.source_id, device.ieee)
        self._attach_device(device)

    def device_left(self, device) -> None:
        device_id = self._device_id(device)
        log.info("[%s] device left: %s", self.source_id, device.ieee)
        self._emit_device_left(device_id)

    def connection_lost(self, exc) -> None:
        """Called by zigpy when the serial connection to the dongle drops."""
        log.error("[%s] dongle connection lost: %r", self.source_id, exc)
        self._emit_connection_lost(exc)

    # ------------------------------------------------------------------ #
    # per-device / per-cluster wiring
    # ------------------------------------------------------------------ #
    def _attach_device(self, device) -> None:
        """Attach a cluster listener to every measurement cluster on the device."""
        # Skip the coordinator — it's infrastructure, not a sensor.
        if getattr(device, "nwk", None) == 0x0000:
            log.debug("[%s] skipping coordinator", self.source_id)
            return
        device_id = self._device_id(device)
        name_map = self._build_metric_name_map(device)
        n_eps = len(device.non_zdo_endpoints)
        n_clusters = 0
        for ep in device.non_zdo_endpoints:
            for cluster in list(ep.in_clusters.values()) + list(ep.out_clusters.values()):
                if cluster.cluster_id not in _MEASUREMENT_ATTRIBUTES:
                    continue
                n_clusters += 1
                try:
                    self._subscribe_cluster_reports(device_id, cluster, name_map)
                except Exception:  # pragma: no cover - defensive
                    log.debug("Could not attach listener to cluster %s", cluster, exc_info=True)
        metrics = sorted(set(name_map.values()))
        # metric name -> [cluster_id, attr_id] (for CLI configure-reporting)
        metric_map = {
            name: [cluster_id, attr_id]
            for (cluster_id, attr_id), name in name_map.items()
        }
        log.info(
            "[%s] attach %s: %d endpoint(s), %d cluster(s), %d metric(s)",
            self.source_id, device_id, n_eps, n_clusters, len(metrics),
        )
        self._emit_device(
            device_id, self._device_name(device), metrics, metric_map=metric_map
        )

    def _build_metric_name_map(self, device) -> dict:
        """Map (cluster_id, attrid) -> metric name for a device.

        Only attributes listed in ``_MEASUREMENT_ATTRIBUTES`` are included --
        one-time config/status attributes (Basic, Time, OTA, battery thresholds,
        alarm masks, min/max bounds, ...) are ignored. Attribute names are not
        unique across clusters: e.g. both the Temperature (0x0402) and Relative
        Humidity (0x0405) clusters expose their main reading as
        ``measured_value``. When a name is shared by more than one cluster we
        prefix it with a short cluster label so the readings don't collide
        (``temperature_measured_value`` vs ``relative_measured_value``).
        """
        from collections import Counter

        entries: list[tuple[int, int, str, str]] = []  # (cluster_id, attrid, name, prefix)
        for ep in device.non_zdo_endpoints:
            for cluster in list(ep.in_clusters.values()) + list(ep.out_clusters.values()):
                allowed = _MEASUREMENT_ATTRIBUTES.get(cluster.cluster_id)
                if not allowed:
                    continue
                prefix = self._cluster_prefix(cluster)
                for attrid, attr in cluster.attributes.items():
                    if attr.name and attrid in allowed:
                        entries.append((cluster.cluster_id, attrid, attr.name, prefix))

        counts = Counter(name for _, _, name, _ in entries)
        name_map: dict[tuple[int, int], str] = {}
        for cluster_id, attrid, name, prefix in entries:
            if counts[name] > 1:
                name_map[(cluster_id, attrid)] = f"{prefix}_{name}"
            else:
                name_map[(cluster_id, attrid)] = name
        return name_map

    @staticmethod
    def _cluster_prefix(cluster) -> str:
        """A short, stable label derived from a cluster's name (first word)."""
        name = getattr(cluster, "name", None)
        if not isinstance(name, str) or not name:
            name = cluster.__class__.__name__
        first = re.sub(r"[^a-z0-9]", "", name.split()[0].lower())
        return first or f"cluster_0x{cluster.cluster_id:04X}"

    def _make_cluster_listener(self, device_id: str, cluster, name_map: dict):
        """Return a listener object that forwards attribute updates.

        Only attributes present in ``name_map`` (i.e. from measurement clusters)
        are emitted; everything else is ignored.
        """
        source_id = self.source_id

        class _ClusterListener:
            def attribute_updated(self, attrid, value, timestamp):
                log.debug(
                    "[%s] attr update %s cluster=0x%04X attr=0x%04X value=%r",
                    source_id, device_id, cluster.cluster_id, attrid, value,
                )
                name = name_map.get((cluster.cluster_id, attrid))
                if name is None:
                    return
                self._emit_reading(device_id, name, value, ts=timestamp)

        return _ClusterListener()

    def _subscribe_cluster_reports(self, device_id: str, cluster, name_map: dict):
        """Subscribe to the zigpy 2.x ``attribute_report`` event on a cluster.

        Unlike the legacy ``attribute_updated`` listener (which only fires when
        the cached value *changes*), ``attribute_report`` fires for **every**
        Report_Attributes command, even when the value is unchanged.  This is
        what we need for periodic sensor readings that often repeat the same
        value.
        """
        source_id = self.source_id
        cluster_id = cluster.cluster_id

        def _on_report(data):
            # data is an AttributeReportedEvent dataclass
            attrid = data.attribute_id
            raw = data.value
            log.debug(
                "[%s] attr report %s cluster=0x%04X attr=0x%04X raw=%r",
                source_id, device_id, cluster_id, attrid, raw,
            )
            name = name_map.get((cluster_id, attrid))
            if name is None:
                return
            # Apply ZCL fixed-point scaling
            scale = _ATTR_SCALE.get((cluster_id, attrid))
            if scale and isinstance(raw, (int, float)):
                divisor, unit = scale
                value = raw / divisor
                if divisor == 1:
                    value = raw  # keep int as int
                self._emit_reading(device_id, name, value, unit=unit)
            else:
                self._emit_reading(device_id, name, raw)

        cluster.on_event("attribute_report", _on_report)

    # ------------------------------------------------------------------ #
    # device identity helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _device_id(device) -> str:
        ieee = getattr(device, "ieee", None)
        if ieee is not None:
            return str(ieee)
        return f"0x{device.nwk:04X}"

    @staticmethod
    def _device_name(device) -> str | None:
        model = getattr(device, "model", None)
        manuf = getattr(device, "manufacturer", None)
        if model and manuf:
            return f"{manuf} {model}"
        return model or manuf

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #
    async def permit_join(self, time_s: int = 60) -> None:
        """Allow new devices to join the Zigbee network."""
        if self._app is None:
            raise RuntimeError("Source is not connected")
        await self._app.permit(time_s)
        log.info("[%s] permit join for %ds", self.source_id, time_s)

    async def remove_device(self, device_id: str) -> None:
        """Remove a device from the Zigbee network (sends ZDO leave)."""
        if self._app is None:
            raise RuntimeError("Source is not connected")
        import zigpy.types as t
        ieee = t.EUI64.convert(device_id)
        if ieee not in self._app.devices:
            raise RuntimeError(
                f"Device {device_id} is not in the coordinator's device table. "
                f"It may have already left, or was never paired with this "
                f"coordinator instance. Factory-reset the device and re-pair."
            )
        log.info("[%s] removing device %s", self.source_id, device_id)
        await self._app.remove(ieee)

    async def configure_reporting(
        self,
        device_id: str,
        cluster_id: int,
        attr_id: int,
        min_interval: int,
        max_interval: int,
        reportable_change: int | None = None,
    ) -> str:
        """Configure attribute reporting on a device's cluster.

        Uses a fire-and-forget send (``expect_reply=False``) so it works
        even when the device is asleep. The ZNP stack queues the frame and
        delivers it on the device's next poll.

        Returns a short status string. Raises on failure.
        """
        if self._app is None:
            raise RuntimeError("Source is not connected")
        import zigpy.types as t
        from zigpy.zcl import foundation

        ieee = t.EUI64.convert(device_id)
        dev = self._app.devices.get(ieee)
        if dev is None:
            raise RuntimeError(f"Device {device_id} not found on the network")

        # Find the cluster on any endpoint.
        cluster = None
        for ep in dev.non_zdo_endpoints:
            if cluster_id in ep.in_clusters:
                cluster = ep.in_clusters[cluster_id]
                break
            if cluster_id in ep.out_clusters:
                cluster = ep.out_clusters[cluster_id]
                break
        if cluster is None:
            raise RuntimeError(
                f"Cluster 0x{cluster_id:04X} not found on device {device_id}"
            )

        if reportable_change is None:
            reportable_change = 1  # report on any change >= 1 unit

        # Build the ZCL AttributeReportingConfig manually so we can send
        # it fire-and-forget (no reply expected).
        attr_def = cluster.find_attribute(attr_id)
        cfg = foundation.AttributeReportingConfig()
        cfg.direction = foundation.ReportingDirection.SendReports
        cfg.attrid = attr_def.id
        cfg.datatype = (
            attr_def.zcl_type
            if attr_def.zcl_type is not None
            else foundation.DataType.from_python_type(attr_def.type).type_id
        )
        cfg.min_interval = min_interval
        cfg.max_interval = max_interval
        cfg.reportable_change = reportable_change

        # Fire-and-forget: the ZNP stack queues the frame for the device's
        # next poll. No need to wait for the device to be awake.
        try:
            await cluster._configure_reporting(
                [cfg],
                manufacturer=None,
                expect_reply=False,
                ask_for_ack=False,
            )
        except Exception as e:
            raise RuntimeError(
                f"configure_reporting failed to send: {e!r}"
            ) from e

        log.info(
            "[%s] configure_reporting (fire-and-forget) %s cluster=0x%04X "
            "attr=0x%04X min=%d max=%d change=%d",
            self.source_id, device_id, cluster_id, attr_id,
            min_interval, max_interval, reportable_change,
        )
        return (
            f"configured 0x{cluster_id:04X}/0x{attr_id:04X} "
            f"(fire-and-forget, delivered on next device poll)"
        )

    # ------------------------------------------------------------------ #
    # Source interface
    # ------------------------------------------------------------------ #
    async def list_devices(self) -> list[DeviceInfo]:
        if self._app is None:
            return []
        out = []
        for dev in self._app.devices.values():
            name_map = self._build_metric_name_map(dev)
            metrics = sorted(set(name_map.values()))
            out.append(
                DeviceInfo(
                    source_id=self.source_id,
                    device_id=self._device_id(dev),
                    name=self._device_name(dev),
                    metrics=metrics,
                )
            )
        return out

"""The pluggable Source abstraction.

A :class:`Source` is anything that can (a) report the devices it knows about
and (b) emit a stream of :class:`Reading` values. The daemon holds a *list* of
sources, so running several (e.g. two zigpy dongles, or zigpy + mock) is just a
matter of configuring more of them.

Concrete implementations:
- :class:`daemon.mock_source.MockSource`  -- simulated, no hardware needed.
- :class:`daemon.zigpy_source.ZigpySource` -- real coordinator via zigpy.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class Reading:
    """A single metric value from a device."""

    source_id: str
    device_id: str
    metric: str
    value: float | None
    unit: str | None
    ts: datetime


@dataclass
class DeviceInfo:
    """A device as known by a source."""

    source_id: str
    device_id: str
    name: str | None = None
    metrics: list[str] = field(default_factory=list)
    # metric name -> [cluster_id, attr_id]; lets the CLI resolve a metric name
    # to the (cluster, attr) pair needed for configure_reporting.
    metric_map: dict[str, list[int]] = field(default_factory=dict)


ReadingHandler = Callable[[Reading], None]
DeviceHandler = Callable[[DeviceInfo], None]
DeviceLeftHandler = Callable[[str, str], None]  # (source_id, device_id)
ConnectionLostHandler = Callable[[str, Exception | None], None]  # (source_id, exc)


class Source(abc.ABC):
    """Base class for all data sources."""

    def __init__(self, source_id: str):
        self.source_id = source_id
        self._on_reading: ReadingHandler | None = None
        self._on_device: DeviceHandler | None = None
        self._on_device_left: DeviceLeftHandler | None = None
        self._on_connection_lost: ConnectionLostHandler | None = None

    # -- handler registration (called by the service) ------------------- #
    def set_reading_handler(self, cb: ReadingHandler) -> None:
        self._on_reading = cb

    def set_device_handler(self, cb: DeviceHandler) -> None:
        self._on_device = cb

    def set_device_left_handler(self, cb: DeviceLeftHandler) -> None:
        self._on_device_left = cb

    def set_connection_lost_handler(self, cb: ConnectionLostHandler) -> None:
        self._on_connection_lost = cb

    # -- helpers for subclasses ----------------------------------------- #
    def _emit_reading(
        self,
        device_id: str,
        metric: str,
        value: Any,
        unit: str | None = None,
        ts: datetime | None = None,
    ) -> None:
        if self._on_reading is None:
            return
        # All monitored attributes are numeric; store the value as a float.
        num = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        self._on_reading(
            Reading(
                source_id=self.source_id,
                device_id=device_id,
                metric=metric,
                value=num,
                unit=unit,
                ts=ts or datetime.now(timezone.utc),
            )
        )

    def _emit_device(
        self,
        device_id: str,
        name: str | None = None,
        metrics: list[str] | None = None,
        metric_map: dict[str, list[int]] | None = None,
    ) -> None:
        if self._on_device is None:
            return
        self._on_device(
            DeviceInfo(
                source_id=self.source_id,
                device_id=device_id,
                name=name,
                metrics=metrics or [],
                metric_map=metric_map or {},
            )
        )

    def _emit_device_left(self, device_id: str) -> None:
        if self._on_device_left is None:
            return
        self._on_device_left(self.source_id, device_id)

    def _emit_connection_lost(self, exc: Exception | None = None) -> None:
        if self._on_connection_lost is None:
            return
        self._on_connection_lost(self.source_id, exc)

    # -- control --------------------------------------------------------- #
    async def permit_join(self, time_s: int = 60) -> None:
        """Allow new devices to join the network for `time_s` seconds."""
        pass  # no-op for sources that don't support it

    async def remove_device(self, device_id: str) -> None:
        """Remove a device from the network (Zigbee leave)."""
        pass  # no-op for sources that don't support it

    async def configure_reporting(
        self,
        device_id: str,
        cluster_id: int,
        attr_id: int,
        min_interval: int,
        max_interval: int,
        reportable_change: int | None = None,
    ) -> str:
        """Configure attribute reporting on a device's cluster."""
        raise NotImplementedError(
            f"Source {self.source_id!r} does not support configure_reporting"
        )

    # -- lifecycle ------------------------------------------------------- #
    @abc.abstractmethod
    async def start(self) -> None:
        """Start producing data (spawn tasks, connect hardware, ...)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop producing data and release resources."""

    @abc.abstractmethod
    async def list_devices(self) -> list[DeviceInfo]:
        """Return the devices this source currently knows about."""

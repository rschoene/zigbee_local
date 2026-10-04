"""A simulated source for running the whole pipeline without hardware.

Each configured device periodically emits a reading for each of its metrics,
with a value that drifts around a base by a random jitter. This lets you
exercise the daemon, the database and the CLI end-to-end before a real
coordinator is connected.
"""

from __future__ import annotations

import asyncio
import random

from .config import MockDeviceSpec
from .source import DeviceInfo, Source


class MockSource(Source):
    def __init__(self, source_id: str, devices: list[MockDeviceSpec], interval: float = 2.0):
        super().__init__(source_id)
        self._devices = devices
        self._interval = interval
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for dev in self._devices:
            self._emit_device(dev.id, dev.name or dev.id, [m.name for m in dev.metrics])
            self._tasks.append(asyncio.create_task(self._run_device(dev)))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def list_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                source_id=self.source_id,
                device_id=d.id,
                name=d.name or d.id,
                metrics=[m.name for m in d.metrics],
            )
            for d in self._devices
        ]

    async def _run_device(self, dev: MockDeviceSpec) -> None:
        # Keep a per-metric current value so the series looks continuous.
        state = {m.name: m.base for m in dev.metrics}
        while self._running:
            for m in dev.metrics:
                state[m.name] = state[m.name] + random.uniform(-m.jitter, m.jitter)
                self._emit_reading(dev.id, m.name, round(state[m.name], 3), unit=m.unit)
            await asyncio.sleep(self._interval)

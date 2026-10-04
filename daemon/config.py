"""Configuration loading for the daemon and CLI.

The config is a single YAML file (see ``config.yaml`` at the project root).
It defines the SQLite database path and a list of *sources*. Each source has
an ``id``, a ``type`` (``mock`` or ``zigpy``) and type-specific options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MetricSpec:
    """A simulated metric for a mock device."""

    name: str
    unit: str | None = None
    base: float = 0.0
    jitter: float = 1.0


@dataclass
class MockDeviceSpec:
    """A simulated device for the mock source."""

    id: str
    name: str | None = None
    metrics: list[MetricSpec] = field(default_factory=list)


@dataclass
class SourceConfig:
    """A single source entry from the config file."""

    id: str
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    """Top-level configuration."""

    database: str
    sources: list[SourceConfig] = field(default_factory=list)


def _parse_mock_devices(raw: list[dict] | None) -> list[MockDeviceSpec]:
    out: list[MockDeviceSpec] = []
    for d in raw or []:
        metrics = [MetricSpec(**m) for m in d.get("metrics", [])]
        out.append(
            MockDeviceSpec(
                id=str(d["id"]),
                name=d.get("name"),
                metrics=metrics,
            )
        )
    return out


def load_config(path: str) -> Config:
    """Load and validate a config file into a :class:`Config`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = yaml.safe_load(p.read_text()) or {}

    sources: list[SourceConfig] = []
    for s in raw.get("sources", []):
        sid = s["id"]
        stype = s.get("type", "mock")
        options = {k: v for k, v in s.items() if k not in ("id", "type")}
        if stype == "mock":
            options["devices"] = _parse_mock_devices(options.get("devices", []))
        sources.append(SourceConfig(id=sid, type=stype, options=options))

    return Config(
        database=raw.get("database", "./data/zigbee.db"),
        sources=sources,
    )

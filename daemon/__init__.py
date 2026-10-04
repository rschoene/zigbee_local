"""Zigbee daemon package.

Runs one or more Zigbee "sources" (real zigpy coordinator, mock, ...),
aggregates their readings, and persists the readings of *monitored*
devices/metrics to a SQLite database.
"""

__all__ = ["config", "db", "source", "mock_source", "zigpy_source", "service"]

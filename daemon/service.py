"""The daemon service: wires sources to the database and runs the loop.

Responsibilities
----------------
- Build every configured :class:`Source`.
- Register handlers so that:
    * device info  -> upserted into the ``devices`` table, and
    * readings     -> written to ``readings`` **only if** the
      ``(source, device, metric)`` is in the ``monitored`` registry.
- Start all sources and block until a stop signal (SIGINT/SIGTERM).
- Run a Unix socket command server for runtime control (e.g. permit join).

The monitored set is re-read from the DB on a short interval so that changes
made by the CLI (``add``/``remove``) take effect without restarting the daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .config import Config, SourceConfig
from .db import Database
from .mock_source import MockSource
from .source import DeviceInfo, Reading, Source
from .zigpy_source import ZigpySource

log = logging.getLogger("zigbee_daemon")


def build_source(cfg: SourceConfig) -> Source:
    """Instantiate a source from its config entry."""
    if cfg.type == "mock":
        return MockSource(
            cfg.id,
            devices=cfg.options.get("devices", []),
            interval=float(cfg.options.get("interval", 2.0)),
        )
    if cfg.type == "zigpy":
        return ZigpySource(cfg.id, cfg.options)
    raise ValueError(f"Unknown source type: {cfg.type!r}")


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.database)
        self.sources: list[Source] = [build_source(s) for s in config.sources]
        self._monitored: set[tuple[str, str, str]] = set()
        self._stop = asyncio.Event()
        self._connection_lost = False
        # Unix socket path for the command server (next to the DB file).
        db_dir = Path(config.database).parent
        self._socket_path = db_dir / "daemon.sock"

        for src in self.sources:
            src.set_device_handler(self._on_device)
            src.set_reading_handler(self._on_reading)
            src.set_device_left_handler(self._on_device_left)
            src.set_connection_lost_handler(self._on_connection_lost)

    # ------------------------------------------------------------------ #
    # handlers
    # ------------------------------------------------------------------ #
    def _on_device(self, info: DeviceInfo) -> None:
        self.db.upsert_device(
            info.source_id, info.device_id, info.name, info.metrics,
            metric_map=info.metric_map,
        )

    def _on_device_left(self, source_id: str, device_id: str) -> None:
        # Mark the device as left but keep its monitored entries.
        # This way, when the device re-joins (e.g. after a coordinator
        # reboot), monitoring resumes automatically without user action.
        # To stop monitoring a device permanently, use `zigbee unmonitor`.
        self.db.mark_device_left(source_id, device_id)
        log.info("[%s] device left: %s", source_id, device_id)

    def _on_reading(self, r: Reading) -> None:
        if (r.source_id, r.device_id, r.metric) in self._monitored:
            self.db.insert_reading(r)

    def _on_connection_lost(self, source_id: str, exc: Exception | None) -> None:
        # A source lost its hardware connection (e.g. dongle unplugged).
        # Flag it so the main loop exits non-zero; systemd will then
        # restart (rate-limited) and the fail-fast start check re-runs.
        log.error("[%s] connection lost; daemon will exit", source_id)
        self._connection_lost = True
        self._stop.set()

    async def _refresh_monitored(self) -> None:
        """Periodically reload the monitored set from the DB."""
        while not self._stop.is_set():
            self._monitored = self.db.monitored_set()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _reapply_reporting_configs(self) -> None:
        """Re-apply stored reporting configs to devices after startup.

        This handles the case where a sensor was reset (e.g. battery swap)
        and lost its reporting configuration. We wait a short delay for
        devices to join, then re-send Configure Reporting for each stored
        entry. For sleepy devices this may take up to ~10 min per device
        (the retry loop in configure_reporting), so we run them
        sequentially in the background.
        """
        # Give devices time to join after the coordinator starts.
        await asyncio.sleep(10)

        configs = self.db.get_reporting_config()
        if not configs:
            return

        log.info("Re-applying %d reporting config(s) on startup", len(configs))
        for (source_id, device_id, cluster_id, attr_id), cfg in configs.items():
            if self._stop.is_set():
                return
            src = next((s for s in self.sources if s.source_id == source_id), None)
            if src is None:
                continue
            try:
                await src.configure_reporting(
                    device_id,
                    cluster_id,
                    attr_id,
                    cfg["min_interval"],
                    cfg["max_interval"],
                    cfg.get("reportable_change"),
                )
                log.info(
                    "Re-applied reporting config: %s/%s cluster=0x%04X attr=0x%04X",
                    source_id, device_id, cluster_id, attr_id,
                )
            except Exception as e:
                log.warning(
                    "Failed to re-apply reporting config for %s/%s "
                    "cluster=0x%04X attr=0x%04X: %s",
                    source_id, device_id, cluster_id, attr_id, e,
                )

    # ------------------------------------------------------------------ #
    # command server (Unix socket)
    # ------------------------------------------------------------------ #
    async def _command_server(self) -> None:
        """Handle JSON commands over a Unix socket.

        Protocol: one JSON object per line.
        Commands:
          {"cmd": "permit_join", "source_id": "dongle1", "time_s": 60}
          {"cmd": "status"}
        Response: one JSON object per line.
        """
        # Remove stale socket file
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

        server = await asyncio.start_unix_server(self._handle_command, path=str(self._socket_path))
        log.info("Command server listening on %s", self._socket_path)
        async with server:
            await server.serve_forever()

    async def _handle_command(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    writer.write(json.dumps({"error": "invalid JSON"}).encode() + b"\n")
                    continue

                cmd = msg.get("cmd")
                if cmd == "permit_join":
                    source_id = msg.get("source_id")
                    time_s = int(msg.get("time_s", 60))
                    src = next((s for s in self.sources if s.source_id == source_id), None)
                    if src is None:
                        writer.write(json.dumps({"error": f"unknown source: {source_id}"}).encode() + b"\n")
                    else:
                        try:
                            await src.permit_join(time_s)
                            writer.write(json.dumps({"ok": True, "message": f"permit join {time_s}s on {source_id}"}).encode() + b"\n")
                        except Exception as e:
                            writer.write(json.dumps({"error": str(e)}).encode() + b"\n")
                elif cmd == "remove_device":
                    source_id = msg.get("source_id")
                    device_id = msg.get("device_id")
                    src = next((s for s in self.sources if s.source_id == source_id), None)
                    if src is None:
                        writer.write(json.dumps({"error": f"unknown source: {source_id}"}).encode() + b"\n")
                    else:
                        try:
                            await src.remove_device(device_id)
                            writer.write(json.dumps({"ok": True, "message": f"removing {device_id} from {source_id}"}).encode() + b"\n")
                        except Exception as e:
                            writer.write(json.dumps({"error": str(e)}).encode() + b"\n")
                elif cmd == "configure_reporting":
                    source_id = msg.get("source_id")
                    device_id = msg.get("device_id")
                    cluster_id = int(msg.get("cluster_id", 0))
                    attr_id = int(msg.get("attr_id", 0))
                    min_interval = int(msg.get("min_interval", 30))
                    max_interval = int(msg.get("max_interval", 60))
                    reportable_change = msg.get("reportable_change")
                    reportable_change = int(reportable_change) if reportable_change is not None else None
                    src = next((s for s in self.sources if s.source_id == source_id), None)
                    if src is None:
                        writer.write(json.dumps({"error": f"unknown source: {source_id}"}).encode() + b"\n")
                    else:
                        try:
                            message = await src.configure_reporting(
                                device_id, cluster_id, attr_id,
                                min_interval, max_interval, reportable_change,
                            )
                            self.db.set_reporting_config(
                                source_id, device_id, cluster_id, attr_id,
                                min_interval, max_interval, reportable_change,
                            )
                            writer.write(json.dumps({"ok": True, "message": message}).encode() + b"\n")
                        except Exception as e:
                            log.exception("configure_reporting failed")
                            writer.write(json.dumps({"error": repr(e)}).encode() + b"\n")
                elif cmd == "status":
                    statuses = self.db.get_source_status()
                    writer.write(json.dumps({"ok": True, "sources": statuses}).encode() + b"\n")
                else:
                    writer.write(json.dumps({"error": f"unknown command: {cmd}"}).encode() + b"\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass

        log.info("Starting %d source(s): %s", len(self.sources), [s.source_id for s in self.sources])
        started: list[Source] = []
        start_failed = False
        for src in self.sources:
            try:
                await src.start()
                self.db.set_source_status(src.source_id, "ok")
                started.append(src)
            except Exception as e:
                log.exception("Failed to start source %s", src.source_id)
                self.db.set_source_status(src.source_id, "error", str(e))
                start_failed = True

        # Fail fast: if any source could not start (e.g. dongle missing or
        # unreadable), stop the ones that did start and exit non-zero so
        # systemd marks the service as failed instead of running broken.
        if start_failed:
            log.error("One or more sources failed to start; exiting.")
            for src in started:
                try:
                    await src.stop()
                except Exception:  # pragma: no cover - best effort
                    log.exception("Error stopping source %s", src.source_id)
            self.db.close()
            raise SystemExit(1)

        refresh = asyncio.create_task(self._refresh_monitored())
        cmd_server = asyncio.create_task(self._command_server())
        reapply = asyncio.create_task(self._reapply_reporting_configs())
        try:
            await self._stop.wait()
        finally:
            refresh.cancel()
            cmd_server.cancel()
            reapply.cancel()
            for src in self.sources:
                try:
                    await src.stop()
                except Exception:  # pragma: no cover - best effort
                    log.exception("Error stopping source %s", src.source_id)
            self.db.close()
            # Clean up socket file
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            log.info("Daemon stopped.")

        # Exit non-zero if a source lost its hardware connection (e.g. dongle
        # unplugged) so systemd restarts (rate-limited) and the fail-fast
        # start check re-runs.
        if self._connection_lost:
            raise SystemExit(1)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Zigbee daemon service")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from .config import load_config

    config = load_config(args.config)
    service = Service(config)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Wire the pieces together and run the concierge: ``python -m motel.concierge`` / ``motel-concierge``.

:class:`Concierge` owns the ledger, the registry, the key store, the per-attempt guest listeners (127.0.0.1, one port each,
see ``motel.concierge.endpoints``) and the admin socket.
The tests build it directly; ``main`` builds it from ``/etc/motel/motel.toml`` and the systemd environment.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import ssl
import sys
import threading
import time
import tomllib

from motel.concierge import admin, endpoints, fdstore, ledger, proxy, settings
from motel.concierge.errors import ConciergeError, ConfigError
from motel.concierge.keys import KeyStore
from motel.concierge.registry import Registry
from motel.concierge.settings import TourConfig, parse_tours_section

CONFIG_FILE = "/etc/motel/motel.toml"
STATE_DIR = "/var/lib/motel-concierge"
VERSION = "1"


class Concierge:
    def __init__(self, cfg: TourConfig, state_dir: str, *, admin_path: str | None = None, admin_fd: int | None = None,
                 bind_host: str = "127.0.0.1", tls_context: ssl.SSLContext | None = None, clock=time.time,
                 allowed_uids=frozenset({0}), fsync: bool = True, ephemeral_ports: bool = False,
                 store: fdstore.FdStore | None = None, inherited: dict | None = None, sweep_interval: float = 1.0,
                 dev_rebind: bool = False):
        """``ephemeral_ports`` lets the operating system pick each attempt's port instead of ``concierge_port_range`` (tests).

        ``store`` keeps listening sockets with systemd across restarts and ``inherited`` is what it gave back (see ``fdstore``).
        Without a store no attempt can be issued, unless ``dev_rebind`` (unsafe, development only) is set."""
        if not ipaddress.ip_address(bind_host).is_loopback:
            raise ConciergeError(f"the guest listeners bind loopback only, not {bind_host}")
        self.cfg = cfg
        self.clock = clock
        self.ledger = ledger.Ledger(os.path.join(state_dir, ledger.LEDGER_NAME), fsync=fsync, clock=clock)
        self.registry = Registry(self.ledger, known_bindings=cfg.bindings.keys(), max_budget_usd=cfg.max_usd_model_per_guest, clock=clock)
        self.keys = KeyStore(cfg.bindings)
        self.core = proxy.Core(cfg, self.registry, self.keys, tls_context)
        self.endpoints = endpoints.Endpoints(self.core, port_range=None if ephemeral_ports else cfg.concierge_port_range,
                                             bind_host=bind_host, fdstore=store, dev_rebind=dev_rebind)
        self.registry.endpoints = self.endpoints
        self._inherited = inherited or {}
        self.sweep_interval = sweep_interval
        self._sweeps = 0  # completed sweep passes; tests wait on it instead of sleeping
        self._swept = threading.Condition()
        self.admin = None
        if admin_path or admin_fd is not None:
            self.admin = admin.AdminServer(self.registry, path=admin_path if admin_fd is None else None, fd=admin_fd,
                                           allowed_uids=allowed_uids, health_extra=self._health_extra, endpoints=self.endpoints)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def _health_extra(self) -> dict:
        low, high = self.cfg.concierge_port_range
        return {"version": VERSION, "listeners": self.endpoints.count(), "port_range": f"{low}-{high}",
                "lost_listeners": dict(self.endpoints.lost), "fd_store": bool(self.endpoints.fdstore and self.endpoints.fdstore.enabled),
                "dev_rebind": self.endpoints.dev_rebind,
                "bindings": {name: self.keys.available(name) for name in sorted(self.cfg.bindings)}}

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.sweep_interval):
            self.sweep_now()

    def sweep_now(self) -> None:
        """One sweep pass (the sweeper thread runs this every ``sweep_interval``); callers may trigger it too."""
        try:
            self.endpoints.sweep()
        except Exception:  # noqa: BLE001 - the sweeper must not die
            logging.getLogger("motel.concierge").exception("listener sweep failed")
        with self._swept:
            self._sweeps += 1
            self._swept.notify_all()

    def wait_for_sweeps(self, passes: int, timeout: float = 30.0) -> bool:
        """Block until ``passes`` more sweep passes have completed (event-driven, bounded by ``timeout``)."""
        with self._swept:
            target = self._sweeps + passes
            return self._swept.wait_for(lambda: self._sweeps >= target, timeout)

    def start(self) -> Concierge:
        self.endpoints.restore(self.registry.live_attempts(), self._inherited)  # every live attempt on its old port, or revoked
        self._threads.append(threading.Thread(target=self._sweep_loop, daemon=True, name="concierge-sweep"))
        self._threads[-1].start()
        if self.admin is not None:
            self._threads.append(self.admin.serve_in_thread())
        return self

    def stop(self) -> None:
        self._stop.set()
        self.endpoints.close_all()
        if self.admin is not None:
            self.admin.shutdown()
            self.admin.server_close()
            if self.admin.path:
                try:
                    os.unlink(self.admin.path)
                except OSError:
                    pass
        self.ledger.close()


def load_config(path: str) -> TourConfig:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {path} ({exc.strerror}); the concierge user must be able to read it") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML ({exc})") from None
    return parse_tours_section(data.get("tours", {}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="motel-concierge", description="motel tours credential proxy (runs as motel-concierge)")
    parser.add_argument("--config", default=CONFIG_FILE)
    parser.add_argument("--state-dir", default=STATE_DIR)
    parser.add_argument("--admin-socket", default=admin.DEFAULT_SOCKET,
                        help="used only when systemd did not pass a socket (LISTEN_FDS); created 0600")
    parser.add_argument("--dev-rebind", action="store_true",
                        help="UNSAFE, development only: run without systemd's file-descriptor store and bind each attempt's recorded "
                             "port again after a restart (another local process may have read its token meanwhile)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        if not cfg.enabled:
            raise ConfigError("[tours] enabled is false; not starting", "set enabled = true in /etc/motel/motel.toml")
        if not cfg.bindings:
            raise ConfigError("no [tours.bindings.*] are configured; nothing to proxy")
        stored, fds = fdstore.inherited_listeners()
        store = fdstore.FdStore(limit=settings.MAX_PORT_RANGE)
        if not store.enabled and not args.dev_rebind:
            raise ConfigError("not started by systemd with a notify socket, so attempts cannot be kept across restarts safely",
                              "run it as the motel-concierge service (see docs/concierge.md); for development only: --dev-rebind (unsafe)")
        if store.enabled and not args.dev_rebind:
            low, high = cfg.concierge_port_range
            problem = store.check_unit(high - low + 1)
            if problem:
                raise ConfigError(problem, "sudo motel install (it writes motel-concierge.service with FileDescriptorStoreMax=512 and "
                                           "the MOTEL_FDSTORE_MAX marker), then systemctl daemon-reload")
        concierge = Concierge(cfg, args.state_dir, admin_path=None if fds else args.admin_socket, admin_fd=fds[0] if fds else None,
                              store=store, inherited=stored, dev_rebind=args.dev_rebind)
    except ConciergeError as exc:
        print(exc.line(), file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"motel: cannot start the concierge ({exc.strerror}). Try: systemctl status motel-concierge", file=sys.stderr)
        return 5
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    concierge.start()
    logging.getLogger("motel.concierge").info("one listener per attempt on 127.0.0.1, ports %d-%d (%d open); bindings: %s",
                                              *cfg.concierge_port_range, concierge.endpoints.count(), ", ".join(sorted(cfg.bindings)))
    stop.wait()
    concierge.stop()
    return 0

"""One loopback listener per attempt.

Why: the shared guest listener could not tell guests apart before they authenticated, so one hostile cabin could keep the
whole connection pool busy with connections that never finished their headers. Now every attempt has its own listener on
a port of its own (``[tours] concierge_port_range``), opened by ``issue`` and closed on revoke or expiry. A connection is
attributed to an attempt by the port it arrives on, before a byte is read, and each listener has a small pool of its own
(``max_concurrent + 2`` authenticated connections plus 2 that have not authenticated yet), so what an attempt does on its
port affects nobody else. A token is accepted only on its own attempt's listener.

The engine reaches the listener through the ssh reverse forward it already opens: the guest keeps one fixed port
(``concierge_port``), and the forward goes to the attempt's port: ``-R 127.0.0.1:<concierge_port>:127.0.0.1:<port>``,
with ``port`` from ``ConciergeClient.endpoint(attempt=...)``.

A live attempt never moves to another port, and a start never binds its port again. The only thing that keeps an attempt live
across a restart is its listening socket coming back from systemd's file-descriptor store (``motel.concierge.fdstore``): the
socket never stopped being bound, so nobody could take the port. A socket the service closes and binds again leaves a window in
which another local process can hold the port, read the placeholder token from the cabin's reconnecting forward and let go
again, and the token would then work. So every live attempt whose socket did not come back is revoked at start, the reason is
logged, and ``health()`` lists it under ``lost_listeners`` (``not_preserved``); the engine sees ``gone`` from ``endpoint`` and
treats the attempt as cut. For the same reason ``issue`` stores each new socket first and fails with ``busy`` when it cannot
(no notify socket, the store full) instead of running the attempt unprotected. ``dev_rebind`` (the ``--dev-rebind`` flag) is the
unsafe exception: no store is needed and a start binds the recorded port again, revoking the attempt only if that fails.
A port freed by a revoke or expiry is not given to another attempt for ``QUARANTINE_S`` while another port is free.
"""

from __future__ import annotations

import logging
import random
import socket
import threading

from motel.concierge import proxy
from motel.concierge.errors import AdminRefused

log = logging.getLogger("motel.concierge")

QUARANTINE_S = 600.0  # a freed port is not handed to another attempt for this long, if the range has another free port


class Endpoints:
    def __init__(self, core: proxy.Core, *, port_range: tuple[int, int] | None, bind_host: str = "127.0.0.1", fdstore=None,
                 dev_rebind: bool = False):
        """``port_range=None`` lets the operating system pick any free port (tests). ``fdstore`` keeps sockets across restarts."""
        self.core = core
        self.fdstore = fdstore
        self.dev_rebind = dev_rebind
        self._freed: dict[int, float] = {}  # port -> when its listener closed (quarantine; in memory, so a restart forgets it)
        self.lost: dict[str, str] = {}  # attempt -> why its listener could not be re-opened (since this process started)
        self.port_range = port_range
        self.bind_host = bind_host
        self._lock = threading.Lock()
        self._servers: dict[str, proxy.GuestServer] = {}

    # -- sockets -------------------------------------------------------------------------------------------------

    def _bind(self, port: int) -> socket.socket | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, port))
            sock.listen(proxy.GuestServer.request_queue_size)
            return sock
        except OSError:
            sock.close()
            return None

    def reserve(self, preferred: int = 0) -> socket.socket:
        """A bound, listening socket on a free port. ``preferred`` is tried first (restart); otherwise any port of the range."""
        if preferred:
            sock = self._bind(preferred)
            if sock is not None:
                return sock
        if self.port_range is None:
            sock = self._bind(0)
            if sock is not None:
                return sock
        else:
            low, high = self.port_range
            now = self.core.registry.clock()
            with self._lock:
                self._freed = {p: t for p, t in self._freed.items() if now - t < QUARANTINE_S and low <= p <= high}  # forget the served ones
                freed = dict(self._freed)
            fresh = [p for p in range(low, high + 1) if p not in freed]
            random.shuffle(fresh)
            # A port whose listener closed minutes ago may still be dialled by a forward or a retry that belonged to the old
            # attempt, so another attempt gets it only when nothing else is free, and then the one freed longest ago.
            for port in fresh + sorted(freed, key=freed.__getitem__):
                sock = self._bind(port)
                if sock is not None:
                    return sock
        raise AdminRefused("no free port for this attempt's listener; revoke finished attempts or widen [tours] concierge_port_range",
                           "busy")

    # -- listeners -----------------------------------------------------------------------------------------------

    def start(self, att, sock: socket.socket) -> int:
        server = proxy.GuestServer(sock, self.core, att.attempt)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                                  name=f"concierge-{att.attempt}")
        with self._lock:
            self._servers[att.attempt] = server
        thread.start()
        return server.server_address[1]

    def preserve(self, attempt: str, sock: socket.socket) -> None:
        """Hand a new attempt's socket to the store before the attempt exists. Refuses (``busy``) when that is impossible."""
        stored = self.fdstore is not None and self.fdstore.add(attempt, sock)
        if not stored and not self.dev_rebind:
            sock.close()
            raise AdminRefused("cannot keep this attempt's listening socket in systemd's file-descriptor store (no notify socket, or the "
                               "store is full); not issuing an attempt that a restart could not keep safe", "busy")

    def unstore(self, attempt: str) -> None:
        if self.fdstore is not None:
            self.fdstore.remove(attempt)

    def port_of(self, attempt: str) -> int | None:
        with self._lock:
            server = self._servers.get(attempt)
        return server.server_address[1] if server is not None else None

    def count(self) -> int:
        with self._lock:
            return len(self._servers)

    def close(self, attempt: str, *, forget: bool = True) -> None:
        """Stop listening. ``forget`` also drops the stored descriptor (the attempt ended); a restart keeps it."""
        with self._lock:
            server = self._servers.pop(attempt, None)
        if server is not None:
            port = server.server_address[1]
            server.shutdown()
            server.server_close()
            if forget and self.port_range is not None:
                with self._lock:
                    self._freed[port] = self.core.registry.clock()
        if forget and self.fdstore is not None:
            self.fdstore.remove(attempt)

    def close_all(self) -> None:
        """Shutdown: close our copies and leave the stored descriptors with systemd for the next start."""
        with self._lock:
            names = list(self._servers)
        for name in names:
            self.close(name, forget=False)

    def sweep(self) -> None:
        """Close the listeners of attempts that are revoked or expired (expiry has no event of its own)."""
        registry = self.core.registry
        with self._lock:
            names = list(self._servers)
        for name in names:
            try:
                att = registry.get(name)
            except Exception:  # noqa: BLE001 - forgotten attempt
                self.close(name)
                continue
            if not registry.live(att):
                self.close(name)

    def restore(self, attempts, inherited: dict | None = None) -> None:
        """After a restart: a live attempt keeps its listener only if its socket came back from the store, on the port the ledger
        records. Every other live attempt is revoked (``lost``); nothing is bound again (except with ``dev_rebind``).

        ``inherited`` is ``{attempt: [sockets]}`` as systemd returned them; names are not unique there, so extras are closed
        and removed. A stored socket that no live attempt claims is closed and removed too."""
        inherited = {name: (list(v) if isinstance(v, list | tuple) else [v]) for name, v in (inherited or {}).items()}
        for att in attempts:
            if self.port_of(att.attempt) is not None:
                continue
            socks = inherited.pop(att.attempt, [])
            keep = next((s for s in socks if att.port and s.getsockname()[1] == att.port), None)
            for sock in socks:
                if sock is not keep:
                    sock.close()
            if keep is not None and self.fdstore is not None:
                if len(socks) > 1:  # removal drops every entry of the name, ours included: put the right one back
                    self.fdstore.remove(att.attempt)
                    if not self.fdstore.add(att.attempt, keep):
                        keep.close()
                        keep = None
                else:
                    self.fdstore.adopt(att.attempt)
            elif socks and keep is None and self.fdstore is not None:
                self.fdstore.remove(att.attempt)  # stored on a port the ledger does not record: not trusted, and not kept
            if keep is None and self.dev_rebind and att.port:
                keep = self._bind(att.port)
                if keep is None:
                    self._lose(att, "port_taken", f"port {att.port} is held by another process (--dev-rebind)")
                    continue
            if keep is None:
                self._lose(att, "not_preserved", "its listening socket did not come back from systemd's file-descriptor store")
                continue
            self.start(att, keep)
        for name, socks in inherited.items():  # stored for an attempt that is no longer live
            for sock in socks:
                sock.close()
            if self.fdstore is not None:
                self.fdstore.remove(name)

    def _lose(self, att, reason: str, detail: str) -> None:
        log.error("attempt %s: %s; revoking it rather than leaving it live on a port that could have been taken", att.attempt, detail)
        self.lost[att.attempt] = reason
        self.core.registry.revoke(attempt=att.attempt)

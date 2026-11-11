"""A TCP relay over one byte stream (the reverse forward that a cabin's sshd does not allow).

Cabins admit ssh sessions only through a forced command, and their sshd has ``AllowTcpForwarding no`` and the
client key has ``restrict``. So ``ssh -R`` cannot work. This module gives the same effect over the one thing a
cabin does allow: a command's stdin and stdout.

    controller                                         cabin
    Relay(controller side) <-- stdout/stdin of ssh --> Relay(cabin side), listening on 127.0.0.1:PORT

The cabin side listens on a loopback port. Each connection to it becomes a numbered stream. The controller side
opens a socket to the real target (the Omnigent server, on the controller's loopback) for each stream and copies
bytes both ways. Standard library only: the same file is the program that runs on the cabin (``python3 -I -c
SOURCE cabin PORT [MAX_STREAMS]``), so it never imports Omnigent.

Wire format. The cabin first writes ``HELLO`` (``\\xffMOTEL-RELAY/<version>\\n``). The controller skips any bytes
before it (a login script that prints to stdout) and reports them; a missing or different hello is an error.
After that, frames: ``type (1 byte) | stream id (4 bytes) | length (4 bytes) | payload``, big endian.

Every stream is flow controlled in each direction. A sender may have at most ``WINDOW`` unacknowledged bytes; the
receiver writes into the socket from a thread of its own for that stream and sends ``CREDIT`` after each write. So a
stream whose reader has stopped blocks only itself. A socket that reads EOF sends ``CLOSE_WRITE`` (a half close,
so the other end sees EOF too); ``CLOSE`` resets the stream.
"""

from __future__ import annotations

import errno
import socket
import struct
import sys
import threading
import time
from collections import deque

VERSION = "2"
MAGIC = b"\xffMOTEL-RELAY/"
HELLO = MAGIC + VERSION.encode() + b"\n"
HEADER = struct.Struct(">BII")
CREDIT_FMT = struct.Struct(">I")
T_OPEN, T_DATA, T_CLOSE, T_READY, T_ERROR, T_CLOSE_WRITE, T_CREDIT = 1, 2, 3, 4, 5, 6, 7
CHUNK = 32768
MAX_FRAME = 1 << 20
WINDOW = 262144
DEFAULT_MAX_STREAMS = 64
MAX_STRAY = 16384
LOOPBACK = "127.0.0.1"


class Stream:
    """One relayed connection: its socket, a pump thread (socket -> peer) and a writer thread (peer -> socket)."""

    def __init__(self, relay: Relay, stream_id: int):
        self.relay, self.id = relay, stream_id
        self.cv = threading.Condition()
        self.sock: socket.socket | None = None
        self.credit = WINDOW  # bytes we may still send to the peer
        self.queue: deque[bytes | None] = deque()  # to write to the socket; None = the peer's FIN
        self.queued = 0
        self.dead = False
        self.sent_fin = False
        self.wrote_fin = False

    def start(self, sock: socket.socket) -> None:
        with self.cv:
            if self.dead:
                _quiet_close(sock)
                return
            self.sock = sock
        threading.Thread(target=self._write_loop, daemon=True).start()
        threading.Thread(target=self._pump_loop, daemon=True).start()

    # -- frames from the peer (called from the relay's single reader thread; never blocks)
    def feed(self, data: bytes) -> None:
        with self.cv:
            if self.dead:
                return
            if self.queued + len(data) <= 2 * WINDOW:
                self.queue.append(data)
                self.queued += len(data)
                self.cv.notify_all()
                return
        self.teardown(send_close=True)  # the peer ignored the window: reset the stream

    def feed_fin(self) -> None:
        with self.cv:
            if not self.dead:
                self.queue.append(None)
                self.cv.notify_all()

    def add_credit(self, count: int) -> None:
        with self.cv:
            self.credit += count
            self.cv.notify_all()

    # -- threads
    def _pump_loop(self) -> None:
        sock = self.sock
        try:
            while True:
                data = sock.recv(CHUNK)
                if not data:
                    break
                view = memoryview(data)
                while len(view):
                    with self.cv:
                        while self.credit <= 0 and not self.dead:
                            self.cv.wait(1.0)
                        if self.dead:
                            return
                        take = min(len(view), self.credit)
                        self.credit -= take
                    if not self.relay.send(T_DATA, self.id, bytes(view[:take])):
                        self.teardown()
                        return
                    view = view[take:]
        except OSError:
            self.teardown(send_close=True)
            return
        with self.cv:
            if self.dead:
                return
            self.sent_fin = True
        if self.relay.send(T_CLOSE_WRITE, self.id):
            self._maybe_finish()

    def _write_loop(self) -> None:
        sock = self.sock
        while True:
            with self.cv:
                while not self.queue and not self.dead:
                    self.cv.wait(1.0)
                if self.dead:
                    return
                item = self.queue.popleft()
                if item is not None:
                    self.queued -= len(item)
            try:
                if item is None:
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    with self.cv:
                        self.wrote_fin = True
                    self._maybe_finish()
                    return
                sock.sendall(item)
            except OSError:
                self.teardown(send_close=True)
                return
            self.relay.send(T_CREDIT, self.id, CREDIT_FMT.pack(len(item)))

    def _maybe_finish(self) -> None:
        with self.cv:
            done = self.sent_fin and self.wrote_fin
        if done:
            self.teardown()

    def teardown(self, send_close: bool = False) -> None:
        with self.cv:
            if self.dead:
                return
            self.dead = True
            sock = self.sock
            self.queue.clear()
            self.cv.notify_all()
        self.relay.forget(self.id)
        if sock is not None:
            _quiet_close(sock)
        if send_close:
            self.relay.send(T_CLOSE, self.id)


def _quiet_close(sock: socket.socket) -> None:
    # shutdown first: close() alone does not wake a thread that is blocked in recv() or sendall() on this socket
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Relay:
    """One end of the relay. ``connect(stream_id)`` is given on the controller side and returns a socket (or
    raises ``OSError``); the cabin side has none and instead accepts connections in :meth:`listen`."""

    def __init__(self, rfile, wfile, connect=None, *, expect_hello: bool = False, max_streams: int = DEFAULT_MAX_STREAMS):
        self.rfile, self.wfile, self.connect = rfile, wfile, connect
        self.expect_hello, self.max_streams = expect_hello, max_streams
        self.write_lock = threading.Lock()
        self.lock = threading.Lock()
        self.streams: dict[int, Stream] = {}
        self.next_id = 1
        self.ready = threading.Event()  # set when the peer answered, or when the stream ended
        self.listening = False  # the cabin side bound its port
        self.error: str | None = None
        self.stray = b""  # bytes the cabin printed before the hello
        self.stop = threading.Event()
        self.server: socket.socket | None = None
        self.refused = 0

    # -- frames
    def send(self, kind: int, stream: int, payload: bytes = b"") -> bool:
        try:
            with self.write_lock:
                self.wfile.write(HEADER.pack(kind, stream, len(payload)) + payload)
                self.wfile.flush()
            return True
        except (OSError, ValueError):
            self.stop.set()
            return False

    def recv(self):
        head = self._read(HEADER.size)
        if head is None:
            return None
        kind, stream, length = HEADER.unpack(head)
        if length > MAX_FRAME:
            return None
        payload = self._read(length) if length else b""
        return None if payload is None else (kind, stream, payload)

    def _read(self, count: int):
        data = b""
        while len(data) < count:
            try:
                chunk = self.rfile.read(count - len(data))
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _handshake(self) -> bool:
        """Find the cabin's hello. Bytes before it are kept in ``stray``; they are not frames."""
        seen = b""
        while MAGIC not in seen:
            try:
                byte = self.rfile.read(1)
            except (OSError, ValueError):
                byte = b""
            if not byte:
                self.error = ("the cabin's relay never said hello" +
                              (f"; the session printed {seen[-200:]!r}" if seen else "; the session ended"))
                return False
            seen += byte
            if len(seen) > MAX_STRAY:
                self.error = f"too much output before the relay's hello (is a login script printing?): {seen[:200]!r}"
                return False
        head, _, rest = seen.partition(MAGIC)
        version = rest
        while not version.endswith(b"\n"):
            byte = self.rfile.read(1) if len(version) < 16 else b""
            if not byte:
                self.error = "the relay's hello was cut short"
                return False
            version += byte
        self.stray = head
        if version.strip().decode("ascii", "replace") != VERSION:
            self.error = f"the cabin speaks relay protocol {version.strip()!r}, this side speaks {VERSION!r}"
            return False
        return True

    # -- streams
    def forget(self, stream: int) -> None:
        with self.lock:
            self.streams.pop(stream, None)

    def close_all(self) -> None:
        self.stop.set()
        with self.lock:
            streams = list(self.streams.values())
        for stream in streams:
            stream.teardown()
        if self.server is not None:
            _quiet_close(self.server)

    def _open(self, stream_id: int) -> None:
        """The cabin announced a connection: connect to the target in a thread of its own (a slow connect must not
        hold up the other streams)."""
        with self.lock:
            if len(self.streams) >= self.max_streams or stream_id in self.streams:
                refuse = True
            else:
                refuse = False
                stream = self.streams[stream_id] = Stream(self, stream_id)
        if refuse:
            self.send(T_CLOSE, stream_id)
            return

        def connect():
            try:
                sock = self.connect(stream_id)
            except OSError:
                stream.teardown(send_close=True)
                return
            stream.start(sock)

        threading.Thread(target=connect, daemon=True).start()

    # -- the loop both sides run
    def serve(self) -> None:
        """Read frames until the stream ends. Returns when the other side is gone."""
        try:
            if self.expect_hello and not self._handshake():
                return
            while not self.stop.is_set():
                frame = self.recv()
                if frame is None:
                    break
                kind, stream_id, payload = frame
                with self.lock:
                    stream = self.streams.get(stream_id)
                if kind == T_DATA:
                    if stream is not None:
                        stream.feed(payload)
                elif kind == T_CLOSE_WRITE:
                    if stream is not None:
                        stream.feed_fin()
                elif kind == T_CREDIT:
                    if stream is not None and len(payload) == CREDIT_FMT.size:
                        stream.add_credit(CREDIT_FMT.unpack(payload)[0])
                elif kind == T_CLOSE:
                    if stream is not None:
                        stream.teardown()
                elif kind == T_OPEN and self.connect is not None:
                    self._open(stream_id)
                elif kind == T_READY:
                    self.listening = True
                    self.ready.set()
                elif kind == T_ERROR:
                    self.error = payload.decode("utf-8", "replace")[:300]
                    self.ready.set()
        finally:
            self.close_all()
            self.ready.set()

    # -- cabin side
    def listen(self, port: int, wait: float = 0.0) -> bool:
        """Say hello, bind ``127.0.0.1:port`` (retrying a busy port for ``wait`` seconds: a previous relay may still
        be shutting down) and turn each connection into a stream. False when the bind fails."""
        self._raw_write(HELLO)
        deadline = time.monotonic() + wait
        while True:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind((LOOPBACK, port))
                server.listen(32)
                break
            except OSError as exc:
                server.close()
                if exc.errno == errno.EADDRINUSE and time.monotonic() < deadline:
                    time.sleep(0.25)
                    continue
                self.send(T_ERROR, 0, f"cannot listen on {LOOPBACK}:{port}: {exc.strerror or exc}".encode())
                return False
        self.server = server
        self.send(T_READY, 0)
        threading.Thread(target=self._accept, args=(server,), daemon=True).start()
        return True

    def _raw_write(self, data: bytes) -> None:
        try:
            with self.write_lock:
                self.wfile.write(data)
                self.wfile.flush()
        except (OSError, ValueError):
            self.stop.set()

    def _accept(self, server: socket.socket) -> None:
        while not self.stop.is_set():
            try:
                sock, _ = server.accept()
            except OSError:
                return
            with self.lock:
                if len(self.streams) >= self.max_streams:
                    stream = None
                else:
                    stream_id, self.next_id = self.next_id, self.next_id + 1
                    stream = self.streams[stream_id] = Stream(self, stream_id)
            if stream is None:
                self.refused += 1
                _quiet_close(sock)  # a clean refusal: the client sees EOF at once
                continue
            if not self.send(T_OPEN, stream_id):
                stream.teardown()
                _quiet_close(sock)
                return
            stream.start(sock)


def cabin_main(port: int, max_streams: int = DEFAULT_MAX_STREAMS) -> int:
    """Entry point on the cabin: speak the protocol on stdin and stdout."""
    relay = Relay(sys.stdin.buffer, sys.stdout.buffer, max_streams=max_streams)
    if not relay.listen(port, wait=10.0):
        return 1
    relay.serve()
    return 0


def controller_relay(rfile, wfile, host: str, port: int, timeout: float = 5.0, max_streams: int = DEFAULT_MAX_STREAMS) -> Relay:
    """The controller end: every stream the cabin opens connects to ``host:port`` here."""

    def connect(_stream: int) -> socket.socket:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(None)
        return sock

    return Relay(rfile, wfile, connect=connect, expect_hello=True, max_streams=max_streams)


if __name__ == "__main__" and len(sys.argv) in (3, 4) and sys.argv[1] == "cabin":
    sys.exit(cabin_main(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) == 4 else DEFAULT_MAX_STREAMS))

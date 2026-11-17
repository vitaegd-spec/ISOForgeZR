"""
single_instance.py
------------------
Coalesces the separate process launches Explorer produces for a
multi-file selection back into one operation.

The problem
============
Registry context-menu verbs receive ONE file per invocation. Selecting
twelve files and clicking "Compress with ISOForge..." therefore starts
twelve copies of ISOForge, each told about a single file. Without
handling that, the user gets twelve windows each containing one file --
the exact opposite of what they asked for.

(A COM shell-extension DLL would receive the whole selection in a single
call, but that isn't buildable from Python -- see `shell_integration.py`
for the full reasoning behind the registry approach.)

How this solves it
===================
The first process to start claims a named local socket and becomes the
PRIMARY. Every later process fails to claim it, connects to it instead,
hands over its file path, and exits immediately.

The primary collects incoming paths and waits for a short quiet period
(`_BATCH_DELAY_MS`) after the most recent one before acting. That
debounce is what turns twelve rapid launches into one grouped request.

Why a debounce rather than a count
===================================
Nothing tells us up front how many files were selected, so there is no
total to wait for. A short idle timeout is the only reliable signal that
Explorer has finished launching processes. The delay is deliberately
generous enough to absorb the gaps between launches on a slow machine,
while staying short enough to feel immediate.

Windows also caps how many files it will invoke a verb for (15 by
default), so very large selections are truncated by Explorer before
ISOForge ever sees them -- a shell limitation, not one introduced here.

Graceful degradation
=====================
Every failure path falls back to "act alone". If the socket can't be
created or connected to -- locked-down machine, unusual profile,
whatever -- the process simply handles its own request by itself. The
result is the old one-window-per-file behaviour rather than a crash,
which is a reasonable worst case.
"""
from __future__ import annotations

import getpass
import json
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Quiet period after the last incoming request before the batch is acted
# on. Long enough to absorb the gaps between Explorer's launches, short
# enough that a single-file action still feels instant.
_BATCH_DELAY_MS = 700

# How long a secondary instance waits to hand its payload over before
# giving up and handling the request itself.
_CONNECT_TIMEOUT_MS = 1500
_WRITE_TIMEOUT_MS = 1500


def server_name() -> str:
    """Per-user socket name.

    Scoping to the username keeps separate users on the same machine
    (or an RDP host) from batching each other's requests together.
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = "default"
    return f"ISOForgeZR-shell-{user}"


def send_to_running_instance(payload: dict) -> bool:
    """Hands `payload` to an existing primary instance.

    Returns True only when the data was genuinely written and flushed --
    the caller uses that to decide whether it may exit. Any doubt returns
    False so the request is still handled locally rather than lost.
    """
    socket = QLocalSocket()
    socket.connectToServer(server_name())
    if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
        return False

    try:
        # Newline-delimited JSON: the receiver needs a frame boundary,
        # since several payloads can arrive coalesced in one read.
        data = (json.dumps(payload) + "\n").encode("utf-8")
        socket.write(data)
        if not socket.waitForBytesWritten(_WRITE_TIMEOUT_MS):
            return False
        socket.flush()
        return True
    except Exception:
        return False
    finally:
        socket.disconnectFromServer()


class RequestBatcher(QObject):
    """Primary-instance listener that groups incoming requests.

    `handler` is called with a list of payload dicts once the stream has
    been quiet for `_BATCH_DELAY_MS`.
    """

    def __init__(self, handler: Callable[[List[dict]], None], parent=None):
        super().__init__(parent)
        self._handler = handler
        self._pending: List[dict] = []
        self._buffers: dict = {}
        self._server: Optional[QLocalServer] = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    # -- lifecycle ----------------------------------------------------- #
    def listen(self) -> bool:
        """Tries to become the primary instance.

        Deliberately attempts `listen()` FIRST and only clears the socket
        name if that fails. The reverse order looks equivalent but isn't:
        on Unix-style sockets `removeServer()` unlinks the socket file
        even when another process is actively listening on it, so
        clearing pre-emptively could silently evict a healthy primary
        when two launches race. Trying first means the name is only ever
        cleared when it is genuinely unusable -- i.e. left behind by a
        process that died without cleaning up.
        """
        server = QLocalServer(self)
        name = server_name()

        if not server.listen(name):
            # The name is taken. Before clearing it, PROVE it's stale by
            # trying to connect: a live primary answers, a leftover
            # doesn't. Skipping that check would be unsafe on Unix-style
            # sockets, where removeServer() unlinks the file even when
            # another process is actively listening -- silently evicting
            # a healthy primary if two launches raced.
            probe = QLocalSocket()
            probe.connectToServer(name)
            alive = probe.waitForConnected(_CONNECT_TIMEOUT_MS)
            probe.disconnectFromServer()
            if alive:
                return False

            QLocalServer.removeServer(name)
            if not server.listen(name):
                return False

        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(server_name())
            self._server = None

    # -- intake -------------------------------------------------------- #
    def add_local(self, payload: dict) -> None:
        """Queues this process's own request alongside any that arrive."""
        self._pending.append(payload)
        self._timer.start(_BATCH_DELAY_MS)

    def _on_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._on_ready(s))
            socket.disconnected.connect(lambda s=socket: self._on_closed(s))

    def _on_ready(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))

        # Consume whole lines only; a partial payload stays buffered
        # until the rest of it arrives.
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer.clear()
            buffer.extend(rest)
            self._accept(line)

    def _on_closed(self, socket: QLocalSocket) -> None:
        # A sender that closed without a trailing newline still has a
        # complete payload worth keeping.
        buffer = self._buffers.pop(socket, None)
        if buffer:
            self._accept(bytes(buffer))
        socket.deleteLater()

    def _accept(self, raw: bytes) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        if isinstance(payload, dict):
            self._pending.append(payload)
            # Restarting the timer on every arrival is what makes this a
            # debounce rather than a fixed window: the batch closes once
            # the launches stop, however long they take to arrive.
            self._timer.start(_BATCH_DELAY_MS)

    # -- dispatch ------------------------------------------------------ #
    def _flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self._handler(batch)

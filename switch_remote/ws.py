"""標準ライブラリだけで書いた最小限の WebSocket (RFC 6455) 実装。"""
import base64
import hashlib
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")


class WebSocket:
    def __init__(self, sock, rfile):
        self.sock = sock
        self.rfile = rfile
        self.closed = False
        self._wlock = threading.Lock()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _read(self, n):
        data = self.rfile.read(n)
        if len(data) < n:
            raise ConnectionError("closed")
        return data

    def recv(self):
        """メッセージを 1 つ受け取る。(is_text, payload) を返し、切断時は None。"""
        parts, first_op = [], None
        while True:
            try:
                b1, b2 = self._read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read(8))[0]
                if n > 4 * 1024 * 1024:
                    raise ConnectionError("frame too large")
                mask = self._read(4) if b2 & 0x80 else None
                payload = self._read(n)
            except (ConnectionError, OSError, ValueError):
                self.closed = True
                return None
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if n < 64 else _unmask(payload, mask)
            op = b1 & 0x0F
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close()
                return None
            if op != OP_CONT:
                first_op = op
            parts.append(payload)
            if b1 & 0x80:
                data = b"".join(parts)
                return first_op == OP_TEXT, data

    def send(self, data, binary=False):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._send_frame(OP_BINARY if binary else OP_TEXT, data)

    def _send_frame(self, op, data):
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x80 | op, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | op, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | op, 127, n)
        with self._wlock:
            if self.closed and op != OP_CLOSE:
                raise ConnectionError("closed")
            self.sock.sendall(head + data)

    def close(self):
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, b"")
            except OSError:
                pass
        self.closed = True


def _unmask(payload, mask):
    # 大きいペイロードは int として XOR したほうが速い
    n = len(payload)
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(m, "little")).to_bytes(n, "little")

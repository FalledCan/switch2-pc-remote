"""最小限の DNS サーバー。すべての A レコード問い合わせに PC の IP を返す。

Switch 2 の DNS をこの PC に向けると、本体の接続テストが PC の Web サーバーに届き、
「ネットワークへの登録が必要です」としてブラウザ（キャプティブポータル画面）が開く。
"""
import socket
import struct
import threading


def _parse_question(data):
    pos = 12
    labels = []
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0:
            raise ValueError("compressed name in question")
        labels.append(data[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length
    qtype, qclass = struct.unpack("!HH", data[pos:pos + 4])
    return ".".join(labels), qtype, qclass, pos + 4


def build_response(query, ip):
    tid, flags, qdcount = struct.unpack("!HHH", query[:6])
    if flags & 0x8000 or qdcount < 1:
        return None
    name, qtype, qclass, end = _parse_question(query)
    question = query[12:end]
    answer = b""
    if qtype in (1, 255) and qclass == 1:  # A / ANY
        answer = (struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ip))
    resp_flags = 0x8000 | (flags & 0x7900) | 0x0080  # QR + opcode/RD をコピー + RA
    header = struct.pack("!HHHHHH", tid, resp_flags, 1, 1 if answer else 0, 0, 0)
    return header + question + answer, name


class DNSServer:
    def __init__(self, ip, bind="0.0.0.0", port=53, log=None):
        self.ip = ip
        self.log = log or (lambda msg: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self._seen = set()

    def start(self):
        threading.Thread(target=self._loop, name="dns", daemon=True).start()

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(1500)
            except OSError:
                # Windows では応答先が閉じていると ConnectionResetError が上がることがある
                continue
            try:
                result = build_response(data, self.ip)
            except Exception:
                continue
            if not result:
                continue
            resp, name = result
            if name not in self._seen:
                self._seen.add(name)
                self.log("DNS %s -> %s (from %s)" % (name, self.ip, addr[0]))
            try:
                self.sock.sendto(resp, addr)
            except OSError:
                pass

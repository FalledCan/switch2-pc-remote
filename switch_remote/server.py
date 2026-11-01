"""HTTP サーバー: Switch 2 のブラウザに操作画面・画面映像を配信し、入力を受け取る。"""
import argparse
import json
import mimetypes
import os
import secrets
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import gamepad, input_win, ws
from .capture import Capturer
from .dns_server import DNSServer

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def detect_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # 実際には送信しない。経路選択で使う IF の IP を得る
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


class State:
    def __init__(self, capturer, ip, port, pin):
        self.capturer = capturer
        self.ip = ip
        self.port = port
        self.pin = pin
        self.tokens = set()
        self.client_frames = {}
        self.buttons_down = set()
        self.keys_down = set()
        self.pads = gamepad.PadManager()
        self.portal_seen = set()
        self.lock = threading.Lock()

    @property
    def base_url(self):
        return "http://%s/" % self.ip if self.port == 80 else "http://%s:%d/" % (self.ip, self.port)

    def authorized(self, token):
        return not self.pin or token in self.tokens


def handle_event(state, ev):
    t = ev.get("t")
    left, top, width, height = state.capturer.rect
    if t == "move":  # 正規化座標 (0..1) でモニター内の絶対位置
        input_win.move_to(left + float(ev["x"]) * (width - 1), top + float(ev["y"]) * (height - 1))
    elif t == "rel":
        input_win.move_rel(round(float(ev["dx"])), round(float(ev["dy"])))
    elif t == "down" or t == "up":
        b = ev.get("b", "left")
        input_win.button(b, t == "down")
        with state.lock:
            (state.buttons_down.add if t == "down" else state.buttons_down.discard)(b)
    elif t == "click":
        input_win.click(ev.get("b", "left"), int(ev.get("n", 1)))
    elif t == "scroll":
        input_win.scroll(int(ev.get("dy", 0)), int(ev.get("dx", 0)))
    elif t == "key":
        input_win.key_combo(ev.get("keys", []))
    elif t == "mrel":  # 生の相対移動（ゲームの視点操作用）
        input_win.mouse_delta(round(float(ev["dx"])), round(float(ev["dy"])))
    elif t == "keystate":
        k, down = str(ev["k"]).lower(), bool(ev.get("down"))
        input_win.key_state(k, down)
        with state.lock:
            (state.keys_down.add if down else state.keys_down.discard)(k)
    elif t == "text":
        input_win.type_text(str(ev.get("s", ""))[:2000])
    elif t == "pad":  # Switch コントローラー → 仮想 Xbox コントローラー
        state.pads.update(ev.get("i", 0), int(ev.get("b", 0)), ev.get("a", []), ev.get("l", "label"))
    elif t == "release":  # 接続が切れたときなどに押しっぱなしを解除
        if gamepad.available:
            state.pads.release_all()
        with state.lock:
            held, state.buttons_down = list(state.buttons_down), set()
            keys, state.keys_down = list(state.keys_down), set()
        for b in held:
            input_win.button(b, False)
        for k in keys:
            input_win.key_state(k, False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SwitchRemote/1.0"
    state = None  # type: State

    def setup(self):
        super().setup()
        # ヘッダと本体が別送信になるため、Nagle + 遅延 ACK で 100ms 以上待たされるのを防ぐ
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass

    # ---- 共通 -----------------------------------------------------------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _is_own_host(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in (self.state.ip, "localhost", "127.0.0.1")

    def _query(self):
        u = urlparse(self.path)
        return u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}

    # ---- ルーティング ---------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._query()
        st = self.state
        if not self._is_own_host():
            # Switch の接続テストなど、他ホスト宛てのアクセスは操作画面へ誘導する
            # Switch はポータル表示中も数秒おきに接続テストを繰り返すので初回だけ記録する
            key = (self.headers.get("Host"), self.client_address[0])
            if key not in st.portal_seen:
                st.portal_seen.add(key)
                log("ポータル誘導: %s%s from %s" % (key[0], path, key[1]))
            self._send(302, b"", headers={"Location": st.base_url})
            return
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path in ("/diag", "/diag.html"):
            return self._file("diag.html")
        if path.startswith("/static/"):
            return self._file(path[len("/static/"):])
        if path == "/api/info":
            left, top, width, height = st.capturer.rect
            return self._json({
                "pin": bool(st.pin), "authorized": st.authorized(q.get("k", "")),
                "monitor": st.capturer.monitor_index, "width": width, "height": height,
                "xpad": gamepad.available,
                "monitors": [{"index": i, "width": m["width"], "height": m["height"]}
                             for i, m in enumerate(st.capturer.monitors)],
            })
        if not st.authorized(q.get("k", "")):
            return self._send(403, b"forbidden")
        if path == "/frame.jpg":
            return self._frame(q)
        if path == "/stream.mjpg":
            return self._mjpeg(q)
        if path == "/ws":
            return self._websocket(q)
        self._send(404, b"not found")

    def do_POST(self):
        path, q = self._query()
        st = self.state
        try:
            body = self._read_json()
        except Exception:
            return self._send(400, b"bad request")
        if path == "/api/diag":
            # Switch のブラウザが何に対応しているかの診断結果（PIN 不要・入力は行わない）
            out = os.path.join(os.path.dirname(WEB_DIR), "..", "diag_result.json")
            with open(os.path.normpath(out), "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            log("診断結果を受信しました: diag_result.json (%s)" % self.client_address[0])
            return self._json({"ok": True})
        if path == "/api/login":
            if not st.pin or str(body.get("pin", "")) == st.pin:
                token = secrets.token_hex(16)
                st.tokens.add(token)
                log("ログイン成功: %s" % self.client_address[0])
                return self._json({"token": token})
            log("PIN 不一致: %s" % self.client_address[0])
            time.sleep(1.0)
            return self._json({"error": "PIN が違います"}, 403)
        if not st.authorized(q.get("k", "")):
            return self._send(403, b"forbidden")
        if path == "/api/input":
            for ev in body.get("events", [])[:200]:
                try:
                    handle_event(st, ev)
                except Exception as e:
                    log("入力エラー %r: %s" % (ev, e))
            left, top, width, height = st.capturer.rect
            x, y = input_win.cursor_pos()
            return self._json({"cx": (x - left) / max(width - 1, 1), "cy": (y - top) / max(height - 1, 1)})
        if path == "/api/monitor":
            st.capturer.set_monitor(int(body.get("index", 1)))
            return self._json({"monitor": st.capturer.monitor_index})
        self._send(404, b"not found")

    # ---- 個別ハンドラ ---------------------------------------------------
    def _file(self, name):
        full = os.path.normpath(os.path.join(WEB_DIR, name))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._send(404, b"not found")
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    @staticmethod
    def _params(q):
        width = max(320, min(int(q.get("w", 1280) or 1280), 3840))
        quality = max(10, min(int(q.get("q", 60) or 60), 95))
        return width, quality

    def _frame(self, q):
        st = self.state
        width, quality = self._params(q)
        client = q.get("c", "")[:40]
        with st.lock:
            after = st.client_frames.get(client, 0)
        frame_id = st.capturer.wait_frame(after, timeout=1.0)
        with st.lock:
            if len(st.client_frames) > 100:
                st.client_frames.clear()
            st.client_frames[client] = frame_id
        self._send(200, st.capturer.jpeg(frame_id, width, quality), "image/jpeg")

    def _mjpeg(self, q):
        st = self.state
        width, quality = self._params(q)
        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + boundary)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last = 0
        try:
            while True:
                last = st.capturer.wait_frame(last, timeout=1.0)
                data = st.capturer.jpeg(last, width, quality)
                self.wfile.write(("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                  % (boundary, len(data))).encode("ascii"))
                self.wfile.write(data + b"\r\n")
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass

    def _websocket(self, q):
        """映像を押し出し送信し、入力を受け取る低遅延経路。

        サーバー → クライアント: バイナリ = 8 バイトヘッダ (frame_id:uint32, encode_ms:uint16, 予約) + JPEG
                                 テキスト = JSON (pong / cursor)
        クライアント → サーバー: テキスト JSON {"t":"ack"} / {"t":"cfg",...} / {"t":"ping",...} / {"ev":[入力...]}
        未表示フレームは最大 MAX_INFLIGHT 枚に抑え、遅いときは古いフレームを送らず最新だけを送る。
        """
        st = self.state
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            return self._send(400, b"websocket only")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = ws.WebSocket(self.connection, self.rfile)
        cfg = {"w": int(q.get("w", 1280) or 1280), "q": int(q.get("q", 55) or 55), "fps": 60}
        cfg["w"], cfg["q"] = self._params({"w": cfg["w"], "q": cfg["q"]})
        cond = threading.Condition()
        inflight = [0]
        log("WebSocket 接続: %s" % self.client_address[0])

        def sender():
            last, last_sent = 0, 0.0
            try:
                while not sock.closed:
                    with cond:
                        while inflight[0] >= MAX_INFLIGHT and not sock.closed:
                            cond.wait(0.5)
                    fid = st.capturer.wait_frame(last, timeout=0.5)
                    if fid == last or sock.closed:
                        continue
                    wait = 1.0 / cfg["fps"] - (time.monotonic() - last_sent)
                    if wait > 0:
                        time.sleep(wait)
                        fid = st.capturer.frame_id
                    t0 = time.perf_counter()
                    data = st.capturer.jpeg(fid, cfg["w"], cfg["q"])
                    enc = min(int((time.perf_counter() - t0) * 1000), 65535)
                    with cond:
                        inflight[0] += 1
                    sock.send(struct.pack("<IHH", fid & 0xFFFFFFFF, enc, 0) + data, binary=True)
                    last, last_sent = fid, time.monotonic()
            except (ConnectionError, OSError):
                sock.closed = True

        threading.Thread(target=sender, name="ws-send", daemon=True).start()
        try:
            while True:
                msg = sock.recv()
                if msg is None:
                    break
                try:
                    m = json.loads(msg[1])
                except ValueError:
                    continue
                t = m.get("t")
                if t == "ack":
                    with cond:
                        inflight[0] = max(0, inflight[0] - int(m.get("n", 1)))
                        cond.notify_all()
                elif t == "ping":
                    left, top, width, height = st.capturer.rect
                    x, y = input_win.cursor_pos()
                    sock.send(json.dumps({"t": "pong", "c": m.get("c"),
                                          "cx": (x - left) / max(width - 1, 1),
                                          "cy": (y - top) / max(height - 1, 1)}))
                elif t == "cfg":
                    if "w" in m or "q" in m:
                        cfg["w"], cfg["q"] = self._params({"w": m.get("w", cfg["w"]), "q": m.get("q", cfg["q"])})
                    if "fps" in m:
                        cfg["fps"] = max(5, min(int(m["fps"]), 120))
                for ev in m.get("ev", [])[:200]:
                    try:
                        handle_event(st, ev)
                    except Exception as e:
                        log("入力エラー %r: %s" % (ev, e))
        finally:
            sock.close()
            with cond:
                cond.notify_all()
            handle_event(st, {"t": "release"})
            log("WebSocket 切断: %s" % self.client_address[0])


MAX_INFLIGHT = 2


def main(argv=None):
    p = argparse.ArgumentParser(description="Switch 2 から PC を遠隔操作するサーバー")
    p.add_argument("--ip", help="PC の LAN IP（省略時は自動検出）")
    p.add_argument("--port", type=int, default=80, help="HTTP ポート（既定 80。接続テスト誘導には 80 が必要）")
    p.add_argument("--no-dns", action="store_true", help="内蔵 DNS サーバーを起動しない")
    p.add_argument("--pin", help="接続用 PIN（省略時はランダムな 4 桁）")
    p.add_argument("--no-pin", action="store_true", help="PIN 認証を無効にする（非推奨）")
    p.add_argument("--monitor", type=int, default=1, help="キャプチャするモニター番号（1 から。0 は全画面結合）")
    p.add_argument("--fps", type=int, default=60, help="最大キャプチャ fps")
    args = p.parse_args(argv)

    input_win.set_dpi_aware()
    ip = args.ip or detect_lan_ip()
    pin = None if args.no_pin else (args.pin or "%04d" % secrets.randbelow(10000))

    capturer = Capturer(monitor=args.monitor, fps=args.fps)
    state = State(capturer, ip, args.port, pin)
    Handler.state = state

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    except OSError as e:
        log("HTTP ポート %d を開けません: %s" % (args.port, e))
        log("他のソフトがポートを使っていないか確認するか、--port で変更してください。")
        return 1
    httpd.daemon_threads = True

    dns_ok = False
    if not args.no_dns:
        try:
            DNSServer(ip, bind=ip, log=log).start()
            dns_ok = True
        except OSError as e:
            log("DNS サーバー (UDP 53) を開始できません: %s" % e)

    print("=" * 60)
    print(" Switch Remote 起動しました")
    print("   操作画面 URL : %s" % state.base_url)
    print("   PIN          : %s" % (pin or "（なし）"))
    print("   エンコーダー : %s / キャプチャ: dxcam があれば DXGI、なければ mss" % capturer.encoder)
    print("   ブラウザ診断 : %sdiag" % state.base_url)
    if gamepad.available:
        print("   コントローラー: 仮想 Xbox コントローラー使用可")
    else:
        print("   コントローラー: 仮想 Xbox 不可（キー割り当てで動作）。install_gamepad.bat で導入できます")
    if dns_ok:
        print("   DNS サーバー : %s:53 で待受中" % ip)
        print()
        print(" Switch 2 側の設定:")
        print("   設定 > インターネット > 接続中のネットワーク > 設定の変更")
        print("   DNS 設定 = 手動 / 優先 DNS = %s / 代替 DNS = %s" % (ip, ip))
        print("   保存して「接続テスト」→「ネットワークへの登録が必要です」→ 次へ")
    print(" 終了するには Ctrl+C")
    print("=" * 60, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("終了します")
    finally:
        handle_event(state, {"t": "release"})
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

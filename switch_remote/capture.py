"""画面キャプチャと JPEG エンコード。クライアントがいる間だけキャプチャする。

高速経路: dxcam (DXGI Desktop Duplication) + OpenCV リサイズ + simplejpeg (libjpeg-turbo)。
これらが使えない環境では mss + Pillow にフォールバックする。
"""
import io
import threading
import time

import numpy as np

from . import input_win

try:
    import mss
    _MSS = getattr(mss, "MSS", None) or mss.mss
except ImportError:  # pragma: no cover
    _MSS = None
try:
    import dxcam
except Exception:
    dxcam = None
try:
    import cv2
    import simplejpeg
except Exception:
    cv2 = simplejpeg = None

# マウスカーソル（矢印）の形。キャプチャにはカーソルが写らないので自前で描く。
_ARROW = np.array([(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)], np.float32)


class _DxcamSource:
    """DXGI で 1 モニターを取り込む。画面に変化がなければ None を返す。"""

    def __init__(self, mon):
        target = None
        for idx in range(16):
            try:
                cam = dxcam.create(output_idx=idx, output_color="BGRA")
            except Exception:
                break
            c = cam._output.desc.DesktopCoordinates
            if (c.left, c.top) == (mon["left"], mon["top"]):
                target = cam
                break
            cam.release()
        if target is None:
            raise RuntimeError("dxcam output not found")
        self.cam = target

    def grab(self):
        frame = self.cam.grab()
        return None if frame is None else np.asarray(frame)

    def close(self):
        try:
            self.cam.release()
        except Exception:
            pass


class _MssSource:
    def __init__(self, mon):
        self.mon = mon
        self.sct = _MSS()
        self.prev = None

    def grab(self):
        shot = self.sct.grab(self.mon)
        raw = shot.bgra
        if raw == self.prev:
            return None
        self.prev = raw
        return np.frombuffer(raw, np.uint8).reshape(shot.height, shot.width, 4)

    def close(self):
        self.sct.close()


class Capturer:
    def __init__(self, monitor=1, fps=60):
        self.fps = fps
        self.monitor_index = monitor
        self._cond = threading.Condition()
        self._frame_id = 0
        self._image = None          # BGRA の numpy 配列
        self._rect = (0, 0, 1, 1)
        self._cursor = (0, 0)
        self._last_demand = 0.0
        self._cache = {}
        self._cache_lock = threading.Lock()
        self.backend = "mss"
        with _MSS() as sct:
            self.monitors = [dict(m) for m in sct.monitors]
        if not 0 <= self.monitor_index < len(self.monitors):
            self.monitor_index = 1
        self.encoder = "simplejpeg" if simplejpeg else "pillow"
        threading.Thread(target=self._run, name="capture", daemon=True).start()

    # ---- 外部 API -------------------------------------------------------
    @property
    def rect(self):
        """現在のモニターの (left, top, width, height)（物理ピクセル）。"""
        m = self.monitors[self.monitor_index]
        return m["left"], m["top"], m["width"], m["height"]

    def set_monitor(self, index):
        if 0 <= index < len(self.monitors):
            self.monitor_index = index
            self.demand()

    def demand(self):
        self._last_demand = time.monotonic()
        with self._cond:
            self._cond.notify_all()

    @property
    def frame_id(self):
        return self._frame_id

    def wait_frame(self, after_id, timeout=1.0):
        """after_id より新しいフレームを待つ。タイムアウト時は最新フレームを返す。"""
        self.demand()
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._frame_id <= after_id or self._image is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 and self._image is not None:
                    break
                self._cond.wait(max(remaining, 0.05))
            return self._frame_id

    def jpeg(self, frame_id, width, quality):
        """frame_id のフレームを指定幅・品質で JPEG 化する（結果はキャッシュ）。"""
        key = (frame_id, width, quality)
        with self._cache_lock:
            data = self._cache.get(key)
        if data is not None:
            return data
        with self._cond:
            img, rect, cursor = self._image, self._rect, self._cursor
        h, w = img.shape[:2]
        scale = 1.0
        if width and width < w:
            scale = width / w
            size = (width, round(h * scale))
            if cv2 is not None:
                img = cv2.resize(img, size, interpolation=cv2.INTER_AREA if scale < 0.75 else cv2.INTER_LINEAR)
            else:
                from PIL import Image
                img = np.asarray(Image.fromarray(img).resize(size, Image.BILINEAR))
        else:
            img = img.copy()
        cx, cy = (cursor[0] - rect[0]) * scale, (cursor[1] - rect[1]) * scale
        if -20 < cx < img.shape[1] and -20 < cy < img.shape[0]:
            pts = (_ARROW * max(scale, 0.6) + (cx, cy)).astype(np.int32)
            if cv2 is not None:
                cv2.fillPoly(img, [pts], (255, 255, 255, 255))
                cv2.polylines(img, [pts], True, (0, 0, 0, 255), 1)
        if simplejpeg is not None:
            data = simplejpeg.encode_jpeg(np.ascontiguousarray(img), quality=quality, colorspace="BGRA",
                                          colorsubsampling="420", fastdct=True)
        else:
            from PIL import Image
            buf = io.BytesIO()
            Image.fromarray(img[:, :, 2::-1].copy()).save(buf, "JPEG", quality=quality)
            data = buf.getvalue()
        with self._cache_lock:
            if len(self._cache) > 8:
                self._cache.clear()
            self._cache[key] = data
        return data

    # ---- キャプチャスレッド --------------------------------------------
    def _open(self, index):
        mon = self.monitors[index]
        if dxcam is not None and index != 0:
            try:
                src = _DxcamSource(mon)
                self.backend = "dxcam"
                return src
            except Exception:
                pass
        self.backend = "mss"
        return _MssSource(mon)

    def _run(self):
        src, src_index = None, None
        prev_cursor = None
        fails = 0
        while True:
            if time.monotonic() - self._last_demand > 5:
                if src is not None:
                    src.close()
                    src = None
                with self._cond:
                    self._cond.wait(1.0)
                continue
            start = time.monotonic()
            index = self.monitor_index
            if src is None or index != src_index:
                if src is not None:
                    src.close()
                src, src_index = self._open(index), index
                prev_cursor = None
            try:
                frame = src.grab()
                fails = 0
            except Exception:
                # UAC 画面や解像度変更などで DXGI が失敗した場合は作り直す
                fails += 1
                src.close()
                src = None
                time.sleep(min(0.1 * fails, 1.0))
                continue
            cursor = input_win.cursor_pos()
            if frame is not None or cursor != prev_cursor or self._image is None:
                if frame is None and self._image is None:
                    time.sleep(0.005)
                    continue
                mon = self.monitors[index]
                with self._cond:
                    if frame is not None:
                        self._image = frame
                    self._rect = (mon["left"], mon["top"], mon["width"], mon["height"])
                    self._cursor = cursor
                    self._frame_id += 1
                    self._cond.notify_all()
                prev_cursor = cursor
            elapsed = time.monotonic() - start
            time.sleep(max(0.001, 1.0 / self.fps - elapsed))

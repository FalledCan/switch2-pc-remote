"""Switch のコントローラー入力を PC 上の仮想 Xbox 360 コントローラーとして再現する。

vgamepad（ViGEmBus ドライバー）が必要。使えない環境では available が False になり、
クライアントはキーボード割り当てにフォールバックする。
"""
import threading

try:
    import vgamepad as vg
    _ERROR = None
except Exception as e:  # ドライバー未導入だと import 時に失敗する
    vg = None
    _ERROR = str(e) or e.__class__.__name__

available = vg is not None
error = _ERROR

if vg is not None:
    B = vg.XUSB_BUTTON
    # Gamepad API の標準ボタン番号 → Xbox ボタン（10〜16 は共通）
    _COMMON = {8: B.XUSB_GAMEPAD_BACK, 9: B.XUSB_GAMEPAD_START, 10: B.XUSB_GAMEPAD_LEFT_THUMB,
               11: B.XUSB_GAMEPAD_RIGHT_THUMB, 12: B.XUSB_GAMEPAD_DPAD_UP, 13: B.XUSB_GAMEPAD_DPAD_DOWN,
               14: B.XUSB_GAMEPAD_DPAD_LEFT, 15: B.XUSB_GAMEPAD_DPAD_RIGHT, 16: B.XUSB_GAMEPAD_GUIDE,
               4: B.XUSB_GAMEPAD_LEFT_SHOULDER, 5: B.XUSB_GAMEPAD_RIGHT_SHOULDER}
    LAYOUTS = {
        # 刻印どおり: Switch の A → Xbox の A（決定ボタンの感覚を保つ）
        "label": {**_COMMON, 1: B.XUSB_GAMEPAD_A, 0: B.XUSB_GAMEPAD_B,
                  3: B.XUSB_GAMEPAD_X, 2: B.XUSB_GAMEPAD_Y},
        # 位置どおり: 下のボタン → Xbox の A
        "position": {**_COMMON, 0: B.XUSB_GAMEPAD_A, 1: B.XUSB_GAMEPAD_B,
                     2: B.XUSB_GAMEPAD_X, 3: B.XUSB_GAMEPAD_Y},
    }


def _axis(v):
    return max(-32768, min(32767, int(float(v) * 32767)))


class PadManager:
    def __init__(self):
        self._pads = {}
        self._lock = threading.Lock()

    def update(self, index, buttons, axes, layout="label"):
        """buttons: 押されているボタン番号のビットマスク、axes: [lx, ly, rx, ry]（下が +）。"""
        if vg is None:
            return False
        index = int(index) % 4
        with self._lock:
            pad = self._pads.get(index)
            if pad is None:
                pad = self._pads[index] = vg.VX360Gamepad()
            table = LAYOUTS.get(layout, LAYOUTS["label"])
            pad.reset()
            for bit, xbtn in table.items():
                if buttons >> bit & 1:
                    pad.press_button(button=xbtn)
            pad.left_trigger(value=255 if buttons >> 6 & 1 else 0)
            pad.right_trigger(value=255 if buttons >> 7 & 1 else 0)
            a = (list(axes) + [0, 0, 0, 0])[:4]
            pad.left_joystick(x_value=_axis(a[0]), y_value=_axis(-a[1]))
            pad.right_joystick(x_value=_axis(a[2]), y_value=_axis(-a[3]))
            pad.update()
        return True

    def release_all(self):
        with self._lock:
            for pad in self._pads.values():
                pad.reset()
                pad.update()

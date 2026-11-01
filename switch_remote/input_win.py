"""Windows のマウス・キーボード入力を SendInput で注入する。"""
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
user32.MapVirtualKeyW.restype = wt.UINT


def set_dpi_aware():
    """物理ピクセル座標で扱えるように DPI 対応を宣言する（キャプチャ座標と一致させる）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "apps": 0x5D,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplay": 0xB3,
    "zenkaku": 0xF3, "kanji": 0x19,
}
for _i in range(1, 13):
    VK["f%d" % _i] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _c in "0123456789":
    VK[_c] = ord(_c)
VK.update({"lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5})

EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
            0x5B, 0x5D, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xA3, 0xA5}
MEDIA = {0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xF3, 0x19}

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _key(vk=0, scan=0, flags=0):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def virtual_screen():
    gsm = user32.GetSystemMetrics
    return (gsm(SM_XVIRTUALSCREEN), gsm(SM_YVIRTUALSCREEN),
            gsm(SM_CXVIRTUALSCREEN), gsm(SM_CYVIRTUALSCREEN))


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move_to(x, y):
    """仮想デスクトップ上の物理ピクセル座標 (x, y) へカーソルを移動する。"""
    vx, vy, vw, vh = virtual_screen()
    x = min(max(int(x), vx), vx + vw - 1)
    y = min(max(int(y), vy), vy + vh - 1)
    nx = round((x - vx) * 65535 / max(vw - 1, 1))
    ny = round((y - vy) * 65535 / max(vh - 1, 1))
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


def move_rel(dx, dy):
    x, y = cursor_pos()
    move_to(x + dx, y + dy)


def mouse_delta(dx, dy):
    """生の相対移動（カーソルを固定する 3D ゲームの視点操作でも効く）。"""
    if dx or dy:
        _send(_mouse(MOUSEEVENTF_MOVE, int(dx), int(dy)))


def button(name, down):
    flags = BUTTONS.get(name, BUTTONS["left"])
    _send(_mouse(flags[0] if down else flags[1]))


def click(name="left", count=1):
    flags = BUTTONS.get(name, BUTTONS["left"])
    seq = []
    for _ in range(max(1, min(count, 3))):
        seq += [_mouse(flags[0]), _mouse(flags[1])]
    _send(*seq)


def scroll(dy=0, dx=0):
    """dy > 0 で上方向、dx > 0 で右方向（WHEEL_DELTA=120 単位）。"""
    if dy:
        _send(_mouse(MOUSEEVENTF_WHEEL, data=int(dy)))
    if dx:
        _send(_mouse(MOUSEEVENTF_HWHEEL, data=int(dx)))


def _vk_input(vk, up):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    if scan and vk not in MEDIA:
        # DirectInput / Raw Input を使うゲームはスキャンコードで判定するため、スキャンコードで送る
        return _key(0, scan, flags | KEYEVENTF_SCANCODE)
    return _key(vk, scan, flags)


def key_combo(keys):
    """['ctrl', 'c'] のようなキー名の組み合わせを順に押して逆順に離す。"""
    vks = [VK[k] for k in (s.lower() for s in keys) if k in VK]
    if not vks:
        return
    _send(*([_vk_input(v, False) for v in vks] + [_vk_input(v, True) for v in reversed(vks)]))


def key_state(name, down):
    vk = VK.get(name.lower())
    if vk is not None:
        _send(_vk_input(vk, not down))


def type_text(text):
    """Unicode 文字列をそのまま入力する（IME を介さず日本語も入力可能）。"""
    seq = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            seq += [_vk_input(VK["enter"], False), _vk_input(VK["enter"], True)]
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            seq += [_key(0, unit, KEYEVENTF_UNICODE),
                    _key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    if seq:
        _send(*seq)

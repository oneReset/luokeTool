import ctypes

import cv2
import mss
import numpy as np
import win32con
import win32gui
import win32ui


def capture_window_bgr(hwnd: int) -> np.ndarray:
    """
    通过 mss 截屏方式抓取窗口内容，对 DirectX/OpenGL 游戏更可靠。
    """
    try:
        rect = win32gui.GetWindowRect(hwnd)
        left, top, right, bottom = rect
        # 去掉标题栏和边框，只取客户区
        client_rect = win32gui.GetClientRect(hwnd)
        client_w = client_rect[2] - client_rect[0]
        client_h = client_rect[3] - client_rect[1]

        if client_w <= 0 or client_h <= 0:
            return np.zeros((1, 1, 3), dtype=np.uint8)

        with mss.mss() as sct:
            monitor = {"left": left, "top": top, "width": client_w, "height": client_h}
            img = np.array(sct.grab(monitor))
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    except Exception:
        # 回退到 PrintWindow
        return _capture_printwindow(hwnd)


def _capture_printwindow(hwnd: int) -> np.ndarray:
    """通过 Windows API 直接抓取窗口内容（回退方案）。"""
    client_rect = win32gui.GetClientRect(hwnd)
    client_w = client_rect[2] - client_rect[0]
    client_h = client_rect[3] - client_rect[1]

    if client_w <= 0 or client_h <= 0:
        return np.zeros((1, 1, 3), dtype=np.uint8)

    hwndDC = win32gui.GetDC(hwnd)
    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()

    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, client_w, client_h)

    saveDC.SelectObject(saveBitMap)

    try:
        result = ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 3)

        if result != 1:
            saveDC.BitBlt((0, 0), (client_w, client_h), mfcDC, (0, 0), win32con.SRCCOPY)

        signedIntsArray = saveBitMap.GetBitmapBits(True)
        img = np.frombuffer(signedIntsArray, dtype='uint8')

        expected_size = client_h * client_w * 4
        if len(img) != expected_size:
            img = np.zeros(expected_size, dtype='uint8')

        img.shape = (client_h, client_w, 4)
    finally:
        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)

    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

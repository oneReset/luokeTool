import atexit
import csv
import ctypes
import os
import random
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

import interception
import win32con
import win32gui

from config import CONFIG
from core.battle_actions import do_escape
from core.capture import capture_window_bgr
from core.input import _ensure_interception, click_at, press_once
from core.pet_detector import PetDetector
from core.util import _ts
from modes.ball import AutoBallMode
from modes.base import BattleEvent

_CALIB_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "aim_calib.csv"
)

# ── Transparent GDI overlay drawn directly on the game window ──────────────

_overlay_class_registered: bool = False


def _register_overlay_class():
    global _overlay_class_registered
    if _overlay_class_registered:
        return

    def _wnd_proc(hwnd, msg, wparam, lparam):
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # Keep a reference so it doesn't get GC'd
    _register_overlay_class._wnd_proc = _wnd_proc

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.lpszClassName = "PetDetOverlay"
    wc.hInstance = win32gui.GetModuleHandle(None)
    wc.hbrBackground = win32gui.GetStockObject(5)  # NULL_BRUSH
    win32gui.RegisterClass(wc)
    _overlay_class_registered = True


class OverlayWindow:
    """Transparent, click-through overlay on top of the game client area."""

    def __init__(self):
        self._hwnd = None
        self._width = 0
        self._height = 0

    def ensure(self, game_hwnd, x, y, w, h):
        """Create or reposition the overlay to cover (x,y,w,h) on screen."""
        if self._hwnd is not None and (w, h) != (self._width, self._height):
            self.destroy()

        if self._hwnd is None:
            _register_overlay_class()
            ex_style = win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
            self._hwnd = win32gui.CreateWindowEx(
                ex_style, "PetDetOverlay", "",
                win32con.WS_POPUP,
                x, y, w, h,
                0, 0, win32gui.GetModuleHandle(None), None,
            )
            win32gui.SetLayeredWindowAttributes(self._hwnd, 0xFF00FF, 0, win32con.LWA_COLORKEY)
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            self._width = w
            self._height = h
        else:
            win32gui.SetWindowPos(
                self._hwnd, win32con.HWND_TOPMOST, x, y, w, h,
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )

    def update(self, detections, frame_w, frame_h):
        """Redraw detection boxes. detections: list of (cx, cy, w, h, conf)."""
        if not self._hwnd:
            return

        hdc = ctypes.windll.user32.GetDC(self._hwnd)
        mem_dc = ctypes.windll.gdi32.CreateCompatibleDC(hdc)
        bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc, self._width, self._height)
        old_bmp = ctypes.windll.gdi32.SelectObject(mem_dc, bmp)

        # Fill magenta (transparent via color key)
        brush = ctypes.windll.gdi32.CreateSolidBrush(0x00FF00FF)
        rect = ctypes.wintypes.RECT(0, 0, self._width, self._height)
        ctypes.windll.user32.FillRect(mem_dc, ctypes.byref(rect), brush)
        ctypes.windll.gdi32.DeleteObject(brush)

        scale_x = self._width / frame_w
        scale_y = self._height / frame_h

        # Green pen for boxes
        pen = ctypes.windll.gdi32.CreatePen(0, 2, 0x0000FF00)
        ctypes.windll.gdi32.SelectObject(mem_dc, pen)
        null_brush = ctypes.windll.gdi32.GetStockObject(5)
        ctypes.windll.gdi32.SelectObject(mem_dc, null_brush)

        # Crosshair at center
        cx_c = self._width // 2
        cy_c = self._height // 2
        ctypes.windll.gdi32.MoveToEx(mem_dc, cx_c - 10, cy_c, None)
        ctypes.windll.gdi32.LineTo(mem_dc, cx_c + 10, cy_c)
        ctypes.windll.gdi32.MoveToEx(mem_dc, cx_c, cy_c - 10, None)
        ctypes.windll.gdi32.LineTo(mem_dc, cx_c, cy_c + 10)

        for d in detections:
            cx, cy, w, h, conf = d
            x1 = int((cx - w // 2) * scale_x)
            y1 = int((cy - h // 2) * scale_y)
            x2 = int((cx + w // 2) * scale_x)
            y2 = int((cy + h // 2) * scale_y)
            if x2 <= x1 or y2 <= y1:
                continue
            ctypes.windll.gdi32.Rectangle(mem_dc, x1, y1, x2, y2)

        # Blit to screen
        ctypes.windll.gdi32.BitBlt(hdc, 0, 0, self._width, self._height,
                                   mem_dc, 0, 0, 0x00CC0020)

        # Cleanup
        ctypes.windll.gdi32.SelectObject(mem_dc, old_bmp)
        ctypes.windll.gdi32.DeleteObject(bmp)
        ctypes.windll.gdi32.DeleteDC(mem_dc)
        ctypes.windll.user32.ReleaseDC(self._hwnd, hdc)

    def hide(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 0)  # SW_HIDE

    def destroy(self):
        if self._hwnd:
            ctypes.windll.user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            self._width = 0
            self._height = 0


_overlay = OverlayWindow()
atexit.register(_overlay.destroy)


class AutoBallPetMode(AutoBallMode):
    label = "自动抓宠"

    def __init__(self) -> None:
        super().__init__()
        self._detector: Optional[PetDetector] = None
        self._last_throw_time: float = 0.0
        self._last_detect_time: float = time.time()
        self._last_find_time: float = time.time()  # 最后找到精灵的时间
        self._last_rotate_time: float = 0.0
        self._aiming: bool = False
        self._overlay_hide_timer: Optional[threading.Timer] = None
        self._sensitivity_ratio_h: float = 1.0
        self._sensitivity_ratio_v: float = 1.0
        self._load_sensitivity_ratio()
        # 战斗内行动（同智能模式）
        self._pollute_action: str = "escape"
        self._normal_action: str = "escape"
        self._current_action: Optional[str] = None
        self._skill1_used: bool = False
        # 状态日志控制
        self._scan_count: int = 0
        self._last_scan_log_time: float = 0.0
        self._no_detect_count: int = 0
        # 目标锁状态
        self._ref_center: Optional[Tuple[int, int]] = None  # 参考点（上一帧选中目标中心）
        self._lock_lost: int = 0                             # 锁定目标连续丢失帧数
        # 标定状态
        self._calib_attempted: bool = False

    def set_battle_actions(self, pollute_action: str, normal_action: str) -> None:
        self._pollute_action = pollute_action
        self._normal_action = normal_action

    @property
    def name(self) -> str:
        return "auto_ball_pet"

    def _get_detector(self) -> PetDetector:
        if self._detector is None:
            self._detector = PetDetector()
            self._detector.preload()
        return self._detector

    def _select_target(self, detections: list, frame_w: int, frame_h: int) -> Optional[Tuple[int, int, int, int, float]]:
        """距离最近优先选目标（conf 仅在距离打平时决胜），并用位置连续性锁定。

        参考点为上一帧选中目标的中心（冷启动用屏幕中心）：
        - 锁定目标须落在匹配半径内才算同一目标，否则计丢失；
        - 连续丢失超过容忍帧数后释放锁，以旧参考点重选（仍选最近的）。
        """
        if not detections:
            self._lock_lost += 1
            if self._lock_lost >= CONFIG.pet_lock_lost_frames:
                self._ref_center = None
                self._lock_lost = 0
            return None

        cx0, cy0 = self._ref_center if self._ref_center is not None else (frame_w // 2, frame_h // 2)

        best = None
        best_key = None
        best_dist = 0.0
        for cx, cy, w, h, conf in detections:
            dist = ((cx - cx0) ** 2 + (cy - cy0) ** 2) ** 0.5
            key = (dist, -conf)
            if best_key is None or key < best_key:
                best = (cx, cy, w, h, conf)
                best_key = key
                best_dist = dist

        if self._ref_center is not None:
            radius = max(best[2], best[3]) * CONFIG.pet_lock_dist_ratio + CONFIG.pet_lock_dist_base
            if best_dist > radius:
                self._lock_lost += 1
                if self._lock_lost < CONFIG.pet_lock_lost_frames:
                    return None  # 锁定目标短暂丢失，本帧视为无目标
                self._lock_lost = 0  # 丢失超限，释放锁并接受当前最近目标
            else:
                self._lock_lost = 0

        self._ref_center = (best[0], best[1])
        return best

    def _detect_and_throw(self, hwnd: int) -> bool:
        """单轮精灵检测+瞄准+丢球。返回 True 表示检测到精灵并执行了丢球。"""
        if win32gui.GetForegroundWindow() != hwnd:
            _overlay.hide()
            return False

        self._scan_count += 1
        now = time.time()

        # 每 10 次扫描输出一次状态（约 4 秒一次，避免刷屏）
        if self._scan_count % 10 == 1 and now - self._last_scan_log_time > 3.0:
            status = "扫描中..." if self._no_detect_count == 0 else f"扫描中... (连续 {self._no_detect_count} 次未检测到精灵)"
            print(f"[{_ts()}] {status}")
            self._last_scan_log_time = now

        detector = self._get_detector()

        frame = capture_window_bgr(hwnd)
        if frame is None or frame.size == 0:
            print(f"[{_ts()}] [警告] 截图失败")
            return False

        try:
            detections = detector.detect(frame)
        except Exception as e:
            print(f"[{_ts()}] [错误] 检测异常: {e}")
            return False

        fh, fw = frame.shape[:2]
        target = self._select_target(detections, fw, fh)
        if target is None:
            self._no_detect_count += 1
            if now - self._last_detect_time > 0.2:
                _overlay.hide()

            # 回拉机制：超时未找到目标，向左下角回拉
            if CONFIG.pet_recovery_timeout > 0:
                time_since_last_find = now - self._last_find_time
                if time_since_last_find > CONFIG.pet_recovery_timeout:
                    self._recovery_pull(hwnd)
                    self._last_find_time = now  # 重置计时，避免频繁回拉
                    return False

            return False

        # 检测到精灵，重置计数
        self._no_detect_count = 0
        self._last_find_time = now
        self._last_detect_time = time.time()

        pet_cx, pet_cy, pet_w, pet_h, conf = target
        print(f"[{_ts()}] ✓ 检测到精灵: conf={conf:.2f} pos=({pet_cx},{pet_cy}) size=({pet_w}x{pet_h})  共{len(detections)}个目标")

        if self._overlay_hide_timer is None or not self._overlay_hide_timer.is_alive():
            _overlay.ensure(hwnd, *self._screen_rect(hwnd, fw, fh))
            _overlay.update(detections, fw, fh)
            self._overlay_hide_timer = threading.Timer(0.2, _overlay.hide)
            self._overlay_hide_timer.daemon = True
            self._overlay_hide_timer.start()

        # 首次检测时自动标定鼠标灵敏度（精灵法，作为后备）
        # 只尝试一次，避免失败后反复触发
        if (not self._calib_attempted and self._sensitivity_ratio_h == 1.0
                and self._sensitivity_ratio_v == 1.0 and CONFIG.pet_aim_calibrate):
            self._calib_attempted = True
            ratio = self._calibrate_sensitivity(hwnd, detector)
            if ratio > 0:
                self._sensitivity_ratio_h = ratio
                self._sensitivity_ratio_v = ratio
                print(f"[{_ts()}] 标定成功，重新检测后瞄准...")
                return True  # 标定成功移动了视角，需要重新检测
            # 标定失败：不 return，直接继续用默认灵敏度瞄准
            print(f"[{_ts()}] 标定失败，使用默认灵敏度直接瞄准")

        # 已标定过灵敏度 → 闭环校正（PID）
        if self._sensitivity_ratio_h != 1.0 or self._sensitivity_ratio_v != 1.0:
            self._aim_calibrated(hwnd, pet_cx, pet_cy, pet_w, pet_h, conf, detector)
        elif CONFIG.pet_iterative_aim:
            self._aim_iterative(hwnd, pet_cx, pet_cy, pet_w, pet_h, conf, detector)
        else:
            self._aim_one_shot(hwnd, pet_cx, pet_cy, pet_w, pet_h, conf)

        # 瞄准后丢球
        if click_at(hwnd):
            self._last_throw_time = time.time()
            print(f"[{_ts()}] 精灵丢球点击 (conf={conf:.2f})")
        else:
            print(f"[{_ts()}] [警告] 精灵丢球点击失败")

        # 丢球后检测一次，有精灵继续处理
        frame = capture_window_bgr(hwnd)
        if frame is not None and frame.size > 0:
            try:
                dets = detector.detect(frame)
            except Exception:
                dets = []
            if dets:
                return True
        self._last_detect_time = time.time()
        return True

    def on_non_battle_no_action(self, event: BattleEvent) -> None:
        hwnd = event.hwnd
        if win32gui.GetForegroundWindow() != hwnd:
            _overlay.hide()
            return
        # 首次进入非战斗状态时输出提示
        if self._scan_count == 0:
            print(f"[{_ts()}] 已进入非战斗状态，开始精灵检测扫描...")

        # 优先：如果丢球 UI 可见，立即丢球（像模式 1）
        elf_score = next((s for n, s in event.end_scores if "elf_p.png" in n.lower()), 0.0)
        exchange_score = next((s for n, s in event.all_scores if "exchange.png" in n.lower()), 0.0)
        if max(elf_score, exchange_score) >= CONFIG.match_threshold:
            if click_at(hwnd):
                print(f"[{_ts()}] 丢球点击（elf_P={elf_score:.3f} exchange={exchange_score:.3f}）")
            else:
                print(f"[{_ts()}] [警告] 丢球点击失败")
            # 丢球后再检测精灵位置，用于下次瞄准
            self._detect_and_throw(hwnd)
        else:
            # 丢球 UI 不可见，只检测精灵位置并转动视角
            self._detect_and_throw(hwnd)

    def _screen_rect(self, hwnd, fw, fh):
        """Return (left, top, width, height) of game client area on screen."""
        from core.window import get_client_rect_on_screen
        return get_client_rect_on_screen(hwnd)

    def _screen_size(self, hwnd: int) -> tuple:
        rect = self._screen_rect(hwnd, 0, 0)
        return rect[2], rect[3]

    # ── Sensitivity calibration ────────────────────────────────────────────

    def _load_sensitivity_ratio(self) -> None:
        """优先从 user_prefs.json 加载已标定的横向/纵向灵敏度。"""
        from config import load_prefs
        prefs = load_prefs()
        ratio_h = prefs.get("pet_aim_pixels_per_unit_h", 0.0)
        ratio_v = prefs.get("pet_aim_pixels_per_unit_v", 0.0)
        if ratio_h > 0 and ratio_v > 0:
            self._sensitivity_ratio_h = ratio_h
            self._sensitivity_ratio_v = ratio_v
            print(f"[{_ts()}] 已加载背景标定灵敏度: ratio_h={ratio_h:.4f}  ratio_v={ratio_v:.4f}")
            return
        # 兼容旧版单一 ratio
        old_ratio = prefs.get("pet_aim_pixels_per_unit", 0.0)
        if old_ratio > 0:
            self._sensitivity_ratio_h = old_ratio
            self._sensitivity_ratio_v = old_ratio
            print(f"[{_ts()}] 已加载旧版标定灵敏度: ratio={old_ratio:.4f} px/unit")
            return
        # config 默认值
        if CONFIG.pet_aim_pixels_per_unit > 0:
            self._sensitivity_ratio_h = CONFIG.pet_aim_pixels_per_unit
            self._sensitivity_ratio_v = CONFIG.pet_aim_pixels_per_unit
            print(f"[{_ts()}] 使用 config 默认灵敏度: ratio={CONFIG.pet_aim_pixels_per_unit:.4f}")
            return
        # 未标定，保留 1.0 默认值，由 _calibrate_sensitivity 自动标定

    def _calibrate_sensitivity(self, hwnd: int, detector: PetDetector) -> float:
        """向右移动已知鼠标单位，测量精灵在屏幕上的像素位移，算出 px/unit 比率。

        注意：精灵可能自行移动导致测不准。推荐使用背景固定物体标定：
            运行 scripts/mark_align.py --capture 进行现场标定，
            结果自动保存到 user_prefs.json，ball_pet 会优先使用。"""
        _ensure_interception()
        step = CONFIG.pet_aim_calib_step

        frame = capture_window_bgr(hwnd)
        if frame is None or frame.size == 0:
            return 0.0
        fh, fw = frame.shape[:2]

        target = self._select_target(detector.detect(frame), fw, fh)
        if target is None:
            return 0.0
        cx1, cy1 = target[0], target[1]

        # 精灵太靠边时标定不可靠（移动后可能出屏）
        margin = fw * 0.2
        if cx1 < margin or cx1 > fw - margin:
            print(f"[{_ts()}] 标定跳过：精灵太靠近屏幕边缘 (cx={cx1})")
            return 0.0

        print(f"[{_ts()}] 灵敏度标定（精灵法，可能不准）：向右移动 {step} 鼠标单位...")
        interception.move_relative(step, 0)
        time.sleep(CONFIG.pet_aim_camera_delay * 2)

        frame2 = capture_window_bgr(hwnd)
        if frame2 is None or frame2.size == 0:
            return 0.0
        fh2, fw2 = frame2.shape[:2]
        target2 = self._select_target(detector.detect(frame2), fw2, fh2)
        if target2 is None:
            print(f"[{_ts()}] 标定失败：移动后未检测到精灵")
            return 0.0
        cx2, cy2 = target2[0], target2[1]

        pixel_shift = abs(cx1 - cx2)
        if pixel_shift < 10:
            print(f"[{_ts()}] 标定失败：像素位移过小 ({pixel_shift}px)，精灵可能在移动")
            return 0.0

        ratio = pixel_shift / step
        print(f"[{_ts()}] 标定完成: {step}u → {pixel_shift}px, 比率={ratio:.3f} px/unit")
        return ratio

    # ── Calibration log ───────────────────────────────────────────────────

    _calib_header_written: bool = False

    def _log_calib(self, screen_w: int, screen_h: int, pet_cx: int, pet_cy: int,
                   pet_w: int, pet_h: int, conf: float, attempt: int,
                   dx: int, dy: int, parabola: float, aim_mode: str) -> None:
        """Append one row to the calibration CSV."""
        try:
            os.makedirs(os.path.dirname(_CALIB_LOG_PATH), exist_ok=True)
            write_header = not (AutoBallPetMode._calib_header_written or os.path.exists(_CALIB_LOG_PATH))
            AutoBallPetMode._calib_header_written = True
            with open(_CALIB_LOG_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow([
                        "timestamp", "screen_w", "screen_h", "center_x", "center_y",
                        "pet_cx", "pet_cy", "pet_w", "pet_h", "conf",
                        "h_error", "v_error", "dx", "dy", "parabola",
                        "distance_ratio", "attempt", "aim_mode",
                    ])
                center_x = screen_w // 2
                center_y = screen_h // 2
                w.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    screen_w, screen_h, center_x, center_y,
                    pet_cx, pet_cy, pet_w, pet_h, f"{conf:.3f}",
                    pet_cx - center_x, pet_cy - center_y,
                    dx, dy, f"{parabola:.1f}",
                    f"{(screen_h - pet_cy) / screen_h:.3f}",
                    attempt, aim_mode,
                ])
        except Exception:
            pass

    # ── Fixed-step aiming ──────────────────────────────────────────────

    def _aim_iterative(self, hwnd: int, pet_cx: int, pet_cy: int,
                       pet_w: int, pet_h: int, conf: float, detector: PetDetector) -> None:
        if self._aiming:
            return
        self._aiming = True
        try:
            self._aim_fixed(hwnd, pet_cx, pet_cy, pet_w, pet_h, conf, detector)
        finally:
            self._aiming = False

    def _aim_fixed(self, hwnd: int, pet_cx: int, pet_cy: int,
                    pet_w: int, pet_h: int, conf: float, detector: PetDetector) -> None:
        _ensure_interception()
        screen_w, screen_h = self._screen_size(hwnd)
        center_x = screen_w // 2 + CONFIG.pet_aim_offset_x
        center_y = screen_h // 2 + CONFIG.pet_aim_offset_y
        settle = CONFIG.pet_aim_camera_delay
        step = CONFIG.pet_aim_step_px
        thresh = CONFIG.pet_aim_center_thresh
        total_dx, total_dy = 0, 0
        orig_pet_cx, orig_pet_cy = pet_cx, pet_cy
        ratio_h = self._sensitivity_ratio_h
        ratio_v = self._sensitivity_ratio_v

        print(
            f"[{_ts()}] 精灵定位: pos=({pet_cx},{pet_cy}) size=({pet_w}x{pet_h}) conf={conf:.2f}"
        )

        interception.mouse_down('right')
        time.sleep(0.05)

        try:
            iterations = 0
            for i in range(CONFIG.pet_aim_max_iters):
                h_error = pet_cx - center_x
                v_error = pet_cy - center_y

                if abs(h_error) < thresh and abs(v_error) < thresh:
                    break

                # 自适应步长：远距离大步快速接近，近距离小步精细调整
                if i == 0 and (ratio_h != 1.0 or ratio_v != 1.0):
                    # 首轮用标定比率做一步到位移动
                    dx = int(h_error / ratio_h)
                    dy = int(v_error / ratio_v)
                    max_first = step * 5
                    dx = max(-max_first, min(max_first, dx))
                    dy = max(-max_first, min(max_first, dy))
                else:
                    # 步长 = min(固定步长, 误差的一半)，避免过冲
                    h_step = min(step, abs(h_error) // 2) if abs(h_error) > thresh else 0
                    v_step = min(step, abs(v_error) // 2) if abs(v_error) > thresh else 0
                    dx = h_step if h_error > thresh else (-h_step if h_error < -thresh else 0)
                    dy = v_step if v_error > thresh else (-v_step if v_error < -thresh else 0)

                interception.move_relative(dx, dy)
                total_dx += dx
                total_dy += dy
                time.sleep(settle)
                iterations = i + 1

                new_frame = capture_window_bgr(hwnd)
                if new_frame is None or new_frame.size == 0:
                    break
                nf_h, nf_w = new_frame.shape[:2]
                target = self._select_target(detector.detect(new_frame), nf_w, nf_h)
                if target is None:
                    break
                pet_cx, pet_cy = target[0], target[1]

            final_h_err = pet_cx - center_x
            final_v_err = pet_cy - center_y
            print(f"  最终误差: h_err={int(final_h_err):+d}px v_err={int(final_v_err):+d}px (steps={iterations})")
        finally:
            interception.mouse_up('right')
            time.sleep(0.10)

        self._log_calib(screen_w, screen_h, orig_pet_cx, orig_pet_cy,
                        pet_w, pet_h, conf, iterations,
                        total_dx, total_dy, 0, "fixed_calib" if (ratio_h != 1.0 or ratio_v != 1.0) else "fixed")

    # ── One-shot aiming (non-PID fallback) ─────────────────────────────

    # ── 视角旋转 ───────────────────────────────────────────────────────

    def _rotate_camera_90(self, hwnd: int) -> None:
        """按住右键顺时针旋转视角约 90 度，用于搜索视野外的精灵。"""
        if self._aiming:
            return
        screen_w, _ = self._screen_size(hwnd)
        # 90 度 ≈ 半屏像素 / 灵敏度，幅度浮动 ±15%
        base_units = screen_w * 0.6 / self._sensitivity_ratio_h
        rotate_units = int(base_units * random.uniform(0.85, 1.15))

        _ensure_interception()
        interception.mouse_down('right')
        time.sleep(0.05)
        interception.move_relative(rotate_units, 0)
        time.sleep(0.3)
        interception.mouse_up('right')
        time.sleep(0.10)
        print(f"[{_ts()}] 视角旋转 90° (→{rotate_units}u)")

    def _recovery_pull(self, hwnd: int) -> None:
        """回拉机制：超时未找到精灵，向左下角回拉视角。"""
        if self._aiming:
            return
        dx = CONFIG.pet_recovery_dx  # 负=左
        dy = CONFIG.pet_recovery_dy  # 正=下

        _ensure_interception()
        interception.mouse_down('right')
        time.sleep(0.05)
        interception.move_relative(dx, dy)
        time.sleep(0.3)
        interception.mouse_up('right')
        time.sleep(0.10)
        print(f"[{_ts()}] 回拉视角: ({dx:+d}, {dy:+d})u")

    def _aim_calibrated(self, hwnd: int, pet_cx: int, pet_cy: int,
                         pet_w: int, pet_h: int, conf: float,
                         detector) -> None:
        """MLP 式估算：mouse = w1*px + w2*px*|px| + w3*px³。
        默认系数退化为线性（w1=1/ratio, w2=w3=0）。
        标定后可通过 scripts/mark_align.py --calib-nn 拟合多项式系数。"""
        from config import load_prefs
        prefs = load_prefs()
        nn_w = prefs.get("nn_weights_h", [1.0 / self._sensitivity_ratio_h, 0.0, 0.0])
        nn_w_v = prefs.get("nn_weights_v", [1.0 / self._sensitivity_ratio_v, 0.0, 0.0])

        screen_w, screen_h = self._screen_size(hwnd)
        cx = screen_w // 2 + CONFIG.pet_aim_offset_x
        cy = screen_h // 2 + CONFIG.pet_aim_offset_y
        thresh = CONFIG.pet_aim_center_thresh
        settle = 0.06

        total_dx, total_dy = 0, 0

        _ensure_interception()
        interception.mouse_down('right')
        time.sleep(0.05)

        try:
            for attempt in range(3):
                h_px = float(pet_cx - cx)
                v_px = float(pet_cy - cy)

                if abs(h_px) < thresh and abs(v_px) < thresh:
                    break

                # mouse = w1*px + w2*px*|px| + w3*px^3
                dx = int(nn_w[0] * h_px + nn_w[1] * h_px * abs(h_px) + nn_w[2] * h_px ** 3)
                dy = int(nn_w_v[0] * v_px + nn_w_v[1] * v_px * abs(v_px) + nn_w_v[2] * v_px ** 3)
                dx = max(-400, min(400, dx))
                dy = max(-400, min(400, dy))

                if abs(dx) < 2 and abs(dy) < 2:
                    break

                interception.move_relative(dx, dy)
                total_dx += dx
                total_dy += dy
                time.sleep(settle * random.uniform(0.85, 1.15))

                frame = capture_window_bgr(hwnd)
                if frame is None or frame.size == 0:
                    break
                cf_h, cf_w = frame.shape[:2]
                target = self._select_target(detector.detect(frame), cf_w, cf_h)
                if target is None:
                    break
                pet_cx, pet_cy = target[0], target[1]

            print(f"  MLP: h_err={int(pet_cx - cx):+d}px v_err={int(pet_cy - cy):+d}px "
                  f"(w_h=[{nn_w[0]:.4f},{nn_w[1]:.6f},{nn_w[2]:.8f}])")
        finally:
            interception.mouse_up('right')
            time.sleep(0.10)

        self._log_calib(screen_w, screen_h, pet_cx, pet_cy,
                        pet_w, pet_h, conf, attempt + 1,
                        total_dx, total_dy, 0, "mlp")

    def _aim_one_shot(self, hwnd: int, pet_cx: int, pet_cy: int,
                      pet_w: int, pet_h: int, conf: float) -> None:
        screen_w, screen_h = self._screen_size(hwnd)
        center_x = screen_w // 2 + CONFIG.pet_aim_offset_x
        center_y = screen_h // 2 + CONFIG.pet_aim_offset_y
        pixel_dx = pet_cx - center_x
        pixel_dy = pet_cy - center_y

        ratio_h = self._sensitivity_ratio_h
        ratio_v = self._sensitivity_ratio_v
        dx = int(pixel_dx / ratio_h)
        dy = int(pixel_dy / ratio_v)

        print(
            f"[{_ts()}] 精灵定位: pos=({pet_cx},{pet_cy}) size=({pet_w}x{pet_h}) "
            f"conf={conf:.2f} → 像素误差=({pixel_dx},{pixel_dy}) "
            f"鼠标位移=({dx},{dy}) [ratio_h={ratio_h:.3f} ratio_v={ratio_v:.3f}]"
        )

        _ensure_interception()
        interception.mouse_down('right')
        time.sleep(0.05)
        interception.move_relative(dx, dy)
        time.sleep(CONFIG.pet_aim_camera_delay)
        interception.mouse_up('right')
        time.sleep(0.10)

        self._log_calib(screen_w, screen_h, pet_cx, pet_cy,
                        pet_w, pet_h, conf, 1, dx, dy, 0, "one_shot")


    # ── 战斗内行动（同智能模式）────────────────────────────────────────

    def _action_label(self, action: str) -> str:
        labels = {"gather": "聚能", "escape": "逃跑", "skill1_gather": "技能1+聚能"}
        return labels.get(action, action)

    def on_battle_start(self, event: BattleEvent) -> None:
        _overlay.hide()
        # 进战斗后旧目标作废，清目标锁
        self._ref_center = None
        self._lock_lost = 0
        is_pollute = event.pollute_capture_score > event.capture_score
        self._current_action = (
            self._pollute_action if is_pollute else self._normal_action
        )
        self._skill1_used = False

        mode_label = "污染" if is_pollute else "普通"
        print(
            f"[{_ts()}] 战斗判型: 本场={mode_label} → {self._action_label(self._current_action)}"
            f"（capture={event.capture_score:.3f}, pollute_capture={event.pollute_capture_score:.3f}）"
        )

    def on_action(self, event: BattleEvent, is_hit: bool, action_score: float) -> Optional[float]:
        if self._current_action is None:
            return None
        if not is_hit:
            return None
        if self._current_action == "none":
            return None

        if self._current_action == "gather":
            press_once(event.hwnd, CONFIG.press_key)
            print(f"[{_ts()}] 战斗动作: 已触发按键 {CONFIG.press_key}（本场=聚能）")
            return None
        elif self._current_action == "escape":
            return do_escape(event.hwnd, event.templates, event.scale, event.window_width, event.window_height)
        elif self._current_action == "skill1_gather":
            if not self._skill1_used:
                press_once(event.hwnd, "1")
                self._skill1_used = True
                print(f"[{_ts()}] 战斗动作: 已释放技能1（本场=技能1+聚能）")
                return 1.0
            else:
                press_once(event.hwnd, CONFIG.press_key)
                print(f"[{_ts()}] 战斗动作: 已触发按键 {CONFIG.press_key}（本场=技能1+聚能）")
                return None
        return None

    def on_battle_end(self, event: BattleEvent) -> None:
        self._current_action = None
        self._skill1_used = False

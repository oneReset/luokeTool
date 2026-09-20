"""一键生成训练新精灵模型所需的配置文件和脚本。

用法:
    uv run python scripts/setup_pet.py --pet <精灵名>
    uv run python scripts/setup_pet.py --pet <精灵名> --base-model xueren   # 基于已有模型微调

生成内容:
  1. datasets/<pet>/images/           — 存放截图
  2. datasets/<pet>/labels/           — 存放标注（label_pets.py 自动生成）
  3. datasets/<pet>/data.yaml         — YOLO 数据集配置
  4. scripts/train_<pet>.py           — 训练脚本
  5. scripts/detect_<pet>.py          — 实时检测测试脚本
"""

import argparse
import os

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _generate_data_yaml(pet_name: str, dataset_dir: str) -> str:
    return (
        f"# YOLO 数据集配置 — {pet_name}\n"
        f"# 由 setup_pet.py 自动生成\n"
        f"path: {dataset_dir}\n"
        f"train: images\n"
        f"val: images\n"
        f"\n"
        f"nc: 1\n"
        f"names:\n"
        f"  0: {pet_name}\n"
    )


def _generate_train_script(pet_name: str, base_model: str) -> str:
    if base_model:
        model_line = f'MODEL_PATH = os.path.join(_BASE, "models", "{base_model}.pt")'
        model_comment = f'print(f"基础模型: {{MODEL_PATH}} (从 {base_model}.pt 继续训练)")'
    else:
        model_line = 'MODEL_PATH = os.path.join(_BASE, "yolo26s.pt")'
        model_comment = 'print(f"预训练模型: {MODEL_PATH}")'

    return f'''"""Train YOLO model on {pet_name} dataset."""
import os
import shutil

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ultralytics import YOLO

DATA_YAML = os.path.join(_BASE, "datasets", "{pet_name}", "data.yaml")
{model_line}

if not os.path.exists(MODEL_PATH):
    MODEL_PATH = "yolo26s.pt"

print(f"数据: {{DATA_YAML}}")
{model_comment}
print(f"开始训练 {pet_name} 模型...")

model = YOLO(MODEL_PATH)
model.train(
    data=DATA_YAML,
    epochs=100,
    batch=8,
    imgsz=640,
    device=0,
    workers=0,
    plots=True,
    verbose=True,
    val=False,
    mosaic=1.0,
    flipud=0.5,
    fliplr=0.5,
    hsv_h=0.015,
    hsv_s=0.7,
    hsv_v=0.4,
    scale=0.5,
    translate=0.1,
    erasing=0.4,
)

# Copy trained model to models/
os.makedirs(os.path.join(_BASE, "models"), exist_ok=True)
src = os.path.join(_BASE, "runs", "detect", "train", "weights", "best.pt")
dst = os.path.join(_BASE, "models", "{pet_name}.pt")
if os.path.exists(src):
    shutil.copy2(src, dst)
    print(f"\\n训练完成！模型已保存: {{dst}}")
else:
    print("\\n训练完成！请手动将 best.pt 复制到 models/{pet_name}.pt")
'''


def _generate_detect_script(pet_name: str) -> str:
    return f'''"""Real-time {pet_name} detection with overlay on game window."""
import atexit
import ctypes
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import win32con
import win32gui

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODEL_PATH = os.path.join(_BASE, "models", "{pet_name}.pt")

# ── Transparent GDI overlay ────────────────────────────────────────────────

_overlay_class_registered: bool = False


def _register_overlay_class():
    global _overlay_class_registered
    if _overlay_class_registered:
        return

    def _wnd_proc(hwnd, msg, wparam, lparam):
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    _register_overlay_class._wnd_proc = _wnd_proc

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.lpszClassName = "{pet_name}DetOverlay"
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
        if self._hwnd is not None and (w, h) != (self._width, self._height):
            self.destroy()
        if self._hwnd is None:
            _register_overlay_class()
            ex_style = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
                        win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW |
                        win32con.WS_EX_NOACTIVATE)
            self._hwnd = win32gui.CreateWindowEx(
                ex_style, "{pet_name}DetOverlay", "",
                win32con.WS_POPUP, x, y, w, h,
                0, 0, win32gui.GetModuleHandle(None), None,
            )
            win32gui.SetLayeredWindowAttributes(self._hwnd, 0xFF00FF, 0, win32con.LWA_COLORKEY)
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            self._width, self._height = w, h
        else:
            win32gui.SetWindowPos(
                self._hwnd, win32con.HWND_TOPMOST, x, y, w, h,
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )

    def update(self, detections, frame_w, frame_h):
        if not self._hwnd:
            return
        hdc = ctypes.windll.user32.GetDC(self._hwnd)
        mem_dc = ctypes.windll.gdi32.CreateCompatibleDC(hdc)
        bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc, self._width, self._height)
        old_bmp = ctypes.windll.gdi32.SelectObject(mem_dc, bmp)

        brush = ctypes.windll.gdi32.CreateSolidBrush(0x00FF00FF)
        rect = ctypes.wintypes.RECT(0, 0, self._width, self._height)
        ctypes.windll.user32.FillRect(mem_dc, ctypes.byref(rect), brush)
        ctypes.windll.gdi32.DeleteObject(brush)

        scale_x = self._width / frame_w
        scale_y = self._height / frame_h

        # Crosshair at center
        cx_c = self._width // 2
        cy_c = self._height // 2
        cross_pen = ctypes.windll.gdi32.CreatePen(0, 1, 0x000000FF)  # Red cross
        ctypes.windll.gdi32.SelectObject(mem_dc, cross_pen)
        ctypes.windll.gdi32.MoveToEx(mem_dc, cx_c - 15, cy_c, None)
        ctypes.windll.gdi32.LineTo(mem_dc, cx_c + 15, cy_c)
        ctypes.windll.gdi32.MoveToEx(mem_dc, cx_c, cy_c - 15, None)
        ctypes.windll.gdi32.LineTo(mem_dc, cx_c, cy_c + 15)
        ctypes.windll.gdi32.DeleteObject(cross_pen)

        # Detection boxes
        for i, d in enumerate(detections):
            cx, cy, w, h, conf = d
            x1 = int((cx - w // 2) * scale_x)
            y1 = int((cy - h // 2) * scale_y)
            x2 = int((cx + w // 2) * scale_x)
            y2 = int((cy + h // 2) * scale_y)
            if x2 <= x1 or y2 <= y1:
                continue
            color = 0x0000FF00 if i == 0 else 0x0000FFFF  # Green for best, cyan for rest
            pen = ctypes.windll.gdi32.CreatePen(0, 2, color)
            ctypes.windll.gdi32.SelectObject(mem_dc, pen)
            null_brush = ctypes.windll.gdi32.GetStockObject(5)
            ctypes.windll.gdi32.SelectObject(mem_dc, null_brush)
            ctypes.windll.gdi32.Rectangle(mem_dc, x1, y1, x2, y2)
            ctypes.windll.gdi32.DeleteObject(pen)

        ctypes.windll.gdi32.BitBlt(hdc, 0, 0, self._width, self._height,
                                   mem_dc, 0, 0, 0x00CC0020)
        ctypes.windll.gdi32.SelectObject(mem_dc, old_bmp)
        ctypes.windll.gdi32.DeleteObject(bmp)
        ctypes.windll.gdi32.DeleteDC(mem_dc)
        ctypes.windll.user32.ReleaseDC(self._hwnd, hdc)

    def hide(self):
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, 0)

    def destroy(self):
        if self._hwnd:
            ctypes.windll.user32.DestroyWindow(self._hwnd)
            self._hwnd = None


_overlay = OverlayWindow()
atexit.register(_overlay.destroy)


def main():
    sys.path.insert(0, _BASE)
    from core.window import find_window_by_keyword, get_client_rect_on_screen
    from core.capture import capture_window_bgr
    from ultralytics import YOLO

    if not os.path.exists(_MODEL_PATH):
        print(f"[错误] 模型未找到: {{_MODEL_PATH}}")
        sys.exit(1)

    hwnd = find_window_by_keyword("洛克王国：世界")
    if hwnd is None:
        print("[错误] 未找到游戏窗口")
        sys.exit(1)

    print(f"[{pet_name}检测] 加载模型: {{_MODEL_PATH}}")
    model = YOLO(_MODEL_PATH)
    print(f"[{pet_name}检测] 开始实时检测 (0.2s/帧)，按 Q 退出...")

    try:
        while True:
            if win32gui.GetForegroundWindow() != hwnd:
                _overlay.hide()
                time.sleep(0.2)
                continue

            t0 = time.perf_counter()
            frame = capture_window_bgr(hwnd)
            if frame is None or frame.size == 0:
                time.sleep(0.2)
                continue

            results = model(frame, verbose=False, conf=0.1)
            detections = []
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    conf = float(box.conf[0])
                    cx = int((x1 + x2) / 2)
                    cy = int((y1 + y2) / 2)
                    w = int(x2 - x1)
                    h = int(y2 - y1)
                    if w > 0 and h > 0:
                        detections.append((cx, cy, w, h, conf))

            detections.sort(key=lambda d: d[4], reverse=True)

            if detections:
                fh, fw = frame.shape[:2]
                rect = get_client_rect_on_screen(hwnd)
                _overlay.ensure(hwnd, *rect)
                _overlay.update(detections, fw, fh)
                best = detections[0]
                elapsed = (time.perf_counter() - t0) * 1000
                ts = datetime.now().strftime("%H:%M:%S")
                status_line = (
                    f"\\r[{{ts}}] {pet_name}: {{len(detections)}}个 | "
                    f"最佳 conf={{best[4]:.2f}} pos=({{best[0]}},{{best[1]}}) | "
                    f"{{elapsed:.0f}}ms   "
                )
            else:
                status_line = f"\\r[{{datetime.now().strftime(\'%H:%M:%S\')}}] 未检测到 {pet_name}    "
                _overlay.hide()

            print(status_line, end="", flush=True)

            elapsed = time.perf_counter() - t0
            sleep_t = max(0, 0.2 - elapsed)
            if sleep_t > 0:
                time.sleep(sleep_t)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        pass
    finally:
        _overlay.destroy()
        cv2.destroyAllWindows()
        print(f"\\n[{pet_name}检测] 已停止")


if __name__ == "__main__":
    main()
'''


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一键生成训练新精灵模型所需的配置文件和脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --pet maoyou                    # 从零训练
  %(prog)s --pet maoyou --base-model xueren  # 从已有模型微调
""",
    )
    parser.add_argument("--pet", "-p", required=True, help="精灵名称 (如 maoyou)")
    parser.add_argument(
        "--base-model", "-b", default=None,
        help="基于已有模型微调 (如 xueren, huolong)；不指定则从 yolo26s.pt 从零训练",
    )
    args = parser.parse_args()

    pet_name = args.pet.strip()
    base_model = args.base_model.strip() if args.base_model else None

    if not pet_name:
        print("[错误] 精灵名称不能为空")
        return

    # ── Validate base model ────────────────────────────────────────────────
    if base_model:
        base_path = os.path.join(_BASE, "models", f"{base_model}.pt")
        if not os.path.exists(base_path):
            print(f"[警告] 基础模型不存在: {base_path}")
            print(f"  将回退到 yolo26s.pt 从零训练")
            base_model = None

    # ── 1. Create dataset directories ──────────────────────────────────────
    dataset_dir = os.path.join(_BASE, "datasets", pet_name)
    images_dir = os.path.join(dataset_dir, "images")
    labels_dir = os.path.join(dataset_dir, "labels")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)
    print(f"[1/4] 数据集目录: {dataset_dir}")

    # Check for existing screenshots in the root dataset dir
    exts = {".png", ".jpg", ".jpeg", ".bmp"}
    raw_images = [
        f for f in os.listdir(dataset_dir)
        if os.path.isfile(os.path.join(dataset_dir, f))
        and os.path.splitext(f)[1].lower() in exts
    ]
    if raw_images:
        print(f"  发现 {len(raw_images)} 张截图，可以直接开始标注")
    else:
        print(f"  请将截图放入: {dataset_dir}")

    # ── 2. Create data.yaml ────────────────────────────────────────────────
    data_yaml_path = os.path.join(dataset_dir, "data.yaml")
    if os.path.exists(data_yaml_path):
        print(f"[2/4] data.yaml 已存在，跳过: {data_yaml_path}")
    else:
        content = _generate_data_yaml(pet_name, os.path.abspath(dataset_dir))
        with open(data_yaml_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[2/4] 已创建: {data_yaml_path}")

    # ── 3. Create train script ─────────────────────────────────────────────
    train_path = os.path.join(_BASE, "scripts", f"train_{pet_name}.py")
    if os.path.exists(train_path):
        print(f"[3/4] 训练脚本已存在，跳过: {train_path}")
    else:
        content = _generate_train_script(pet_name, base_model or "")
        with open(train_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[3/4] 已创建: {train_path}")

    # ── 4. Create detect script ────────────────────────────────────────────
    detect_path = os.path.join(_BASE, "scripts", f"detect_{pet_name}.py")
    if os.path.exists(detect_path):
        print(f"[4/4] 检测脚本已存在，跳过: {detect_path}")
    else:
        content = _generate_detect_script(pet_name)
        with open(detect_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[4/4] 已创建: {detect_path}")

    # ── Next steps ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 50}")
    print(f"设置完成！接下来的步骤：")
    print(f"")
    print(f"  1. 把截图放入: datasets/{pet_name}/")
    print(f"  2. 标注:")
    print(f"     uv run python scripts/label_pets.py --pet {pet_name}")
    print(f"  3. 训练:")
    print(f"     uv run python scripts/train_{pet_name}.py")
    print(f"  4. 测试:")
    print(f"     uv run python scripts/detect_{pet_name}.py")
    print(f"")
    print(f"训练完成后 models/{pet_name}.pt 会自动出现在 main.py 的模型列表中。")


if __name__ == "__main__":
    main()

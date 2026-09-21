"""战斗通用动作：从各模式中提取的可复用战斗操作。"""

import time as _time

from config import CONFIG
from core.capture import capture_window_bgr
from core.input import click_at, press_once
from core.util import _ts
from core.vision import best_yes_score_and_loc


def do_escape(hwnd: int, templates, scale: float, window_width: int, window_height: int) -> float:
    """执行逃跑：按 ESC → 轮询寻找确认按钮 yes.png → 点击确认。

    返回额外冷却秒数（直接作为 Engine.on_action 的返回值使用）。
    """
    press_once(hwnd, "esc")
    print(f"[{_ts()}] 战斗动作: 已触发 ESC（逃跑）")

    yes_threshold = CONFIG.match_threshold * 0.8
    for _ in range(10):
        _time.sleep(0.3)
        full_shot = capture_window_bgr(hwnd)
        best_score, best_loc = best_yes_score_and_loc(full_shot, templates, scale)

        if best_score >= yes_threshold:
            cap_h, cap_w = full_shot.shape[:2]
            click_x, click_y = best_loc
            if cap_w > 0 and cap_h > 0 and (cap_w != window_width or cap_h != window_height):
                click_x = int(round(best_loc[0] * window_width / cap_w))
                click_y = int(round(best_loc[1] * window_height / cap_h))
                click_x = max(0, min(window_width - 1, click_x))
                click_y = max(0, min(window_height - 1, click_y))

            if click_at(hwnd, click_x, click_y):
                print(f"[{_ts()}] 逃跑确认点击成功")
                _time.sleep(0.5)
                click_at(hwnd, click_x, click_y)
                break
    else:
        print(f"[{_ts()}] [警告] 触发 ESC 后未找到确认按钮 yes.png")

    return 2.0

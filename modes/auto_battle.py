"""模式4：自动战斗 — 污染战斗打虚脱后自动捕捉，失败重试直到成功。

状态流转（子状态机 _phase）：
    attack  按攻击键打虚脱（污染战斗进入）
      │ 转移：capture 分数 ≥ 阈值 且 > pollute 分数，连续 DEBOUNCE_ROUNDS 轮
      ▼
    catch   点击捕捉按钮 → 等球槽界面 → 按球槽键 → 按确认键
      │ 返回额外冷却覆盖捕捉动画（失败后界面复原，capture 仍可见 → 自动重试）
      ▼
    战斗结束模板出现 → on_battle_end 复位（捕捉成功）

普通战斗按 NORMAL_BATTLE_ACTION 配置分流：直接捕捉或逃跑。
非战斗行为预留 _roaming 注入口（后续接入"寻找目标→丢球"前置流程）。
"""

import os
import time as _time
from typing import Callable, Optional

import battle_config as BC
from config import CONFIG
from core.battle_actions import do_escape
from core.battle_classify import classify_battle
from core.capture import capture_window_bgr
from core.input import click_at, press_once
from core.util import _ts
from core.vision import normalize_template_name, single_template_score_and_loc
from modes.base import BaseMode, BattleEvent


def describe_config() -> str:
    """返回当前生效配置的一行摘要（启动时打印）。"""
    normal_label = "逃跑" if BC.NORMAL_BATTLE_ACTION == "escape" else "直接捕捉"
    return (
        f"攻击键={BC.ATTACK_KEY}  球槽={BC.BALL_SLOT_KEY}  普通战斗={normal_label}  "
        f"点击延迟={BC.CATCH_CLICK_DELAY}s  确认延迟={BC.CATCH_CONFIRM_DELAY}s  "
        f"动画冷却={BC.CATCH_COOLDOWN}s  捕捉阈值={BC.CATCH_DETECT_THRESHOLD}  "
        f"防抖={BC.DEBOUNCE_ROUNDS}轮"
    )


def _battle_roi(full_bgr, width: int, height: int):
    """计算引擎行动检测 ROI（右下 1/4），与 core/engine.py 的裁剪口径一致。

    返回 (left, top, roi_image)，roi 内坐标加上 left/top 即为客户区坐标。
    """
    left = max(0, min(width - 1, int(width * CONFIG.roi_left_ratio)))
    top = max(0, min(height - 1, int(height * CONFIG.roi_top_ratio)))
    w = max(1, min(width - left, int(width * CONFIG.roi_width_ratio)))
    h = max(1, min(height - top, int(height * CONFIG.roi_height_ratio)))
    return left, top, full_bgr[top:top + h, left:left + w]


def _panel_roi(full_bgr, width: int, height: int):
    """球槽面板搜索 ROI（屏幕左下 1/4，与捕捉按钮区域不重叠）。"""
    left_r, top_r, w_r, h_r = BC.PANEL_ROI
    left = max(0, min(width - 1, int(width * left_r)))
    top = max(0, min(height - 1, int(height * top_r)))
    w = max(1, min(width - left, int(width * w_r)))
    h = max(1, min(height - top, int(height * h_r)))
    return full_bgr[top:top + h, left:left + w]


def _panel_template_scale(event: BattleEvent) -> float:
    """面板模板匹配缩放：原生截图用 1.0，2560 基准截图沿用引擎 scale。"""
    if BC.PANEL_TEMPLATE_NATIVE:
        return 1.0
    return event.scale


class AutoBattleMode(BaseMode):
    """自动战斗：打虚脱 → 捕捉 → 失败重试 → 成功结束。纯战斗模式。"""

    label = "自动战斗"

    def __init__(self) -> None:
        self._phase: str = "idle"        # idle / attack / catch / escape
        self._debounce_count: int = 0    # 虚脱判定连续命中轮数
        self._catch_attempts: int = 0    # 本场捕捉尝试次数
        # 球槽面板模板闭环：缺图时全局禁用；连续失败时本场降级（下场恢复）
        self._panel_loop_enabled: bool = os.path.exists(
            os.path.join(CONFIG.template_dir, BC.PANEL_TEMPLATE_NAME)
        )
        self._panel_miss_count: int = 0
        self._locate_fail_count: int = 0  # 捕捉按钮连续定位失败次数（卡死检测）
        # 预留：非战斗行为注入口（后续"寻找目标→丢球"前置流程）
        self._roaming: Optional[Callable[[BattleEvent], None]] = None
        self._warn_missing_exhausted_template()
        if not self._panel_loop_enabled:
            print(
                f"[{_ts()}] [提示] 未找到球槽面板模板 {BC.PANEL_TEMPLATE_NAME}，"
                f"选球将使用固定延时（开环）；模板放入 templates/ 后重启启用闭环"
            )

    def _warn_missing_exhausted_template(self) -> None:
        """虚脱专用模板未放置时给出提示（自动降级为仅用 capture.png，不影响运行）。"""
        path = os.path.join(CONFIG.template_dir, BC.EXHAUSTED_TEMPLATE_NAME)
        if not os.path.exists(path):
            print(
                f"[{_ts()}] [提示] 未找到虚脱专用模板 {BC.EXHAUSTED_TEMPLATE_NAME}，"
                f"虚脱判定仅使用 capture.png；模板制作后放入 templates/ 即自动生效"
            )

    @property
    def name(self) -> str:
        return "auto_battle"

    def set_roaming(self, roaming: Callable[[BattleEvent], None]) -> None:
        """注入非战斗行为（预留接口，当前未使用）。"""
        self._roaming = roaming

    # ── 生命周期回调 ──────────────────────────────────────────────────

    def on_battle_start(self, event: BattleEvent) -> None:
        self._debounce_count = 0
        self._catch_attempts = 0
        self._panel_miss_count = 0
        self._locate_fail_count = 0
        # 面板闭环：缺图保持禁用；上场连续失败降级的，本场自动恢复
        self._panel_loop_enabled = os.path.exists(
            os.path.join(CONFIG.template_dir, BC.PANEL_TEMPLATE_NAME)
        )

        is_pollute = event.pollute_capture_score > event.capture_score
        if is_pollute:
            self._phase = "attack"
            action_desc = f"按 {BC.ATTACK_KEY} 攻击至虚脱后捕捉"
        elif BC.NORMAL_BATTLE_ACTION == "escape":
            self._phase = "escape"
            action_desc = "逃跑"
        else:
            self._phase = "catch"
            action_desc = "直接捕捉"

        init_state = "污染战斗" if is_pollute else "普通战斗"
        print(f"[{_ts()}] 当前战斗初始状态为：{init_state}")
        print(
            f"[{_ts()}] 战斗判型: 本场={init_state} → {action_desc}"
            f"（capture={event.capture_score:.3f}, pollute_capture={event.pollute_capture_score:.3f}）"
        )

    def on_action(self, event: BattleEvent, is_hit: bool, action_score: float) -> Optional[float]:
        if not is_hit:
            return None

        if self._phase == "attack":
            return self._tick_attack(event)
        if self._phase == "catch":
            return self._tick_catch(event)
        if self._phase == "escape":
            return do_escape(event.hwnd, event.templates, event.scale, event.window_width, event.window_height)
        return None

    def on_battle_end(self, event: BattleEvent) -> None:
        if self._catch_attempts > 0:
            print(f"[{_ts()}] 战斗结束，本场共尝试捕捉 {self._catch_attempts} 次")
        self._phase = "idle"
        self._debounce_count = 0
        self._catch_attempts = 0
        self._locate_fail_count = 0

    def on_non_battle_no_action(self, event: BattleEvent) -> None:
        if self._roaming is None:
            return
        self._roaming(event)

    # ── 攻击阶段：打虚脱 ─────────────────────────────────────────────

    def _tick_attack(self, event: BattleEvent) -> Optional[float]:
        capture_s, pollute_s = self._read_capture_scores(event)

        # 虚脱检测：主判据为反超差值（capture - pollute ≥ margin），
        # 绝对阈值仅作低分下限；连续 N 轮防抖后延迟判型复判确认
        catchable = (
            capture_s >= BC.CATCH_DETECT_THRESHOLD
            and (capture_s - pollute_s) >= BC.CATCH_REVERSE_MARGIN
        )
        if catchable:
            self._debounce_count += 1
            if self._debounce_count >= BC.DEBOUNCE_ROUNDS:
                # 防抖达标后再用延迟判型复判一次（等 0.5s 重截图），
                # 排除技能特效/过渡帧导致的瞬时反超
                result = classify_battle(
                    event.hwnd, event.templates, event.scale,
                    event.window_width, event.window_height,
                )
                if not result.is_pollute:
                    print(
                        f"[{_ts()}] ✓ 污染褪色（连续 {self._debounce_count} 轮反超，"
                        f"复判 capture={result.capture_score:.3f} pollute_capture={result.pollute_capture_score:.3f}）"
                    )
                    self._phase = "catch"
                    return self._tick_catch(event)
                # 复判仍是污染态，视为误报，重新累积防抖
                print(
                    f"[{_ts()}] [复判] 仍为污染态 (capture={result.capture_score:.3f} "
                    f"pollute_capture={result.pollute_capture_score:.3f})，疑似特效干扰，重新累积防抖"
                )
                self._debounce_count = 0
            else:
                print(
                    f"[{_ts()}] 虚脱疑似（防抖 {self._debounce_count}/{BC.DEBOUNCE_ROUNDS}），暂停攻击观察"
                    f"  [capture={capture_s:.3f} pollute_capture={pollute_s:.3f}]"
                )
            # 防抖观察期不出手：对已虚脱目标攻击会触发完整行动回合
            # （拖延十余秒才轮到下次检测，且有直接击杀目标的风险）
            return None

        self._debounce_count = 0
        press_once(event.hwnd, BC.ATTACK_KEY)
        print(f"[{_ts()}] 攻击中（按 {BC.ATTACK_KEY}）  [capture={capture_s:.3f} pollute_capture={pollute_s:.3f}]")
        return None

    # ── 捕捉阶段：点击按钮 → 选球 → 确认 ──────────────────────────────

    def _tick_catch(self, event: BattleEvent) -> float:
        hwnd = event.hwnd
        self._catch_attempts += 1

        # 重新截图，在行动 ROI 内定位捕捉按钮
        full_bgr = capture_window_bgr(hwnd)
        if full_bgr is None or full_bgr.size == 0:
            print(f"[{_ts()}] [警告] 捕捉截图失败，稍后重试")
            return 1.0

        roi_left, roi_top, roi_bgr = _battle_roi(full_bgr, event.window_width, event.window_height)
        # 双模板定位：虚脱专用模板与通用 capture 模板取分高者
        best = single_template_score_and_loc(
            roi_bgr, event.templates, BC.EXHAUSTED_TEMPLATE_NAME, scale=event.scale,
        )
        fallback = single_template_score_and_loc(
            roi_bgr, event.templates, CONFIG.capture_template_name, scale=event.scale,
        )
        if fallback[0] > best[0]:
            best = fallback
        score, (loc_x, loc_y) = best
        if score < BC.CATCH_DETECT_THRESHOLD:
            self._locate_fail_count += 1
            if self._locate_fail_count >= BC.LOCATE_FAIL_LIMIT:
                # 卡死检测：按钮连续消失但战斗未结束（如球槽面板滞留），
                # 触发恢复而不是继续盲试
                return self._recover_stuck_catch(hwnd, event)
            print(f"[{_ts()}] [警告] 未定位到捕捉按钮 (score={score:.3f})，稍后重试")
            return 1.0

        self._locate_fail_count = 0

        click_x = roi_left + loc_x
        click_y = roi_top + loc_y
        if not click_at(hwnd, click_x, click_y):
            print(f"[{_ts()}] [警告] 捕捉按钮点击失败，稍后重试")
            return 1.0

        # 等待球槽面板出现（模板闭环）；降级模式下内部退回固定延时
        if not self._wait_ball_panel(hwnd, event):
            return 1.0

        press_once(hwnd, BC.BALL_SLOT_KEY)
        _time.sleep(BC.CATCH_CONFIRM_DELAY)
        press_once(hwnd, BC.CONFIRM_KEY)
        print(
            f"[{_ts()}] 第 {self._catch_attempts} 次捕捉指令已发出"
            f"（按钮分={score:.3f}，球槽={BC.BALL_SLOT_KEY}），等待捕捉动画..."
        )
        # 额外冷却覆盖捕捉动画：动画期间失败界面未复原，避免误触发重试
        return BC.CATCH_COOLDOWN

    def _wait_ball_panel(self, hwnd: int, event: BattleEvent) -> bool:
        """等待球槽面板出现（左下角 ROI 模板闭环），返回是否可继续选球。

        闭环禁用（模板缺失或本场连续失败降级）时退回固定延时（开环）。
        连续 PANEL_MISS_LIMIT 次超时则本场禁用闭环，防止模板假阴性时
        反复空等；面板位于左下角，不遮挡右下角的捕捉按钮，降级重试安全。
        """
        if not self._panel_loop_enabled:
            _time.sleep(BC.CATCH_CLICK_DELAY)
            return True

        deadline = _time.time() + BC.PANEL_WAIT_TIMEOUT
        best_score = 0.0
        while _time.time() < deadline:
            _time.sleep(BC.PANEL_POLL_INTERVAL)
            frame = capture_window_bgr(hwnd)
            if frame is None or frame.size == 0:
                continue
            panel_bgr = _panel_roi(frame, event.window_width, event.window_height)
            score, _ = single_template_score_and_loc(
                panel_bgr, event.templates, BC.PANEL_TEMPLATE_NAME,
                scale=_panel_template_scale(event),
            )
            best_score = max(best_score, score)
            if score >= BC.PANEL_DETECT_THRESHOLD:
                self._panel_miss_count = 0
                _time.sleep(BC.PANEL_SETTLE_DELAY)
                print(f"[{_ts()}] 球槽面板已出现 (score={score:.3f})，执行选球")
                return True

        self._panel_miss_count += 1
        if self._panel_miss_count >= BC.PANEL_MISS_LIMIT:
            self._panel_loop_enabled = False
            print(
                f"[{_ts()}] [警告] 球槽面板连续 {self._panel_miss_count} 次未检测到"
                f"（最高分 {best_score:.3f} < {BC.PANEL_DETECT_THRESHOLD}），"
                f"本场降级为固定延时选球（模板 {BC.PANEL_TEMPLATE_NAME} 或 PANEL_ROI 需要修正）"
            )
        else:
            print(
                f"[{_ts()}] [警告] 等待球槽面板超时（第 {self._panel_miss_count} 次，"
                f"最高分 {best_score:.3f}），重试捕捉"
            )
        return False

    def _recover_stuck_catch(self, hwnd: int, event: BattleEvent) -> float:
        """捕捉环节卡死恢复：按钮连续消失且战斗未结束时的逃生通道。

        球槽面板还开着 → 补发选球指令（可能直接救回捕捉流程）；
        面板不在 → 转逃跑兜底脱离战斗（任何界面下 ESC+确认总能尝试脱离）。
        """
        self._locate_fail_count = 0

        frame = capture_window_bgr(hwnd)
        if frame is not None and frame.size > 0:
            panel_bgr = _panel_roi(frame, event.window_width, event.window_height)
            panel_score, _ = single_template_score_and_loc(
                panel_bgr, event.templates, BC.PANEL_TEMPLATE_NAME,
                scale=_panel_template_scale(event),
            )
            if panel_score >= BC.PANEL_DETECT_THRESHOLD:
                print(
                    f"[{_ts()}] [恢复] 球槽面板仍开着 (score={panel_score:.3f})，补发选球指令"
                )
                press_once(hwnd, BC.BALL_SLOT_KEY)
                _time.sleep(BC.CATCH_CONFIRM_DELAY)
                press_once(hwnd, BC.CONFIRM_KEY)
                return BC.CATCH_COOLDOWN

        print(f"[{_ts()}] [恢复] 捕捉按钮连续消失且面板不在，转逃跑兜底脱离战斗")
        self._phase = "escape"
        return 1.0

    # ── 工具 ──────────────────────────────────────────────────────────

    @staticmethod
    def _read_capture_scores(event: BattleEvent):
        """从引擎本轮全模板分数中取可捕捉分数及 pollute 分数。

        可捕捉分数取 capture 与虚脱专用模板（capture_exhausted）中的较高者：
        虚脱后按钮样式与 capture.png 匹配度有限，专用模板命中时分数更高。
        专用模板未放置时其分数恒为 0，自动退化为仅用 capture.png。
        """
        capture_key = normalize_template_name(CONFIG.capture_template_name)
        pollute_key = normalize_template_name(CONFIG.pollute_capture_template_name)
        exhausted_key = normalize_template_name(BC.EXHAUSTED_TEMPLATE_NAME)
        capture_s = 0.0
        exhausted_s = 0.0
        pollute_s = 0.0
        for name, s in (event.all_scores or []):
            key = normalize_template_name(name)
            if key == capture_key:
                capture_s = s
            elif key == pollute_key:
                pollute_s = s
            elif key == exhausted_key:
                exhausted_s = s
        return max(capture_s, exhausted_s), pollute_s

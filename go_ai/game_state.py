# -*- coding: utf-8 -*-
"""对局状态判定 (纯逻辑, 不依赖屏幕/引擎, 便于单元测试)。

TerminalDetector: 终局识别。放在独立模块的原因: go_controller 会 import
pyautogui/cv2 这类 GUI 依赖, 在无显示的 CI 上无法导入, 而终局判定是纯状态机,
值得在任何环境下都能测。
"""


class TerminalDetector:
    """判断对局是否已经结束。三个独立信号, 命中任一即判定终局:

      1) resign —— 引擎直接返回 resign (最强信号)
      2) two_passes —— 我方连续两手**真实**虚着 (引擎认为该收工了),
         **且**盘面已过开局 (子数下限校验, 见下)
      3) idle —— 盘面连续 N 轮完全不变, **且**有"没人行棋"的旁证
         (我方已 pass 过一次, 或界面上已找不到"XX方行棋"印章)

    第 3 条必须带旁证: 单纯"盘面不变"在对手长考时也会发生,
    所以只有出现旁证时才开始累计, 避免把长考误判成终局。

    第 2 条的两个约束各有来历:
      * forced_pass=False 才计数: 思考超时/引擎报错时 controller 用 (-1,-1)
        兜底, 那**不是**引擎主动认输。曾经开局连超时两次被当成"双虚着",
        直接判终局自动停 AI —— 现在只有引擎主动 pass 才累计。
      * 盘面子数下限: 双虚着只在盘面已铺开时才可信; 开局即便真双 pass 也不该收工。
        stones 数不出子数时(如测试桩)跳过该校验, 保持纯状态机可测。
    """

    def __init__(self, stable_cycles=12, min_stones_two_passes=10):
        self.stable_cycles = max(3, int(stable_cycles))
        self.min_stones_two_passes = max(0, int(min_stones_two_passes))
        self._last_sig = None
        self.stable = 0
        self.my_passes = 0
        self.engine_resign = False
        self.last_total = None      # 最近一次 update 的盘面总子数 (取不到为 None)

    def reset(self):
        self._last_sig = None
        self.stable = 0
        self.my_passes = 0
        self.engine_resign = False
        self.last_total = None

    @staticmethod
    def _count_total(stones):
        """尽力数出盘面总子数; 数不出来返回 None (测试桩只有 tobytes 时走这里)。"""
        try:
            import numpy as _np
            if isinstance(stones, _np.ndarray):
                return int((stones != 0).sum())
        except Exception:
            pass
        try:
            return sum(1 for row in stones for v in row if v)
        except Exception:
            return None

    def update(self, stones, mv=None, side_turn=None, forced_pass=False):
        """stones: 当前盘面 (需有 tobytes, 或任何可比较对象);
        mv: 本轮决策点 (None 表示本轮没决策);
        side_turn: UI 识别的行棋方, None 表示界面已看不出谁在行棋;
        forced_pass: True 表示 mv=(-1,-1) 是超时/报错兜底, 不算真实虚着。
        返回终局原因字符串或 None。"""
        try:
            sig = stones.tobytes()
        except AttributeError:
            sig = str(stones)
        self.last_total = self._count_total(stones)
        if self._last_sig is None:
            self._last_sig, self.stable = sig, 0
        elif sig != self._last_sig:
            self._last_sig, self.stable = sig, 0       # 盘面动了 -> 重新计数
        else:
            idle_hint = (self.my_passes >= 1) or (side_turn is None)
            if idle_hint:
                self.stable += 1
        if mv == (-1, -1):
            if not forced_pass:                        # 超时/报错兜底不算真虚着
                self.my_passes += 1
        elif mv is not None:
            self.my_passes = 0
        return self.reason()

    def reason(self):
        if self.engine_resign:
            return 'resign'
        if self.my_passes >= 2:
            # 盘面还很小(开局)时, 双虚着多半是误判 -> 不判终局
            if self.min_stones_two_passes <= 0 or self.last_total is None \
                    or self.last_total >= self.min_stones_two_passes:
                return 'two_passes'
        if self.stable >= self.stable_cycles:
            return 'idle_%d_cycles' % self.stable
        return None

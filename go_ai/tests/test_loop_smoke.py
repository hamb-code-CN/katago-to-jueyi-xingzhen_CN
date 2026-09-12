# -*- coding: utf-8 -*-
"""控制器主循环冒烟测试 (端到端, 但完全离线)。

做法: 把"截屏"换成合成棋盘图, 把"点击"换成记录, 关掉 KataGo(走本地兜底引擎),
然后真的跑一遍 go_controller.main()。这样主循环里所有接线 (配置读取 -> 识别 ->
轮局判定 -> 决策 -> 落子 -> 核对 -> 终局判定) 都会被执行到,
但**不会真的动鼠标、不碰屏幕、不加载 GPU 引擎**。

这是升级改动最容易悄悄弄坏的地方 (缩进/变量名/分支), 所以值得有一个能自动跑的守卫。
"""
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 同目录用例互引

from test_vision import make_board_image, fake_board, BLACK_PTS, WHITE_PTS  # noqa: E402

# go_controller 依赖 pyautogui (GUI 自动化库)。装了就跑完整主循环,
# 没装 (例如极简 CI 环境) 就整类跳过, 不让测试套件变红。
try:
    import go_controller as _gc_mod   # noqa: F401

    _CAN_IMPORT = True
    _SKIP_REASON = ''
except Exception as _e:               # pragma: no cover
    _CAN_IMPORT = False
    _SKIP_REASON = 'go_controller 不可导入 (%r), 需要 pyautogui' % (_e,)


@unittest.skipUnless(_CAN_IMPORT, _SKIP_REASON)
class LoopSmokeTest(unittest.TestCase):
    def setUp(self):
        import go_controller as gc
        import go_vision as gv
        self.gc, self.gv = gc, gv
        self._saved = {}
        self.clicks = []

        def _save(name, value):
            self._saved[name] = getattr(gc, name)
            setattr(gc, name, value)

        img = make_board_image()
        board = fake_board()

        def _shot(path=None, cfg=None):
            return img.copy(), None

        def _board_from(img_, cfg_):
            b = gv.detect_board(img_, roi_x_max=getattr(cfg_, 'BOARD_ROI_X_MAX', 960),
                                window_rect=None)
            if b is None:
                return None, None
            b = gv.refine_pts(img_, b)
            stones, _ = gv.read_board(img_, b)
            return b, stones

        _save('take_screenshot', _shot)
        _save('board_from_screenshot', _board_from)
        _save('click_board', lambda b, c, r: (self.clicks.append((c, r)) or (0, 0)))
        _save('click_confirm', lambda cfg_: False)
        _save('find_go_window', lambda: None)
        _save('find_browser_window', lambda: None)
        _save('KATAGO_AVAILABLE', False)          # 不碰 GPU 引擎
        _save('SEAL_OCR_AVAILABLE', False)        # 不跑 OCR, 强制用子数法判轮次
        _save('AVATAR_DETECT_AVAILABLE', False)
        _save('select_move', lambda b, color, time_budget=25.0: (3, 3))
        self._saved['_pyautogui_size'] = gc.pyautogui.size
        gc.pyautogui.size = lambda: (1920, 1080)
        self._saved['_sleep'] = time.sleep
        time.sleep = lambda s: None               # 跳过动画/轮询等待
        # 单实例锁隔离: main() 会先抢 _controller.lock, 若撞上本机真实运行的
        # controller 就会拒绝启动 -> 本轮一次都不点 -> 测试假红。
        # 指到临时文件, 既保证可重复, 又仍会真实走一遍加锁/解锁代码。
        fd, self._lock = tempfile.mkstemp(prefix='_ctrl_smoke_', suffix='.lock')
        os.close(fd)
        os.remove(self._lock)
        _save('CONTROLLER_LOCK', self._lock)
        self._argv = sys.argv
        # 我方执白: 合成图黑 4 白 3 -> 子数法判定轮到白, 保证进入决策分支
        sys.argv = ['go_controller.py', '--once', '--color', 'white']

    def tearDown(self):
        gc = self.gc
        for k, v in self._saved.items():
            if k == '_pyautogui_size':
                gc.pyautogui.size = v
            elif k == '_sleep':
                time.sleep = v
            else:
                setattr(gc, k, v)
        sys.argv = self._argv
        try:
            os.remove(self._lock)
        except OSError:
            pass

    def test_one_full_cycle(self):
        """跑一整轮: 应该识别出棋盘 -> 判定轮到我 -> 决策 -> 落子一次 -> 退出。"""
        try:
            self.gc.main()
        except SystemExit:
            pass
        self.assertEqual(self.clicks, [(3, 3)], '应当恰好点击一次决策点')

    def test_no_terminal_false_positive(self):
        """正常落子的一轮不能被判成终局 (否则会自动停止, 属于严重误判)。"""
        import go_controller as gc
        det = gc.TerminalDetector(3)
        img = make_board_image()
        stones = np.zeros((19, 19), dtype=np.int8)
        for c, r in BLACK_PTS:
            stones[r, c] = 1
        for c, r in WHITE_PTS:
            stones[r, c] = 2
        r = det.update(stones, mv=(3, 3), side_turn='known')
        self.assertIsNone(r)

    def test_click_board_applies_window_origin(self):
        """窗口取图时, 点击坐标必须加上窗口原点 —— 否则整盘落子都会偏移一个窗口位置。"""
        gc = self.gc
        gv = self.gv
        board = {
            'pts': np.zeros((19, 19, 2), dtype=np.float64),
            'vx': [10.0] * 19, 'hy': [20.0] * 19, 'step': 5.0,
            'x0': 10, 'y0': 20, 'x1': 100, 'y1': 200,
        }
        board['pts'][4][6] = (66.0, 44.0)      # (col=6,row=4)
        moved = []
        mock_click = gc.click_board
        saved_moveTo = gc.pyautogui.moveTo
        saved_click = gc.pyautogui.click
        gc.click_board = self._saved['click_board']      # 用真实的 click_board
        gc.pyautogui.moveTo = lambda x, y, **kw: moved.append((x, y))
        gc.pyautogui.click = lambda *a, **kw: None       # 别真的点鼠标
        try:
            gc.click_board(board, 6, 4, origin=(0, 0))
            self.assertEqual(moved[-1], (66, 44))
            gc.click_board(board, 6, 4, origin=(100, 50))
            self.assertEqual(moved[-1], (166, 94))
        finally:
            gc.click_board = mock_click
            gc.pyautogui.moveTo = saved_moveTo
            gc.pyautogui.click = saved_click


if __name__ == '__main__':
    unittest.main()

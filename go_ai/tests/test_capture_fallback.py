# -*- coding: utf-8 -*-
"""取图回落测试。

背景: controller 默认"全屏截图优先"。全屏拍的是屏幕上可见内容, 任何盖住棋盘的
东西 (星阵的登录/注册弹窗、别的浏览器窗口) 都会让 detect_board 认不出棋盘,
于是 AI 卡在 "未检测到棋盘, 重试..." 空转 (实盘遇到过连续 85 轮)。

修复: auto 模式下全屏认不出棋盘时, 自动改用 PrintWindow 窗口取图重试 ——
窗口图拿的是窗口自己的渲染内容, 遮挡不影响。
"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.append(ROOT)      # append: 避免根目录脚本抢在同名模块前被 discover 到

import go_controller as gc  # noqa: E402

FULL = np.zeros((40, 60, 3), np.uint8)          # 全屏帧 (认不出棋盘)
WIN = np.full((50, 70, 3), 9, np.uint8)         # 窗口帧 (认得出棋盘)


class TestCaptureFallback(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in ('take_screenshot', 'grab_for_read', 'board_from_screenshot',
                  'CAPTURE_MODE', 'CAPTURE_MIRROR', 'PLATFORM'):
            self._saved[k] = getattr(gc, k, None) if hasattr(gc, k) else getattr(gc.Config, k, None)
        self._orig_origin = gc._SHOT_ORIGIN
        self.calls = []

    def tearDown(self):
        gc.take_screenshot = self._saved['take_screenshot']
        gc.grab_for_read = self._saved['grab_for_read']
        gc.board_from_screenshot = self._saved['board_from_screenshot']
        gc.Config.CAPTURE_MODE = self._saved['CAPTURE_MODE']
        gc.Config.CAPTURE_MIRROR = self._saved['CAPTURE_MIRROR']
        gc.Config.PLATFORM = self._saved['PLATFORM']
        gc._SHOT_ORIGIN = self._orig_origin

    def _setup(self, mode='auto', window_ok=True, screen_ok=False):
        gc.Config.CAPTURE_MODE = mode
        gc.Config.CAPTURE_MIRROR = False
        gc.Config.PLATFORM = 'xingzhen'

        def fake_shot(path=None, cfg=None):
            gc._SHOT_ORIGIN = (0, 0)
            gc._LAST_FRAME_MODE = 'screen'
            return FULL, (0, 0, 200, 100)          # 全屏帧总是"能截到图"

        def fake_grab(platform='tencent', prefer_window=True, mirror=True):
            self.calls.append(('grab', mirror))
            if not window_ok:
                return None, (0, 0), 'none'
            return WIN, (12, 34), 'window'

        def fake_bfs(img, cfg):
            # 只有窗口帧 (值全为 9) 才认得出棋盘
            if img is WIN or (hasattr(img, 'shape') and img.shape == WIN.shape
                              and int(img[0, 0, 0]) == 9):
                return ({'step': 1.0, 'pts': None}, np.zeros((19, 19), np.uint8))
            return (None, None) if not screen_ok else (
                {'step': 1.0, 'pts': None}, np.zeros((19, 19), np.uint8))

        gc.take_screenshot = fake_shot
        gc.grab_for_read = fake_grab
        gc.board_from_screenshot = fake_bfs

    def test_falls_back_to_window_when_fullscreen_blind(self):
        """全屏认不出 -> 自动改用窗口取图, 并返回窗口帧/原点。"""
        self._setup()
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNotNone(board, '应回落到窗口取图并识别成功')
        self.assertIsNotNone(stones)
        self.assertTrue(np.array_equal(img, WIN), '返回的应是窗口帧')
        self.assertEqual(gc._SHOT_ORIGIN, (12, 34), '点击原点必须切到窗口左上角')
        self.assertEqual(rect, (0, 0, 70, 50), '窗口模式 ROI = 整幅窗口图')
        self.assertEqual(len(self.calls), 1, '应恰好回落一次')

    def test_no_fallback_when_fullscreen_works(self):
        """全屏能认出棋盘时不抓窗口 (省一次 PrintWindow)。"""
        self._setup(screen_ok=True)
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNotNone(board)
        self.assertTrue(np.array_equal(img, FULL))
        self.assertEqual(gc._SHOT_ORIGIN, (0, 0))
        self.assertEqual(self.calls, [], '不应触发回落')

    def test_mode_screen_never_falls_back(self):
        """显式指定 screen 模式 -> 不回落 (用户要的就是全屏)。"""
        self._setup(mode='screen')
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNone(board)
        self.assertEqual(self.calls, [])

    def test_window_capture_unavailable_degrades_gracefully(self):
        """回落也抓不到窗口 -> 返回 None 而不是抛异常。"""
        self._setup(window_ok=False)
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNone(board)
        self.assertIsNone(stones)

    def test_board_still_missing_after_fallback(self):
        """窗口图也认不出 -> 返回 None (调用方照旧打印重试)。"""
        self._setup()
        gc.board_from_screenshot = lambda img, cfg: (None, None)
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNone(board)

    def test_window_frame_uses_local_roi(self):
        """窗口帧识别时 ROI 必须换成窗口局部矩形。

        踩过的坑: 全屏帧留下的 GO_WINDOW 是屏幕绝对坐标 (-8,0,1288,1398),
        拿它去裁"窗口局部图"会把棋盘裁到 ROI 外面 -> 图里有棋盘却认不出。
        """
        self._setup()
        gc.Config.GO_WINDOW = (-8, 0, 1288, 1398)      # 全屏帧留下的陈旧 ROI
        seen = []

        def spy(img, cfg):
            seen.append(getattr(cfg, 'GO_WINDOW', None))
            if img.shape == WIN.shape:
                return {'step': 1.0, 'pts': None}, np.zeros((19, 19), np.uint8)
            return None, None

        gc.board_from_screenshot = spy
        img, board, stones, rect = gc.read_board_frame(gc.Config)
        self.assertIsNotNone(board)
        self.assertIn((0, 0, 70, 50), seen, '窗口帧识别时 ROI 应为窗口局部矩形')
        self.assertNotIn((-8, 0, 1288, 1398), seen[1:], '不应拿全屏坐标裁窗口图')
        self.assertEqual(gc.Config.GO_WINDOW, (-8, 0, 1288, 1398),
                         '识别后应还原 ROI (由主循环按帧更新)')


class TestCfgInstanceNotClass(unittest.TestCase):
    """取图必须读 **Config 实例** 的参数, 不能读类默认值。

    踩过的坑: 代码里写 getattr(Config, 'PLATFORM', 'tencent') —— Config 是类,
    而 main() 用的是 Config() 实例。于是星阵(浏览器平台)取图时:
      platform 拿到类默认 'tencent' -> 去找"腾讯围棋"窗口, 找不到 ->
      grab_for_read 回落到全屏 -> 窗口取图这条路整个失效;
      mirror 拿到类默认 True -> 就算抓到了窗口也是镜像的, 点击会左右反。
    """

    def setUp(self):
        self.saved_grab = gc.grab_for_read
        self.saved_pyautogui = gc.pyautogui
        self.calls = []
        self.origin = gc._SHOT_ORIGIN
        self.mode = gc._LAST_FRAME_MODE

    def tearDown(self):
        gc.grab_for_read = self.saved_grab
        gc.pyautogui = self.saved_pyautogui
        gc._SHOT_ORIGIN = self.origin
        gc._LAST_FRAME_MODE = self.mode

    def test_take_screenshot_uses_instance_params(self):
        class FakeCfg:
            CAPTURE_MODE = 'window'      # 直接走窗口取图分支
            CAPTURE_MIRROR = False
            PLATFORM = 'xingzhen'

        def fake_grab(platform='tencent', prefer_window=True, mirror=True):
            self.calls.append((platform, mirror))
            return WIN, (-6, 0), 'window'

        gc.grab_for_read = fake_grab
        img, rect = gc.take_screenshot(None, cfg=FakeCfg())
        self.assertEqual(self.calls, [('xingzhen', False)],
                         '窗口取图必须用实例的 platform/mirror, 不是类默认值')
        self.assertTrue(np.array_equal(img, WIN))
        self.assertEqual(rect, (0, 0, 70, 50))
        self.assertEqual(gc._LAST_FRAME_MODE, 'window')

    def test_class_default_platform_is_tencent(self):
        """留个证: 类默认值是 tencent —— 所以读类就等于永远走腾讯分支。"""
        self.assertEqual(getattr(gc.Config, 'PLATFORM', 'tencent'), 'tencent')


if __name__ == '__main__':
    unittest.main()

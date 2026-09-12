# -*- coding: utf-8 -*-
"""辅助模式测试: AI 只给胜率/局面分析, 玩家在看板点选落子。

链路: 看板点棋盘 -> POST /api/pick -> 写 _command.json
      -> controller handle_command 暂存 -> 本轮校验后点击客户端

这里逐段测:
  1. assist_play: 玩家点选的合法性把关 (已有子 / 劫自杀 / 正常)
  2. handle_command: play 指令解析 (含越界/非法 JSON)
  3. api_pick: 看板侧写指令
  4. start_ai: 是否正确把 --assist / --analyze-only 传给 controller
  5. config_store: mode 归一化
  6. 落子历史退化提示: 每局只提示一次, 之后静默 (消除每手刷屏)
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 用 append 而非 insert(0): go_ai 根目录残留了几份早期调试用的 test_*.py,
# 若把 ROOT 插到最前, unittest discover 会把这些同名文件当成测试模块, 报
# "module incorrectly imported"。追加到末尾即可正常 import go_controller。
sys.path.append(ROOT)

import numpy as np  # noqa: E402

import go_controller as gc  # noqa: E402
import config_store  # noqa: E402
from go_engine import Board, BLACK, WHITE  # noqa: E402


def empty_stones():
    return np.zeros((19, 19), dtype=np.int8)


class TestAssistPlay(unittest.TestCase):
    """assist_play: 玩家点选的校验 + 点击。"""

    def setUp(self):
        self.board = {'pts': np.zeros((19, 19, 2), dtype=np.float64),
                      'vx': [10.0] * 19, 'hy': [20.0] * 19, 'step': 5.0,
                      'x0': 10, 'y0': 20, 'x1': 100, 'y1': 200}
        self._click = gc.click_board
        self._confirm = gc.click_confirm
        self.clicked = []
        gc.click_board = lambda b, c, r, origin=None: (self.clicked.append((c, r)) or (1, 2))
        gc.click_confirm = lambda cfg: False

    def tearDown(self):
        gc.click_board = self._click
        gc.click_confirm = self._confirm

    def test_rejects_occupied_point(self):
        st = empty_stones()
        st[3][3] = BLACK
        ok, msg = gc.assist_play(gc.Config(), self.board, st, (3, 3), [], BLACK)
        self.assertFalse(ok)
        self.assertIn('已有子', msg)
        self.assertEqual(self.clicked, [], '被拒绝时不该点鼠标')

    def test_rejects_out_of_range(self):
        ok, msg = gc.assist_play(gc.Config(), self.board, empty_stones(), (99, 0), [], BLACK)
        self.assertFalse(ok)
        self.assertIn('超出棋盘', msg)
        self.assertEqual(self.clicked, [])

    def test_rejects_suicide(self):
        # 白子把 (0,0) 围死; 让白再下 (0,0) 属自杀
        st = empty_stones()
        st[0][1] = BLACK
        st[1][0] = BLACK
        st[1][1] = BLACK
        ok, msg = gc.assist_play(gc.Config(), self.board, st, (0, 0), [], WHITE)
        self.assertFalse(ok)
        self.assertIn('非法', msg)
        self.assertEqual(self.clicked, [])

    def test_accepts_legal_point_and_clicks(self):
        ok, msg = gc.assist_play(gc.Config(), self.board, empty_stones(), (3, 3), [], BLACK)
        self.assertTrue(ok, msg)
        self.assertEqual(self.clicked, [(3, 3)])


class TestPlayCommand(unittest.TestCase):
    """handle_command: 看板下发的 play 指令 -> 暂存待执行。"""

    def setUp(self):
        fd, self.cmd = tempfile.mkstemp(prefix='_cmd_test_', suffix='.json')
        os.close(fd)
        os.remove(self.cmd)
        self._orig = gc.COMMAND_FILE
        gc.COMMAND_FILE = self.cmd
        gc._PENDING_PLAY['pt'] = None
        gc._CMD_SEEN = None

    def tearDown(self):
        gc.COMMAND_FILE = self._orig
        gc._PENDING_PLAY['pt'] = None
        gc._CMD_SEEN = None
        try:
            os.remove(self.cmd)
        except OSError:
            pass

    def _write(self, obj):
        with open(self.cmd, 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def test_play_command_is_stashed(self):
        self._write({'cmd': 'play', 'col': 15, 'row': 3})
        gc.handle_command(gc.Config(), None, False, [])
        self.assertEqual(gc._PENDING_PLAY['pt'], (15, 3))

    def test_out_of_range_play_ignored(self):
        self._write({'cmd': 'play', 'col': 42, 'row': 3})
        gc.handle_command(gc.Config(), None, False, [])
        self.assertIsNone(gc._PENDING_PLAY['pt'])

    def test_missing_coords_ignored(self):
        self._write({'cmd': 'play', 'col': None, 'row': 'x'})
        gc.handle_command(gc.Config(), None, False, [])
        self.assertIsNone(gc._PENDING_PLAY['pt'])

    def test_reset_clears_pending(self):
        gc._PENDING_PLAY['pt'] = (4, 4)
        self._write({'cmd': 'reset'})
        gc.handle_command(gc.Config(), None, False, [])
        self.assertIsNone(gc._PENDING_PLAY['pt'])


class TestApiPick(unittest.TestCase):
    """api_pick: 看板点选 -> 写 _command.json。"""

    def setUp(self):
        import analysis_watch as aw
        self.aw = aw
        fd, self.cmd = tempfile.mkstemp(prefix='_cmd_api_', suffix='.json')
        os.close(fd)
        os.remove(self.cmd)
        self._orig_cmd = aw.COMMAND_FILE
        self._orig_run = aw.is_running
        aw.COMMAND_FILE = self.cmd
        aw.is_running = lambda: (True, True, None, [(123, 'go_controller.py')], [])

    def tearDown(self):
        self.aw.COMMAND_FILE = self._orig_cmd
        self.aw.is_running = self._orig_run
        try:
            os.remove(self.cmd)
        except OSError:
            pass

    def test_pick_writes_play_command(self):
        r = self.aw.api_pick({'col': 2, 'row': 16})
        self.assertTrue(r['ok'], r)
        with open(self.cmd, encoding='utf-8') as f:
            body = json.load(f)
        self.assertEqual((body['cmd'], body['col'], body['row']), ('play', 2, 16))

    def test_pick_out_of_range(self):
        r = self.aw.api_pick({'col': 19, 'row': 0})
        self.assertFalse(r['ok'])
        self.assertIn('越界', r['err'])

    def test_pick_non_integer(self):
        r = self.aw.api_pick({'col': 'a', 'row': 1})
        self.assertFalse(r['ok'])

    def test_pick_refused_when_controller_down(self):
        self.aw.is_running = lambda: (False, True, None, [], [])
        r = self.aw.api_pick({'col': 3, 'row': 3})
        self.assertFalse(r['ok'])
        self.assertIn('未运行', r['err'])


class TestStartAiMode(unittest.TestCase):
    """start_ai: 模式必须变成 controller 的命令行开关。"""

    def setUp(self):
        import go_launcher as gl
        self.gl = gl
        self._saved = {k: getattr(gl, k) for k in ('is_running', 'start_preload',
                                                   'subprocess', '_invalidate_proc_cache', 'LOG_FILE')}
        fd, self.log = tempfile.mkstemp(prefix='_launcher_', suffix='.log')
        os.close(fd)
        gl.LOG_FILE = self.log
        gl.is_running = lambda: (False, False, None, [], [])
        gl.start_preload = lambda *_a, **_k: True
        gl._invalidate_proc_cache = lambda: None
        self.captured = captured = []

        class FakePopen:
            def __init__(self, args, **kw):
                captured.append(args)
        gl.subprocess = type('S', (), {'Popen': FakePopen, 'STDOUT': -2})

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.gl, k, v)
        try:
            os.remove(self.log)
        except OSError:
            pass

    def _run(self, mode):
        cfg = config_store.load_config()
        self.gl.start_ai('black', cfg, False, mode=mode)
        return self.captured[-1]

    def test_assist_flag(self):
        self.assertIn('--assist', self._run('assist'))

    def test_analyze_flag(self):
        self.assertIn('--analyze-only', self._run('analyze'))

    def test_auto_has_no_flag(self):
        args = self._run('auto')
        self.assertNotIn('--assist', args)
        self.assertNotIn('--analyze-only', args)

    def test_unknown_mode_falls_back_to_auto(self):
        args = self._run('bogus')
        self.assertNotIn('--assist', args)
        self.assertNotIn('--analyze-only', args)


class TestConfigMode(unittest.TestCase):
    def test_invalid_mode_normalized(self):
        cfg = config_store._normalize({'mode': 'nonsense'})
        self.assertEqual(cfg['mode'], 'auto')

    def test_valid_modes_kept(self):
        for m in config_store.VALID_MODES:
            self.assertEqual(config_store._normalize({'mode': m})['mode'], m)

    def test_default_has_mode(self):
        self.assertIn('mode', config_store.DEFAULT_CONFIG)
        self.assertIn(config_store.DEFAULT_CONFIG['mode'], config_store.VALID_MODES)


class TestPlayFailBlacklist(unittest.TestCase):
    """落子核对连续失败 -> 拉黑该点, 掐断"反复点同一个点"的死循环。

    真实事故 (2026-09-12): 星阵最后一手红标记让白子被读成空点, 引擎于是连续
    115 轮点同一个已被占据的点。视觉已修, 这里是双保险。
    """

    def setUp(self):
        gc._FAILED_PTS.clear()

    def tearDown(self):
        gc._FAILED_PTS.clear()

    def test_not_blacklisted_initially(self):
        self.assertFalse(gc.is_play_blacklisted(3, 3))

    def test_blacklisted_after_max_failures(self):
        for i in range(1, gc.PLAY_FAIL_PT_MAX + 1):
            n = gc.note_play_result(3, 3, False)
            self.assertEqual(n, i)
            self.assertEqual(gc.is_play_blacklisted(3, 3), i >= gc.PLAY_FAIL_PT_MAX)

    def test_success_clears_blacklist(self):
        gc.note_play_result(3, 3, False)
        gc.note_play_result(3, 3, False)
        self.assertTrue(gc.is_play_blacklisted(3, 3))
        self.assertEqual(gc.note_play_result(3, 3, True), 0)
        self.assertFalse(gc.is_play_blacklisted(3, 3))
        self.assertEqual(gc._FAILED_PTS, {})

    def test_other_points_unaffected(self):
        gc.note_play_result(3, 3, False)
        gc.note_play_result(3, 3, False)
        self.assertFalse(gc.is_play_blacklisted(4, 3))

    def test_counter_survives_between_cycles(self):
        """黑名单跨轮次生效 (每轮重新决策也必须记得上一次失败)。"""
        gc.note_play_result(7, 2, False)
        self.assertEqual(gc._FAILED_PTS[(7, 2)], 1)
        gc.note_play_result(7, 2, False)
        self.assertTrue(gc.is_play_blacklisted(7, 2))


class TestHistoryWarningOnce(unittest.TestCase):
    """落子历史退化 (对手已应手/提子导致重放对不上) 属正常现象:
    每局只提示一行说明, 之后必须完全静默 —— 否则每手刷两行纯噪音。"""

    def setUp(self):
        gc.reset_history_warning()

    def tearDown(self):
        gc.reset_history_warning()

    @staticmethod
    def _warn(cycle, why):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gc.warn_history_once(cycle, why)
        return buf.getvalue()

    def test_first_call_prints_then_goes_silent(self):
        out = self._warn(5, '落子后历史不一致')
        self.assertIn('[5]', out)
        self.assertIn('退化为交替重建', out)
        # 后续同类退化 (含另一种触发点) 一律静默
        self.assertEqual(self._warn(6, '落子后历史不一致'), '')
        self.assertEqual(self._warn(7, '落子历史与棋盘不一致'), '')
        self.assertEqual(self._warn(8, '落子后历史不一致'), '')

    def test_reset_allows_one_more_warning(self):
        self._warn(5, '落子后历史不一致')
        gc.reset_history_warning()
        self.assertIn('退化为交替重建', self._warn(9, '落子历史与棋盘不一致'))
        self.assertEqual(self._warn(10, '落子历史与棋盘不一致'), '')


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""SGF 导出 / 配置存储 / 通知 / 终局识别 的回归测试 (全部纯逻辑, 不碰屏幕与网络)。"""
import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sgf as sgf_mod          # noqa: E402
import config_store            # noqa: E402
import notify as notify_mod    # noqa: E402


class TestSGF(unittest.TestCase):
    def test_basic_moves(self):
        text = sgf_mod.moves_to_sgf(moves=[(3, 2, 1), (15, 15, 2)], date='2026-01-01')
        self.assertIn('SZ[19]', text)
        self.assertIn(';B[dc];W[pp]', text)      # (3,2)->d c, (15,15)->p p
        self.assertTrue(text.startswith('(') and text.endswith(')'))

    def test_pass_written_as_empty(self):
        text = sgf_mod.moves_to_sgf(moves=[(0, 0, 1), (-1, -1, 2), (-1, -1, 1)])
        self.assertIn(';B[aa]', text)
        self.assertIn(';W[]', text)
        self.assertIn(';B[]', text)

    def test_invalid_coordinates_skipped(self):
        text = sgf_mod.moves_to_sgf(moves=[(0, 0, 1), (99, 99, 2), (1, 1, 1)])
        self.assertIn(';B[aa]', text)
        self.assertIn(';B[bb]', text)
        self.assertNotIn(';W', text)

    def test_color_strings(self):
        text = sgf_mod.moves_to_sgf(moves=[(0, 0, 'B'), (1, 0, 'W')])
        self.assertIn(';B[aa];W[ba]', text)

    def test_setup_from_stones_without_history(self):
        stones = [[0] * 19 for _ in range(19)]
        stones[3][3] = 1     # 黑 (3,3)
        stones[5][5] = 2     # 白 (5,5)
        text = sgf_mod.moves_to_sgf(stones=stones)
        self.assertIn('AB[dd]', text)
        self.assertIn('AW[ff]', text)
        self.assertNotIn(';B[', text)

    def test_save_writes_file(self):
        d = tempfile.mkdtemp()
        try:
            p = sgf_mod.save_sgf(moves=[(0, 0, 1)], out_dir=d, name='x/y.sgf')
            self.assertTrue(os.path.isfile(p))
            self.assertNotIn('/', os.path.basename(p))     # 文件名里的路径分隔符被清理
            with open(p, encoding='utf-8') as f:
                self.assertIn(';B[aa]', f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unescaped_brackets_escaped(self):
        text = sgf_mod.moves_to_sgf(moves=[(0, 0, 1)], comment='a]b\\c')
        self.assertIn('a\\]b\\\\c', text)


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.backup = None
        if os.path.exists(config_store.CONFIG_PATH):
            with open(config_store.CONFIG_PATH, encoding='utf-8') as f:
                self.backup = f.read()

    def tearDown(self):
        if self.backup is not None:
            with open(config_store.CONFIG_PATH, 'w', encoding='utf-8') as f:
                f.write(self.backup)
        elif os.path.exists(config_store.CONFIG_PATH):
            os.remove(config_store.CONFIG_PATH)

    def test_defaults_and_groups(self):
        c = config_store.load_config()
        for k in ('tmin', 'tmax', 'interval', 'color', 'platform', 'katago',
                  'weights', 'auto_stop', 'notify', 'sgf', 'watchdog'):
            self.assertIn(k, c)
        self.assertFalse(c['notify']['enabled'])      # 默认不发通知

    def test_level_drives_times(self):
        c = config_store.save_config({'level': '高级'})
        self.assertEqual((c['tmin'], c['tmax']), config_store.LEVELS['高级'])

    def test_custom_level_when_manual(self):
        c = config_store.save_config({'tmin': 7.0, 'tmax': 9.0, 'level': '自定义'})
        self.assertEqual(c['level'], '自定义')
        self.assertEqual((c['tmin'], c['tmax']), (7.0, 9.0))

    def test_clamps_and_swaps(self):
        c = config_store.save_config({'tmin': 99.0, 'tmax': 1.0, 'interval': 0.1})
        self.assertLessEqual(c['tmin'], c['tmax'])
        self.assertGreaterEqual(c['interval'], 1.0)
        self.assertLessEqual(c['tmax'], 300.0)

    def test_invalid_enum_falls_back(self):
        c = config_store.save_config({'color': 'purple', 'platform': 'nope'})
        self.assertEqual(c['color'], 'black')
        self.assertEqual(c['platform'], 'tencent')

    def test_unknown_keys_pruned_from_groups(self):
        c = config_store.save_config({'auto_stop': {'enabled': False, 'junk': 1}})
        self.assertNotIn('junk', c['auto_stop'])
        self.assertFalse(c['auto_stop']['enabled'])

    def test_patch_merge_keeps_other_values(self):
        config_store.save_config({'interval': 7.0, 'color': 'white'})
        c = config_store.save_config(config_store.load_config(),
                                    {'notify': {'enabled': True, 'url': 'http://x'}})
        self.assertEqual(c['interval'], 7.0)
        self.assertEqual(c['color'], 'white')
        self.assertTrue(c['notify']['enabled'])

    def test_file_is_valid_json(self):
        config_store.save_config({'interval': 4.0})
        with open(config_store.CONFIG_PATH, encoding='utf-8') as f:
            json.load(f)


class TestNotify(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = {'enabled': False, 'url': 'http://127.0.0.1:1/x', 'events': []}
        self.assertFalse(notify_mod.enabled('terminal', cfg))
        self.assertFalse(notify_mod.send('terminal', 't', 'x', config=cfg))

    def test_event_filter(self):
        cfg = {'enabled': True, 'url': 'http://127.0.0.1:1/x', 'events': ['terminal']}
        self.assertTrue(notify_mod.enabled('terminal', cfg))
        self.assertFalse(notify_mod.enabled('error', cfg))

    def test_failure_does_not_raise(self):
        cfg = {'enabled': True, 'url': 'http://127.0.0.1:1/none', 'events': []}
        # 端口 1 必然连不上 -> 应静默返回 False, 不抛异常
        self.assertFalse(notify_mod.send('test', 't', 'x', config=cfg, block=True, timeout=1.0))


class TestTerminalDetector(unittest.TestCase):
    def _det(self, cycles=3):
        import game_state

        class FakeStones:
            def __init__(self, sig):
                self._s = sig.encode()

            def tobytes(self):
                return self._s
        return game_state.TerminalDetector(cycles), FakeStones

    def test_no_terminal_while_board_is_moving(self):
        det, FakeStones = self._det(3)
        r = None
        for i in range(10):
            r = det.update(FakeStones('board%d' % i), mv=(i, i), side_turn='known')
        self.assertIsNone(r)

    def test_two_passes_triggers(self):
        det, FakeStones = self._det(99)
        det.update(FakeStones('same'), mv=(-1, -1), side_turn='known')
        r = det.update(FakeStones('same'), mv=(-1, -1), side_turn='known')
        self.assertEqual(r, 'two_passes')

    def test_forced_passes_do_not_count(self):
        """思考超时/报错兜底的 (-1,-1) 不是引擎主动虚着, 连续多次也不能判终局。
        (曾经开局连超时两次被当成双虚着直接自动停 AI)"""
        det, FakeStones = self._det(99)
        r = None
        for _ in range(5):
            r = det.update(FakeStones('same'), mv=(-1, -1),
                           side_turn='known', forced_pass=True)
        self.assertIsNone(r, 'r=%r' % r)
        self.assertEqual(det.my_passes, 0)

    def test_two_passes_blocked_on_empty_board(self):
        """真双虚着但盘面还很空(开局) -> 不判终局, 防误停。"""
        import numpy as np
        det, _ = self._det(99)          # 用真实 ndarray 才会走子数校验
        empty = np.zeros((19, 19), dtype=np.int8)
        det.update(empty, mv=(-1, -1), side_turn='known')
        r = det.update(empty, mv=(-1, -1), side_turn='known')
        self.assertIsNone(r, 'r=%r' % r)

    def test_two_passes_fires_when_board_filled(self):
        """盘面已铺开(>=10 子)时的真双虚着仍判终局。"""
        import numpy as np
        det, _ = self._det(99)
        b = np.zeros((19, 19), dtype=np.int8)
        for i in range(15):
            b[i // 19][i % 19] = 1
        det.update(b, mv=(-1, -1), side_turn='known')
        r = det.update(b, mv=(-1, -1), side_turn='known')
        self.assertEqual(r, 'two_passes')

    def test_idle_requires_hint(self):
        det, FakeStones = self._det(3)
        # 盘面不动, 但界面明确显示有人在行棋 (长考) -> 不能判终局
        for _ in range(10):
            r = det.update(FakeStones('same'), mv=None, side_turn='known')
        self.assertIsNone(r)

    def test_idle_triggers_without_turn_indicator(self):
        det, FakeStones = self._det(3)
        # 第 1 次只做初始化, 之后每次静止 +1 -> 第 4 次达到 3
        for _ in range(3):
            r = det.update(FakeStones('same'), mv=None, side_turn=None)
            self.assertIsNone(r)
        r = det.update(FakeStones('same'), mv=None, side_turn=None)
        self.assertTrue(r and r.startswith('idle_'), 'r=%r' % r)

    def test_reset_clears(self):
        det, FakeStones = self._det(2)
        det.update(FakeStones('same'), mv=None, side_turn=None)
        det.update(FakeStones('same'), mv=None, side_turn=None)
        det.reset()
        self.assertIsNone(det.update(FakeStones('same'), mv=None, side_turn=None))


if __name__ == '__main__':
    unittest.main()

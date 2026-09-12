# -*- coding: utf-8 -*-
"""controller 打劫修复集成测试 (由原 go_ai/test_ko_integration.py 迁移为 unittest).

模拟 controller 的真实链路:
  逐帧读盘(stones 快照) -> merge_move_history 累积真实落子顺序
  -> history_matches 一致性校验 -> _stones_to_board(history) 还原劫(ko)
  -> is_legal 拦截劫点回提
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import go_controller as GC          # noqa: E402
from go_engine import Board, BLACK, WHITE, N   # noqa: E402


def snapshot(board):
    """Board -> numpy 19x19 stones (模拟截屏读盘结果)。"""
    a = np.zeros((N, N), dtype=np.int8)
    for r in range(N):
        for c in range(N):
            a[r][c] = board.g[r][c]
    return a


# 一局真实落子序列, 最后黑提劫 (劫点 (1,1), 约束白方)
REAL_MOVES = [
    (1, 0, BLACK), (2, 0, WHITE), (0, 1, BLACK), (1, 1, WHITE),
    (1, 2, BLACK), (3, 1, WHITE), (5, 5, BLACK), (2, 2, WHITE),
    (2, 1, BLACK),
]


def _build_frames():
    """返回 (最终棋盘, 逐帧快照列表, 累积历史)。"""
    b = Board()
    frames = [snapshot(b)]          # 第 0 帧: 空盘
    for c, r, color in REAL_MOVES:
        ok, _, _ = b.play(c, r, color)
        assert ok, '构造对局失败 @ (%d,%d)' % (c, r)
        frames.append(snapshot(b))
    hist = []
    prev = None
    for stones in frames:
        GC.merge_move_history(hist, prev, stones)
        prev = stones
    return b, frames, hist


class TestKoIntegration(unittest.TestCase):

    def test_built_game_final_ko(self):
        b, _, _ = _build_frames()
        self.assertEqual(b.ko, (1, 1))
        self.assertEqual(b.ko_color, WHITE)

    def test_merge_move_history_matches_real_moves(self):
        _, _, hist = _build_frames()
        self.assertEqual(hist, REAL_MOVES)

    def test_history_matches_detects_missing_move(self):
        _, frames, hist = _build_frames()
        self.assertTrue(GC.history_matches(hist, frames[-1]))
        bad = hist[:5] + hist[6:]        # 漏读一手
        self.assertFalse(GC.history_matches(bad, frames[-1]))

    def test_stones_to_board_restores_ko(self):
        _, frames, hist = _build_frames()
        tb = GC._stones_to_board(frames[-1], BLACK, hist)
        self.assertEqual(tb.ko, (1, 1))
        self.assertEqual(tb.ko_color, WHITE)
        self.assertFalse(tb.is_legal(1, 1, WHITE))   # 拦截回提
        self.assertTrue(tb.is_legal(1, 1, BLACK))
        # 退化路径: 无历史 -> 静态快照无 ko
        sb = GC._stones_to_board(frames[-1], BLACK, None)
        self.assertIsNone(sb.ko)

    def test_partial_history_inconsistent(self):
        _, frames, _ = _build_frames()
        hist2 = []
        prev2 = None
        for stones in frames[4:]:        # 中途启动, 只看到后 5 手
            GC.merge_move_history(hist2, prev2, stones)
            prev2 = stones
        self.assertFalse(GC.history_matches(hist2, frames[-1]))


if __name__ == '__main__':
    unittest.main()

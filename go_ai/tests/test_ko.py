# -*- coding: utf-8 -*-
"""打劫(ko)规则单元测试 (由原 go_ai/test_ko.py 迁移为 unittest).

构造经典劫形, 验证:
1. 提子后正确产生劫点与劫点约束方
2. is_legal 能拦截被提方的立即回提
3. 找劫材后劫自动解除, 可正常消劫
4. 非劫形的普通提子不应产生劫点
5. 按真实落子顺序(history)重放棋盘能还原正确的 ko 状态
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from go_engine import Board, BLACK, WHITE, EMPTY, N  # noqa: E402


def make_ko_board():
    """构造教科书劫形.

        c0 c1 c2 c3
    r0   .  B  W  .
    r1   B  W  .  W      <- 黑下 (2,1) 可提掉白 (1,1)
    r2   .  B  W  .

    黑下 (2,1) 后: 提白 (1,1) 一子, 黑 (2,1) 只剩一气 (1,1) -> 形成劫,
    劫点 = (1,1), 约束方 = 白 (被提方不能立即回提)。
    """
    b = Board()
    b.g = [[EMPTY] * N for _ in range(N)]
    b.g[0][1] = BLACK
    b.g[0][2] = WHITE
    b.g[1][0] = BLACK
    b.g[1][1] = WHITE
    b.g[1][3] = WHITE
    b.g[2][1] = BLACK
    b.g[2][2] = WHITE
    b.turn = BLACK
    return b


class TestKoRules(unittest.TestCase):

    def test_capture_creates_ko(self):
        b = make_ko_board()
        ok, cap, ko = b.play(2, 1, BLACK)
        self.assertTrue(ok)
        self.assertEqual(cap, 1)
        self.assertEqual(ko, (1, 1))
        self.assertEqual(b.ko_color, WHITE)

    def test_is_legal_blocks_immediate_retake(self):
        b = make_ko_board()
        b.play(2, 1, BLACK)
        self.assertFalse(b.is_legal(1, 1, WHITE))   # 被提方不能立即回提
        self.assertTrue(b.is_legal(1, 1, BLACK))    # 非被提方可以
        self.assertTrue(b.is_legal(10, 10, WHITE))

    def test_ko_threat_lifts_ko(self):
        b = make_ko_board()
        b.play(2, 1, BLACK)
        b2 = b.clone()
        ok2, _, _ = b2.play(10, 10, WHITE)          # 白找劫材
        self.assertTrue(ok2)
        self.assertIsNone(b2.ko)
        self.assertIsNone(b2.ko_color)
        self.assertTrue(b2.is_legal(1, 1, BLACK))

    def test_normal_capture_not_ko(self):
        b3 = Board()
        b3.g = [[EMPTY] * N for _ in range(N)]
        # g[row][col]: 白 (c=5,r=5) 三面被黑围, 唯一气在 (c=5,r=6)
        b3.g[5][4] = BLACK
        b3.g[5][6] = BLACK
        b3.g[4][5] = BLACK
        b3.g[5][5] = WHITE
        b3.turn = BLACK
        ok3, cap3, ko3 = b3.play(5, 6, BLACK)
        self.assertEqual((ok3, cap3), (True, 1))
        self.assertIsNone(ko3)
        self.assertIsNone(b3.ko_color)

    def test_replay_history_restores_ko(self):
        hist = [
            (1, 0, BLACK), (2, 0, WHITE), (0, 1, BLACK), (1, 1, WHITE),
            (1, 2, BLACK), (3, 1, WHITE), (5, 5, BLACK), (2, 2, WHITE),
        ]
        replay = Board()
        for (c, r, color) in hist:
            ok_r, _, _ = replay.play(c, r, color)
            self.assertTrue(ok_r, 'replay failed at (%d,%d)' % (c, r))
        self.assertIsNone(replay.ko)
        ok_last, cap_last, ko_last = replay.play(2, 1, BLACK)   # 黑提劫
        self.assertTrue(ok_last)
        self.assertEqual(cap_last, 1)
        self.assertEqual(replay.ko, (1, 1))
        self.assertEqual(replay.ko_color, WHITE)
        self.assertFalse(replay.is_legal(1, 1, WHITE))
        self.assertTrue(replay.is_legal(10, 10, WHITE))


if __name__ == '__main__':
    unittest.main()

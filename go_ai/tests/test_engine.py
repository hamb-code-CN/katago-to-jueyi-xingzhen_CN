# -*- coding: utf-8 -*-
"""规则引擎回归测试 (纯逻辑, 不需要屏幕/引擎)。

运行: python -m unittest discover -s go_ai/tests -t .
  或: cd go_ai && python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from go_engine import Board, BLACK, WHITE, EMPTY, N, final_score, atari_groups  # noqa: E402


def board_from(rows, turn=BLACK):
    """用字符画建盘: '.'空 'X'黑 'O'白 (左上角为第 0 行第 0 列)"""
    b = Board()
    b.g = [[EMPTY] * N for _ in range(N)]
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch == 'X':
                b.g[r][c] = BLACK
            elif ch == 'O':
                b.g[r][c] = WHITE
    b.turn = turn
    return b


def count(board, color):
    return sum(1 for r in range(N) for c in range(N) if board.g[r][c] == color)


class TestBasicRules(unittest.TestCase):
    def test_place_and_occupied(self):
        b = Board()
        ok, cap, ko = b.play(3, 3, BLACK)
        self.assertTrue(ok)
        self.assertEqual((cap, ko), (0, None))
        self.assertEqual(b.g[3][3], BLACK)
        # 同一位置不能再下
        self.assertFalse(b.play(3, 3, WHITE)[0])
        # 落子后轮到对方
        self.assertEqual(b.turn, WHITE)

    def test_out_of_range(self):
        b = Board()
        self.assertFalse(b.play(-1, 5, BLACK)[0])
        self.assertFalse(b.play(19, 5, BLACK)[0])
        self.assertFalse(b.play(5, 19, BLACK)[0])

    def test_capture_single_stone(self):
        # 白子被黑三面包围, 黑补最后一口气 -> 提 1 子
        b = board_from([
            '.X.',
            'XO.',
            '.X.',
        ], turn=BLACK)
        ok, cap, _ = b.play(2, 1, BLACK)
        self.assertTrue(ok)
        self.assertEqual(cap, 1)
        self.assertEqual(b.g[1][1], EMPTY)
        self.assertEqual(b.caps_w, 1)     # 黑提白 -> 计入 caps_w

    def test_capture_group(self):
        # 一条 2 子白龙只剩 1 气 (3,1), 黑落子提 2 子
        b = board_from([
            '.XX.',
            'XOO.',
            '.XX.',
        ], turn=BLACK)
        self.assertEqual(len(b.liberties(b.group(1, 1)[0])), 1)   # 前置: 只有一口气
        ok, cap, _ = b.play(3, 1, BLACK)
        self.assertTrue(ok)
        self.assertEqual(cap, 2)
        self.assertEqual(count(b, WHITE), 0)

    def test_suicide_rejected_and_board_restored(self):
        # 白围出的一个眼, 黑自己填进去是自杀 -> 非法且盘面不变
        b = board_from([
            'OOO',
            'O.O',
            'OOO',
        ], turn=BLACK)
        before = [row[:] for row in b.g]
        ok, cap, _ = b.play(1, 1, BLACK)
        self.assertFalse(ok)
        self.assertEqual(b.g, before)      # 必须完全还原
        self.assertFalse(b.is_legal(1, 1, BLACK))

    def test_capture_allows_descent_into_own_eye(self):
        # 若填进去能提掉对方 (不是自杀), 则合法
        b = board_from([
            'XXX',
            'X.X',
            'XXO',
        ], turn=BLACK)
        self.assertTrue(b.is_legal(1, 1, BLACK))

    def test_ko_immediate_recapture_is_illegal(self):
        """标准劫: 黑提白 1 子后, 白不能立即回提; 白他投(pass)后即可提回。"""
        b = Board()
        b.g = [[EMPTY] * N for _ in range(N)]
        # 白 (2,1) 只剩 (3,1) 一口气; (3,1) 周围 (3,0)(3,2)(4,1) 都是白
        for c, r in ((2, 0), (2, 2), (1, 1)):
            b.g[r][c] = BLACK
        for c, r in ((2, 1), (3, 0), (3, 2), (4, 1)):
            b.g[r][c] = WHITE
        b.turn = BLACK
        self.assertEqual(len(b.liberties(b.group(2, 1)[0])), 1)   # 前置: 白在打吃

        ok, cap, ko = b.play(3, 1, BLACK)
        self.assertTrue(ok)
        self.assertEqual(cap, 1)                 # 提掉白 (2,1)
        self.assertEqual(ko, (2, 1))             # 劫点 = 刚被提的位置
        self.assertEqual(b.ko_color, WHITE)      # 劫只约束被提方

        # 白不能立即回提
        self.assertFalse(b.is_legal(2, 1, WHITE))
        # 黑方自己不受限 (劫点约束的是白)
        b.turn = BLACK
        self.assertTrue(b.is_legal(2, 1, BLACK))

        # 白他投(pass) -> 劫解除, 可以提回
        b.pass_move()
        self.assertIsNone(b.ko)
        self.assertTrue(b.is_legal(2, 1, WHITE))

    def test_pass_switches_turn(self):
        b = Board()
        b.pass_move()
        self.assertEqual(b.turn, WHITE)
        self.assertEqual(b.passes, 1)

    def test_legal_moves_skips_occupied(self):
        b = Board()
        b.play(0, 0, BLACK)
        moves = b.legal_moves(WHITE)
        self.assertNotIn((0, 0), moves)
        self.assertEqual(len(moves), 361 - 1)

    def test_atari_detection(self):
        b = board_from([
            '.X.',
            'XO.',
            '.X.',
        ])
        atari = atari_groups(b, WHITE)
        self.assertEqual(len(atari), 1)
        self.assertEqual(len(atari[0][1]), 1)


class TestScoring(unittest.TestCase):
    def test_empty_board_is_tie(self):
        self.assertEqual(final_score(Board()), BLACK)   # 平局判黑收

    def test_single_stone_territory(self):
        b = board_from([
            'XXX',
            'X.X',
            'XXX',
        ])
        # 黑 8 子 + 1 目空点 -> 黑胜
        self.assertEqual(final_score(b), BLACK)

    def test_white_wins_with_more(self):
        b = board_from([
            'OOOOOOOOOO',
            'OOOOOOOOOO',
            'OOOOOOOOOO',
        ])
        self.assertEqual(final_score(b), WHITE)


if __name__ == '__main__':
    unittest.main()

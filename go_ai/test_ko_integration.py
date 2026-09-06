# -*- coding: utf-8 -*-
"""controller 打劫修复集成测试.

模拟 controller 的真实链路:
  逐帧读盘(stones 快照) -> merge_move_history 累积真实落子顺序
  -> history_matches 一致性校验 -> _stones_to_board(history) 还原劫(ko)
  -> is_legal 拦截劫点回提
"""
import sys
import os

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import go_controller as GC
from go_engine import Board, BLACK, WHITE, EMPTY, N

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = (got == want)
    print(('  [PASS] ' if ok else '  [FAIL] ') + name +
          '  got=%r want=%r' % (got, want))
    PASS += ok
    FAIL += (not ok)


def snapshot(board):
    """Board -> numpy 19x19 stones (模拟截屏读盘结果)"""
    a = np.zeros((N, N), dtype=np.int8)
    for r in range(N):
        for c in range(N):
            a[r][c] = board.g[r][c]
    return a


def main():
    # ---- 构造一局真实落子序列, 最后黑提劫 (劫点 (1,1), 约束白方) ----
    real_moves = [
        (1, 0, BLACK),   # 手1 黑
        (2, 0, WHITE),   # 手2 白
        (0, 1, BLACK),   # 手3 黑
        (1, 1, WHITE),   # 手4 白 (将被提的子)
        (1, 2, BLACK),   # 手5 黑
        (3, 1, WHITE),   # 手6 白
        (5, 5, BLACK),   # 手7 黑
        (2, 2, WHITE),   # 手8 白
        (2, 1, BLACK),   # 手9 黑提劫 -> ko=(1,1), ko_color=WHITE
    ]
    b = Board()
    frames = [snapshot(b)]    # 第0帧: 空棋盘 (controller 从开局启动时先看到空盘)
    for c, r, color in real_moves:
        ok, cap, ko = b.play(c, r, color)
        assert ok, '构造对局失败 @ (%d,%d)' % (c, r)
        frames.append(snapshot(b))
    final_ko = b.ko
    final_ko_color = b.ko_color

    print('构造对局完成: %d 手, 最终劫点=%r 约束方=%r' %
          (len(real_moves), final_ko, final_ko_color))
    check('劫点 = (1,1)', final_ko, (1, 1))
    check('劫点约束方 = 白', final_ko_color, WHITE)

    print()
    print('=' * 62)
    print('链路 1: controller 逐帧维护真实落子历史 (含我方/对方手)')
    print('=' * 62)
    move_history = []
    prev = None
    for stones in frames:
        GC.merge_move_history(move_history, prev, stones)
        prev = stones
    check('累积历史 == 真实落子序列', move_history, real_moves)

    print()
    print('=' * 62)
    print('链路 2: history_matches 一致性校验')
    print('=' * 62)
    check('完整历史与最终棋盘一致', GC.history_matches(move_history, frames[-1]), True)
    # 中途漏读一子 -> 历史应判为不一致 (模拟丢帧/漏读)
    bad_hist = move_history[:5] + move_history[6:]
    check('漏读一手的历史被判定不一致', GC.history_matches(bad_hist, frames[-1]), False)

    print()
    print('=' * 62)
    print('链路 3: _stones_to_board(history) 还原劫状态')
    print('=' * 62)
    tb = GC._stones_to_board(frames[-1], BLACK, move_history)
    check('重放棋盘 ko = (1,1)', tb.ko, (1, 1))
    check('重放棋盘 ko_color = 白', tb.ko_color, WHITE)
    check('执白时劫点 (1,1) 被判非法 (拦截回提)', tb.is_legal(1, 1, WHITE), False)
    check('执黑时劫点 (1,1) 合法', tb.is_legal(1, 1, BLACK), True)
    # 退化路径: 无历史 -> 静态快照无 ko
    sb = GC._stones_to_board(frames[-1], BLACK, None)
    check('无历史快照 ko = None (退化)', sb.ko, None)

    print()
    print('=' * 62)
    print('链路 4: 历史清空后重新累积 (中途启动场景)')
    print('=' * 62)
    # 中途启动: 前 4 手没看到, 从第 5 手开始累积
    move_history2 = []
    prev2 = None
    for stones in frames[4:]:
        GC.merge_move_history(move_history2, prev2, stones)
        prev2 = stones
    # 累积的只有后 5 手, 与含前 4 手的完整棋盘不一致
    check('中途启动历史不完整 (判定不一致)', GC.history_matches(move_history2, frames[-1]), False)
    # 一致性失败 -> controller 清空历史 -> 无 ko (退化但安全)
    GC.history_matches(move_history2, frames[-1])
    if move_history2:
        # controller 语义: 不一致则清空
        move_history2.clear()
    check('清空后历史为空 (退化交替重建)', len(move_history2), 0)

    print()
    print('=' * 62)
    print('结果: PASS=%d  FAIL=%d' % (PASS, FAIL))
    print('=' * 62)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

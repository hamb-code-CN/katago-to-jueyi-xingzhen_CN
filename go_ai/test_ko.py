# -*- coding: utf-8 -*-
"""打劫(ko)逻辑单元测试.

构造经典劫形, 验证:
1. 提子后正确产生劫点与劫点约束方
2. is_legal 能拦截被提方的立即回提
3. 找劫材后劫自动解除, 可正常消劫
4. 按真实落子顺序(history)重放棋盘能还原正确的 ko 状态
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from go_engine import Board, BLACK, WHITE, EMPTY, N

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = (got == want)
    print(('  [PASS] ' if ok else '  [FAIL] ') + name +
          '  got=%r want=%r' % (got, want))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def make_ko_board():
    """构造教科书劫形.

        c0 c1 c2 c3
    r0   .  B  W  .
    r1   B  W  .  W      <- 黑下 (2,1) 可提掉白 (1,1)
    r2   .  B  W  .

    黑下 (2,1) 后:
      - 提掉白 (1,1) 一子
      - 黑 (2,1) 只剩一气 = (1,1)  (其余三面 (3,1)(2,0)(2,2) 均为白)
      - 白若下 (1,1) 即可提回黑 (2,1) -> 形成劫
      - 故劫点 = (1,1), 约束方 = 白 (被提方不能立即回提)
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


def main():
    print('=' * 62)
    print('测试 1: 提子后正确产生劫点与劫点约束方')
    print('=' * 62)
    b = make_ko_board()
    ok, cap, ko = b.play(2, 1, BLACK)
    check('黑下 (2,1) 落子成功', ok, True)
    check('提子数 = 1', cap, 1)
    check('劫点 = (1,1)', ko, (1, 1))
    check('劫点约束方 = 白(被提方)', b.ko_color, WHITE)

    print()
    print('=' * 62)
    print('测试 2: is_legal 拦截被提方的立即回提 (核心修复点)')
    print('=' * 62)
    check('白不能立即下劫点 (1,1) [回提]', b.is_legal(1, 1, WHITE), False)
    check('黑可以下劫点 (1,1) [非被提方]', b.is_legal(1, 1, BLACK), True)
    check('白可以下其它空点 (10,10)', b.is_legal(10, 10, WHITE), True)

    print()
    print('=' * 62)
    print('测试 3: 找劫材后劫解除, 可正常消劫')
    print('=' * 62)
    b2 = b.clone()
    ok2, _, _ = b2.play(10, 10, WHITE)  # 白找劫材
    check('白下劫材成功', ok2, True)
    check('劫点已清除', b2.ko, None)
    check('劫点约束方已清除', b2.ko_color, None)
    check('黑可下 (1,1) 消劫', b2.is_legal(1, 1, BLACK), True)

    print()
    print('=' * 62)
    print('测试 4: 非劫形的普通提子不应产生劫点')
    print('=' * 62)
    b3 = Board()
    b3.g = [[EMPTY] * N for _ in range(N)]
    # 注意: g[row][col], 即 g[r][c]
    # 白一子在 (c=5, r=5), 三面被黑包围, 唯一气在 (c=5, r=6)
    b3.g[5][4] = BLACK   # (c=4, r=5)
    b3.g[5][6] = BLACK   # (c=6, r=5)
    b3.g[4][5] = BLACK   # (c=5, r=4)
    b3.g[5][5] = WHITE   # (c=5, r=5) 白一子
    b3.turn = BLACK
    # 黑下在白唯一的气点 (c=5, r=6)
    ok3, cap3, ko3 = b3.play(5, 6, BLACK)
    check('黑下 (5,6) 提一子', (ok3, cap3), (True, 1))
    check('提子后黑子多气 -> 不是劫', ko3, None)
    check('劫点约束方 = None', b3.ko_color, None)

    print()
    print('=' * 62)
    print('测试 5: 按真实落子顺序重放能还原 ko 状态 (controller 用)')
    print('=' * 62)
    # 从空盘按真实落子顺序重放 (黑先, 黑白交替), 最后一手黑提劫
    # 坐标均为 (col, row)
    hist = [
        (1, 0, BLACK),   # 对应 g[0][1]
        (2, 0, WHITE),   # 对应 g[0][2]
        (0, 1, BLACK),   # 对应 g[1][0]
        (1, 1, WHITE),   # 对应 g[1][1]  (将被提掉的子)
        (1, 2, BLACK),   # 对应 g[2][1]
        (3, 1, WHITE),   # 对应 g[1][3]
        (5, 5, BLACK),   # 远处补一手, 保持黑白交替
        (2, 2, WHITE),   # 对应 g[2][2]
    ]
    replay = Board()
    for (c, r, color) in hist:
        ok_r, _, _ = replay.play(c, r, color)
        if not ok_r:
            print('    !! 重放失败于 (%d,%d) color=%d' % (c, r, color))
    check('重放(提劫前)劫点 = None', replay.ko, None)
    ok_last, cap_last, ko_last = replay.play(2, 1, BLACK)  # 黑提劫
    check('黑提劫落子成功', ok_last, True)
    check('黑提劫提子数 = 1', cap_last, 1)
    check('重放"黑提劫"后劫点 = (1,1)', replay.ko, (1, 1))
    check('重放后劫点约束方 = 白', replay.ko_color, WHITE)
    check('重放后白不能立即回提', replay.is_legal(1, 1, WHITE), False)
    check('重放后白可下别处(找劫材)', replay.is_legal(10, 10, WHITE), True)

    print()
    print('=' * 62)
    print('结果: PASS=%d  FAIL=%d' % (PASS, FAIL))
    print('=' * 62)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

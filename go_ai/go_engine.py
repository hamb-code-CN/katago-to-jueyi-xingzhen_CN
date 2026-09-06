# -*- coding: utf-8 -*-
"""围棋引擎: 规则 + 启发式 + 轻量 Monte Carlo"""
import random
import time
from copy import deepcopy

N = 19
EMPTY, BLACK, WHITE = 0, 1, 2


class Board:
    __slots__ = ('g', 'ko', 'ko_color', 'turn', 'passes', 'caps_b', 'caps_w', 'history')

    def __init__(self, g=None, turn=BLACK):
        self.g = [[EMPTY] * N for _ in range(N)] if g is None else g
        self.ko = None        # 劫点 (col,row) or None: 上一手提子后被提子方不能立即回提的点
        self.ko_color = None  # 劫点约束的一方 (被提子的一方); None 表示无劫
        self.turn = turn
        self.passes = 0
        self.caps_b = 0  # 黑被提子数
        self.caps_w = 0  # 白被提子数
        self.history = []  # 落子历史 [(col,row,color, captured_count)]

    def clone(self):
        b = Board(g=[row[:] for row in self.g], turn=self.turn)
        b.ko = self.ko
        b.ko_color = self.ko_color
        b.passes = self.passes
        b.caps_b = self.caps_b
        b.caps_w = self.caps_w
        b.history = list(self.history)
        return b

    @staticmethod
    def opp(c):
        return BLACK if c == WHITE else WHITE

    def neighbors(self, c, r):
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nc, nr = c + dc, r + dr
            if 0 <= nc < N and 0 <= nr < N:
                yield nc, nr

    def group(self, c, r):
        """返回 (group set, color) 起点所在的连通块"""
        color = self.g[r][c]
        if color == EMPTY:
            return set(), color
        seen = {(c, r)}
        stack = [(c, r)]
        while stack:
            x, y = stack.pop()
            for nx, ny in self.neighbors(x, y):
                if (nx, ny) in seen:
                    continue
                if self.g[ny][nx] == color:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        return seen, color

    def liberties(self, group):
        libs = set()
        for c, r in group:
            for nc, nr in self.neighbors(c, r):
                if self.g[nr][nc] == EMPTY:
                    libs.add((nc, nr))
        return libs

    def play(self, c, r, color=None):
        """落子 c,r (col,row) - 列, 行. 返回 (ok, captured_count, new_ko)"""
        if self.g[r][c] != EMPTY:
            return False, 0, None
        if color is None:
            color = self.turn
        # 自杀/打劫检查
        opp = self.opp(color)
        # 模拟放子
        self.g[r][c] = color
        captured = 0
        # 提对方无气子
        for nc, nr in self.neighbors(c, r):
            if self.g[nr][nc] == opp:
                grp, _ = self.group(nc, nr)
                if not self.liberties(grp):
                    for x, y in grp:
                        self.g[y][x] = EMPTY
                        captured += 1
        # 自杀?
        own_grp, _ = self.group(c, r)
        if not self.liberties(own_grp):
            # 还原
            for x, y in own_grp:
                self.g[y][x] = EMPTY
            return False, 0, None
        # 打劫: 提 1 子, 自己只剩 1 气 (即刚提的位置就是自己唯一的气, 恢复后能重下)
        new_ko = None
        if captured == 1 and len(own_grp) == 1:
            # 检查自己周围是否只有被提的那个点为气
            libs = self.liberties(own_grp)
            # 注意: 此处不再用 (c,r)-(0,0) 的无意义元组运算;
            # ko 判定依赖下方更完整的逻辑 (若落子后仅1气且能回提, 才记为劫)
            pass
        # 更直接的 ko: 如果只提了 1 子, 且那个位置是唯一气, 则 ko = 被提点
        if captured == 1 and len(own_grp) == 1:
            # 找被提子位置
            for nc, nr in self.neighbors(c, r):
                if (nc, nr) != (c, r) and False:
                    pass
            # 直接: 找邻接 opp 群中, 只有一个子被提的
            for nc, nr in self.neighbors(c, r):
                if (nc, nr) == (c, r):
                    continue
            # 简化: 把刚提的对方子位置记下 (BFS 反查)
            # 实际上 ko 的标准定义: 提 1 子后, 对方能立即在那个位置落子提回同样的形状
            # 简单判断: 提了 1 子, 且落子后该子是 1 气 (即新位置有 1 气 = 对方能提的点)
            libs = self.liberties(own_grp)
            if len(libs) == 1:
                # 找出被提的对方子位置
                ko_point = None
                for nc, nr in self.neighbors(c, r):
                    if self.g[nr][nc] == opp:
                        continue
                    # 检查这个点是不是被提后变成空的
                    if 0 <= nc < N and 0 <= nr < N and self.g[nr][nc] == EMPTY:
                        # 可能是 ko 点 (刚提掉的位置)
                        # 验证: 若在此点放 opp 色, 是否能提回 own_grp
                        if self._is_ko_point(c, r, nc, nr, opp):
                            ko_point = (nc, nr)
                if ko_point:
                    new_ko = ko_point

        # 切换行棋方, 计数
        if color == BLACK:
            self.caps_w += captured
        else:
            self.caps_b += captured
        self.ko = new_ko
        # 劫点只约束"刚被提子的一方" (即对手 opp): 他不能立即回提
        self.ko_color = None if new_ko is None else opp
        self.turn = opp
        self.passes = 0
        self.history.append((c, r, color, captured))
        return True, captured, new_ko

    def _is_ko_point(self, bc, br, kc, kr, color):
        """模拟在 (kc,kr) 放 color, 是否能立即提回 (bc,br) 群 (1 子)"""
        if self.g[kr][kc] != EMPTY:
            return False
        self.g[kr][kc] = color
        for nc, nr in self.neighbors(kc, kr):
            if self.g[nr][nc] == self.opp(color):
                grp, _ = self.group(nc, nr)
                if (bc, br) in grp and len(grp) == 1 and not self.liberties(grp):
                    self.g[kr][kc] = EMPTY
                    return True
        self.g[kr][kc] = EMPTY
        return False

    def pass_move(self):
        self.passes += 1
        self.turn = self.opp(self.turn)
        self.ko = None
        self.ko_color = None
        self.history.append((-1, -1, self.opp(self.turn), 0))

    def is_legal(self, c, r, color=None):
        """是否合法 (不修改状态).
        劫(ko)判定要点: 劫点是"上一手提子后, 被提方不能立即回提"的那个点,
        因此必须检查**本局面已存在的** self.ko, 而不是落子之后新产生的 ko.
        (旧实现检查 b2.ko==(c,r) 恒不成立 -> 劫点永远被误判为合法,
         导致 AI 反复强行走劫点, 被客户端拒绝后死循环)"""
        if c == -1 and r == -1:
            return True
        if self.g[r][c] != EMPTY:
            return False
        if color is None:
            color = self.turn
        # ko: 劫点存在且约束的正是当前落子方 -> 禁止立即回提
        if self.ko is not None and (c, r) == self.ko:
            if self.ko_color is None or self.ko_color == color:
                return False
        b2 = self.clone()
        ok, cap, _ = b2.play(c, r, color)
        if not ok:
            return False
        return True

    def legal_moves(self, color=None):
        if color is None:
            color = self.turn
        out = []
        for r in range(N):
            for c in range(N):
                if self.g[r][c] != EMPTY:
                    continue
                if self.is_legal(c, r, color):
                    out.append((c, r))
        return out


# ===== 评估 =====

def atari_groups(board, color):
    """返回 color 的处于打吃状态的 (group, liberties) 列表"""
    seen = set()
    res = []
    for r in range(N):
        for c in range(N):
            if board.g[r][c] != color or (c, r) in seen:
                continue
            grp, _ = board.group(c, r)
            seen |= grp
            libs = board.liberties(grp)
            if len(libs) == 1:
                res.append((grp, libs))
    return res


def heuristic_score(board, c, r, color):
    """对单步着法给启发分 (粗略评估)"""
    score = 0.0
    opp = Board.opp(color)
    # 模拟
    nb = board.clone()
    ok, cap, _ = nb.play(c, r, color)
    if not ok:
        return -1e9
    score += cap * 12  # 提子
    # 救己方打吃
    my_atari_before = sum(1 for grp, _ in atari_groups(board, color) if (c, r) in grp)
    score += my_atari_before * 8
    # 攻击对方打吃: 落子后对方有打吃群?
    opp_atari = sum(1 for grp, libs in atari_groups(nb, opp))
    score += opp_atari * 5
    # 自身气数
    grp, _ = nb.group(c, r)
    score += len(nb.liberties(grp)) * 0.6
    # 距已有子近 (扩展/防守)
    near = 0
    for nc, nr in [(c-1, r), (c+1, r), (c, r-1), (c, r+1)]:
        if 0 <= nc < N and 0 <= nr < N and board.g[nr][nc] != EMPTY:
            near += 1
    score += near * 0.4
    # 离对方弱子近 (靠近可攻击位置)
    weak = 0
    for r2 in range(max(0, r-2), min(N, r+3)):
        for c2 in range(max(0, c-2), min(N, c+3)):
            if board.g[r2][c2] == opp:
                grp, _ = board.group(c2, r2)
                libs = board.liberties(grp)
                if len(libs) <= 2:
                    weak += (3 - len(libs)) * 0.5
    score += weak
    return score


# ===== Monte Carlo 模拟 =====

def random_playout(board, start_color, max_moves=200, rng=None):
    """快速随机模拟: 随机选空点 -> 尝试落子, 失败就 pass.
    大幅简化以保证在 30s 预算内能做尽可能多的 playout."""
    rng = rng or random
    b = board.clone()
    color = start_color
    passes = 0
    for _ in range(max_moves):
        # 收集空点 (一次 N^2 但只 361)
        empties = []
        g = b.g
        for r in range(N):
            row = g[r]
            for c in range(N):
                if row[c] == EMPTY:
                    empties.append((c, r))
        if not empties:
            return final_score(b)
        # 加权: 靠近已有子的位置更可能被选
        # 用一个简单 trick: 从 empties 中按权重采样
        # 但 361 个点全采样很慢, 这里直接 random.choice + 失败则再选
        for _try in range(6):
            c, r = rng.choice(empties)
            ok, _, _ = b.play(c, r, color)
            if ok:
                break
        else:
            b.pass_move()
            passes += 1
            if passes >= 2:
                break
            continue
        passes = 0
        color = Board.opp(color)
    return final_score(b)


def final_score(board):
    """估算胜负: 数双方棋子 + 简化领地 (被棋子相邻的空点算谁的)"""
    s_black = 0
    s_white = 0
    visited = [[False] * N for _ in range(N)]
    for r in range(N):
        for c in range(N):
            if board.g[r][c] == BLACK:
                s_black += 1
            elif board.g[r][c] == WHITE:
                s_white += 1
            elif not visited[r][c]:
                # flood fill 空地区域, 边界颜色
                stack = [(c, r)]
                region = []
                touches = set()
                visited[r][c] = True
                while stack:
                    x, y = stack.pop()
                    region.append((x, y))
                    for nx, ny in Board.neighbors(None, x, y) if False else [(x+1, y), (x-1, y), (x, y+1), (x, y-1)]:
                        if 0 <= nx < N and 0 <= ny < N and not visited[ny][nx]:
                            if board.g[ny][nx] == EMPTY:
                                visited[ny][nx] = True
                                stack.append((nx, ny))
                            else:
                                touches.add(board.g[ny][nx])
                if touches == {BLACK}:
                    s_black += len(region)
                elif touches == {WHITE}:
                    s_white += len(region)
    # 加提子
    s_black += board.caps_b
    s_white += board.caps_w
    if s_black > s_white:
        return BLACK
    elif s_white > s_black:
        return WHITE
    return BLACK  # 平黑收


def select_move(board, my_color, time_budget=25.0, top_k=20, rng=None):
    """主入口: 返回 (col, row) 或 (-1, -1) 表示 pass.
    时间预算内: 启发式排序 + 对 top_k 做 Monte Carlo."""
    rng = rng or random
    t0 = time.time()
    moves = board.legal_moves(my_color)
    if not moves:
        return (-1, -1)
    # 第一手/很少子时: 天元或星位
    stone_count = sum(1 for r in range(N) for c in range(N) if board.g[r][c] != EMPTY)
    if stone_count < 6:
        # 开局: 选启发分最高
        scores = [(heuristic_score(board, c, r, my_color), c, r) for c, r in moves]
        scores.sort(reverse=True)
        return (scores[0][1], scores[0][2])

    # 启发评分, 取 top_k
    scores = [(heuristic_score(board, c, r, my_color), c, r) for c, r in moves]
    scores.sort(reverse=True)
    candidates = scores[:top_k]

    # 对 top_k 做 MC, 收集胜率
    elapsed = time.time() - t0
    remaining = max(1.0, time_budget - elapsed)
    per_budget = remaining / max(1, len(candidates))
    playouts_each = []
    for sc, c, r in candidates:
        # 估算每个 playout 耗时
        t1 = time.time()
        nb = board.clone()
        ok, _, _ = nb.play(c, r, my_color)
        if not ok:
            playouts_each.append((c, r, 0.0))
            continue
        wins = 0
        n = 0
        t2 = time.time()
        single_t = max(0.001, t2 - t1)
        max_n = int(per_budget / single_t)
        max_n = min(max_n, 2000)
        for _ in range(max_n):
            try:
                res = random_playout(nb, Board.opp(my_color), rng=rng)
            except Exception:
                break
            if res == my_color:
                wins += 1
            n += 1
            if time.time() - t2 > per_budget * 0.9:
                break
        rate = wins / max(1, n)
        playouts_each.append((c, r, rate, n))
    # 综合: 启发分 (归一) + 胜率
    max_h = max(abs(s) for s, _, _ in candidates) or 1
    best = None
    best_score = -1e9
    for (sc, c, r), item in zip(candidates, playouts_each):
        if len(item) == 3:
            mc = 0.5
        else:
            mc = item[2]
        h_norm = sc / max_h
        combined = 0.5 * h_norm + 1.0 * mc
        if combined > best_score:
            best_score = combined
            best = (c, r)
    return best

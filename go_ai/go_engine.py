# -*- coding: utf-8 -*-
"""围棋引擎: 规则 + 启发式 + 轻量 Monte Carlo

用途: KataGo 不可用时的**兜底**引擎 (棋力弱, 仅保证不卡死)。
v1.0.3 清理: 删掉 play() 里的空操作分支与 final_score 的死代码, 并给选点加了
邻域候选过滤 (原实现对全部 361 个点都做一遍完整启发评估, 空盘周围的下法还全是废着)。
"""
import random
import time

N = 19
EMPTY, BLACK, WHITE = 0, 1, 2


class Board:
    __slots__ = ('g', 'ko', 'ko_color', 'turn', 'passes', 'caps_b', 'caps_w', 'history')

    def __init__(self, g=None, turn=BLACK):
        self.g = [[EMPTY] * N for _ in range(N)] if g is None else g
        self.ko = None        # 劫点 (col,row): 上一手提子后, 被提子方不能立即回提的点
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
        """落子 (col,row) - 列, 行. 返回 (ok, captured_count, new_ko)"""
        if not (0 <= c < N and 0 <= r < N):
            return False, 0, None
        if self.g[r][c] != EMPTY:
            return False, 0, None
        if color is None:
            color = self.turn
        opp = self.opp(color)
        self.g[r][c] = color

        # 提对方无气子
        captured_pts = []
        for nc, nr in self.neighbors(c, r):
            if self.g[nr][nc] == opp:
                grp, _ = self.group(nc, nr)
                if not self.liberties(grp):
                    for x, y in grp:
                        self.g[y][x] = EMPTY
                        captured_pts.append((x, y))
        captured = len(captured_pts)

        # 自杀? (提子后自己仍无气 -> 非法, 还原)
        own_grp, _ = self.group(c, r)
        own_libs = self.liberties(own_grp)
        if not own_libs:
            for x, y in own_grp:
                self.g[y][x] = EMPTY
            for x, y in captured_pts:   # 防御性还原 (实际 captured 为 0 时才会走到这里)
                self.g[y][x] = opp
            return False, 0, None

        # 劫: 只提 1 子, 自己只有 1 气且那口气就是刚提掉的点, 且对方确实能立即回提
        new_ko = None
        if captured == 1 and len(own_grp) == 1 and len(own_libs) == 1:
            kr, kc = captured_pts[0][1], captured_pts[0][0]
            if (kc, kr) in own_libs and self._is_ko_point(c, r, kc, kr, opp):
                new_ko = (kc, kr)

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
        """模拟在 (kc,kr) 放 color, 是否能立即提回 (bc,br) 的 1 子群"""
        if self.g[kr][kc] != EMPTY:
            return False
        self.g[kr][kc] = color
        hit = False
        for nc, nr in self.neighbors(kc, kr):
            if self.g[nr][nc] == self.opp(color):
                grp, _ = self.group(nc, nr)
                if (bc, br) in grp and len(grp) == 1 and not self.liberties(grp):
                    hit = True
                    break
        self.g[kr][kc] = EMPTY
        return hit

    def pass_move(self):
        self.passes += 1
        self.turn = self.opp(self.turn)
        self.ko = None
        self.ko_color = None
        self.history.append((-1, -1, self.opp(self.turn), 0))

    def is_legal(self, c, r, color=None):
        """是否合法 (不修改状态)。
        劫(ko)判定要点: 劫点是"上一手提子后, 被提方不能立即回提"的那个点,
        因此必须检查**本局面已存在的** self.ko, 而不是落子之后新产生的 ko。
        (旧实现检查落子后的新 ko 恒不成立 -> 劫点永远被误判为合法,
         导致 AI 反复强行走劫点, 被客户端拒绝后死循环)"""
        if c == -1 and r == -1:
            return True
        if not (0 <= c < N and 0 <= r < N):
            return False
        if self.g[r][c] != EMPTY:
            return False
        if color is None:
            color = self.turn
        if self.ko is not None and (c, r) == self.ko:
            if self.ko_color is None or self.ko_color == color:
                return False
        b2 = self.clone()
        return b2.play(c, r, color)[0]

    def legal_moves(self, color=None):
        if color is None:
            color = self.turn
        out = []
        for r in range(N):
            row = self.g[r]
            for c in range(N):
                if row[c] != EMPTY:
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


def build_context(board, color):
    """预计算一次评估上下文, 避免在 361 个候选点上重复扫描全盘 (原来的主要耗时)。"""
    my_atari = atari_groups(board, color)
    # 我方被打吃的棋子的"气"位置: 落在这些点可以紧气/延气
    my_atari_libs = set()
    for _grp, libs in my_atari:
        my_atari_libs |= libs
    return {'my_atari_libs': my_atari_libs}


def heuristic_score(board, c, r, color, ctx=None):
    """对单步着法给启发分 (粗略评估)。ctx 由 build_context 预计算。"""
    score = 0.0
    opp = Board.opp(color)
    nb = board.clone()
    ok, cap, _ = nb.play(c, r, color)
    if not ok:
        return -1e9
    score += cap * 12                      # 提子
    if ctx and (c, r) in ctx['my_atari_libs']:
        score += 8                         # 救己方打吃 (预计算命中)
    # 攻击对方打吃: 落子后对方有打吃群?
    score += sum(1 for _g, _l in atari_groups(nb, opp)) * 5
    grp, _ = nb.group(c, r)
    score += len(nb.liberties(grp)) * 0.6  # 自身气数
    near = 0
    for nc, nr in ((c - 1, r), (c + 1, r), (c, r - 1), (c, r + 1)):
        if 0 <= nc < N and 0 <= nr < N and board.g[nr][nc] != EMPTY:
            near += 1
    score += near * 0.4                    # 距已有子近 (扩展/防守)
    weak = 0
    for r2 in range(max(0, r - 2), min(N, r + 3)):
        for c2 in range(max(0, c - 2), min(N, c + 3)):
            if board.g[r2][c2] == opp:
                grp2, _ = board.group(c2, r2)
                libs2 = board.liberties(grp2)
                if len(libs2) <= 2:
                    weak += (3 - len(libs2)) * 0.5
    score += weak                          # 离对方弱子近
    return score


# ===== Monte Carlo 模拟 =====

def random_playout(board, start_color, max_moves=200, rng=None):
    """快速随机模拟: 随机选空点 -> 尝试落子, 失败就 pass。
    大幅简化以保证在预算内能做尽可能多的 playout。"""
    rng = rng or random
    b = board.clone()
    color = start_color
    passes = 0
    empties = []
    g = b.g
    for r in range(N):
        row = g[r]
        for c in range(N):
            if row[c] == EMPTY:
                empties.append((c, r))
    removed = 0
    for _ in range(max_moves):
        if removed > 40:
            # 提子会让已移除的点重新变空, 累积到一定量后重建一次, 保证不提前终局
            empties = [(c, r) for r in range(N) for c in range(N) if g[r][c] == EMPTY]
            removed = 0
        if not empties:
            return final_score(b)
        moved = False
        for _try in range(6):
            if not empties:
                break
            idx = rng.randrange(len(empties))
            c, r = empties[idx]
            if g[r][c] != EMPTY:           # 已在他处被填/提, 惰性清理
                empties[idx] = empties[-1]
                empties.pop()
                continue
            ok, _, _ = b.play(c, r, color)
            if ok:
                moved = True
                empties[idx] = empties[-1]
                empties.pop()
                removed += 1
                break
        if not moved:
            b.pass_move()
            passes += 1
            if passes >= 2:
                break
            continue
        passes = 0
        color = Board.opp(color)
    return final_score(b)


def final_score(board):
    """估算胜负: 数双方棋子 + 简化领地 (被单色包围的空点算谁的)"""
    s_black = 0
    s_white = 0
    visited = [[False] * N for _ in range(N)]
    for r in range(N):
        for c in range(N):
            v = board.g[r][c]
            if v == BLACK:
                s_black += 1
            elif v == WHITE:
                s_white += 1
            elif not visited[r][c]:
                stack = [(c, r)]
                region = []
                touches = set()
                visited[r][c] = True
                while stack:
                    x, y = stack.pop()
                    region.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
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
    s_black += board.caps_b
    s_white += board.caps_w
    if s_black > s_white:
        return BLACK
    if s_white > s_black:
        return WHITE
    return BLACK  # 平黑收


def near_candidates(board, dist=4):
    """只保留已有棋子附近 dist 格内的空点 (围棋的标准着法剪枝)。
    空盘/棋子很少时返回 None (表示不过滤), 避免开局只能下在已有子旁边。"""
    stones = [(c, r) for r in range(N) for c in range(N) if board.g[r][c] != EMPTY]
    if not stones or len(stones) >= 300:
        return None
    near = set()
    for c, r in stones:
        for dc in range(-dist, dist + 1):
            for dr in range(-dist, dist + 1):
                nc, nr = c + dc, r + dr
                if 0 <= nc < N and 0 <= nr < N and board.g[nr][nc] == EMPTY:
                    near.add((nc, nr))
    return near or None


def select_move(board, my_color, time_budget=25.0, top_k=20, rng=None):
    """主入口: 返回 (col, row) 或 (-1, -1) 表示 pass。
    时间预算内: 启发式排序 + 对 top_k 做 Monte Carlo。"""
    rng = rng or random
    t0 = time.time()
    moves = board.legal_moves(my_color)
    if not moves:
        return (-1, -1)

    stones = sum(1 for r in range(N) for c in range(N) if board.g[r][c] != EMPTY)
    ctx = build_context(board, my_color)

    # 邻域剪枝: 只评估已有子附近的点 (原实现评估全部 361 点, 其中大量是远方的废着)
    near = near_candidates(board)
    pool = [m for m in moves if m in near] if near else moves
    if not pool:
        pool = moves
    if stones < 6:
        pool = moves          # 开局不过滤

    scores = [(heuristic_score(board, c, r, my_color, ctx), c, r) for c, r in pool]
    scores.sort(reverse=True)
    candidates = scores[:top_k]
    if not candidates:
        return (-1, -1)

    # 对 top_k 做 MC, 收集胜率
    elapsed = time.time() - t0
    remaining = max(1.0, time_budget - elapsed)
    per_budget = remaining / max(1, len(candidates))
    results = []
    for sc, c, r in candidates:
        nb = board.clone()
        ok, _, _ = nb.play(c, r, my_color)
        if not ok:
            results.append({'c': c, 'r': r, 'rate': 0.0, 'n': 0})
            continue
        wins = n = 0
        t2 = time.time()
        # 用第一个 playout 的耗时估算本候选能跑多少轮 (下限 1)
        while n < 2000:
            if time.time() - t2 > per_budget * 0.9 and n > 0:
                break
            try:
                res = random_playout(nb, Board.opp(my_color), rng=rng)
            except Exception:
                break
            if res == my_color:
                wins += 1
            n += 1
        results.append({'c': c, 'r': r, 'rate': wins / max(1, n), 'n': n})

    max_h = max(abs(s) for s, _c, _r in candidates) or 1
    best, best_score = None, -1e9
    for (sc, c, r), item in zip(candidates, results):
        h_norm = sc / max_h
        combined = 0.5 * h_norm + 1.0 * item['rate']
        if combined > best_score:
            best_score, best = combined, (c, r)
    return best if best else (-1, -1)

# -*- coding: utf-8 -*-
"""SGF 导出 (对局留档 / 复盘).

move_history 本来就是**按真实顺序**的落子表 [(col, row, color), ...], 所以导出只是
坐标转换 + 文本拼接; 没有历史时退化为 AB/AW 摆盘 (仍然可看, 只是没有手数顺序)。

SGF 坐标约定: 左上角为 aa, 先列后行; pass 写空 B[]/W[]。
"""
import os
import re
import time

SGFTIME = None  # 便于测试注入固定时间
APP_TAG = 'GoAI:AutoPlayer'   # SGF 的 AP[] 字段 (生成该棋谱的程序)

_SGF_COLS = 'abcdefghijklmnopqrs'   # 19 路
COLOR_NAME = {1: 'B', 2: 'W'}
COLOR_CN = {1: '黑', 2: '白'}


def _pt(col, row):
    """(col,row) -> SGF 坐标。越界/虚着返回 ''(pass)。"""
    if col is None or row is None:
        return ''
    c, r = int(col), int(row)
    if not (0 <= c < 19 and 0 <= r < 19):
        return ''
    return f'{_SGF_COLS[c]}{_SGF_COLS[r]}'


def _norm_color(color):
    if isinstance(color, str):
        return 'B' if color.strip().upper().startswith('B') else 'W'
    return COLOR_NAME.get(int(color), 'B')


def _esc(text):
    return str(text).replace('\\', '\\\\').replace(']', '\\]')


def moves_to_sgf(moves=None, stones=None, size=19, black='GoAI', white='对手',
                 result=None, komi=7.5, rules='Chinese', handicap=0,
                 comment=None, date=None, event='GoAI Auto-Player'):
    """生成 SGF 文本。

    moves  : [(col,row,color)] 真实落子顺序 (color: 1/2 或 'B'/'W'), 可为空
    stones : 19x19 序列 (0空/1黑/2白), 无 moves 时用 AB/AW 摆盘
    result : 如 'B+3.5' / 'W+R' / None
    """
    dt = date or (SGFTIME or time.strftime('%Y-%m-%d %H:%M:%S'))
    parts = [
        '(;GM[1]FF[4]CA[UTF-8]AP[%s]' % APP_TAG,
        f'SZ[{int(size)}]',
        'RU[%s]' % _esc(rules),
        'KM[%s]' % komi,
        'DT[%s]' % _esc(dt),
        'EV[%s]' % _esc(event),
        'PB[%s]' % _esc(black),
        'PW[%s]' % _esc(white),
    ]
    if handicap:
        parts.append('HA[%d]' % int(handicap))
    if result:
        parts.append('RE[%s]' % _esc(result))

    # 无历史 -> 摆盘
    if not moves and stones is not None:
        ab, aw = [], []
        for r in range(size):
            for c in range(size):
                v = int(stones[r][c])
                if v == 1:
                    ab.append(_pt(c, r))
                elif v == 2:
                    aw.append(_pt(c, r))
        if ab:
            parts.append('AB' + ''.join(f'[{p}]' for p in ab))
        if aw:
            parts.append('AW' + ''.join(f'[{p}]' for p in aw))
        parts.append('C[%s]' % _esc(comment or '由盘面快照导出 (无落子顺序)'))
        parts.append(')')
        return ''.join(parts)

    body = []
    n = 0
    for item in (moves or []):
        try:
            c, r, color = item[0], item[1], item[2]
        except (TypeError, IndexError, ValueError):
            continue
        p = _pt(c, r)          # 虚着 -> ''
        if p == '' and int(c) != -1:
            continue           # 非法坐标直接跳过, 不污染棋谱
        body.append(';%s[%s]' % (_norm_color(color), p))
        n += 1
    parts.append(''.join(body))
    if comment:
        parts.append('C[%s]' % _esc(comment))
    parts.append(')')
    return ''.join(parts)


def default_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'games')


def save_sgf(moves=None, stones=None, out_dir=None, name=None, **meta):
    """写 SGF 文件, 返回绝对路径 (失败抛异常, 调用方决定是否吞掉)。"""
    out_dir = out_dir or default_dir()
    os.makedirs(out_dir, exist_ok=True)
    if not name:
        name = 'game_%s.sgf' % time.strftime('%Y%m%d_%H%M%S')
    if not name.lower().endswith('.sgf'):
        name += '.sgf'
    # 防路径穿越: 只取文件名部分
    name = re.sub(r'[\\/:*?"<>|]+', '_', os.path.basename(name))
    path = os.path.join(out_dir, name)
    text = moves_to_sgf(moves=moves, stones=stones, **meta)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


if __name__ == '__main__':
    demo = [(3, 2, 1), (15, 15, 2), (-1, -1, 1), (-1, -1, 2)]
    print(moves_to_sgf(moves=demo))

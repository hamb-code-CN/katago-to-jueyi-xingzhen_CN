# -*- coding: utf-8 -*-
"""围棋 AI 自动落子主控制器
- 每 20s 截屏, 读棋盘, 决策
- 思考上限 30s, 最后 10s 完成点击+确认
- 确认前再截屏核对"""
import os
import sys
import time
import json
import argparse
import threading
# 强制 stdout/stderr 用 UTF-8 输出, 防止命令行含 ✓ 等字符时 GBK 编码报错
# (此前曾因此导致 KataGo 连接失败后回退本地启发式引擎, AI 思考失效)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.abspath(__file__))
COMMAND_FILE = os.path.join(BASE, '_command.json')  # 网页启动器的运行中指令 (重置/换色, 不重启进程)

import cv2
import numpy as np
import pyautogui
import random

from go_vision import detect_board, read_board, N, board_to_pixel, detect_turn_side, refine_pts, snap_to_cal, find_go_window, find_browser_window, capture_go_window
from go_engine import Board, select_move, BLACK, WHITE, EMPTY
try:
    from seal_turn_detect import detect_turn_side as seal_detect
    SEAL_OCR_AVAILABLE = True
except ImportError:
    SEAL_OCR_AVAILABLE = False
try:
    from avatar_turn_detect import detect_turn_side as avatar_detect, is_my_turn as avatar_is_my_turn
    AVATAR_DETECT_AVAILABLE = True
except ImportError:
    AVATAR_DETECT_AVAILABLE = False
try:
    from katago_engine import KataServiceClient, KATAGO_MODEL, KATAGO_EXE, KATAGO_CFG
    KATAGO_AVAILABLE = os.path.exists(KATAGO_EXE) and os.path.exists(KATAGO_MODEL)
except ImportError:
    KATAGO_AVAILABLE = False
# 预加载服务端口 (kata_service.py 常驻监听); 未启动服务时可正常回退本地引擎
KATA_SERVICE_PORT = 8124


# ============== 配置 ==============
class Config:
    # 屏幕分辨率 (运行时实测)
    SCREEN_W = 2560
    SCREEN_H = 1440

    # 棋盘软件在屏幕左侧
    BOARD_ROI_X_MAX = 1280  # 棋盘软件在 x<1280 区域

    # 思考/动作时间预算
    THINK_MAX = 30.0        # 最多 30s 思考
    KATAGO_MAX_TIME = 10.0  # KataGo 每手搜索上限 (用户要求 10s, 每手 ~18s)
    # 每手思考时间随机化 (防止被识别为 AI): 每手在 [MIN, MAX] 间取随机
    KATAGO_TIME_RANDOM = True   # 是否启用随机化
    KATAGO_TIME_MIN = 5.0       # 随机下限 (秒)
    KATAGO_TIME_MAX = 10.0      # 随机上限 (秒)
    CLICK_DEADLINE = 3.0    # 思考后, 整步节奏目标: 3s 内完成思考+点击 (提速模式)
    SCREENSHOT_INTERVAL = 2.0  # 截屏间隔秒数

    # 中盘低胜率权重: 非开局(≥60手) 且 我方胜率<30% 时, 延长思考时间 (最多 20s)
    WEIGHT_MIN_MOVES = 60     # 触发门槛: 棋盘总手数 (黑+白)
    WEIGHT_WINRATE = 0.30     # 胜率阈值 (0~1)
    WEIGHT_MAX_TIME = 20.0    # 延长后思考上限 (秒)
    WEIGHT_EVAL_TIME = 1.5    # 预评估时长 (秒, kata-analyze)

    # 我方颜色
    MY_COLOR = BLACK  # 默认黑, 可改为 WHITE

    # 腾讯围棋窗口 rect (窗口锁定, 拖到哪都能找到)
    GO_WINDOW = None

    # 确认按钮位置 (None 表示不点确认, 落子即提交). (x, y) 屏幕坐标
    CONFIRM_BTN = None  # 例如 (cx, cy)

    # 调试
    DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug')


def take_screenshot(path=None):
    """全屏截图 (窗口可见即可识别). 返回 (img_bgr, window_rect).
    window_rect 传 None (由调用方 board_from_screenshot 用 cfg.GO_WINDOW 限定棋盘范围)."""
    img = pyautogui.screenshot()
    if path:
        img.save(path)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), None


def board_from_screenshot(img, cfg):
    """返回 (board_dict, stones_array) 或 (None, None). img 是全屏截图,
    用 window_rect 限制只在腾讯围棋窗口内检测."""
    board = detect_board(img, roi_x_max=cfg.BOARD_ROI_X_MAX, window_rect=cfg.GO_WINDOW)
    if board is None:
        return None, None
    board = refine_pts(img, board)      # 棋子质心整体校正
    stones, _ = read_board(img, board)
    return board, stones


def stones_diff(prev, cur):
    """对比两帧, 返回新落子列表 [(col, row, color)]"""
    if prev is None or cur is None:
        return []
    diff = []
    for r in range(N):
        for c in range(N):
            if prev[r][c] != cur[r][c] and cur[r][c] != EMPTY:
                diff.append((c, r, int(cur[r][c])))
    return diff


def count_stones(stones):
    b = w = 0
    for r in range(N):
        for c in range(N):
            if stones[r][c] == BLACK:
                b += 1
            elif stones[r][c] == WHITE:
                w += 1
    return b, w


def detect_new_moves(prev, cur):
    """对比两帧, 返回新增落子 [(col,row,color), ...]. prev 为 None 时无法判断."""
    if prev is None or cur is None:
        return []
    added = []
    for r in range(N):
        for c in range(N):
            if prev[r][c] == EMPTY and cur[r][c] != EMPTY:
                added.append((c, r, int(cur[r][c])))
    return added


def merge_move_history(move_history, prev, cur):
    """把 prev->cur 的新增落子追加到真实落子历史 (用于还原劫 ko 状态).
    一帧内多个新增(漏读补全等)按黑先白后排序, 尽量贴近真实顺序.
    返回是否有新增."""
    added = detect_new_moves(prev, cur)
    if not added:
        return False
    added.sort(key=lambda x: x[2])  # BLACK=1 < WHITE=2
    move_history.extend(added)
    return True


def history_matches(move_history, stones):
    """校验 move_history 按真实顺序重放后, 是否与当前识别棋盘完全一致.
    一致 => 历史完整, 可把真实顺序同步给引擎 (KataGo 劫状态正确);
    不一致(中途启动/漏读) => 回退交替重建, 靠防死循环兜底."""
    if not move_history:
        return False
    b = Board()
    for c, r, color in move_history:
        c, r, color = int(c), int(r), int(color)
        if not (0 <= c < N and 0 <= r < N):
            return False
        if not b.play(c, r, color)[0]:
            return False
    for r in range(N):
        for c in range(N):
            if b.g[r][c] != int(stones[r][c]):
                return False
    return True


def clamp_random(lo, hi, rng=None):
    """返回 [lo, hi] 内的随机浮点数 (1位小数). 用于每手思考时间随机化."""
    if hi < lo:
        lo, hi = hi, lo
    return round((rng or random).uniform(lo, hi), 1)


_CMD_SEEN = None  # 已处理过的指令内容指纹 (防删除失败时重复执行刷屏)


def handle_command(cfg, prev_stones, last_played_by_me):
    """处理网页启动器下发的运行中指令 (_command.json, 先执行后尝试删除).
    支持:
      {"cmd":"set_color","color":"black"|"white"}  重开一局并切换执子颜色 (AI 进程不重启)
      {"cmd":"reset"}                               仅重置对局状态 (颜色不变)
    返回 (prev_stones, last_played_by_me) 可能被重置."""
    global _CMD_SEEN
    if not os.path.exists(COMMAND_FILE):
        _CMD_SEEN = None
        return prev_stones, last_played_by_me
    try:
        with open(COMMAND_FILE, encoding='utf-8') as f:
            content = f.read()
        if content == _CMD_SEEN:
            return prev_stones, last_played_by_me  # 已处理过 (删除被拦截时的幂等保护)
        _CMD_SEEN = content
        cmd = json.loads(content)
        c = cmd.get('cmd')
        if c == 'set_color':
            color = cmd.get('color')
            if color in ('black', 'white'):
                cfg.MY_COLOR = BLACK if color == 'black' else WHITE
                print(f'[指令] 重开一局, 已切换为执{"黑" if cfg.MY_COLOR == BLACK else "白"} (AI 进程未重启)')
                prev_stones, last_played_by_me = None, False
            else:
                print('[指令] set_color 颜色参数无效, 忽略')
        elif c == 'reset':
            print('[指令] 重开一局, 已重置对局状态, 重新开始分析')
            prev_stones, last_played_by_me = None, False
        elif c:
            print(f'[指令] 未知指令: {c}')
        # 尝试删除; 删除失败(如沙箱拦截)时靠 _CMD_SEEN 指纹防重复, 不影响功能
        try:
            os.unlink(COMMAND_FILE)
        except OSError:
            pass
    except Exception as e:
        print(f'[指令] 处理失败: {e}')
    return prev_stones, last_played_by_me


def decide_think_time(cfg, katago, engine_board, total_moves, cycle, per_move_time,
                      move_history=None):
    """中盘低胜率权重: 非开局(≥WEIGHT_MIN_MOVES手) 且 我方胜率 < WEIGHT_WINRATE 时,
    把思考时间延长到 min(WEIGHT_MAX_TIME, max(原时间*2, 10s)).
    返回 (最终思考时间, 是否延长)."""
    if katago is None or total_moves < cfg.WEIGHT_MIN_MOVES:
        return per_move_time, False
    try:
        katago.set_board(engine_board.g, history=move_history)
        wr = katago.get_winrate(time=cfg.WEIGHT_EVAL_TIME)
        if wr is None:
            return per_move_time, False
        if cfg.MY_COLOR == WHITE:
            wr = 1.0 - wr  # KataGo 输出为黑方胜率, 转为我方胜率
        if wr < cfg.WEIGHT_WINRATE:
            new_t = min(cfg.WEIGHT_MAX_TIME, max(per_move_time * 2.0, 10.0))
            print(f"[{cycle}] [权重] 中盘{total_moves}手 我方胜率{wr*100:.1f}%<{cfg.WEIGHT_WINRATE*100:.0f}%, "
                  f"延长思考 {per_move_time:.1f}s → {new_t:.1f}s (上限{cfg.WEIGHT_MAX_TIME:.0f}s)")
            return new_t, True
        print(f"[{cycle}] [权重] 中盘{total_moves}手 胜率{wr*100:.1f}%≥阈值, 正常思考")
    except Exception as e:
        print(f"[{cycle}] [权重] 胜率评估失败: {e}")
    return per_move_time, False


def is_my_turn_from_img(img, my_color, window_rect=None, platform='tencent'):
    """用界面视觉识别轮次. platform 控制策略:
      'tencent' : 红章 OCR 识别顶部栏 '黑方行棋/白方行棋' (seal_turn_detect)
      'xingzhen': 头像蓝水滴检测 (avatar_turn_detect) - 不需 OCR, <10ms
    红章/水滴固定显示当前行棋方, 不受子数法在提子后失效的影响.
    window_rect 传入窗口位置后按窗口比例定位 (窗口拖动/缩放也能识别).
    返回 True/False 表示是否轮到我方. 识别不到时返回 None (交由子数法回退)."""
    if platform == 'xingzhen':
        if not AVATAR_DETECT_AVAILABLE or window_rect is None:
            return None
        return avatar_is_my_turn(img, my_color, window_rect)
    # tencent (默认)
    if not SEAL_OCR_AVAILABLE:
        return None
    side = seal_detect(img, window_rect=window_rect)
    if side is None:
        return None
    if side == 'B':
        return my_color == BLACK
    if side == 'W':
        return my_color == WHITE
    return None


def is_my_turn(prev_stones, cur_stones, my_color, last_played_by_me):
    """判断是否轮到我. 规则: 黑先手, 黑子数<=白子数 轮到黑, 否则轮到白.
    提子不影响判断: 黑吃白 -> nb>nw 轮到白; 白吃黑 -> nb<nw 轮到黑.
    但子数法在预设局面(常见问题/死活题)失效, 此时用 UI 红章更可靠.
    返回 None 表示让调用方用其它方法判断."""
    nb = int((cur_stones == BLACK).sum())
    nw = int((cur_stones == WHITE).sum())
    return my_color == (BLACK if nb <= nw else WHITE)


def click_board(board, col, row):
    """落子: board pts 已是全屏坐标 (detect_board window_rect 模式加过 ox/oy), 不再加窗口偏移."""
    x, y = board_to_pixel(board, col, row)
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.click()
    return x, y


def click_confirm(cfg):
    if cfg.CONFIRM_BTN is None:
        return False
    x, y = cfg.CONFIRM_BTN
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.click()
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--color', choices=['black', 'white'], default='black',
                   help='我方颜色 (默认黑)')
    p.add_argument('--confirm', default=None,
                   help='确认按钮 x,y 坐标 (例如 100,900). 留空则不点确认')
    p.add_argument('--interval', type=float, default=Config.SCREENSHOT_INTERVAL,
                   help='截屏间隔秒数')
    p.add_argument('--think', type=float, default=None,
                   help='KataGo 每手搜索时间上限秒数 (默认 %(default)s)')
    p.add_argument('--tmin', type=float, default=None, help='每手随机思考下限 (默认 5s)')
    p.add_argument('--tmax', type=float, default=None, help='每手随机思考上限 (默认 10s)')
    p.add_argument('--norandom', action='store_true', help='关闭随机思考时间(用固定 --think)')
    p.add_argument('--once', action='store_true', help='只跑一轮 (思考+落子) 后退出')
    p.add_argument('--debug', action='store_true', help='每步保存调试图')
    p.add_argument('--analyze-only', action='store_true',
                   help='仅分析模式: 持续调用 KataGo 评估当前局面并更新看板, 不自动落子')
    p.add_argument('--platform', choices=['tencent', 'xingzhen'], default='tencent',
                   help='对手软件: tencent=腾讯围棋 (原窗口), xingzhen=星阵围棋 (Edge浏览器)')
    args = p.parse_args()

    cfg = Config()
    cfg.MY_COLOR = BLACK if args.color == 'black' else WHITE
    cfg.SCREENSHOT_INTERVAL = args.interval
    if args.think:
        cfg.KATAGO_MAX_TIME = max(1.0, min(float(args.think), 120.0))
    if args.norandom:
        cfg.KATAGO_TIME_RANDOM = False
    else:
        if args.tmin is not None:
            cfg.KATAGO_TIME_MIN = max(1.0, float(args.tmin))
        if args.tmax is not None:
            cfg.KATAGO_TIME_MAX = float(args.tmax)
        if cfg.KATAGO_TIME_MAX < cfg.KATAGO_TIME_MIN:
            cfg.KATAGO_TIME_MAX = cfg.KATAGO_TIME_MIN
    cfg.GO_WINDOW = (find_browser_window() if args.platform == 'xingzhen' else find_go_window())
    if cfg.GO_WINDOW:
        if args.platform == 'xingzhen':
            print(f"[启动] 锁定星阵围棋 (Edge浏览器) 窗口: {cfg.GO_WINDOW}")
        else:
            print(f"[启动] 锁定腾讯围棋窗口: {cfg.GO_WINDOW}")
    else:
        plat_cn = '星阵围棋' if args.platform == 'xingzhen' else '腾讯围棋'
        print(f"[启动] 未找到{plat_cn}窗口, 用全屏 x<1280 搜索")
    if args.confirm:
        x, y = args.confirm.split(',')
        cfg.CONFIRM_BTN = (int(x), int(y))
    if args.debug:
        os.makedirs(cfg.DEBUG_DIR, exist_ok=True)

    analyze_only = args.analyze_only
    print(f"=== Go AI Auto-Player{' (仅分析模式)' if analyze_only else ''} ===")
    print(f"my color: {'BLACK' if cfg.MY_COLOR == BLACK else 'WHITE'}")
    print(f"screen: {pyautogui.size()}")
    print(f"screenshot interval: {cfg.SCREENSHOT_INTERVAL}s")
    print(f"think max: {cfg.THINK_MAX}s, click deadline: {cfg.CLICK_DEADLINE}s")
    if cfg.KATAGO_TIME_RANDOM:
        print(f"katago per-move: 随机 {cfg.KATAGO_TIME_MIN}~{cfg.KATAGO_TIME_MAX}s (防 AI 特征)")
    else:
        print(f"katago per-move: 固定 {cfg.KATAGO_MAX_TIME}s")
    if cfg.CONFIRM_BTN:
        print(f"confirm btn: {cfg.CONFIRM_BTN}")
    else:
        print("confirm btn: <none> (落子即提交)")
    print(f"platform: {args.platform} ({'星阵围棋' if args.platform == 'xingzhen' else '腾讯围棋'})")
    print()

    # === 0) 预启动 KataGo: 优先复用预加载服务(免冷启动), 否则本地冷启动 ===
    katago = None
    if KATAGO_AVAILABLE:
        print("[启动] 连接 KataGo (优先复用预加载服务)...")
        try:
            client = KataServiceClient(port=KATA_SERVICE_PORT, max_time=cfg.KATAGO_MAX_TIME, log_cb=print)
            mode, ready = client.connect()
            if ready:
                katago = client
                if mode == 'service':
                    print("[启动] 复用预加载 KataGo 服务, 免冷启动 ✓")
                else:
                    print("[启动] 本地 KataGo 冷启动完成, 本会话复用该引擎")
            else:
                katago = None
        except Exception as e:
            print(f"[启动] KataGo 连接失败: {e}, 回退本地启发式引擎")
            katago = None
    else:
        print("[启动] 未找到 KataGo 可执行/模型, 使用本地启发式引擎")

    prev_stones = None
    last_played_by_me = False  # 启动时, 假定对方刚下完, 准备我方思考
    last_move_played_at = 0  # 记录我方落子时间
    move_history = []  # 真实落子顺序 [(col,row,color),...], 还原劫(ko)状态用
    cycle = 0

    while True:
        cycle += 1
        t_cycle = time.time()
        # === 0) 处理网页启动器指令 (重置/换色, 不重启进程) ===
        prev_stones, last_played_by_me = handle_command(cfg, prev_stones, last_played_by_me)
        if prev_stones is None:
            move_history.clear()  # 指令重置了对局 (set_color/reset 返回 None), 清空旧历史
        # === 1) 截屏 (PrintWindow 抓窗口, 隐藏/最小化也能截) ===
        ss_path = os.path.join(cfg.DEBUG_DIR, f'cycle_{cycle:03d}.png') if args.debug else None
        img, win_rect = take_screenshot(ss_path)
        if win_rect is not None:
            cfg.GO_WINDOW = win_rect
        # === 2) 读棋盘 ===
        board, stones = board_from_screenshot(img, cfg)
        if board is None:
            print(f"[{cycle}] 未检测到棋盘, 重试...")
            time.sleep(min(5, cfg.SCREENSHOT_INTERVAL))
            continue
        n_black, n_white = count_stones(stones)
        print(f"[{cycle}] t={t_cycle:.0f} 棋盘: 黑={n_black} 白={n_white} (我方={'黑' if cfg.MY_COLOR==BLACK else '白'})")
        # === 2.5) 维护真实落子历史 (prev -> 当前的新增子), 用于还原劫(ko)状态 ===
        # 历史与识别不一致(中途启动/漏读)时清空, 本局退化为交替重建(无劫还原, 但有防死循环兜底)
        merge_move_history(move_history, prev_stones, stones)
        if move_history and not history_matches(move_history, stones):
            print(f"[{cycle}] 落子历史与棋盘不一致, 清空重来 (本局暂无法还原劫状态)")
            move_history.clear()
        prev_stones = stones
        if args.debug:
            from go_vision import N as _N
            dbg = img.copy()
            step = board['step']
            for j in range(_N):
                for i in range(_N):
                    x, y = int(board['pts'][j, i, 0]), int(board['pts'][j, i, 1])
                    s = stones[j, i]
                    col = {0: (0, 255, 0), 1: (0, 0, 0), 2: (0, 200, 255)}[s]
                    cv2.circle(dbg, (x, y), int(step * 0.32), col, 2)
            cv2.imwrite(os.path.join(cfg.DEBUG_DIR, f'cycle_{cycle:03d}_dbg.png'), dbg)

        # === 3A) 仅分析模式: 不落子, 只让 KataGo 评估当前局面并更新看板 ===
        if analyze_only:
            # 判断当前该谁下: 优先 UI 视觉识别 (红章 / 头像水滴), 回退子数法
            analyze_side = None
            side_src = '?'
            red = None
            if args.platform == 'xingzhen' and AVATAR_DETECT_AVAILABLE and cfg.GO_WINDOW:
                red = avatar_detect(img, window_rect=cfg.GO_WINDOW)
                if red in ('B', 'W'):
                    side_src = '头像水滴'
            elif SEAL_OCR_AVAILABLE:
                red = seal_detect(img, window_rect=cfg.GO_WINDOW)
                if red in ('B', 'W'):
                    side_src = 'UI红章'
            if red in ('B', 'W'):
                analyze_side = BLACK if red == 'B' else WHITE
            else:
                nb_, nw_ = count_stones(stones)
                analyze_side = BLACK if nb_ <= nw_ else WHITE
                side_src = '子数'
            if katago is not None:
                per_move_time = clamp_random(cfg.KATAGO_TIME_MIN, cfg.KATAGO_TIME_MAX) if cfg.KATAGO_TIME_RANDOM else cfg.KATAGO_MAX_TIME
                print(f"[{cycle}] [仅分析] 当前轮: {'黑' if analyze_side==BLACK else '白'} ({side_src}), "
                      f"评估上限 {per_move_time:.1f}s ...")
                try:
                    eb = _stones_to_board(stones, analyze_side, move_history)
                    katago.set_board(eb.g, history=move_history)
                    mv = katago.genmove(analyze_side, max_time=per_move_time)
                    print(f"[{cycle}] [仅分析] KataGo 推荐 ({mv[0]},{mv[1]}) — 已写入日志, 看板将更新")
                except Exception as e:
                    print(f"[{cycle}] [仅分析] 评估出错: {e}")
            else:
                print(f"[{cycle}] [仅分析] 无 KataGo 引擎, 跳过评估")
            prev_stones = stones
            time.sleep(cfg.SCREENSHOT_INTERVAL)
            continue

        # === 3) 决策: 是否我方行棋 (优先用 UI 红章判断, 子数法 fallback) ===
        side_turn = is_my_turn_from_img(img, cfg.MY_COLOR, cfg.GO_WINDOW, platform=args.platform)
        nbw_turn = is_my_turn(prev_stones, stones, cfg.MY_COLOR, last_played_by_me)
        if side_turn is not None:
            my_turn = side_turn
            turn_src = 'UI'
        else:
            my_turn = nbw_turn
            turn_src = '子数'
        if not my_turn:
            print(f"[{cycle}] 对方行棋或非我方回合 (UI={side_turn}, 子数={nbw_turn}), 等 {cfg.SCREENSHOT_INTERVAL}s")
            prev_stones = stones
            time.sleep(cfg.SCREENSHOT_INTERVAL)
            continue

        # === 4) 思考 ===
        # 每手随机思考时间 (5~10s), 防止固定节奏被识别为 AI
        per_move_time = cfg.KATAGO_MAX_TIME
        if cfg.KATAGO_TIME_RANDOM:
            per_move_time = clamp_random(cfg.KATAGO_TIME_MIN, cfg.KATAGO_TIME_MAX)
        engine_board = _stones_to_board(stones, cfg.MY_COLOR, move_history)
        # 中盘低胜率权重: 胜率<30% 时延长思考 (最多 20s)
        per_move_time, extended = decide_think_time(
            cfg, katago, engine_board, n_black + n_white, cycle, per_move_time, move_history)
        engine_label = 'KataGo' if katago is not None else '本地启发式'
        print(f"[{cycle}] >>> 我方行棋, 开始思考 (引擎={engine_label}, 本手上限 {per_move_time:.1f}s, "
              f"延长={'是' if extended else '否'})")
        # 后台思考线程, 以便在 30s 截止时强制返回
        result = {}
        def think():
            try:
                t0 = time.time()
                if katago is not None:
                    # 快照历史传给引擎, 还原劫(ko)状态, KataGo 才会在劫争中主动找劫材
                    katago.set_board(engine_board.g, history=list(move_history))
                    mv = katago.genmove(cfg.MY_COLOR, max_time=per_move_time)
                else:
                    mv = select_move(engine_board, cfg.MY_COLOR, time_budget=cfg.THINK_MAX - 2)
                result['mv'] = mv
                result['dt'] = time.time() - t0
            except Exception as e:
                result['err'] = str(e)
        th = threading.Thread(target=think, daemon=True)
        th.start()
        # 等待时长跟随本手随机时间上限(加 10s 缓冲), 且不小于固定思考截止, 确保随机时间调到 100s 也能等到
        th.join(max(per_move_time + 10, cfg.THINK_MAX + 2))
        if 'err' in result:
            print(f"[{cycle}] 思考出错: {result['err']}")
            mv = (-1, -1)
        elif 'mv' not in result:
            print(f"[{cycle}] 思考超时, 退一手 (-1, -1)")
            mv = (-1, -1)
        else:
            mv = result['mv']
            print(f"[{cycle}] 决策: ({mv[0]}, {mv[1]}) 用时 {result.get('dt', 0):.2f}s")

        # === 4.5) 决策合法性校验 (含劫) ===
        #   非法情形:
        #     1) 已占: set_board 漏读某子, KataGo 视角少子, 会下到已占位置 -> 客户端拒绝
        #     2) 劫/自杀/禁手: 空点但非法. 劫点由真实历史还原 (move_history), 本地 is_legal 拦截
        #   非法则重截图+重读+重决策; 重试后仍非法 -> 本轮放弃落子 (杜绝反复强行走劫点死循环)
        #   注意: legal_check 必须 _stones_to_board(history) 重建, 否则 ko=None 拦不住劫点.
        for retry in range(4):
            legal_check = _stones_to_board(stones, cfg.MY_COLOR, move_history)
            if katago is None or mv == (-1, -1) or not (0 <= mv[0] < N and 0 <= mv[1] < N):
                break  # 虚着/无引擎, 无需校验
            if stones[mv[1], mv[0]] != EMPTY:
                reason = '已占'
            elif not legal_check.is_legal(mv[0], mv[1], cfg.MY_COLOR):
                reason = '劫/自杀/禁手'
            else:
                break  # 决策点合法且为空, 通过
            if retry >= 3:
                # 已重试多次仍非法 -> 放弃本轮, 等对方落子/局面变化, 防死循环
                print(f"[{cycle}] 决策点 ({mv[0]},{mv[1]}) 经重试仍非法 ({reason}), 本轮放弃落子")
                prev_stones = stones
                time.sleep(cfg.SCREENSHOT_INTERVAL)
                continue
            print(f"[{cycle}] 决策点 ({mv[0]},{mv[1]}) 非法 ({reason}), retry {retry+1} 换点")
            img_v, win_rect = take_screenshot(None)
            if win_rect:
                cfg.GO_WINDOW = win_rect
            board_v, stones_v = board_from_screenshot(img_v, cfg)
            if board_v is None or stones_v is None:
                break
            # 新截图的局面可能与决策前不同 (对方动了/动画未稳): 同步历史再重建
            merge_move_history(move_history, prev_stones, stones_v)
            if move_history and not history_matches(move_history, stones_v):
                move_history.clear()
            board = board_v
            stones = stones_v
            prev_stones = stones
            engine_board = _stones_to_board(stones, cfg.MY_COLOR, move_history)
            katago.set_board(engine_board.g, history=list(move_history))
            mv = katago.genmove(cfg.MY_COLOR, max_time=per_move_time)
            if mv != (-1, -1):
                print(f"[{cycle}] 重决策: ({mv[0]},{mv[1]})")

        # === 5) 提速: 思考完立即落子, 不再强制凑 CLICK_DEADLINE 时长 ===
        # (原逻辑会 sleep 到 10s 才点击, 导致整步 ~10s, 现在直接落子,
        #  单步耗时 = 思考时间(≤tmax) + 点击核对, 控制在 3s 内)
        elapsed = time.time() - t_cycle
        if mv != (-1, -1):
            print(f"[{cycle}] 本步已用 {elapsed:.1f}s, 直接落子")


        # === 6) 点击落子 ===
        if mv == (-1, -1):
            # 虚着: 点击棋盘中央的 "pass" 区域 (有些客户端有), 或只点确认
            print(f"[{cycle}] 虚着 (pass)")
        else:
            c, r = mv
            x, y = click_board(board, c, r)
            print(f"[{cycle}] 已点击 ({c},{r}) -> 屏幕 ({x},{y})")
            last_played_by_me = True
            last_move_played_at = time.time()

        # === 7) 落子后截屏核对 ===
        time.sleep(2.5)  # 等棋子动画稳定 (刚落的子 0.6s 内识别不到)
        verify_path = os.path.join(cfg.DEBUG_DIR, f'cycle_{cycle:03d}_after.png') if args.debug else None
        img2, win_rect2 = take_screenshot(verify_path)
        if win_rect2:
            cfg.GO_WINDOW = win_rect2
        board2, stones2 = board_from_screenshot(img2, cfg)
        if board2 is not None and stones2 is not None:
            # 把我方刚落的子记入真实历史 (stones2 = 落子后局面)
            merge_move_history(move_history, prev_stones, stones2)
            if move_history and not history_matches(move_history, stones2):
                print(f"[{cycle}] 落子后历史不一致, 清空重来")
                move_history.clear()
            n_b2, n_w2 = count_stones(stones2)
            n_b1, n_w1 = count_stones(stones)
            if cfg.MY_COLOR == BLACK:
                mine_delta = n_b2 - n_b1
                opp_delta = n_w2 - n_w1
            else:
                mine_delta = n_w2 - n_w1
                opp_delta = n_b2 - n_b1
            # 我方子数增加 => OK; 我方子数不变但对方已应手 => 我方子大概率落成 (动画被对方落子覆盖)
            ok = mine_delta > 0 or (mine_delta == 0 and opp_delta > 0)
            print(f"[{cycle}] 核对: 落子前 黑={n_b1}白={n_w1} -> 落子后 黑={n_b2}白={n_w2} "
                  f"(我方+{mine_delta} 对方+{opp_delta}) {'OK' if ok else 'FAIL'}")
            if not ok:
                print(f"[{cycle}] 警告: 落子核对失败, 可能需要重试")
        # === 8) 点确认 ===
        if click_confirm(cfg):
            print(f"[{cycle}] 已点确认 {cfg.CONFIRM_BTN}")
            time.sleep(0.3)

        prev_stones = stones2 if stones2 is not None else stones
        last_played_by_me = True
        if args.once:
            if katago is not None:
                katago.stop()
            break
        # 等下一轮
        time.sleep(2.0)


def _stones_to_board(stones, turn, move_history=None):
    """numpy 19x19 -> go_engine.Board.
    move_history 完整(重放后与 stones 一致)时按真实顺序重放, 还原劫(ko)状态,
    使 is_legal 能正确拦截劫点回提; 历史为空/不一致时退化为静态快照(无 ko 信息)."""
    if move_history:
        b = Board()
        ok = True
        for c, r, color in move_history:
            c, r, color = int(c), int(r), int(color)
            if not (0 <= c < N and 0 <= r < N):
                ok = False
                break
            if not b.play(c, r, color)[0]:
                ok = False
                break
        if ok:
            match = True
            for r in range(N):
                for c in range(N):
                    if b.g[r][c] != int(stones[r][c]):
                        match = False
                        break
                if not match:
                    break
            if match:
                b.turn = turn
                return b
    b = Board()
    b.g = [[int(stones[r][c]) for c in range(N)] for r in range(N)]
    b.turn = turn
    return b


if __name__ == '__main__':
    main()

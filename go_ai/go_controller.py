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
GAME_RESULT_FILE = os.path.join(BASE, 'game_result.json')  # 终局结果 (看板显示 / 看门狗识别主动退出)
CONTROLLER_LOCK = (os.environ.get('GOAI_CONTROLLER_LOCK')        # 测试可隔离到临时路径
                   or os.path.join(BASE, '_controller.lock'))     # 单实例锁 (防两个 AI 同时抢一块棋盘)
# 辅助模式 (--assist) 专用: 看板下发 {"cmd":"play","col","row"} 后暂存于此,
# 等本轮拿到 board/stones (要算落点像素坐标) 再执行, 见 _assist_play()
_PENDING_PLAY = {'pt': None, 'ts': 0.0}

# 落子核对连续失败的点: {(col,row): 连续失败次数}。
# 用于掐断"引擎认定某点空着 -> 点击 -> 子数不变 -> 再点同一点"的死循环
# (2026-09-12 实测: 星阵在最后一手上画朱砂红方块, 旧版视觉把那颗白子读成空点,
#  于是 AI 连续 115 轮点同一个已被占的点, 一次都没落成)。
# 连续失败 FAIL_PT_MAX 次即拉黑该点, 让引擎改下候选列表里的下一个点;
# 一旦有一手核对成功说明链路恢复正常, 立刻清空。
_FAILED_PTS = {}
PLAY_FAIL_PT_MAX = 2


def is_play_blacklisted(col, row):
    """该点是否因连续落子未生效被拉黑 (见 _FAILED_PTS 说明)。"""
    return _FAILED_PTS.get((col, row), 0) >= PLAY_FAIL_PT_MAX


def note_play_result(col, row, ok):
    """记录一次落子核对结果。ok=True 清空黑名单 (链路已恢复);
    否则累计该点失败次数, 返回累计值。"""
    if ok:
        _FAILED_PTS.clear()
        return 0
    k = (col, row)
    _FAILED_PTS[k] = _FAILED_PTS.get(k, 0) + 1
    return _FAILED_PTS[k]


# 落子历史与识别棋盘对不上 -> 清空历史, 本局退化为"交替重建"(不还原劫)。
# 这是**正常现象**而不是故障: 落子后等 2.5s 才读帧, 对手往往已经应手甚至提子,
# 一帧内向棋盘追加的两手(我方+对方)顺序无法确定, 重放必然对不上; 中盘挂上十几手的
# 对局也常常对不上。清空后靠防死循环兜底, 结果正确。
# 因此只在**首次**退化时提示一次, 之后完全静默, 避免每手刷两行噪音。
_HISTORY_WARNED = {'v': False}


def reset_history_warning():
    """重开一局/换色时允许再提示一次历史退化 (状态已重置)。"""
    _HISTORY_WARNED['v'] = False


def warn_history_once(cycle, why):
    """历史退化 -> 首次打印一行说明, 之后静默 (清空动作由调用方执行)。"""
    if _HISTORY_WARNED['v']:
        return
    _HISTORY_WARNED['v'] = True
    print(f"[{cycle}] {why}, 已清空落子历史并退化为交替重建 "
          f"(本局不再还原劫; 对手已应手/提子属正常现象, 后续同类情况自动静默)")


# ============== 单实例保护 ==============
# 为什么必须放在 controller 内部: 启动路径有很多 (bat / 菜单启动器 / 看板按钮 / 看门狗自动重启),
# 只在某一个入口做判断挡不住重复。曾经出现过两个 controller 同时执黑点同一块棋盘 ——
# 互相打断、子数乱跳、胜率在 0% 与 98% 之间甩, 看起来就是"AI 突然变傻、乱下"。
def _pid_alive(pid):
    """进程是否存活 (复用 kata_service 的实现, 避免两处逻辑不一致)。"""
    try:
        from kata_service import _pid_alive as _pa
        return _pa(pid)
    except Exception:
        return True          # 判不准时保守视为存活 (宁可拒绝启动, 也不要双开)


def acquire_controller_lock():
    """取得 controller 独占权。返回 (ok, 占用者pid)。陈旧锁(进程已死)会被清理。"""
    import json as _json
    existing = None
    try:
        with open(CONTROLLER_LOCK, encoding='utf-8') as f:
            existing = _json.load(f)
    except Exception:
        existing = None
    if isinstance(existing, dict):
        pid = existing.get('pid')
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            pid = None
        if pid and pid != os.getpid() and _pid_alive(pid):
            return False, pid
        try:                     # 陈旧锁 (进程已退出) -> 清理后重试
            os.remove(CONTROLLER_LOCK)
        except OSError:
            pass
    try:
        fd = os.open(CONTROLLER_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, _json.dumps({'pid': os.getpid(), 'ts': time.time(),
                                      'color': None}).encode())
        finally:
            os.close(fd)
    except FileExistsError:
        return False, None       # 竞态: 另一个进程刚好抢先, 本进程退出
    import atexit
    atexit.register(release_controller_lock)
    return True, os.getpid()


def release_controller_lock():
    """退出时释放锁 (只删自己写的那个)。"""
    import json as _json
    try:
        with open(CONTROLLER_LOCK, encoding='utf-8') as f:
            d = _json.load(f)
        if int(d.get('pid', -1)) == os.getpid():
            os.remove(CONTROLLER_LOCK)
    except Exception:
        pass


def _update_lock_meta(color=None, platform=None):
    """把执子颜色/平台写进锁文件, 便于看板与排障看清"现在是哪个 AI 在占棋盘"。"""
    import json as _json
    try:
        with open(CONTROLLER_LOCK, encoding='utf-8') as f:
            d = _json.load(f)
        if int(d.get('pid', -1)) != os.getpid():
            return
        d.update({'pid': os.getpid(), 'ts': time.time(),
                  'color': color, 'platform': platform,
                  'color_cn': ('黑' if color == BLACK else ('白' if color == WHITE else None))})
        with open(CONTROLLER_LOCK, 'w', encoding='utf-8') as f:
            _json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass

import cv2
import numpy as np
import pyautogui
import random

from go_vision import (detect_board, read_board, N, board_to_pixel, refine_pts,
                       find_go_window, find_browser_window, grab_for_read, offset_board)
from go_engine import Board, select_move, BLACK, WHITE, EMPTY
# 读取 KataGo 每次搜索的候选点列表 (gtp_logs), 供"最佳点非法时自动切换次优"使用
from show_analysis import find_latest_valid_log, extract_last_search
import config_store
import sgf as sgf_mod
import notify as notify_mod
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
KATA_SERVICE_PORT = config_store.SERVICE_PORT


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

    # ---- 以下由 settings.json 填充 (见 apply_settings), 值域见 config_store ----
    PLAYOUT_DOUBLING = 0.0      # >0 让 AI 变弱/放水
    NUM_SEARCH_THREADS = 0      # 0 = 引擎自适应
    AUTO_STOP = True            # 终局自动停止
    AUTO_STOP_CYCLES = 12       # 盘面连续 N 轮无变化 + 无行棋迹象 => 终局
    AUTO_EXPORT_SGF = True      # 终局自动导出 SGF
    SGF_DIR = ''                # 空 = go_ai/games/
    NOTIFY = None               # 通知配置 (dict), 由 config_store 提供

    # ---- 取图 (见 take_screenshot) ----
    CAPTURE_MODE = 'auto'       # auto=先全屏后窗口 | screen=只全屏 | window=只窗口
    CAPTURE_MIRROR = True       # 窗口取图是否水平翻转
    PLATFORM = 'tencent'
    SHOT_ORIGIN = (0, 0)        # 最近一次取图的原点 (窗口模式非 0)

    # 调试
    DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug')


def apply_settings(cfg, cli=None):
    """把 settings.json 的值灌进 Config 实例 (命令行参数优先)。
    单一真源: 只有这里读配置文件, 其它地方不再各自解析默认值。"""
    s = config_store.load_config()
    cli = cli or {}
    w = s['weights']
    cfg.WEIGHT_MIN_MOVES = int(w['min_moves'])
    cfg.WEIGHT_WINRATE = float(w['winrate'])
    cfg.WEIGHT_MAX_TIME = float(w['max_time'])
    cfg.WEIGHT_EVAL_TIME = float(w['eval_time'])
    cfg.WEIGHT_ENABLED = bool(w['enabled'])

    kg = s['katago']
    cfg.PLAYOUT_DOUBLING = float(kg['playout_doubling_advantage'])
    cfg.NUM_SEARCH_THREADS = int(kg['num_search_threads'])
    if cli.get('norandom'):
        cfg.KATAGO_TIME_RANDOM = False
    else:
        cfg.KATAGO_TIME_RANDOM = bool(kg['time_random'])
    if not cli.get('think'):
        cfg.KATAGO_MAX_TIME = float(kg['max_time'])
    if cli.get('tmin') is None:
        cfg.KATAGO_TIME_MIN = s['tmin']
    if cli.get('tmax') is None:
        cfg.KATAGO_TIME_MAX = s['tmax']

    a = s['auto_stop']
    cfg.AUTO_STOP = bool(a['enabled'])
    cfg.AUTO_STOP_CYCLES = int(a['stable_cycles'])
    cfg.AUTO_EXPORT_SGF = bool(a['export_sgf'])
    cfg.SGF_DIR = s['sgf']['dir']
    cfg.NOTIFY = s['notify']
    # 取图
    cp = s.get('capture') or {}
    cfg.CAPTURE_MODE = cp.get('mode') or 'auto'
    m = cp.get('mirror') or 'auto'
    cfg.CAPTURE_MIRROR = (s.get('platform') != 'xingzhen') if m == 'auto' else (m == 'on')
    return cfg


def take_screenshot(path=None, cfg=None):
    """取一张用于识别的屏幕图。返回 (img_bgr, roi_rect)。

    优先级 (由 settings.json 的 capture.mode 控制):
      1) 全屏截图 (pyautogui) —— 坐标就是屏幕绝对坐标, 点击最直观; 默认首选
      2) 窗口取图 (PrintWindow) —— 全屏截图不可用时 (被占用 / 远程桌面 / 某些显卡驱动)
         回落到"直接抓窗口内容", 窗口被遮挡也能识别; 此时棋盘坐标是窗口局部的,
         要用 cfg.SHOT_ORIGIN 才能换回屏幕坐标 (click_board 已处理)
    roi_rect 供 detect_board 限定搜索范围; 窗口模式返回窗口尺寸 (即整幅图)。

    cfg 必须传 **Config 实例** (main() 里的那个)。曾误用类对象 Config 读取,
    于是拿到的是类默认值: platform 永远是 'tencent'、mirror 永远是 True ——
    星阵(浏览器)会去找腾讯围棋窗口, 自然找不到。"""
    global _SHOT_ORIGIN, _LAST_FRAME_MODE
    cfg = cfg if cfg is not None else Config
    mode = getattr(cfg, 'CAPTURE_MODE', 'auto')
    platform = getattr(cfg, 'PLATFORM', 'tencent')
    err = None
    if mode != 'window':
        try:
            img = pyautogui.screenshot()
            if path:
                img.save(path)
            _SHOT_ORIGIN = (0, 0)
            _LAST_FRAME_MODE = 'screen'
            # 全屏图: ROI 每帧重新锁定窗口 (窗口被拖动/缩放也能跟上), 找不到则 None
            win = (find_browser_window() if platform == 'xingzhen' else find_go_window())
            return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), win
        except Exception as e:
            err = e
            if mode == 'screen':
                raise
    # ---- 回落/指定: 窗口取图 ----
    mirror = getattr(cfg, 'CAPTURE_MIRROR', True)
    img, origin, got = grab_for_read(platform, prefer_window=True, mirror=mirror)
    if got == 'window' and img is not None:
        _SHOT_ORIGIN = origin
        _LAST_FRAME_MODE = 'window'
        if path:
            try:
                cv2.imwrite(path, img)
            except Exception:
                pass
        h, w = img.shape[:2]
        # 返回窗口尺寸当 ROI: 图像本身就是窗口内容, 从 (0,0) 开始就是整幅
        return img, (0, 0, w, h)
    raise RuntimeError(f'取图失败 (全屏截图不可用: {err!r}, 窗口取图: {got})')


_SHOT_ORIGIN = (0, 0)   # 最近一次取图的原点 (全屏=0,0; 窗口=窗口左上角屏幕坐标)
_LAST_FRAME_MODE = 'screen'   # 最近一帧来自全屏('screen')还是窗口取图('window')


def read_board_frame(cfg, path=None):
    """取一帧并识别棋盘; auto 模式下全屏认不出棋盘时自动改用窗口取图重试。

    为什么需要: 全屏截图拍的是"屏幕上可见的内容", 任何盖在棋盘上的东西
    (登录/注册弹窗、别的浏览器窗口、系统提示) 都会把棋盘挡掉一大块,
    于是 detect_board 找不到棋盘 -> 整个 AI 卡在"未检测到棋盘"。
    窗口取图走 PrintWindow, 拿的是窗口自己的渲染内容, 弹窗/遮挡都不影响。

    返回 (img, board, stones, win_rect); board 为 None 表示这一帧都没认出来。
    """
    global _SHOT_ORIGIN
    img, win_rect = take_screenshot(path, cfg=cfg)
    board, stones = _detect_frame(img, cfg, _LAST_FRAME_MODE == 'window')
    if board is not None:
        return img, board, stones, win_rect
    if getattr(cfg, 'CAPTURE_MODE', 'auto') != 'auto':
        return img, None, None, win_rect
    # ---- 全屏认不出 -> 抓窗口内容再试 ----
    try:
        mirror = getattr(cfg, 'CAPTURE_MIRROR', True)
        wimg, origin, got = grab_for_read(getattr(cfg, 'PLATFORM', 'tencent'),
                                          prefer_window=True, mirror=mirror)
    except Exception as e:
        print(f'[回落] 窗口取图失败: {e!r}')
        return img, None, None, win_rect
    if wimg is None or got != 'window':
        print(f'[回落] 窗口取图不可用 (got={got}), 仍用全屏帧')
        return img, None, None, win_rect
    _SHOT_ORIGIN = origin
    h, w = wimg.shape[:2]
    wboard, wstones = _detect_frame(wimg, cfg, local=True)
    if wboard is None:
        print(f'[回落] 窗口取图 (shape={wimg.shape} 原点={origin}) 也认不出棋盘')
        return img, None, None, win_rect
    print('[回落] 全屏取图认不出棋盘 (可能被弹窗/其他窗口盖住), 已改用窗口取图')
    return wimg, wboard, wstones, (0, 0, w, h)


def _detect_frame(img, cfg, local):
    """识别一帧。local=True 表示图是**窗口局部**坐标, ROI 要用整幅窗口矩形。

    踩过的坑: 全屏帧的 GO_WINDOW 是屏幕绝对坐标 (如 -8,0,1288,1398),
    直接拿去裁窗口图会把棋盘裁到 ROI 外面 -> 明明图里有棋盘却认不出。
    """
    if not local:
        return board_from_screenshot(img, cfg)
    h, w = img.shape[:2]
    saved = getattr(cfg, 'GO_WINDOW', None)
    try:
        setattr(cfg, 'GO_WINDOW', (0, 0, w, h))
        return board_from_screenshot(img, cfg)
    finally:
        setattr(cfg, 'GO_WINDOW', saved)


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


def katago_candidates():
    """解析 KataGo 最近一次搜索的候选点列表 (gtp_logs, 与看板同源).
    返回 [(row, col, visits), ...] 按 visits 降序 (即引擎自身排序的优劣顺序).
    失败/无日志返回 []. 仅在决策点非法时调用, 频率极低."""
    try:
        logp = find_latest_valid_log()
        if not logp:
            return []
        d = extract_last_search(logp)
        if not d or not d.get('cands'):
            return []
        cs = sorted(d['cands'], key=lambda x: -(x.get('visits') or 0))
        out = []
        for c in cs:
            r_, c_ = c.get('row'), c.get('col')
            if r_ is not None and c_ is not None:
                out.append((int(r_), int(c_), int(c.get('visits') or 0)))
        return out
    except Exception:
        return []


def next_legal_candidate(stones, legal_board, tried, my_color):
    """在 KataGo 候选列表里, 跳过已试/已占/劫·禁手点, 返回第一个可合法落子的点.
    tried: {(col,row),...} 已尝试且被拒的点. 找不到返回 None."""
    for r_, c_, _v in katago_candidates():
        if not (0 <= c_ < N and 0 <= r_ < N):
            continue
        if (c_, r_) in tried:
            continue
        if stones[r_, c_] != EMPTY:
            continue
        if not legal_board.is_legal(c_, r_, my_color):
            continue
        return (c_, r_)
    return None


def clamp_random(lo, hi, rng=None):
    """返回 [lo, hi] 内的随机浮点数 (1位小数). 用于每手思考时间随机化."""
    if hi < lo:
        lo, hi = hi, lo
    return round((rng or random).uniform(lo, hi), 1)


_CMD_SEEN = None  # 已处理过的指令内容指纹 (防删除失败时重复执行刷屏)


# ============== 终局识别 ==============
# TerminalDetector 定义在 game_state.py (纯逻辑, 无 GUI 依赖, 可单测)
from game_state import TerminalDetector  # noqa: E402

_TERMINAL_CN = {
    'resign': '引擎认输',
    'two_passes': '双方停手(连续虚着)',
}


def do_notify(cfg, event, title, text=''):
    """按配置发通知 (默认关闭 -> 空操作, 不影响原有行为)。"""
    try:
        return notify_mod.send(event, title, text, config=getattr(cfg, 'NOTIFY', None))
    except Exception:
        return False


def export_sgf_file(cfg, move_history, stones=None, reason='', extra=''):
    """导出 SGF, 返回路径或 None。带落子顺序时导出的棋谱可直接复盘。"""
    try:
        ts = time.strftime('%Y%m%d_%H%M%S')
        comment = 'GoAI 自动对局导出'
        if reason:
            comment += ' | 终局原因: %s' % _TERMINAL_CN.get(reason, reason)
        if extra:
            comment += ' | ' + extra
        path = sgf_mod.save_sgf(
            moves=list(move_history) if move_history else None,
            stones=None if move_history else stones,
            out_dir=(cfg.SGF_DIR or None),
            name='game_%s.sgf' % ts,
            black='GoAI(黑)', white='对手(白)',
            comment=comment, date=time.strftime('%Y-%m-%d %H:%M:%S'))
        return path
    except Exception as e:
        print('[SGF] 导出失败: %r' % (e,))
        return None


def finish_game(cfg, reason, move_history, stones, winrate=None, engine=None, auto=True):
    """终局收尾: 导出 SGF + 落结果文件 + 发通知 (+ 返回 True 表示调用方应退出)。"""
    n = len(move_history)
    reason_cn = _TERMINAL_CN.get(reason, reason)
    wr_txt = ('AI胜率 %.1f%%' % (winrate * 100)) if winrate is not None else ''
    path = None
    if cfg.AUTO_EXPORT_SGF:
        path = export_sgf_file(cfg, move_history, stones, reason, wr_txt)
    print('=' * 56)
    print('[终局] 判定对局结束: %s (共 %d 手)' % (reason_cn, n))
    if path:
        print('[终局] SGF 已导出: %s' % path)
    print('=' * 56)
    # 结果文件: 看板显示"上一局结果", 同时也是看门狗判断"主动退出"的依据
    try:
        with open(GAME_RESULT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'reason': reason, 'reason_cn': reason_cn, 'moves': n,
                       'winrate': winrate, 'sgf': path,
                       'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'ts': time.time()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('[终局] 写入结果文件失败: %r' % (e,))
    do_notify(cfg, 'terminal',
              '对局结束 · %s' % reason_cn,
              '共 %d 手; %s%s' % (n, wr_txt, ('; 棋谱: %s' % path) if path else ''))
    if auto and cfg.AUTO_STOP:
        print('[终局] 自动停止 controller')
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        return True
    return False


def handle_command(cfg, prev_stones, last_played_by_me, move_history=None):
    """处理网页启动器下发的运行中指令 (_command.json, 先执行后尝试删除)。
    支持:
      {"cmd":"set_color","color":"black"|"white"}  重开一局并切换执子颜色 (AI 进程不重启)
      {"cmd":"reset"}                               仅重置对局状态 (颜色不变)
      {"cmd":"export_sgf"}                          立即导出当前棋谱 (复盘用)
      {"cmd":"play","col":c,"row":r}                辅助模式: 玩家点选落子 (c/r 0-based)
    返回 (prev_stones, last_played_by_me) 可能被重置。"""
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
                _PENDING_PLAY['pt'] = None
                _FAILED_PTS.clear()
                reset_history_warning()
            else:
                print('[指令] set_color 颜色参数无效, 忽略')
        elif c == 'reset':
            print('[指令] 重开一局, 已重置对局状态, 重新开始分析')
            prev_stones, last_played_by_me = None, False
            _PENDING_PLAY['pt'] = None
            _FAILED_PTS.clear()
            reset_history_warning()
        elif c == 'export_sgf':
            p = export_sgf_file(cfg, list(move_history or []), prev_stones,
                                extra='手动导出')
            print(f'[指令] 棋谱导出: {p or "失败"}')
        elif c == 'play':
            # 辅助模式: 玩家在看板点选了一个点 -> 暂存, 本轮拿到 board 后执行
            try:
                col, row = int(cmd.get('col')), int(cmd.get('row'))
            except (TypeError, ValueError):
                col = row = None
            if col is not None and 0 <= col < N and 0 <= row < N:
                _PENDING_PLAY['pt'] = (col, row)
                _PENDING_PLAY['ts'] = time.time()
                print(f'[指令] 玩家点选 ({col},{row}), 本轮执行')
            else:
                print(f'[指令] play 坐标无效: {cmd.get("col")},{cmd.get("row")}, 忽略')
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
    返回 (最终思考时间, 是否延长)。"""
    if katago is None or not getattr(cfg, 'WEIGHT_ENABLED', True):
        return per_move_time, False
    if total_moves < cfg.WEIGHT_MIN_MOVES:
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


def click_board(board, col, row, origin=None):
    """落子。origin 为取图原点: 全屏截图=(0,0); 窗口取图=窗口在屏幕上的左上角。

    窗口取图时 board 的坐标是**窗口局部**的, 必须先换回屏幕绝对坐标再点鼠标,
    否则点击会整体偏移一个窗口位置 (典型表现: 棋子落在棋盘外)。"""
    if origin is None:
        origin = _SHOT_ORIGIN
    if origin and origin != (0, 0):
        board = offset_board(board, origin[0], origin[1])
        print(f'[点击] 窗口取图坐标换算: +{origin} -> 屏幕绝对坐标')
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


PLAY_PICK_MAX_AGE = 20.0    # 玩家点选的有效期: 超过这么久还没执行就作废(棋盘可能已变)


def assist_play(cfg, board, stones, pt, move_history, side):
    """辅助模式: 玩家在看板上点选了一个点, 校验后帮其在客户端落子。

    与自动落子的唯一区别是"点从哪来" —— 这里点来自玩家, 所以必须自己把关:
    坐标范围、是否已有子、是否劫/自杀/禁手。非法就拒绝并返回原因, 不点鼠标。
    返回 (ok, msg)。
    """
    try:
        col, row = int(pt[0]), int(pt[1])
    except (TypeError, ValueError, IndexError):
        return False, '坐标无效'
    if not (0 <= col < N and 0 <= row < N):
        return False, f'({col},{row}) 超出棋盘'
    if stones[row][col] != EMPTY:
        return False, f'({col},{row}) 已有子'
    check = _stones_to_board(stones, side, move_history)
    if not check.is_legal(col, row, side):
        return False, f'({col},{row}) 非法 (劫/自杀/禁手)'
    x, y = click_board(board, col, row)
    print(f'[辅助] 玩家点选 ({col},{row}) -> 屏幕 ({x},{y}), 已落子')
    if click_confirm(cfg):
        print(f'[辅助] 已点确认 {cfg.CONFIRM_BTN}')
    return True, f'已落子 ({col},{row})'


def main():
    # === -1) 单实例保护: 同机只允许一个 controller 占棋盘 ===
    ok, owner = acquire_controller_lock()
    if not ok:
        print('=' * 56)
        print(f'[拒绝启动] 已有 controller 在运行 (PID {owner})')
        print('           两个 AI 同时执子会互相打架, 表现为"突然变傻、乱下、子数乱跳"。')
        print(f'           请先在看板点「停止 AI」, 或结束进程 PID {owner} 后重试。')
        print('=' * 56)
        # 必须"有界退出": 早期版本在这里 input() 等回车, 而看门狗拉起 controller 时
        # stdin 继承了控制台 tty -> isatty() 为真 -> 进程永远卡在 input(),
        # 既不下棋也不退出, 短时间就堆出十几个僵尸 (本机实测曾同时 11 个)。
        # 现在只短暂停留让人看清提示, 然后一定退出。
        time.sleep(3)
        sys.exit(0)

    p = argparse.ArgumentParser()
    p.add_argument('--color', choices=['black', 'white'], default=None,
                   help='我方颜色 (缺省读 settings.json, 默认黑)')
    p.add_argument('--confirm', default=None,
                   help='确认按钮 x,y 坐标 (例如 100,900). 留空则不点确认')
    p.add_argument('--interval', type=float, default=None,
                   help='截屏间隔秒数 (缺省读 settings.json)')
    p.add_argument('--think', type=float, default=None,
                   help='KataGo 每手搜索时间上限秒数 (缺省读 settings.json)')
    p.add_argument('--tmin', type=float, default=None, help='每手随机思考下限 (缺省读 settings.json)')
    p.add_argument('--tmax', type=float, default=None, help='每手随机思考上限 (缺省读 settings.json)')
    p.add_argument('--norandom', action='store_true', help='关闭随机思考时间(用固定 --think)')
    p.add_argument('--once', action='store_true', help='只跑一轮 (思考+落子) 后退出')
    p.add_argument('--debug', action='store_true', help='每步保存调试图')
    p.add_argument('--analyze-only', action='store_true',
                   help='仅分析模式: 持续调用 KataGo 评估当前局面并更新看板, 不自动落子')
    p.add_argument('--assist', action='store_true',
                   help='辅助模式: AI 只给胜率/候选, 由玩家在看板点选落子 (点棋盘或候选点即可)')
    p.add_argument('--platform', choices=['tencent', 'xingzhen'], default=None,
                   help='对手软件 (缺省读 settings.json: tencent=腾讯围棋, xingzhen=星阵围棋)')
    p.add_argument('--no-auto-stop', action='store_true',
                   help='关闭终局自动停止 (默认开启: 识别到终局后导出棋谱并退出)')
    args = p.parse_args()

    cfg = Config()
    # 单一真源: 先吃 settings.json, 命令行参数再覆盖 (见 config_store.py)
    apply_settings(cfg, {'norandom': args.norandom, 'think': args.think,
                         'tmin': args.tmin, 'tmax': args.tmax})
    if args.no_auto_stop:
        cfg.AUTO_STOP = False
    # 屏幕分辨率自适应 (最低要求 1080p)
    scr_w, scr_h = pyautogui.size()
    if scr_w < 1920 or scr_h < 1080:
        print(f'[错误] 屏幕分辨率 {scr_w}x{scr_h} 低于最低要求 1920x1080 (1080p), 无法保证棋盘识别.')
        input('按回车退出...')
        sys.exit(1)
    cfg.SCREEN_W, cfg.SCREEN_H = scr_w, scr_h
    cfg.BOARD_ROI_X_MAX = max(960, int(scr_w * 0.5))  # 棋盘软件假定在屏幕左半 (2K=1280, 1080p=960)
    print(f'[启动] 屏幕 {scr_w}x{scr_h}, 棋盘搜索区域 x<{cfg.BOARD_ROI_X_MAX}')
    if args.color is not None:
        cfg.MY_COLOR = BLACK if args.color == 'black' else WHITE
    else:
        cfg.MY_COLOR = BLACK if config_store.load_config()['color'] == 'black' else WHITE
    cfg.SCREENSHOT_INTERVAL = args.interval if args.interval else config_store.load_config()['interval']
    if args.think:
        cfg.KATAGO_MAX_TIME = max(1.0, min(float(args.think), 120.0))
    if cfg.KATAGO_TIME_MAX < cfg.KATAGO_TIME_MIN:
        cfg.KATAGO_TIME_MAX = cfg.KATAGO_TIME_MIN
    platform = args.platform or config_store.load_config()['platform']
    cfg.PLATFORM = platform
    _update_lock_meta(cfg.MY_COLOR, platform)
    print(f"[启动] 取图方式: {cfg.CAPTURE_MODE}"
          f"{' (窗口镜像已开启)' if cfg.CAPTURE_MIRROR else ''}"
          f" · 全屏截图不可用时自动回落到窗口取图")
    cfg.GO_WINDOW = (find_browser_window() if platform == 'xingzhen' else find_go_window())
    if cfg.GO_WINDOW:
        if platform == 'xingzhen':
            print(f"[启动] 锁定星阵围棋 (Edge浏览器) 窗口: {cfg.GO_WINDOW}")
        else:
            print(f"[启动] 锁定腾讯围棋窗口: {cfg.GO_WINDOW}")
    else:
        plat_cn = '星阵围棋' if platform == 'xingzhen' else '腾讯围棋'
        print(f"[启动] 未找到{plat_cn}窗口, 用全屏 x<1280 搜索")
    if args.confirm:
        x, y = args.confirm.split(',')
        cfg.CONFIRM_BTN = (int(x), int(y))
    if args.debug:
        os.makedirs(cfg.DEBUG_DIR, exist_ok=True)

    analyze_only = args.analyze_only
    assist = args.assist and not analyze_only     # 辅助模式包含分析, 二者只需其一
    _mode_cn = ' (辅助模式: 只分析, 玩家点选落子)' if assist else (' (仅分析模式)' if analyze_only else '')
    print(f"=== Go AI Auto-Player{_mode_cn} ===")
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
    print(f"platform: {platform} ({'星阵围棋' if platform == 'xingzhen' else '腾讯围棋'})")
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

    # 引擎参数 (来自 settings.json): 放水档 / 搜索线程数
    if katago is not None:
        if cfg.PLAYOUT_DOUBLING > 0:
            ok = katago.set_param('playoutDoublingAdvantage', cfg.PLAYOUT_DOUBLING)
            print(f"[启动] 放水档 playoutDoublingAdvantage={cfg.PLAYOUT_DOUBLING} "
                  f"({'已生效' if ok else '引擎不支持, 已忽略'})")
        if cfg.NUM_SEARCH_THREADS > 0:
            katago.set_param('numSearchThreads', cfg.NUM_SEARCH_THREADS)
            print(f"[启动] 搜索线程数 = {cfg.NUM_SEARCH_THREADS}")
    else:
        do_notify(cfg, 'engine_down', 'KataGo 不可用',
                  '已回退本地轻量引擎 (棋力弱), 请检查模型是否放在 katago/ 目录')

    prev_stones = None
    last_played_by_me = False  # 启动时, 假定对方刚下完, 准备我方思考
    last_move_played_at = 0  # 记录我方落子时间
    move_history = []  # 真实落子顺序 [(col,row,color),...], 还原劫(ko)状态用
    cycle = 0
    term = TerminalDetector(cfg.AUTO_STOP_CYCLES)   # 终局识别
    state = {'reported': False, 'err_notified': False}   # 终局/错误 已提示过 (防刷屏)
    print(f"[启动] 终局识别: {'开' if cfg.AUTO_STOP else '关'} "
          f"(盘面静止 {cfg.AUTO_STOP_CYCLES} 轮 + 无行棋迹象触发)")
    print(f"[启动] 配置来源: settings.json (权重阈值 {cfg.WEIGHT_WINRATE*100:.0f}%/{cfg.WEIGHT_MIN_MOVES}手)")

    while True:
        cycle += 1
        t_cycle = time.time()
        # === 0) 处理网页启动器指令 (重置/换色/导出棋谱, 不重启进程) ===
        prev_stones, last_played_by_me = handle_command(cfg, prev_stones,
                                                        last_played_by_me, move_history)
        if prev_stones is None:
            move_history.clear()  # 指令重置了对局 (set_color/reset 返回 None), 清空旧历史
            term.reset()          # 新对局 -> 终局计数归零
            state['reported'] = False
        # === 1) 截屏 (全屏优先; 认不出棋盘自动回落 PrintWindow 窗口取图) ===
        ss_path = os.path.join(cfg.DEBUG_DIR, f'cycle_{cycle:03d}.png') if args.debug else None
        img, board, stones, win_rect = read_board_frame(cfg, ss_path)
        if win_rect is not None:
            cfg.GO_WINDOW = win_rect
        # === 2) 读棋盘 ===
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
            move_history.clear()
            warn_history_once(cycle, '落子历史与棋盘不一致')
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

        # === 3A) 仅分析 / 辅助模式: AI 不自动落子 ===
        #   --analyze-only : 只评估, 写看板, 永不出手
        #   --assist       : 评估 + 等玩家在看板点选, 收到后帮他在客户端落子
        if analyze_only or assist:
            # 判断当前该谁下: 优先 UI 视觉识别 (红章 / 头像水滴), 回退子数法
            analyze_side = None
            side_src = '?'
            red = None
            if platform == 'xingzhen' and AVATAR_DETECT_AVAILABLE and cfg.GO_WINDOW:
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
            tag = '辅助' if assist else '仅分析'
            if katago is not None:
                per_move_time = clamp_random(cfg.KATAGO_TIME_MIN, cfg.KATAGO_TIME_MAX) if cfg.KATAGO_TIME_RANDOM else cfg.KATAGO_MAX_TIME
                print(f"[{cycle}] [{tag}] 当前轮: {'黑' if analyze_side==BLACK else '白'} ({side_src}), "
                      f"评估上限 {per_move_time:.1f}s ...")
                try:
                    eb = _stones_to_board(stones, analyze_side, move_history)
                    katago.set_board(eb.g, history=move_history)
                    mv = katago.genmove(analyze_side, max_time=per_move_time)
                    print(f"[{cycle}] [{tag}] KataGo 推荐 ({mv[0]},{mv[1]}) — 已写入日志, 看板将更新")
                except Exception as e:
                    print(f"[{cycle}] [{tag}] 评估出错: {e}")
            else:
                print(f"[{cycle}] [{tag}] 无 KataGo 引擎, 跳过评估")

            # ---- 辅助模式: 执行玩家点选 ----
            if assist:
                pt = _PENDING_PLAY['pt']
                if pt is not None and (time.time() - _PENDING_PLAY['ts']) > PLAY_PICK_MAX_AGE:
                    print(f"[{cycle}] [辅助] 点选 {pt} 已过期 (>{PLAY_PICK_MAX_AGE:g}s), 作废")
                    _PENDING_PLAY['pt'] = None
                    pt = None
                if pt is not None:
                    _PENDING_PLAY['pt'] = None
                    ok, msg = assist_play(cfg, board, stones, pt, move_history, cfg.MY_COLOR)
                    print(f"[{cycle}] [辅助] {'✓' if ok else '✗'} {msg}")
                    if ok:
                        # 等动画稳定再读一帧, 把玩家这一手并入历史 (劫 ko 状态要用真实手顺)
                        time.sleep(2.2)
                        img2, _b2, s2, wr2 = read_board_frame(cfg)
                        if wr2:
                            cfg.GO_WINDOW = wr2
                        if s2 is not None:
                            merge_move_history(move_history, stones, s2)
                            if move_history and not history_matches(move_history, s2):
                                move_history.clear()
                            prev_stones = s2
                            last_played_by_me = True
                        else:
                            prev_stones = stones
                    time.sleep(1.0)
                    continue
            prev_stones = stones
            time.sleep(cfg.SCREENSHOT_INTERVAL)
            continue

        # === 3) 决策: 是否我方行棋 (优先用 UI 红章判断, 子数法 fallback) ===
        side_turn = is_my_turn_from_img(img, cfg.MY_COLOR, cfg.GO_WINDOW, platform=platform)
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
            # 终局识别 (对方回合也不会漏判): 盘面长期静止且已无行棋迹象 => 结束
            try:
                # side_turn 为 None 表示界面已识别不出"谁在行棋" (终局界面的典型特征)
                r = term.update(stones, mv=None,
                                side_turn=(None if side_turn is None else 'known'))
            except Exception:
                r = None
            if r:
                if finish_game(cfg, r, move_history, stones, None, katago):
                    return
                term.reset()
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
        mv_forced = False     # True = 超时/报错兜底的 (-1,-1), 不算引擎主动虚着
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
            if not state['err_notified']:      # 出错只通知一次, 避免刷屏
                do_notify(cfg, 'error', 'KataGo 思考出错', str(result['err'])[:300])
                state['err_notified'] = True
            mv = (-1, -1)
            mv_forced = True                       # 引擎报错兜底, 非主动虚着
        elif 'mv' not in result:
            print(f"[{cycle}] 思考超时, 退一手 (-1, -1)")
            mv = (-1, -1)
            mv_forced = True                       # 超时兜底, 非主动虚着
        else:
            mv = result['mv']
            print(f"[{cycle}] 决策: ({mv[0]}, {mv[1]}) 用时 {result.get('dt', 0):.2f}s")

        # === 4.5) 决策合法性校验 (含劫) + 非法自动切换次优候选 ===
        #   非法情形: 1) 已占(KataGo 视角与读盘不一致)  2) 劫/自杀/禁手
        #   策略: 最佳点非法 -> 自动切换到 KataGo 本次搜索的下一候选 (gtp_logs, 与看板同源),
        #         跳过已试/已占/劫·禁手点; 候选列表无可用的 -> 重截图让 KataGo 重算;
        #         多轮仍无可用 -> 本轮放弃落子 (杜绝无限循环强行走同一非法点)
        tried = set()
        for retry in range(6):
            legal_check = _stones_to_board(stones, cfg.MY_COLOR, move_history)
            if katago is None or mv == (-1, -1) or not (0 <= mv[0] < N and 0 <= mv[1] < N):
                break  # 虚着/无引擎, 无需校验
            occupied = stones[mv[1], mv[0]] != EMPTY
            blacklisted = is_play_blacklisted(mv[0], mv[1])
            if not occupied and not blacklisted and legal_check.is_legal(mv[0], mv[1], cfg.MY_COLOR):
                break  # 决策点合法且为空, 通过
            reason = ('连续落子失败已拉黑' if blacklisted
                      else ('已占' if occupied else '劫/自杀/禁手'))
            tried.add((mv[0], mv[1]))
            # 1) 自动切换 KataGo 本次搜索的下一合法候选点
            alt = next_legal_candidate(stones, legal_check, tried, cfg.MY_COLOR)
            if alt is not None:
                print(f"[{cycle}] 决策点 ({mv[0]},{mv[1]}) {reason}, 自动改下候选 ({alt[0]},{alt[1]})")
                mv = alt
                break
            # 2) 候选列表无可用的(可能局面已变) -> 重截图刷新, 让 KataGo 重算
            if retry >= 4:
                # 仍无可下点 -> 放弃本轮, 等对方落子/局面变化, 防死循环
                print(f"[{cycle}] 决策点 ({mv[0]},{mv[1]}) {reason} 且无可用候选, 本轮放弃落子")
                prev_stones = stones
                time.sleep(cfg.SCREENSHOT_INTERVAL)
                continue
            print(f"[{cycle}] 决策点 ({mv[0]},{mv[1]}) {reason}, retry {retry+1} 重算")
            img_v, board_v, stones_v, win_rect = read_board_frame(cfg)
            if win_rect:
                cfg.GO_WINDOW = win_rect
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
        img2, board2, stones2, win_rect2 = read_board_frame(cfg, verify_path)
        if win_rect2:
            cfg.GO_WINDOW = win_rect2
        if board2 is not None and stones2 is not None:
            # 把我方刚落的子记入真实历史 (stones2 = 落子后局面)
            merge_move_history(move_history, prev_stones, stones2)
            if move_history and not history_matches(move_history, stones2):
                move_history.clear()
                warn_history_once(cycle, '落子后历史不一致')
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
            if ok:
                if _FAILED_PTS:
                    print(f"[{cycle}] 落子链路恢复正常, 清空失败点黑名单 ({len(_FAILED_PTS)} 个)")
                note_play_result(mv[0], mv[1], True)
            elif mv != (-1, -1):
                n_fail = note_play_result(mv[0], mv[1], False)
                if n_fail >= PLAY_FAIL_PT_MAX:
                    print(f"[{cycle}] 警告: ({mv[0]},{mv[1]}) 连续 {n_fail} 次落子未生效, "
                          f"已拉黑该点, 下一手改下候选列表里的其他点")
                else:
                    print(f"[{cycle}] 警告: 落子核对失败, 可能需要重试")
        # === 7.5) 终局识别 (引擎认输 / 连续虚着 / 盘面长期静止) ===
        try:
            r = term.update(stones2 if stones2 is not None else stones, mv=mv,
                            side_turn='known', forced_pass=mv_forced)
        except Exception:
            r = None
        if r and not state['reported']:
            wr = None
            if katago is not None:
                try:
                    wr = katago.get_winrate(time=1.5)
                    if wr is not None and cfg.MY_COLOR == WHITE:
                        wr = 1.0 - wr            # 转成我方胜率
                except Exception:
                    wr = None
            if finish_game(cfg, r, move_history, stones2, wr, katago):
                return                            # 自动停止: 直接退出进程
            state['reported'] = True              # 未开自动停止: 只提示一次, 不刷屏
        elif r:
            term.reset()                          # 已提示过, 继续跑但不重复通知
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

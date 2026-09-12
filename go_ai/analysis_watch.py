# -*- coding: utf-8 -*-
"""KataGo 实时分析看板 + 网页版启动器后端 (星阵风格):
- 后台线程每 25s 解析最新日志 → data.json + 更新棋盘标记图 analysis_overlay.png
- 本地 HTTP 服务 (端口 8123) → 浏览器打开 analysis.html 实时查看
- 提供网页版启动器 API (整合 bat/launcher 全部功能):
    GET  /api/status   进程状态 + 配置
    POST /api/start    启动 AI (执黑/执白/辅助模式, 可传 tmin/tmax/interval/mode)
    POST /api/stop     停止 AI / KataGo 预加载 (看板自身保留)
    POST /api/preload  确保 KataGo 常驻服务在跑
    POST /api/config   修改并保存启动配置 (tmin/tmax/interval/color)
    POST /api/reset    重开一局 (AI 进程不重启, 通过 _command.json 切色+重置)
    POST /api/pick     辅助模式: 玩家点选落子 {col,row} -> 写入 _command.json 由 controller 执行
"""
import sys, os, json, re, time, threading, functools, http.server, socketserver, socket
import numpy as np
import pyautogui
import cv2

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, OUT_DIR)
from show_analysis import (find_latest_valid_log, extract_last_search, draw,
                          read_tail_text, prune_gtp_logs)
from go_vision import detect_board, read_board, refine_pts, N, grab_for_read
from go_controller import count_stones
import go_launcher as L
from go_launcher import (
    SERVICE_PORT, LEVELS, PLATFORMS, PLATFORM_LABELS, load_config, save_config, is_running, status,
    start_ai, start_watch, start_preload, kill_matching, _stop_preload,
)
import notify as notify_mod
import sgf as sgf_mod
import config_store

DATA_JSON = os.path.join(OUT_DIR, 'data.json')
WIN_HIST_JSON = os.path.join(OUT_DIR, 'win_history.json')  # 胜率走势: {"ai_color":..,"pts":[[手数,AI胜率],..]}
COMMAND_FILE = os.path.join(OUT_DIR, '_command.json')
GAME_RESULT_JSON = os.path.join(OUT_DIR, 'game_result.json')   # 终局结果 (controller 写)
PORT = 8123
REFRESH = 2  # 秒, 后端更新频率

# ---- 看门狗状态 (controller 异常退出后自动拉起) ----
_RESTART = {'armed': False, 'count': 0, 'seen_at': 0.0, 'last_try': 0.0}
_LAST_STOP = [0.0]      # 最近一次主动停止的时间戳 (停止后短时间内不自动重启)

# 胜率历史去重状态 (进程内)
_hist_last_total = None  # 上次记录时的棋盘总手数


def _ai_color():
    """AI 实际执子颜色: 优先从运行中 controller 命令行解析 --color, 否则用配置默认色."""
    try:
        ctrl_r, _, _, ctrl_list, _ = is_running()
        for _pid, _cmd in ctrl_list:
            m = re.search(r'--color\s+(black|white)', _cmd or '')
            if m:
                return m.group(1)
    except Exception:
        pass
    return load_config().get('color', 'black')


def ai_win_of(side, win, ai_color):
    """把 '行棋方(分析方)胜率' win (0~1) 转成 AI 视角胜率 (0~1).
    行棋方 == AI 执色 => win 即 AI 胜率; 否则取补 (对手胜率)."""
    if not side or win is None:
        return None
    if side == ai_color[0].upper():
        return win
    return 1.0 - win


def load_win_history():
    try:
        with open(WIN_HIST_JSON, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'ai_color': None, 'pts': []}


def save_win_history(ai_color, pts):
    tmp = WIN_HIST_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'ai_color': ai_color, 'pts': pts}, f, ensure_ascii=False)
    os.replace(tmp, WIN_HIST_JSON)


def clear_win_history():
    """重开一局时清空胜率历史 (文件删除 + 状态复位)."""
    global _hist_last_total
    _hist_last_total = None
    try:
        if os.path.exists(WIN_HIST_JSON):
            os.remove(WIN_HIST_JSON)
    except OSError:
        pass


def record_win_history(ai_win_pct, total_stones):
    """记录 (总子数, AI胜率%) 到历史曲线.
    - 总子数相比上次骤降(>10) -> 视为新对局, 清空重记
    - 提子导致的小幅下降不重复记点 (x 保持单调, 曲线不回折)
    - 手数推进才新增数据点"""
    global _hist_last_total
    if ai_win_pct is None or total_stones is None:
        return
    his = load_win_history()
    pts = his.get('pts', [])
    if _hist_last_total is not None and total_stones < _hist_last_total - 10:
        pts = []  # 新对局
    _hist_last_total = int(total_stones)
    if not pts or total_stones > pts[-1][0]:
        pts.append([int(total_stones), float(ai_win_pct)])
        save_win_history(his.get('ai_color'), pts[-500:])  # 最多保留 500 点


_last_stones = None      # 上一次识别的盘面 (用于推算"最后一手", 数字棋盘标记用)
_last_move = None        # [[col, row], ...] 最近一手
_last_capture = 'none'   # 最近一次取图方式: window / screen / none


def _capture_cfg():
    """取图配置 -> (mode, mirror)。mirror='auto' 时按平台给默认值。

    说明: PrintWindow 抓出来是否镜像因客户端而异 (实测 Chromium 系一般**不需要**翻转)。
    腾讯客户端历史上一直走翻转, 这里保持原行为; 浏览器 (星阵) 默认不翻转。
    看板里能直接看到数字棋盘, 左右反了一眼就能发现 -> 面板里有一键切换。
    """
    cfg = load_config().get('capture') or {}
    mode = cfg.get('mode') or 'auto'
    m = cfg.get('mirror') or 'auto'
    platform = load_config().get('platform') or 'tencent'
    if m == 'auto':
        mirror = (platform != 'xingzhen')
    else:
        mirror = (m == 'on')
    return mode, mirror


def cur_board_snapshot():
    """识别当前盘面。返回 (stones, counts, mode) 或 (None, None, mode)。

    优先用 PrintWindow 抓**窗口内容**再识别 —— 这是后台运行的关键:
    围棋窗口被别的程序盖住、部分遮挡甚至最小化时都能读到盘, 不再依赖前台全屏截图。
    抓不到窗口才回落到全屏截图。mode 为 'window'/'screen'/'none', 供看板显示来源。
    """
    global _last_stones, _last_move, _last_capture
    cfg_all = load_config()
    platform = cfg_all.get('platform') or 'tencent'
    mode_cfg, mirror = _capture_cfg()
    prefer_window = (mode_cfg != 'screen')
    img_bgr, _origin, mode = grab_for_read(platform, prefer_window=prefer_window,
                                           mirror=mirror)
    if mode_cfg == 'window' and mode != 'window':
        _last_capture = mode
        return None, None, mode          # 指定只用窗口取图时不做全屏回落
    _last_capture = mode
    if img_bgr is None:
        return None, None, 'none'
    try:
        board = detect_board(img_bgr, roi_x_max=1280)
        if board is None:
            return None, None, mode
        board = refine_pts(img_bgr, board)
        stones, _ = read_board(img_bgr, board)
        # 与上一帧比, 找出新增的点作为"最后一手"
        if _last_stones is not None and _last_stones.shape == stones.shape:
            diff = np.argwhere((_last_stones == 0) & (stones != 0))
            if len(diff):
                _last_move = [[int(c), int(r)] for r, c in diff]
            elif (stones == 0).all():
                _last_move = None
        _last_stones = stones
        return stones, count_stones(stones), mode
    except Exception:
        return None, None, mode


def cur_board_counts():
    """截屏识别当前棋盘子数 (黑, 白). 失败返回 None (兼容旧调用)。"""
    stones, counts, _mode = cur_board_snapshot()
    return counts


def build_json(data, stale=False, cur=None, log=None, ai_color='black', snap=None):
    """构造 data.json 载荷. 新增:
      - ai_color : AI 实际执子颜色 (black/white)
      - ai_win   : 当前局面 AI 胜率 (0~100, AI 视角)
      - cands[].ai_win : 候选点落子后 AI 胜率 (AI 视角)
      - stones   : 19x19 盘面 (0空/1黑/2白) —— 前端"数字棋盘"直接渲染, 不需要截图
      - last_move: [[col,row],...] 最近一手 (数字棋盘标注用)
      - capture  : 'window' 后台窗口取图 / 'screen' 全屏截图
    root_win / cands[].win 仍保留原始'行棋方胜率'以兼容旧逻辑."""
    cands = sorted(data['cands'], key=lambda c: (-c['visits'], -c['win']))
    side = data['side']
    root_ai = ai_win_of(side, data.get('root_win'), ai_color)
    out_c = []
    for c in cands[:15]:
        out_c.append({
            'pt': c['pt'],
            'win': round(c['win'] * 100, 1),          # 行棋方视角 (原始)
            'ai_win': round((ai_win_of(side, c['win'], ai_color) or 0) * 100, 1),
            'visits': c['visits'],
            'score': c.get('score'),
        })
    side_cn = '黑方' if data['side'] == 'B' else ('白方' if data['side'] == 'W' else '?')
    root = data.get('root_win')
    stones_json = None
    last_move = None
    capture = 'none'
    if snap:
        st, _cnt, mode = snap
        capture = mode
        if st is not None:
            stones_json = [[int(v) for v in row] for row in st]
            last_move = _last_move
    return {
        'ts': time.strftime('%H:%M:%S'),
        'side': data['side'],
        'side_cn': side_cn,
        'ai_color': ai_color,
        'root_win': round(root * 100, 1) if root is not None else None,
        'ai_win': round(root_ai * 100, 1) if root_ai is not None else None,
        'think_s': round(data['time'], 1),
        'cands': [] if stale else out_c,
        'total': len(data['cands']),
        'stale': stale,
        'cur_counts': cur,
        'log_counts': log,
        'stones': stones_json,
        'last_move': last_move,
        'capture': capture,
    }


def _supervise():
    """看门狗: controller 进程异常退出后自动拉起。

    三种"不该重启"的情况都排除掉了:
      1) 用户主动点了停止 (120s 冷却)
      2) 终局自动停止退出 (10 分钟内有 game_result.json)
      3) 从未启动过 (只监督, 不擅自开跑)
    连续重启次数超过 max_restarts 会停下, 避免崩溃循环。
    """
    wd = load_config().get('watchdog') or {}
    if not wd.get('auto_restart', True):
        return
    now = time.time()
    if is_running()[0]:
        _RESTART['armed'] = True
        _RESTART['seen_at'] = now
        if _RESTART['count'] and now - _RESTART['last_try'] > 300:
            _RESTART['count'] = 0        # 稳定跑够 5 分钟, 重启计数清零
        return
    if not _RESTART['armed']:
        return
    if now - _RESTART['seen_at'] < 20:          # 刚消失, 留出正常启动/退出时间
        return
    if now - _LAST_STOP[0] < 120:               # 主动停止
        return
    try:
        if os.path.exists(GAME_RESULT_JSON) and \
                now - os.path.getmtime(GAME_RESULT_JSON) < 600:
            return                              # 终局自动退出
    except OSError:
        pass
    if _RESTART['count'] >= int(wd.get('max_restarts', 5)):
        return
    if now - _RESTART['last_try'] < 30:
        return
    _RESTART['last_try'] = now
    _RESTART['count'] += 1
    cfg = load_config()
    print(f"[watchdog] controller 已退出, 自动重启 (第 {_RESTART['count']} 次)", flush=True)

    def _bg():
        try:
            # start_ai 内部可能阻塞等 KataGo 冷启动, 必须放后台, 否则卡住看板
            start_ai(cfg.get('color', 'black'), cfg, False, mode=cfg.get('mode', 'auto'))
            print('[watchdog] 重启完成', flush=True)
        except Exception as e:
            print('[watchdog] 重启失败:', repr(e), flush=True)
    threading.Thread(target=_bg, daemon=True).start()


def loop():
    _prune_tick = 0
    try:
        prune_gtp_logs()      # 启动先清一次旧日志
    except Exception:
        pass
    while True:
        try:
            _prune_tick += 1
            if _prune_tick % 300 == 0:     # 约每 10 分钟(2s 周期)清一次
                try:
                    n = prune_gtp_logs()
                    if n:
                        print(f'[prune] 清理 {n} 个旧 gtp 日志')
                except Exception:
                    pass
            if _prune_tick % 10 == 0:      # 约每 20s 检查一次 controller 存活
                try:
                    _supervise()
                except Exception as e:
                    print('[watchdog] ERR', repr(e))
            log = find_latest_valid_log()
            if log:
                data = extract_last_search(log)
                if data and data['cands'] and data['root_win'] is not None:
                    # 后台取图 + 识别 (窗口 PrintWindow 优先, 失败回落全屏)
                    snap = cur_board_snapshot()
                    cur = snap[1] if snap[0] is not None else None
                    stale = False
                    lb = data.get('board_counts')
                    if cur is not None and lb and lb[0] is not None:
                        # 屏幕棋盘与 KataGo 日志局面子数差异大 => 已换对局/未同步
                        if abs(cur[0] - lb[0]) > 3 or abs(cur[1] - lb[1]) > 3:
                            stale = True
                    ai_color = _ai_color()
                    ai_pct = None
                    total = None
                    if not stale:
                        # AI 视角胜率 (0~100) + 盘面总子数(曲线 x 轴)
                        ai_frac = ai_win_of(data['side'], data.get('root_win'), ai_color)
                        ai_pct = round(ai_frac * 100.0, 1) if ai_frac is not None else None
                        if cur is not None:
                            total = int(cur[0] + cur[1])
                        elif lb and lb[0] is not None:
                            total = int(lb[0] + lb[1])
                        record_win_history(ai_pct, total)
                    payload = json.dumps(build_json(data, stale=stale, cur=cur, log=lb,
                                                    ai_color=ai_color, snap=snap),
                                         ensure_ascii=False)
                    tmp = DATA_JSON + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        f.write(payload)
                    os.replace(tmp, DATA_JSON)  # 原子替换, 前端不会读到半截 JSON
                    draw(stale=stale)  # 更新 analysis_overlay.png (实拍视图用)
                    print(f"[{time.strftime('%H:%M:%S')}] updated "
                          f"side={data['side']} ai={ai_color} ai_win={ai_pct} "
                          f"cands={len(data['cands'])} cur={cur} log={lb} stale={stale} "
                          f"capture={snap[2] if snap else 'none'}")
        except Exception as e:
            print('ERR', repr(e))
        time.sleep(REFRESH)


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """禁用缓存, 保证前端每次拉取最新棋盘图/数据; 同时承担启动器 API 路由."""
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    # ---- API 路由 ----
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/status':
            return self._send_json(api_status())
        if path == '/api/kata_progress':
            return self._send_json(api_kata_progress())
        if path == '/api/game_result':
            return self._send_json(api_game_result())
        if path == '/api/board':
            return self._send_json(api_board())
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
        try:
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}
        path = self.path.split('?')[0]
        try:
            if path == '/api/start':   resp = api_start(body)
            elif path == '/api/stop':  resp = api_stop(body)
            elif path == '/api/preload': resp = api_preload(body)
            elif path == '/api/config': resp = api_config(body)
            elif path == '/api/reset':  resp = api_reset(body)
            elif path == '/api/platform': resp = api_platform(body)
            elif path == '/api/export_sgf': resp = api_export_sgf(body)
            elif path == '/api/notify': resp = api_notify(body)
            elif path == '/api/capture': resp = api_capture(body)
            elif path == '/api/pick': resp = api_pick(body)
            else: resp = {'ok': False, 'err': f'unknown:{path}'}
        except Exception as e:
            resp = {'ok': False, 'err': str(e)}
        return self._send_json(resp)


# ============== API 实现 ==============
def _controller_pid_list():
    ctrl_r, _, _, ctrl_list, _ = is_running()
    return [p[0] for p in ctrl_list]


def api_status():
    """进程状态 + 当前配置 + 看板最新数据时间."""
    ctrl_r, watch_r, svc, ctrl_list, watch_list = is_running()
    cfg = load_config()
    last_ts = None
    try:
        with open(DATA_JSON, encoding='utf-8') as f:
            last_ts = json.load(f).get('ts')
    except Exception:
        pass
    return {
        'ok': True,
        'controller': {'running': bool(ctrl_r), 'pids': [p[0] for p in ctrl_list]},
        'watch':      {'running': bool(watch_r), 'pids': [p[0] for p in watch_list]},
        'kata_preload': {'running': bool(svc), 'port': SERVICE_PORT},
        'config': cfg,
        'mode': cfg.get('mode', 'auto'),          # 当前落子方式 (auto/assist/analyze)
        'modes': [{'id': m, 'label': config_store.MODE_LABELS[m]} for m in config_store.VALID_MODES],
        'levels': [{'name': k, 'tmin': v[0], 'tmax': v[1]} for k, v in LEVELS.items()],
        'platforms': [{'id': p, 'label': PLATFORM_LABELS[p]} for p in PLATFORMS],
        'data_ts': last_ts,
        'sgf_dir': _sgf_dir(),
        'game_result': _read_game_result(),
        'watchdog': dict(_RESTART, auto=bool((cfg.get('watchdog') or {}).get('auto_restart', True))),
        'capture': dict(cfg.get('capture') or {}, last=_last_capture,
                        mirror_on=_capture_cfg()[1]),
        'dashboard': cfg.get('dashboard') or {},
    }


def _read_game_result():
    try:
        with open(GAME_RESULT_JSON, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def api_kata_progress():
    """KataGo 预加载/OpenCL 调优进度 (供看板启动闸门轮询).

    preload.running = 引擎服务已就绪 (端口可连, 即调优+加载完成)
    tuning.step/total = 最近一次调优进度 (Tuning x/55)
    cold_start = 最近日志正处于冷启动阶段
    """
    _ctrl_r, _watch_r, svc, _cl, _wl = is_running()
    log_path = os.path.join(OUT_DIR, 'kata_service.log')
    lines = []
    try:
        lines = read_tail_text(log_path, 128 * 1024).splitlines()   # 只读尾部 128KB
    except Exception:
        pass
    step = total = None
    for ln in reversed(lines):
        m = re.search(r'Tuning\s+(\d+)/(\d+)', ln)
        if m:
            step, total = int(m.group(1)), int(m.group(2))
            break
    cold = any('冷启动 KataGo' in ln or 'Tuning ' in ln or 'KataGo 启动失败' in ln
               for ln in lines[-8:])
    starting = False
    try:
        if OUT_DIR not in sys.path:
            sys.path.insert(0, OUT_DIR)
        from kata_service import service_state as _ss
        _st = _ss(SERVICE_PORT)
        starting = (_st == 'starting')
        if _st == 'ready':
            svc = True
    except Exception:
        starting = False
    return {
        'ok': True,
        'preload': {'running': bool(svc), 'starting': bool(starting), 'port': SERVICE_PORT},
        'tuning': {'step': step, 'total': total},
        'cold_start': bool(cold or starting),
        'log_tail': lines[-18:],
    }


_START_SEQ = [0]     # 启动序号: 新的启动或停止都会递增, 使在途启动作废


def api_start(body):
    """启动 AI controller. 已运行则拒绝.
    body: {color, tmin, tmax, interval, watch, mode}
    mode: auto=AI 自动落子 | assist=只分析+玩家点选落子 | analyze=只分析不出手"""
    color = body.get('color', 'black')
    if color not in ('black', 'white'):
        return {'ok': False, 'err': 'color must be black/white'}
    cfg = load_config()
    if 'mode' in body and body['mode'] is not None:
        if body['mode'] not in config_store.VALID_MODES:
            return {'ok': False, 'err': 'mode must be auto/assist/analyze'}
        cfg['mode'] = body['mode']
    mode = cfg.get('mode', 'auto')
    for k in ('tmin', 'tmax', 'interval'):
        if k in body and body[k] is not None:
            try: cfg[k] = float(body[k])
            except (TypeError, ValueError): pass
    if 'watch' in body: cfg['watch'] = bool(body['watch'])
    cfg['color'] = color
    if cfg['tmin'] > cfg['tmax']:
        cfg['tmin'], cfg['tmax'] = cfg['tmax'], cfg['tmin']
    save_config(cfg)
    if is_running()[0]:
        return {'ok': False, 'msg': 'controller 已在运行, 请先停止', 'config': cfg}

    _START_SEQ[0] += 1
    token = _START_SEQ[0]
    # 看门狗: 由本次启动"武装", 之后 controller 若异常消失会自动拉起
    _RESTART['armed'] = True
    _RESTART['seen_at'] = time.time()
    _RESTART['last_try'] = time.time()

    def _bg():
        try:
            start_ai(color, cfg, cfg['watch'], mode=mode)
        except Exception as e:
            print('ERR start_ai:', repr(e), flush=True)
            return
        if token != _START_SEQ[0]:
            # 启动过程中用户点了停止/重新启动 -> 撤销本次启动, 避免留下孤儿 controller
            try:
                n = kill_matching('go_controller')
                print(f'[start] 启动期间收到停止指令, 已撤销本次启动 (杀掉 {n} 个 controller)',
                      flush=True)
            except Exception:
                pass

    # 后台启动: start_ai 内部可能同步等 KataGo 冷启动(最长 180s), 不能阻塞 HTTP 请求
    threading.Thread(target=_bg, daemon=True).start()
    mode_cn = config_store.MODE_LABELS.get(mode, mode)
    return {
        'ok': True,
        'starting': True,
        'mode': mode,
        'msg': f'AI 启动中 ({"执黑" if color == "black" else "执白"} · {mode_cn}), 详见 controller_launcher.log',
        'config': cfg,
    }


def api_pick(body):
    """辅助模式: 玩家在看板点选了一个落子点 -> 写入 _command.json, controller 下一轮执行。
    body: {col, row} (0-based, col 左→右, row 上→下, 与看板数字棋盘一致)"""
    if not is_running()[0]:
        return {'ok': False, 'err': 'controller 未运行 (先以「辅助模式」启动 AI)'}
    try:
        col, row = int(body.get('col')), int(body.get('row'))
    except (TypeError, ValueError):
        return {'ok': False, 'err': 'col/row 必须是整数'}
    if not (0 <= col < N and 0 <= row < N):
        return {'ok': False, 'err': f'坐标越界: ({col},{row})'}
    try:
        with open(COMMAND_FILE, 'w', encoding='utf-8') as f:
            json.dump({'cmd': 'play', 'col': col, 'row': row,
                       'ts': time.time()}, f, ensure_ascii=False)
    except OSError as e:
        return {'ok': False, 'err': f'写入指令失败: {e}'}
    return {'ok': True, 'msg': f'已提交落子 ({col},{row}), 等待 controller 执行',
            'col': col, 'row': row}


def api_platform(body):
    """切换对手软件 (腾讯围棋/星阵围棋). 不影响运行中 controller, 下次启动生效."""
    cfg = load_config()
    pf = body.get('platform')
    if pf not in PLATFORMS:
        return {'ok': False, 'err': f'platform must be one of {PLATFORMS}', 'config': cfg}
    cfg['platform'] = pf
    save_config(cfg)
    return {
        'ok': True,
        'msg': f'已切换到 {PLATFORM_LABELS[pf]}, 下次启动 AI 生效',
        'config': cfg,
    }


def api_stop(body):
    """停止 AI / KataGo 预加载. 看板自身永不自杀 (关浏览器即可不再查看)."""
    what = body.get('what', 'all')
    _START_SEQ[0] += 1          # 作废在途的异步启动, 防止停掉之后又被拉起来
    _LAST_STOP[0] = time.time()  # 主动停止 -> 看门狗 120s 内不自动重启
    _RESTART['count'] = 0
    n_ai = kill_matching('go_controller') if what in ('all', 'ai') else 0
    n_ka = _stop_preload() if what in ('all', 'katago') else 0
    color_cn = '黑' if load_config().get('color', 'black') == 'black' else '白'
    return {
        'ok': True,
        'killed_ai': n_ai, 'killed_katago': n_ka,
        'msg': f'已停止 AI x{n_ai}, KataGo 预加载 x{n_ka} (看板保留)',
    }


def api_preload(body):
    """启动 KataGo 预加载. 立即返回, 实际启动在后台线程 (冷启动最长 180s).

    原来在请求线程里同步等待整个冷启动, 浏览器请求会挂住数分钟,
    用户以为按钮没反应 -> 反复点击. 进度改由 /api/kata_progress 查询."""
    cfg = load_config()
    mt = max(cfg.get('tmin', 5.0), cfg.get('tmax', 10.0))

    def _bg():
        try:
            start_preload(mt)
        except Exception as e:
            print('ERR preload:', repr(e), flush=True)

    threading.Thread(target=_bg, daemon=True).start()
    return {
        'ok': True,
        'starting': True,
        'msg': 'KataGo 预加载已启动 (后台冷启动中), 进度见 /api/kata_progress',
    }


def api_config(body):
    """保存参数 (level/tmin/tmax/interval/color/watch). 不影响运行中进程 (仅下次启动生效).
    - 传 level (如 '中级') -> 自动按 LEVELS 映射设置 tmin/tmax
    - 手动传 tmin/tmax -> level 自动置 '自定义'"""
    cfg = load_config()
    # 智力档位优先
    if 'level' in body:
        lv = body['level']
        if lv in LEVELS:
            cfg['level'] = lv
            cfg['tmin'], cfg['tmax'] = LEVELS[lv]
        elif lv == '自定义':
            cfg['level'] = '自定义'
    for k in ('tmin', 'tmax', 'interval', 'color', 'watch'):
        if k in body:
            cfg[k] = body[k]
    # 手动微调思考时间 -> 脱离内置档位
    if ('tmin' in body or 'tmax' in body) and cfg.get('level') in LEVELS:
        cfg['level'] = '自定义'
    if cfg['tmin'] > cfg['tmax']:
        cfg['tmin'], cfg['tmax'] = cfg['tmax'], cfg['tmin']
    if cfg.get('color') not in ('black', 'white'):
        cfg['color'] = 'black'
    save_config(cfg)
    return {'ok': True, 'config': cfg, 'msg': f"已保存 (档位: {cfg.get('level')})"}


def api_reset(body):
    """重开一局: 切色 + 清零对局状态, AI 进程不重启.
    流程: 写 _command.json -> controller 下次循环自动切色并重置;
          立即把 data.json 置 stale -> 前端立刻显示等待 AI 同步;
          清空胜率历史; 重新画一张 stale overlay 图."""
    color = body.get('color', 'black')
    if color not in ('black', 'white'):
        return {'ok': False, 'err': 'color must be black/white'}
    # 1) 写 _command.json (controller handle_command 会读+删+执行)
    with open(COMMAND_FILE, 'w', encoding='utf-8') as f:
        json.dump({'cmd': 'set_color', 'color': color}, f, ensure_ascii=False)
    # 1.5) 清空胜率历史 (新对局从零开始) + 清掉上局结果 (看门狗据此识别"终局主动退出")
    clear_win_history()
    try:
        if os.path.exists(GAME_RESULT_JSON):
            os.remove(GAME_RESULT_JSON)
    except OSError:
        pass
    # 2) 立即 stale 看板数据, 提示前端进入"等待新对局"状态
    stale_payload = {
        'ts': time.strftime('%H:%M:%S'),
        'side': '?', 'side_cn': '?',
        'ai_color': color, 'ai_win': None,
        'root_win': None, 'think_s': 0,
        'cands': [], 'total': 0,
        'stale': True,
        'cur_counts': None, 'log_counts': [None, None],
        'reset': {'color': color, 'time': time.strftime('%H:%M:%S'),
                  'msg': '已重置, 等待 controller 切换并同步新对局'},
    }
    tmp = DATA_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(stale_payload, ensure_ascii=False))
    os.replace(tmp, DATA_JSON)
    # 3) 重新画一张 stale overlay 图 (画完新 controller 会覆盖)
    try:
        draw(stale=True)
    except Exception as e:
        print('reset overlay fail:', repr(e))
    # 4) 持久化新颜色
    cfg = load_config()
    cfg['color'] = color
    save_config(cfg)
    color_cn = '黑' if color == 'black' else '白'
    return {
        'ok': True, 'color': color,
        'msg': f'已重置对局, 执{color_cn}, AI 进程未重启 (下个循环生效)',
    }


def _sgf_dir():
    cfg = load_config()
    d = (cfg.get('sgf') or {}).get('dir') or ''
    return d or sgf_mod.default_dir()


def api_game_result():
    """返回上一局结果 (controller 终局时写入) 与棋谱目录信息。"""
    res = None
    try:
        with open(GAME_RESULT_JSON, encoding='utf-8') as f:
            res = json.load(f)
    except Exception:
        res = None
    files = []
    try:
        d = _sgf_dir()
        if os.path.isdir(d):
            fs = [os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith('.sgf')]
            fs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            files = [{'name': os.path.basename(p), 'time': time.strftime(
                '%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(p))),
                'size': os.path.getsize(p)} for p in fs[:20]]
    except Exception:
        pass
    return {'ok': True, 'result': res, 'sgf_dir': _sgf_dir(), 'files': files}


def api_export_sgf(body):
    """导出当前棋谱。
    controller 在跑 -> 下发指令让它用内存里的真实落子顺序导出 (最准);
    没跑 -> 直接用当前盘面快照导出 (只有摆盘, 无手数顺序)。"""
    cfg = load_config()
    if is_running()[0]:
        with open(COMMAND_FILE, 'w', encoding='utf-8') as f:
            json.dump({'cmd': 'export_sgf'}, f, ensure_ascii=False)
        return {'ok': True, 'msg': '已请求 controller 导出棋谱 (下个循环执行, 见 controller 日志)'}
    # 无 controller: 用当前局面快照导出
    try:
        img = np.array(pyautogui.screenshot())
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        board = detect_board(img_bgr, roi_x_max=1280)
        if board is None:
            return {'ok': False, 'err': '未识别到棋盘, 无法导出'}
        board = refine_pts(img_bgr, board)
        stones, _ = read_board(img_bgr, board)
        path = sgf_mod.save_sgf(stones=stones, out_dir=(cfg.get('sgf') or {}).get('dir') or None,
                                comment='盘面快照导出 (AI 未运行, 无落子顺序)')
        return {'ok': True, 'path': path, 'msg': f'已按当前盘面导出: {path}'}
    except Exception as e:
        return {'ok': False, 'err': str(e)}


def api_notify(body):
    """读取/保存通知设置; body 带 test=true 时立即发一条测试通知。"""
    cfg = load_config()
    if 'enabled' in body or 'url' in body:
        n = dict(cfg.get('notify') or {})
        if 'enabled' in body:
            n['enabled'] = bool(body['enabled'])
        if 'url' in body:
            n['url'] = str(body['url'] or '').strip()
        cfg['notify'] = n
        save_config(cfg)
    cur = load_config().get('notify') or {}
    if body.get('test'):
        n_cfg = cur if cur.get('enabled') and cur.get('url') else \
            {'enabled': bool(cur.get('url')), 'url': cur.get('url'), 'events': []}
        if not n_cfg.get('url'):
            return {'ok': False, 'err': '请先填写通知地址', 'notify': cur}
        ok = notify_mod.send('test', 'GoAI 测试通知', '通知通道工作正常 ✓',
                             config=n_cfg, block=True)
        return {'ok': bool(ok), 'notify': cur,
                'msg': '测试通知已发送' if ok else '发送失败, 详见 go_ai/notify.log'}
    return {'ok': True, 'notify': cur}


def api_board():
    """按需识别一次盘面 (数字棋盘刷新/`切换视图`用)。返回 stones + 最后一手。"""
    stones, counts, mode = cur_board_snapshot()
    return {
        'ok': stones is not None,
        'stones': None if stones is None else [[int(v) for v in row] for row in stones],
        'counts': list(counts) if counts else None,
        'last_move': _last_move,
        'capture': mode,
    }


def api_capture(body):
    """读取/修改取图方式。body: {mode?: auto|window|screen, mirror?: auto|on|off}"""
    cfg = load_config()
    if 'mode' in body or 'mirror' in body:
        cp = dict(cfg.get('capture') or {})
        if 'mode' in body:
            cp['mode'] = str(body['mode'])
        if 'mirror' in body:
            cp['mirror'] = str(body['mirror'])
        cfg['capture'] = cp
        save_config(cfg)
        global _last_stones, _last_move
        _last_stones = None      # 换取图方式后旧盘面作废 (镜像状态变了)
        _last_move = None
    cur = load_config().get('capture') or {}
    mode, mirror = _capture_cfg()
    return {'ok': True, 'capture': cur, 'resolved': {'mode': mode, 'mirror': mirror,
                                                     'last': _last_capture}}


class DualStackServer(socketserver.ThreadingTCPServer):
    """IPv6 socket 双栈监听 (IPv4 + IPv6 同时可访问).

    daemon_threads/block_on_close: 每个请求线程用完即弃, 不保留在 _threads 列表里
    (默认配置下该列表只增不减, 长时间轮询会持续吃内存).

    allow_reuse_address 在 Windows 上**必须关掉**: 与 Linux 只允许复用 TIME_WAIT
    不同, Windows 的 SO_REUSEADDR 允许两个进程**同时** bind 同一端口 —— 曾因此
    同时跑起两个看板(两个看门狗), 各自去拉起 controller, 造成两个 AI 抢一块棋盘、
    子数乱跳、胜率乱甩, 看起来就是"AI 突然变傻"。关掉后第二个实例 bind 失败并友好退出。
    """
    address_family = socket.AF_INET6
    allow_reuse_address = (os.name != 'nt')
    daemon_threads = True
    block_on_close = False

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def serve():
    # 双栈监听 (IPv4 + IPv6): 手机可通过 IPv4 或 IPv6 访问
    host = os.environ.get('BGI_WATCH_HOST', '::')
    handler = functools.partial(NoCacheHandler, directory=OUT_DIR)
    httpd = None
    for attempt in range(3):
        try:
            httpd = DualStackServer((host, PORT), handler)
            break
        except OSError as e:
            if attempt < 2:          # 可能只是上个实例刚退出, 端口还在 TIME_WAIT
                print(f'[看板] 端口 {PORT} 暂不可用 ({e}), 1s 后重试...', flush=True)
                time.sleep(1.0)
                continue
            print('=' * 56)
            print(f'[看板] 无法绑定端口 {PORT}: {e}')
            print('       很可能已有一个看板在运行 —— 同一台机器只应跑一个看板')
            print('       (两个看板会各自拉起 controller, 互相抢棋盘, 表现为 AI "变傻")。')
            print('       请先关掉多余的看板窗口再重试。')
            print('=' * 56)
            return
    print(f'serving http://{host}:{PORT} (IPv4+IPv6 双栈)')
    print(f'  本机: http://127.0.0.1:{PORT}/analysis.html')
    print(f'  局域网 IPv4: http://<电脑IPv4>:{PORT}/analysis.html')
    print(f'  局域网 IPv6: http://[<电脑IPv6>]:{PORT}/analysis.html')
    httpd.serve_forever()


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    serve()

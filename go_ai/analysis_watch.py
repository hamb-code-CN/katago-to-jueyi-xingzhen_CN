# -*- coding: utf-8 -*-
"""KataGo 实时分析看板 + 网页版启动器后端 (星阵风格):
- 后台线程每 25s 解析最新日志 → data.json + 更新棋盘标记图 analysis_overlay.png
- 本地 HTTP 服务 (端口 8123) → 浏览器打开 analysis.html 实时查看
- 提供网页版启动器 API (整合 bat/launcher 全部功能):
    GET  /api/status   进程状态 + 配置
    POST /api/start    启动 AI (执黑/执白, 可传 tmin/tmax/interval)
    POST /api/stop     停止 AI / KataGo 预加载 (看板自身保留)
    POST /api/preload  确保 KataGo 常驻服务在跑
    POST /api/config   修改并保存启动配置 (tmin/tmax/interval/color)
    POST /api/reset    重开一局 (AI 进程不重启, 通过 _command.json 切色+重置)
"""
import sys, os, json, re, time, threading, functools, http.server, socketserver, socket
import numpy as np
import pyautogui
import cv2

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, OUT_DIR)
from show_analysis import (find_latest_valid_log, extract_last_search, draw,
                          read_tail_text, prune_gtp_logs)
from go_vision import detect_board, read_board, refine_pts, N
from go_controller import count_stones
import go_launcher as L
from go_launcher import (
    SERVICE_PORT, LEVELS, PLATFORMS, PLATFORM_LABELS, load_config, save_config, is_running, status,
    start_ai, start_watch, start_preload, kill_matching, _stop_preload,
)

DATA_JSON = os.path.join(OUT_DIR, 'data.json')
WIN_HIST_JSON = os.path.join(OUT_DIR, 'win_history.json')  # 胜率走势: {"ai_color":..,"pts":[[手数,AI胜率],..]}
COMMAND_FILE = os.path.join(OUT_DIR, '_command.json')
PORT = 8123
REFRESH = 2  # 秒, 后端更新频率

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


def cur_board_counts():
    """截屏识别当前棋盘子数 (黑, 白). 失败返回 None"""
    try:
        img = np.array(pyautogui.screenshot())
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        board = detect_board(img_bgr, roi_x_max=1280)
        if board is None:
            return None
        board = refine_pts(img_bgr, board)
        stones, _ = read_board(img_bgr, board)
        return count_stones(stones)
    except Exception:
        return None


def build_json(data, stale=False, cur=None, log=None, ai_color='black'):
    """构造 data.json 载荷. 新增:
      - ai_color : AI 实际执子颜色 (black/white)
      - ai_win   : 当前局面 AI 胜率 (0~100, AI 视角)
      - cands[].ai_win : 候选点落子后 AI 胜率 (AI 视角)
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
    }


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
            log = find_latest_valid_log()
            if log:
                data = extract_last_search(log)
                if data and data['cands'] and data['root_win'] is not None:
                    cur = cur_board_counts()
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
                                                    ai_color=ai_color),
                                         ensure_ascii=False)
                    tmp = DATA_JSON + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        f.write(payload)
                    os.replace(tmp, DATA_JSON)  # 原子替换, 前端不会读到半截 JSON
                    draw(stale=stale)  # 更新 analysis_overlay.png
                    print(f"[{time.strftime('%H:%M:%S')}] updated "
                          f"side={data['side']} ai={ai_color} ai_win={ai_pct} "
                          f"cands={len(data['cands'])} cur={cur} log={lb} stale={stale}")
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
        if self.path.split('?')[0] == '/api/status':
            return self._send_json(api_status())
        if self.path.split('?')[0] == '/api/kata_progress':
            return self._send_json(api_kata_progress())
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
        'levels': [{'name': k, 'tmin': v[0], 'tmax': v[1]} for k, v in LEVELS.items()],
        'platforms': [{'id': p, 'label': PLATFORM_LABELS[p]} for p in PLATFORMS],
        'data_ts': last_ts,
    }


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
    """启动 AI controller (执黑/执白). 已运行则拒绝."""
    color = body.get('color', 'black')
    if color not in ('black', 'white'):
        return {'ok': False, 'err': 'color must be black/white'}
    cfg = load_config()
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

    def _bg():
        try:
            start_ai(color, cfg, cfg['watch'])
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
    return {
        'ok': True,
        'starting': True,
        'msg': f'AI 启动中 ({"执黑" if color == "black" else "执白"}), 详见 controller_launcher.log',
        'config': cfg,
    }


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
    # 1.5) 清空胜率历史 (新对局从零开始)
    clear_win_history()
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


class DualStackServer(socketserver.ThreadingTCPServer):
    """IPv6 socket 双栈监听 (IPv4 + IPv6 同时可访问).

    daemon_threads/block_on_close: 每个请求线程用完即弃, 不保留在 _threads 列表里
    (默认配置下该列表只增不减, 长时间轮询会持续吃内存)."""
    address_family = socket.AF_INET6
    allow_reuse_address = True
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
    httpd = DualStackServer((host, PORT), handler)
    print(f'serving http://{host}:{PORT} (IPv4+IPv6 双栈)')
    print(f'  本机: http://127.0.0.1:{PORT}/analysis.html')
    print(f'  局域网 IPv4: http://<电脑IPv4>:{PORT}/analysis.html')
    print(f'  局域网 IPv6: http://[<电脑IPv6>]:{PORT}/analysis.html')
    httpd.serve_forever()


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    serve()

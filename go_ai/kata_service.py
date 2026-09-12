# -*- coding: utf-8 -*-
"""KataGo 常驻服务 (预加载) + 引擎自愈看门狗

- 启动一次 KataGo (冷启动 60-90s) 后常驻后台
- 通过本地 TCP 端口对外提供接口, controller 重启时快速复用, 无需重新冷加载
- **看门狗**: KataGo 进程异常退出(OOM/显卡驱动重启/模型损坏)后自动重建引擎。
  旧版本只返回 engine_not_ready 就永远不再重建, 表现为"看板还活着、棋不下了",
  必须手动重开服务。
接口协议 (JSON lines, 一请求一响应):
  {"cmd":"ping"}                                -> {"ok":true,"ready":true,...}
  {"cmd":"status"}                              -> 引擎健康详情
  {"cmd":"set_board","stones":[[...]]}          -> {"ok":true}
  {"cmd":"genmove","color":2,"max_time":20.0}   -> {"ok":true,"mv":[c,r]}
  {"cmd":"winrate","time":1.5}                  -> {"ok":true,"winrate":0.53}
  {"cmd":"score_lead","time":1.5}               -> {"ok":true,"lead":2.5}
  {"cmd":"set_param","name":"playoutDoublingAdvantage","value":1.0}
  {"cmd":"stop"}                                -> 退出服务
用法: python kata_service.py [--port 8124] [--max-time 20.0] [--no-watchdog]
"""
import os
import sys
import json
import socket
import threading
import time
import argparse
import atexit

# 复用 KataGo 引擎封装
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from katago_engine import KataGoEngine, BLACK, WHITE

DEFAULT_PORT = 8124
HEALTH_INTERVAL = 10.0     # 健康检查周期(秒)
RESTART_BACKOFF = 30.0     # 重建失败后的退避(秒), 防止疯狂重启


class KataService:
    def __init__(self, port, max_time, watchdog=True):
        self.port = port
        self.engine = None
        self.lock = threading.Lock()
        self.max_time = max_time
        self.watchdog = watchdog
        self.restarts = 0
        self.last_error = None
        self.engine_started_at = None
        self._stopping = False
        self._next_try = 0.0

    # ---- 引擎生命周期 ----
    def start_engine(self):
        """冷启动 KataGo (首次). 阻塞直到 ready."""
        print('[kata-service] 冷启动 KataGo (60-90s)...', flush=True)
        eng = KataGoEngine(max_time=self.max_time, log_cb=lambda m: print('[KG]', m, flush=True))
        eng.start()  # 阻塞等待 GTP ready
        with self.lock:
            self.engine = eng
            self.engine_started_at = time.time()
            self.restarts = eng.restarts
        print('[kata-service] KataGo 就绪, 常驻服务运行中', flush=True)
        return eng

    def stop_engine(self):
        with self.lock:
            eng, self.engine = self.engine, None
        if eng:
            try:
                eng.stop()
            except Exception:
                pass

    def _engine_dead(self):
        """引擎是否需要重建 (进程不存在 / 已退出)。"""
        eng = self.engine
        return eng is None or not eng.alive

    def _health_loop(self):
        """看门狗: 引擎挂了就自动重建, 失败退避后再试。"""
        while not self._stopping:
            time.sleep(HEALTH_INTERVAL)
            if self._stopping:
                break
            if not self._engine_dead():
                continue
            now = time.time()
            if now < self._next_try:
                continue
            # 记录死因
            rc = None
            try:
                if self.engine is not None and self.engine.proc is not None:
                    rc = self.engine.proc.returncode
            except Exception:
                pass
            print(f'[kata-service] 检测到 KataGo 进程已退出 (returncode={rc}), 自动重建引擎...',
                  flush=True)
            try:
                self.stop_engine()
                eng = KataGoEngine(max_time=self.max_time,
                                   log_cb=lambda m: print('[KG]', m, flush=True))
                eng.start()
                with self.lock:
                    self.engine = eng
                    self.engine_started_at = time.time()
                    self.restarts = eng.restarts
                self.last_error = None
                self._next_try = 0.0
                print(f'[kata-service] 引擎已重建 (第 {eng.restarts} 次), 服务恢复', flush=True)
            except Exception as e:
                self.last_error = repr(e)
                self._next_try = time.time() + RESTART_BACKOFF
                print(f'[kata-service] 引擎重建失败: {e!r}; {RESTART_BACKOFF:.0f}s 后重试',
                      flush=True)

    def status(self):
        eng = self.engine
        return {
            'ok': True,
            'ready': bool(eng is not None and eng.alive),
            'restarts': int(self.restarts),
            'last_error': self.last_error,
            'uptime': round(time.time() - self.engine_started_at, 1)
            if self.engine_started_at else None,
            'watchdog': bool(self.watchdog),
            'max_time': self.max_time,
        }

    # ---- 请求处理 ----
    def handle(self, req):
        cmd = req.get('cmd')
        if cmd == 'ping':
            st = self.status()
            return {'ok': True, **st}
        if cmd == 'status':
            return self.status()
        if cmd == 'stop':
            self._stopping = True
            self.stop_engine()
            return {'ok': True, 'stopping': True}
        if self._engine_dead():
            return {'ok': False, 'err': 'engine_not_ready'}
        try:
            if cmd == 'set_board':
                stones = req['stones']
                history = req.get('history')
                with self.lock:
                    self.engine.set_board(stones, history=history)
                return {'ok': True}
            if cmd == 'genmove':
                color = req.get('color', WHITE)
                mt = float(req.get('max_time', self.max_time))
                with self.lock:
                    mv = self.engine.genmove(color, max_time=mt)
                return {'ok': True, 'mv': list(mv)}
            if cmd == 'winrate':
                t = float(req.get('time', 1.5))
                with self.lock:
                    wr = self.engine.get_winrate(time=t)
                return {'ok': wr is not None, 'winrate': wr}
            if cmd == 'score_lead':
                t = float(req.get('time', 1.5))
                with self.lock:
                    lead = self.engine.get_score_lead(time=t)
                return {'ok': lead is not None, 'lead': lead}
            if cmd == 'set_param':
                with self.lock:
                    ok = self.engine.set_param(req.get('name'), req.get('value'))
                return {'ok': bool(ok)}
            return {'ok': False, 'err': f'unknown_cmd:{cmd}'}
        except Exception as e:
            return {'ok': False, 'err': str(e)}


def _port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(('127.0.0.1', port)) == 0
    finally:
        s.close()


# ---------------- 启动权独占 (防止冷启动期间重复拉起引擎) ----------------
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_kata_service.lock')
LOCK_TTL = 600.0   # 锁最长有效期(秒): 超过视为异常残留


def _pid_alive(pid):
    """判断进程是否存活 (不用 os.kill: Windows 下会误杀)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = wintypes.DWORD()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return True   # 保守: 判不准时视为存活, 避免重复拉起
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_lock_info():
    """读取锁文件内容 (dict) 或 None."""
    try:
        with open(LOCK_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def service_state(port=None):
    """KataGo 常驻服务状态:
    'ready'    端口已在监听 (引擎就绪)
    'starting' 已有实例认领启动权, 正在冷启动
    'idle'     无人启动
    """
    port = DEFAULT_PORT if port is None else port
    if _port_in_use(port):
        return 'ready'
    d = read_lock_info()
    if d:
        fresh = (time.time() - float(d.get('ts') or 0)) < LOCK_TTL
        if _pid_alive(d.get('pid')) and fresh:
            return 'starting'
        try:                      # 陈旧锁 (进程已死/超时) -> 清理
            os.remove(LOCK_PATH)
        except OSError:
            pass
    return 'idle'


def _claim(port):
    """认领启动权. True=归本实例; False=已有实例(运行中或冷启动中), 本实例应退出."""
    if _port_in_use(port):
        print(f'[kata-service] 端口 {port} 已有 KataGo 服务在运行, 本实例退出 (不重复拉起引擎)',
              flush=True)
        return False
    for attempt in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({'pid': os.getpid(), 'port': port,
                                         'ts': time.time()}).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            d = read_lock_info() or {}
            if _pid_alive(d.get('pid')) and (time.time() - float(d.get('ts') or 0)) < LOCK_TTL:
                print(f'[kata-service] 另一实例 (PID {d.get("pid")}) 正在冷启动 KataGo, '
                      f'本实例退出 (不重复拉起引擎)', flush=True)
                return False
            try:                  # 陈旧锁 -> 删除后重试一次
                os.remove(LOCK_PATH)
            except OSError:
                return False
    return False


def _release_lock():
    d = read_lock_info()
    if d and d.get('pid') == os.getpid():
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


atexit.register(_release_lock)


def serve(port, max_time, watchdog=True):
    if not _claim(port):
        return 0
    try:
        svc = KataService(port, max_time, watchdog=watchdog)
        # 启动引擎 (冷加载一次)
        try:
            svc.start_engine()
        except Exception as e:
            print(f'[kata-service] KataGo 启动失败: {e}', flush=True)
            print('[kata-service] 服务仍会监听, 由看门狗自动重试冷启动', flush=True)
        if watchdog:
            threading.Thread(target=svc._health_loop, daemon=True).start()
            print(f'[kata-service] 自愈看门狗已启用 (每 {HEALTH_INTERVAL:.0f}s 检查一次)',
                  flush=True)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows 下 SO_REUSEADDR 允许同一 addr:port 被两个 socket 同时绑定(双绑),
        # 会让两个服务都"监听"同一端口、请求随机落到不同引擎; 改用独占绑定.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', port))
        srv.listen(5)
        print(f'[kata-service] 监听 127.0.0.1:{port}', flush=True)

        def client_loop(conn):
            f = conn.makefile('rwb', buffering=0)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        break
                    try:
                        req = json.loads(line.decode('utf-8').strip())
                    except Exception:
                        f.write(b'{"ok":false,"err":"bad_json"}\n')
                        continue
                    resp = svc.handle(req)
                    f.write((json.dumps(resp) + '\n').encode('utf-8'))
                    if resp.get('stopping'):
                        break
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                break
            # 注意: 引擎暂时不可用也不再退出服务 (旧版本会 break, 导致看门狗无从恢复)
            threading.Thread(target=client_loop, args=(conn,), daemon=True).start()
    finally:
        _release_lock()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--max-time', type=float, default=20.0)
    ap.add_argument('--no-watchdog', action='store_true', help='关闭引擎自愈看门狗')
    args = ap.parse_args()
    sys.exit(serve(args.port, args.max_time, watchdog=not args.no_watchdog))

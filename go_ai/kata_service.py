# -*- coding: utf-8 -*-
"""KataGo 常驻服务 (预加载)
- 启动一次 KataGo (冷启动 60-90s) 后常驻后台
- 通过本地 TCP 端口对外提供接口, controller 重启时快速复用, 无需重新冷加载
- 接口协议 (JSON lines, 一请求一响应):
    {"cmd":"ping"}                       -> {"ok":true,"ready":true}
    {"cmd":"set_board","stones":[[...]]} -> {"ok":true}
    {"cmd":"genmove","color":2,"max_time":20.0} -> {"ok":true,"mv":[c,r]}
    {"cmd":"stop"}                       -> 退出服务
用法: python kata_service.py [--port 8124] [--max-time 20.0]
"""
import os
import sys
import json
import socket
import threading
import time
import argparse

# 复用 KataGo 引擎封装
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from katago_engine import KataGoEngine, BLACK, WHITE

DEFAULT_PORT = 8124


class KataService:
    def __init__(self, port, max_time):
        self.port = port
        self.engine = None
        self.lock = threading.Lock()
        self.max_time = max_time
        self.busy = threading.Event()  # 复位状态: 用于串行化 genmove

    # ---- 引擎生命周期 ----
    def start_engine(self):
        """冷启动 KataGo (仅此一次). 阻塞直到 ready."""
        print('[kata-service] 冷启动 KataGo (60-90s)...', flush=True)
        eng = KataGoEngine(max_time=self.max_time, log_cb=lambda m: print('[KG]', m, flush=True))
        eng.start()  # 阻塞等待 GTP ready
        self.engine = eng
        print('[kata-service] KataGo 就绪, 常驻服务运行中', flush=True)
        return eng

    def stop_engine(self):
        if self.engine:
            try:
                self.engine.stop()
            except Exception:
                pass
            self.engine = None

    # ---- 请求处理 ----
    def handle(self, req):
        cmd = req.get('cmd')
        if cmd == 'ping':
            ready = self.engine is not None
            return {'ok': True, 'ready': ready}
        if cmd == 'stop':
            self.stop_engine()
            return {'ok': True, 'stopping': True}
        if self.engine is None or self.engine.proc is None or self.engine.proc.poll() is not None:
            return {'ok': False, 'err': 'engine_not_ready'}
        try:
            if cmd == 'set_board':
                stones = req['stones']
                # history: 真实落子顺序 [(col,row,color),...], 用于还原劫(ko)状态
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
            return {'ok': False, 'err': f'unknown_cmd:{cmd}'}
        except Exception as e:
            return {'ok': False, 'err': str(e)}


def serve(port, max_time):
    svc = KataService(port, max_time)
    # 启动引擎 (冷加载一次)
    try:
        svc.start_engine()
    except Exception as e:
        print(f'[kata-service] KataGo 启动失败: {e}', flush=True)
        svc.engine = None
        return 1

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        if svc.engine is None:
            conn.close()
            break
        t = threading.Thread(target=client_loop, args=(conn,), daemon=True)
        t.start()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--max-time', type=float, default=20.0)
    args = ap.parse_args()
    sys.exit(serve(args.port, args.max_time))

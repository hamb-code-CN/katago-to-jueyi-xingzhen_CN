# -*- coding: utf-8 -*-
"""KataGo 引擎封装 (GTP 协议)

I/O 设计 (v1.0.3 重写):
  旧的实现是"每发一条命令起一个临时线程 readline(), 超时就把线程塞进 _pending,
  下次发命令前逐个 join (最长 45s)" —— 能用, 但本质是用线程泄漏换正确性,
  一旦读写交错就会拖慢甚至卡住整步。
  现在改成: 进程启动时起 **1 个常驻 reader 线程** 把 stdout 逐行写进 queue,
  _send 只是"写命令 + 带超时取队列"。超时的响应靠 _stale 计数在下次发送前丢弃,
  竞态从设计上消失, 且超时不会再污染后续命令。
"""
import os
import sys
import re
import queue
import subprocess
import threading
import time
# 强制 stdout/stderr 用 UTF-8, 防止 print 含 ✓ 等字符在 GBK 控制台/文件报编码错
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

N = 19
EMPTY, BLACK, WHITE = 0, 1, 2


# ---- KataGo 路径解析 (便携化) ----
# 优先找代码目录同级 ../katago (便携包结构: <包>/go_ai + <包>/katago),
# 找不到则回退本机原路径 D:\katago. 整目录拷到别的电脑后自动识别, 无需改代码.
def _find_katago_root():
    _base = os.path.dirname(os.path.abspath(__file__))
    _cands = [
        os.path.join(_base, '..', 'katago'),   # 便携包内
        r'D:\katago',                           # 本机原部署位置
    ]
    for _c in _cands:
        if (os.path.exists(os.path.join(_c, 'opencl171', 'katago.exe'))
                and os.path.exists(os.path.join(_c, 'gtp.cfg'))):
            return _c
    return _cands[-1]  # 都找不到时返回本机路径, 启动报错更明确


def _resolve_model(root):
    """解析网络权重路径 (支持换更强模型, 无需改代码):
      1) 环境变量 GOAI_KATAGO_MODEL: 绝对路径, 或相对 katago 根的文件名/子路径
      2) katago 根目录下恰好只有一个 *.bin.gz -> 自动采用 (文件名随意, 下载即用)
      3) 多个/零个 *.bin.gz -> 回退默认文件名 kata-b18c384nbt.bin.gz"""
    _env = os.environ.get('GOAI_KATAGO_MODEL')
    if _env:
        return _env if os.path.isabs(_env) else os.path.join(root, _env)
    _gz = []
    try:
        _gz = [f for f in os.listdir(root)
               if f.lower().endswith('.bin.gz')
               and os.path.isfile(os.path.join(root, f))]
    except Exception:
        _gz = []
    if len(_gz) == 1:
        return os.path.join(root, _gz[0])  # 目录唯一模型, 自动采用
    if len(_gz) > 1:
        sys.stderr.write('[katago] 引擎目录含多个 .bin.gz, 默认用 kata-b18c384nbt.bin.gz; '
                         '如需换模型请设环境变量 GOAI_KATAGO_MODEL\n')
    return os.path.join(root, 'kata-b18c384nbt.bin.gz')


KATAGO_ROOT = _find_katago_root()
KATAGO_EXE = os.path.join(KATAGO_ROOT, 'opencl171', 'katago.exe')
KATAGO_MODEL = _resolve_model(KATAGO_ROOT)
KATAGO_CFG = os.path.join(KATAGO_ROOT, 'gtp.cfg')

# GTP 列字母 (A-T, 跳过 I)
_GTP_COLS = 'ABCDEFGHJKLMNOPQRST'


class KataGoEngine:
    def __init__(self, exe=None, model=None, cfg=None, max_time=25.0, log_cb=print):
        self.exe = exe or KATAGO_EXE
        self.model = model or KATAGO_MODEL
        self.cfg = cfg or KATAGO_CFG
        self.max_time = max_time
        self.log_cb = log_cb
        self.proc = None
        self.lock = threading.Lock()      # 串行化所有 GTP 交互
        self._q = queue.Queue()           # stdout 行队列 (常驻 reader 线程填充)
        self._reader = None
        self._err_thread = None
        self._err_tail = []               # stderr 尾部 (有上限)
        self._ready_evt = threading.Event()
        self._stale = 0                   # 超时未取走的响应条数
        self.restarts = 0
        self.last_error = None

    # ---------------- 生命周期 ----------------
    @property
    def alive(self):
        return bool(self.proc) and self.proc.poll() is None

    def start(self, wait_ready=True):
        if self.alive:
            return self.proc
        cmd = [self.exe, 'gtp', '-model', self.model, '-config', self.cfg]
        self.log_cb(f'[KataGo] 启动: {" ".join(cmd)}')
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding='utf-8', errors='replace',
            cwd=os.path.dirname(self.exe))
        self._q = queue.Queue()
        self._stale = 0
        self._ready_evt.clear()
        self._start_reader()
        if wait_ready:
            # 等待 "GTP ready" (冷启动 shader 编译 + tuning 缓存可能需要 150s+)
            self._wait_ready(180.0)
            self.log_cb('[KataGo] 就绪')
        return self.proc

    def restart(self, wait_ready=True):
        """进程已死时重建 (供 kata_service 看门狗调用)。"""
        self.stop()
        self.restarts += 1
        self.log_cb(f'[KataGo] 重启引擎 (第 {self.restarts} 次)')
        return self.start(wait_ready=wait_ready)

    def _start_reader(self):
        """常驻 reader: stdout 逐行入队, EOF 时投递 None 哨兵。"""
        def _loop():
            try:
                for line in self.proc.stdout:
                    self._q.put(line)
            except Exception:
                pass
            finally:
                try:
                    self._q.put(None)
                except Exception:
                    pass
        self._reader = threading.Thread(target=_loop, daemon=True)
        self._reader.start()

        def _err_loop():
            try:
                for line in self.proc.stderr:
                    self._err_tail.append(line)
                    if len(self._err_tail) > 200:
                        del self._err_tail[:100]
                    if 'GTP ready' in line or 'beginning main protocol loop' in line:
                        self._ready_evt.set()
                    if 'Uncaught exception' in line or 'Fatal' in line:
                        self.last_error = line.strip()
            except Exception:
                pass
        self._err_thread = threading.Thread(target=_err_loop, daemon=True)
        self._err_thread.start()

    def _wait_ready(self, timeout=180.0):
        start = time.time()
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f'KataGo 进程提前退出 (exit={self.proc.returncode})')
            if self.last_error:
                raise RuntimeError(f'KataGo 启动失败: {self.last_error}')
            if self._ready_evt.wait(0.5):
                return
        raise TimeoutError(f'KataGo 未就绪 (等待 {timeout}s): ...{"".join(self._err_tail)[-500:]}')

    def stop(self):
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write('quit\n')
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for th in (self._reader, self._err_thread):
            try:
                if th and th.is_alive():
                    th.join(timeout=2)
            except Exception:
                pass
        self._reader = self._err_thread = None

    # ---------------- GTP I/O ----------------
    def _take_line(self, deadline):
        """从队列取一行 (相对 deadline 超时)。EOF 时抛错。"""
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError('KataGo 响应超时')
            try:
                ln = self._q.get(timeout=min(remain, 0.25))
            except queue.Empty:
                continue
            if ln is None:
                raise RuntimeError('KataGo 输出流已关闭 (进程可能已退出)')
            return ln

    def _read_response(self, timeout):
        """读一条完整 GTP 响应 (读到空行为止)。
        返回 (prefix, content_lines); prefix 为 '=' 或 '?'。"""
        deadline = time.time() + timeout
        s = self._take_line(deadline).rstrip('\r\n')
        # 容错: 丢弃非响应行 (引擎偶发的裸输出)
        guard = 0
        while s and s[0] not in '=?' and guard < 5:
            guard += 1
            s = self._take_line(deadline).rstrip('\r\n')
        if not s:
            raise RuntimeError('KataGo 返回空行, 响应异常')
        prefix, content = s[0], []
        if s[1:].strip():
            content.append(s[1:].strip())
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break                      # 超时: 拿到的部分照常返回
            try:
                ln = self._take_line(deadline)
            except TimeoutError:
                break
            t = ln.rstrip('\r\n')
            if not t.strip():
                break                      # 空行 = 响应结束
            content.append(t.strip())
        return prefix, content

    def _discard_stale(self, budget=3.0):
        """丢弃此前超时未取走的响应 (最多等 budget 秒)。
        用条数计数而不是 join 临时线程, 不会阻塞整步。"""
        if not self._stale or not self.alive:
            self._stale = 0
            return
        end = time.time() + budget
        while self._stale > 0 and time.time() < end:
            try:
                self._read_response(max(0.2, end - time.time()))
            except Exception:
                break
            self._stale -= 1

    def _write(self, cmd):
        if not self.alive:
            raise RuntimeError('KataGo 进程未运行')
        try:
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            raise RuntimeError(f'KataGo stdin 写入失败: {e}')

    def _send(self, cmd, timeout=10.0):
        """发命令读单行响应, 返回响应文本 (不含 =/?)。错误响应抛 RuntimeError。"""
        with self.lock:
            self._discard_stale()
            self._write(cmd)
            try:
                prefix, content = self._read_response(timeout)
            except TimeoutError:
                self._stale += 1            # 记一笔: 下次发送前丢弃这条迟到响应
                raise
            if prefix == '?':
                raise RuntimeError(f'KataGo 返回错误: {" ".join(content)}')
            return ' '.join(content).strip()

    def _send_multiline(self, cmd, timeout=15.0):
        """发命令读多行响应 (kata-raw-nn / kata-analyze 等), 返回内容行列表。"""
        with self.lock:
            self._discard_stale()
            self._write(cmd)
            try:
                prefix, content = self._read_response(timeout)
            except TimeoutError:
                self._stale += 1
                raise
            if prefix == '?':
                raise RuntimeError(f'KataGo 返回错误: {" ".join(content)}')
            return content

    # ---------------- 高级接口 ----------------
    def set_param(self, name, value, timeout=5.0):
        """设置 KataGo 扩展参数 (kata-set-param)。失败静默 (旧版本可能不支持)。"""
        try:
            self._send(f'kata-set-param {name} {value}', timeout=timeout)
            return True
        except Exception:
            return False

    def get_winrate(self, time=1.5):
        """黑方胜率 (0~1, KataGo 惯例)。用 kata-raw-nn 0 (一次性多行响应)。
        调用方按我方颜色自行转换 (执白取 1-wr)。失败返回 None。"""
        raw = self._raw_nn(time)
        if raw is None:
            return None
        return raw.get('black_win')

    def get_score_lead(self, time=1.5):
        """黑方领先目数 (正=黑领先)。不可用时返回 None。"""
        raw = self._raw_nn(time)
        return None if raw is None else raw.get('black_lead')

    def _raw_nn(self, time=1.5):
        """解析 kata-raw-nn 0 输出。
        raw-nn 每行一个字段 (whiteWin/whiteLoss/whiteLead/...), 均为白方视角。"""
        try:
            lines = self._send_multiline('kata-raw-nn 0', timeout=max(8.0, float(time) + 5))
        except Exception:
            return None
        out = {}
        try:
            for ln in lines:
                parts = ln.split()
                if len(parts) < 2:
                    continue
                k, v = parts[0], parts[1]
                if k == 'whiteWin':
                    out['black_win'] = max(0.0, min(1.0, 1.0 - float(v)))
                elif k in ('whiteLead', 'whiteScoreLead'):
                    out['black_lead'] = -float(v)     # 白方视角 -> 黑方视角
        except Exception:
            return None
        return out or None

    def reset(self, handicap=0):
        """重置对局 (清空棋盘)"""
        self._send('clear_board')
        if handicap:
            self._send(f'fixed_handicap {handicap}')

    def set_board(self, stones, history=None):
        """把棋盘同步给 KataGo. 三种策略, 优先级从高到低:

        1. history 完整 -> 按**真实顺序**逐手 play (KataGo 内部 ko 状态正确,
           劫争中主动找劫材; 从开局监视的对局走此路径)
        2. 无完整历史(中途接入/漏读) -> **set_position 快照摆盘**: 直接把当前
           读到的 19x19 盘面一次性设给 KataGo, 盘面与真实 100% 一致
           (仅"接入瞬间恰在打劫"时缺劫点, 由候选切换/客户端拒绝兜底)
        3. 快照失败(旧引擎不支持等) -> 兜底按位置排序交替重建 (盘面可能错)
        返回 True 表示局面已精确同步, False 表示用了不可靠的交替重建."""
        if history:
            try:
                self._send('clear_board')
                for item in history:
                    c, r, color = int(item[0]), int(item[1]), int(item[2])
                    if not (0 <= c < N and 0 <= r < N):
                        raise ValueError('history 坐标越界 (%d,%d)' % (c, r))
                    name = 'B' if color == BLACK else 'W'
                    self._send('play %s %s' % (name, self._pt_to_gtp(c, r)), timeout=10)
                return True  # 真实顺序重建成功, ko 状态正确
            except Exception as e:
                # 重放失败(如某帧漏读导致顺序非法) -> 落空后改走快照
                try:
                    self._send('clear_board')
                except Exception:
                    pass
                if self.log_cb:
                    self.log_cb('[KataGo] 真实顺序重放失败(%s), 改用快照摆盘' % e)
        # ---- 策略 2: set_position 快照摆盘 (KataGo GTP 扩展, v1.17 支持) ----
        try:
            self._set_position_snapshot(stones)
            return True
        except Exception as e:
            if self.log_cb:
                self.log_cb('[KataGo] set_position 快照失败(%s), 兜底交替重建' % e)
        # ---- 策略 3: 按位置排序 + 黑白交替重建 (仅兜底) ----
        self._send('clear_board')
        black_pts, white_pts = [], []
        for r in range(N):
            for c in range(N):
                if stones[r][c] == BLACK:
                    black_pts.append(self._pt_to_gtp(c, r))
                elif stones[r][c] == WHITE:
                    white_pts.append(self._pt_to_gtp(c, r))
        merged = []
        i = j = 0
        turn = BLACK
        while i < len(black_pts) or j < len(white_pts):
            if turn == BLACK and i < len(black_pts):
                merged.append((BLACK, black_pts[i])); i += 1
            elif turn == WHITE and j < len(white_pts):
                merged.append((WHITE, white_pts[j])); j += 1
            turn = WHITE if turn == BLACK else BLACK
        for color, pt in merged:
            name = 'B' if color == BLACK else 'W'
            self._send(f'play {name} {pt}', timeout=10)
        return False  # 交替重建: 盘面/ko 均不可靠

    def _set_position_snapshot(self, stones):
        """用 GTP set_position 直接把 19x19 盘面快照设给 KataGo (无需落子顺序)。
        注意: 快照被视为无历史 -> 无劫(superko)限制, 无历史提子信息。"""
        toks = ['set_position']
        for r in range(N):
            for c in range(N):
                v = int(stones[r][c])
                if v == BLACK:
                    toks.append('B'); toks.append(self._pt_to_gtp(c, r))
                elif v == WHITE:
                    toks.append('W'); toks.append(self._pt_to_gtp(c, r))
        self._send(' '.join(toks), timeout=15)

    def genmove(self, color, max_time=None):
        """生成着法. 返回 (col, row) 或 (-1,-1) pass。超时按 maxTime+10。"""
        name = 'B' if color == BLACK else 'W'
        mt = float(max_time) if max_time is not None else float(self.max_time)
        self.set_param('maxTime', f'{mt:.1f}')          # 覆盖 cfg 里的 maxTime
        resp = self._send(f'genmove {name}', timeout=mt + 10)
        if resp in ('resign', 'pass', ''):
            return (-1, -1)
        return self._gtp_to_pt(resp)

    # ---- 坐标转换 ----
    def _pt_to_gtp(self, c, r):
        """(col, row) 图像坐标 (col 0=左, row 0=顶) -> GTP 'A1' (A=左, 1=底)"""
        return f'{_GTP_COLS[c]}{N - r}'

    def _gtp_to_pt(self, gtp):
        """GTP 'A1' -> (col, row) 图像坐标"""
        m = re.match(r'^([A-Za-z])(\d+)$', gtp.strip())
        if not m:
            raise ValueError(f'无法解析 GTP 坐标: {gtp!r}')
        return (_GTP_COLS.index(m.group(1).upper()), N - int(m.group(2)))


def select_move_katago(stones, my_color, max_time=25.0, log_cb=print):
    """一站式: 重建棋盘 -> genmove. 返回 (col,row) 或 (-1,-1)"""
    eng = KataGoEngine(max_time=max_time, log_cb=log_cb)
    try:
        eng.start()
        eng.set_board(stones)
        return eng.genmove(my_color)
    finally:
        eng.stop()


# ============================================================
# 预加载服务客户端: 连接 kata_service.py 常驻进程, 复用已就绪的 KataGo
# ============================================================
import socket
import json


class KataServiceClient:
    """连接常驻 KataGo 服务 (预加载复用). 若服务不在线则自动回退本地引擎。"""

    def __init__(self, port=8124, max_time=20.0, log_cb=print):
        self.port = port
        self.max_time = max_time
        self.log_cb = log_cb
        self._local = None  # 回退用的本地引擎
        self._mode = 'init'  # 'service' | 'local' | 'none'

    # ---- 连接 ----
    def _conn(self, timeout=10.0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('127.0.0.1', self.port))
        return s

    def ping(self):
        """测试服务是否就绪。返回 True 表示服务在线且 KataGo 已加载。"""
        try:
            with self._conn(5.0) as s:
                s.sendall((json.dumps({'cmd': 'ping'}) + '\n').encode('utf-8'))
                line = s.makefile('r').readline()
                d = json.loads(line)
                return bool(d.get('ready'))
        except Exception:
            return False

    @property
    def mode(self):
        return self._mode

    def connect(self):
        """初始化: 优先连接服务; 失败则回退本地引擎(冷启动). 返回 (mode, ready)。"""
        if self.ping():
            self._mode = 'service'
            self.log_cb(f'[KataGo] 复用预加载服务 (127.0.0.1:{self.port}), 免冷启动')
            return 'service', True
        self.log_cb('[KataGo] 服务未在线, 回退本地引擎 (需冷启动)...')
        try:
            self._local = KataGoEngine(max_time=self.max_time, log_cb=self.log_cb)
            self._local.start()
            self._mode = 'local'
            return 'local', True
        except Exception as e:
            self._mode = 'none'
            self.log_cb(f'[KataGo] 引擎不可用: {e}')
            return 'none', False

    def stop(self):
        if self._local:
            try:
                self._local.stop()
            except Exception:
                pass
            self._local = None

    # ---- GTP 转发 ----
    def _req(self, obj, timeout=30.0):
        with self._conn(timeout) as s:
            s.sendall((json.dumps(obj) + '\n').encode('utf-8'))
            line = s.makefile('r').readline()
            if not line:
                raise RuntimeError('KataGo 服务无响应')
            d = json.loads(line)
            if not d.get('ok'):
                raise RuntimeError(f'KataGo 服务错误: {d.get("err")}')
            return d

    def set_board(self, stones, history=None):
        """同步棋盘. history 为真实落子顺序 [(col,row,color),...], 用于还原劫(ko)状态。"""
        if self._mode == 'service':
            req = {'cmd': 'set_board',
                   'stones': [[int(v) for v in row] for row in stones]}
            if history:
                req['history'] = [[int(c), int(r), int(col)] for (c, r, col) in history]
            self._req(req)
        elif self._mode == 'local':
            self._local.set_board(stones, history=history)
        else:
            raise RuntimeError('引擎未就绪')

    def genmove(self, color, max_time=None):
        mt = float(max_time) if max_time is not None else float(self.max_time)
        if self._mode == 'service':
            d = self._req({'cmd': 'genmove', 'color': int(color), 'max_time': mt},
                          timeout=mt + 15)
            return tuple(d['mv'])
        elif self._mode == 'local':
            return self._local.genmove(color, max_time=mt)
        else:
            raise RuntimeError('引擎未就绪')

    def get_winrate(self, time=1.5):
        """当前局面黑方胜率 (0~1), 供中盘低胜率延长思考权重使用。"""
        if self._mode == 'service':
            d = self._req({'cmd': 'winrate', 'time': float(time)}, timeout=15)
            return d.get('winrate')
        elif self._mode == 'local':
            return self._local.get_winrate(time=time)
        else:
            raise RuntimeError('引擎未就绪')

    def get_score_lead(self, time=1.5):
        """当前局面黑方领先目数 (正=黑领先)。不可用时返回 None。"""
        if self._mode == 'service':
            try:
                d = self._req({'cmd': 'score_lead', 'time': float(time)}, timeout=15)
                return d.get('lead')
            except Exception:
                return None
        elif self._mode == 'local':
            return self._local.get_score_lead(time=time)
        return None

    def set_param(self, name, value):
        """转发 kata-set-param (放水/线程数等)。服务未就绪时静默失败。"""
        try:
            if self._mode == 'service':
                self._req({'cmd': 'set_param', 'name': name, 'value': value})
                return True
            if self._mode == 'local':
                return self._local.set_param(name, value)
        except Exception:
            pass
        return False


if __name__ == '__main__':
    stones = [[0] * N for _ in range(N)]
    stones[2][3] = BLACK
    stones[2][16] = BLACK
    stones[17][3] = BLACK
    stones[15][3] = WHITE
    stones[9][10] = WHITE
    stones[16][3] = WHITE
    t0 = time.time()
    mv = select_move_katago(stones, WHITE, max_time=8.0)
    print(f'KataGo 选点: {mv} 用时 {time.time()-t0:.1f}s')

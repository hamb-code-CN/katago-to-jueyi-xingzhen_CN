# -*- coding: utf-8 -*-
"""KataGo 引擎封装 (GTP 协议)
- subprocess 启动 katago gtp
- 输入: 19x19 棋盘 (0空/1黑/2白) + 我方颜色
- 输出: (col, row) 落点 或 (-1,-1) pass
- 超时保护: 思考超时则中断并重启进程
"""
import os
import sys
import re
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
# 引擎根只需含 opencl171/katago.exe + gtp.cfg; 模型可单独下载/替换, 见 _resolve_model.
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
        self.lock = threading.Lock()
        self._pending = []  # 超时命令的迟到响应读取线程 (下次发命令前排空)

    def start(self, wait_ready=True):
        if self.proc and self.proc.poll() is None:
            return
        cmd = [self.exe, 'gtp', '-model', self.model, '-config', self.cfg]
        self.log_cb(f'[KataGo] 启动: {" ".join(cmd)}')
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=os.path.dirname(self.exe))
        if wait_ready:
            # 等待 stderr 出现 "GTP ready" (冷启动 shader 编译 + tuning 缓存可能需要 150s+)
            self._wait_ready(180.0)
            self.log_cb('[KataGo] 就绪')
        return self.proc

    def _wait_ready(self, timeout=180.0):
        """读 stderr 直到出现 'GTP ready', 超时则抛错 (Windows 兼容: 线程读)"""
        import queue
        q = queue.Queue()
        def _reader():
            try:
                for line in self.proc.stderr:
                    q.put(line)
            except Exception:
                pass
        rt = threading.Thread(target=_reader, daemon=True)
        rt.start()
        start = time.time()
        tail_buf = []
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f'KataGo 进程提前退出 (exit={self.proc.returncode})')
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                continue
            tail_buf.append(line)
            if len(tail_buf) > 200:
                tail_buf.pop(0)
            if 'GTP ready' in line or 'beginning main protocol loop' in line:
                return
            if 'Uncaught exception' in line or 'Fatal' in line:
                raise RuntimeError(f'KataGo 启动失败: {line.strip()}')
        raise TimeoutError(f'KataGo 未就绪 (等待 {timeout}s): ...{"".join(tail_buf)[-500:]}')

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self._send('quit')
            except Exception:
                pass
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _flush_pending(self, timeout=45.0):
        """排空此前超时命令的迟到响应, 保证 GTP 请求/响应一一对应."""
        for th in getattr(self, "_pending", []):
            try:
                th.join(timeout)
            except Exception:
                pass
        self._pending = []

    def _send(self, cmd, timeout=10.0):
        """发命令, 读响应. 返回响应文本 (不含 =/?)"""
        self._flush_pending()
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError('KataGo 进程未运行')
        # 清空可能残留的 stderr 不阻塞 (用非阻塞读丢弃)
        try:
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise RuntimeError(f'KataGo stdin 写入失败: {e}')

        result = {}
        def _read():
            try:
                line = self.proc.stdout.readline()
                result['line'] = line
            except Exception as e:
                result['err'] = str(e)
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._pending.append(t)  # 迟到响应由 _flush_pending 排空
            raise TimeoutError(f'KataGo 响应超时: {cmd}')
        if 'err' in result:
            raise RuntimeError(f'KataGo 读取失败: {result["err"]}')
        line = result.get('line', '')
        if line.startswith('?'):
            raise RuntimeError(f'KataGo 返回错误: {line.strip()}')
        if not line.startswith('='):
            return ''
        # 消费 GTP 响应后的空行分隔符 (\r\n), 防止污染下一次 readline
        # 非阻塞: 用短超时线程读掉残留空行
        rest = {}
        def _drain():
            try:
                rest['line'] = self.proc.stdout.readline()
            except Exception:
                pass
        t2 = threading.Thread(target=_drain, daemon=True)
        t2.start()
        t2.join(2.0)
        return line[1:].strip()

    def _send_multiline(self, cmd, timeout=15.0):
        """发命令并读取多行响应 (kata-analyze 等), 直到空行或超时.
        返回内容行列表 (不含 GTP 前缀行 '= ...' 与结尾空行)."""
        self._flush_pending()
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError('KataGo 进程未运行')
        try:
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise RuntimeError(f'KataGo stdin 写入失败: {e}')
        lines = []
        def _read():
            try:
                while True:
                    ln = self.proc.stdout.readline()
                    if not ln:
                        break
                    lines.append(ln)
                    if ln.strip() == '':
                        break
            except Exception:
                pass
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._pending.append(t)
            raise TimeoutError(f'KataGo 多行响应超时: {cmd}')
        # 去掉首行 GTP 前缀行 (="..."/"?..."), 返回内容行
        out = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith('=') or s.startswith('?'):
                continue
            out.append(s)
        return out

    def get_winrate(self, time=1.5):
        """黑方胜率 (0~1, KataGo 惯例). 用 kata-raw-nn 0 (一次性多行响应, 网络前馈几十 ms).
        raw-nn 输出字段为 whiteWin/whiteLoss 等 (每行一个), 无 winrate 字段;
        kata-analyze 是流式输出无结束符, 不适合同步读取, 故用 raw-nn.
        调用方按我方颜色自行转换 (执白取 1-wr). 失败返回 None."""
        try:
            lines = self._send_multiline('kata-raw-nn 0', timeout=8)  # 需带 symmetry 参数 (0-7)
            for ln in lines:
                if ln.startswith('whiteWin'):
                    parts = ln.split()
                    if len(parts) >= 2:
                        white_win = float(parts[1])
                        return max(0.0, min(1.0, 1.0 - white_win))
            return None
        except Exception:
            return None

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
        # 按手数交替重建 (保证合法顺序) - 用 plays 列表合并交替
        merged = []
        nb, nw = len(black_pts), len(white_pts)
        i = j = 0
        turn = BLACK
        while i < nb or j < nw:
            if turn == BLACK and i < nb:
                merged.append((BLACK, black_pts[i])); i += 1
            elif turn == WHITE and j < nw:
                merged.append((WHITE, white_pts[j])); j += 1
            turn = WHITE if turn == BLACK else BLACK
        for color, pt in merged:
            name = 'B' if color == BLACK else 'W'
            self._send(f'play {name} {pt}', timeout=10)
        return False  # 交替重建: 盘面/ko 均不可靠

    def _set_position_snapshot(self, stones):
        """用 GTP set_position 直接把 19x19 盘面快照设给 KataGo (无需落子顺序).
        KataGo 文档: set_position 用颜色-坐标对指定初始局面并替换当前棋盘.
        注意: 快照被视为无历史 -> 无劫(superko)限制, 无历史提子信息."""
        toks = ['set_position']
        for r in range(N):
            for c in range(N):
                v = int(stones[r][c])
                if v == BLACK:
                    toks.append('B')
                    toks.append(self._pt_to_gtp(c, r))
                elif v == WHITE:
                    toks.append('W')
                    toks.append(self._pt_to_gtp(c, r))
        self._send(' '.join(toks), timeout=15)

    def genmove(self, color, max_time=None):
        """生成着法. 返回 (col, row) 或 (-1,-1) pass. 搜索耗时按 maxTime+10 超时.
        max_time 可每手传入 (用于随机化思考时间); 缺省用 self.max_time."""
        name = 'B' if color == BLACK else 'W'
        mt = float(max_time) if max_time is not None else float(self.max_time)
        # 动态设置搜索时间 (KataGo 扩展 GTP 命令), 覆盖 cfg 里的 maxTime
        try:
            self._send(f'kata-set-param maxTime {mt:.1f}', timeout=5)
        except Exception:
            pass  # 某些版本不支持, 忽略
        resp = self._send(f'genmove {name}', timeout=mt + 10)
        if resp in ('resign', 'pass', ''):
            return (-1, -1)
        return self._gtp_to_pt(resp)

    # ---- 坐标转换 ----
    def _pt_to_gtp(self, c, r):
        """(col, row) 图像坐标 (col 0=左, row 0=顶) -> GTP 'A1' (A=左, 1=底)"""
        col_letter = _GTP_COLS[c]
        row_num = N - r
        return f'{col_letter}{row_num}'

    def _gtp_to_pt(self, gtp):
        """GTP 'A1' -> (col, row) 图像坐标"""
        m = re.match(r'^([A-Za-z])(\d+)$', gtp.strip())
        if not m:
            raise ValueError(f'无法解析 GTP 坐标: {gtp!r}')
        letter, num = m.group(1).upper(), int(m.group(2))
        c = _GTP_COLS.index(letter)
        r = N - num
        return (c, r)


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
    """连接常驻 KataGo 服务 (预加载复用). 若服务不在线则自动回退本地引擎."""

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
        """测试服务是否就绪. 返回 True 表示服务在线且 KataGo 已加载."""
        try:
            with self._conn(5.0) as s:
                s.sendall((json.dumps({'cmd': 'ping'}) + '\n').encode('utf-8'))
                line = s.makefile('r').readline()
                d = json.loads(line)
                return bool(d.get('ready'))
        except Exception:
            return False

    def connect(self):
        """初始化: 优先连接服务; 失败则回退本地引擎(冷启动). 返回 (mode, ready)."""
        if self.ping():
            self._mode = 'service'
            self.log_cb(f'[KataGo] 复用预加载服务 (127.0.0.1:{self.port}), 免冷启动')
            return 'service', True
        # 回退本地引擎
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
        """同步棋盘. history 为真实落子顺序 [(col,row,color),...], 用于还原劫(ko)状态."""
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
        """当前行棋方胜率 (0~1), 供中盘低胜率延长思考权重使用."""
        if self._mode == 'service':
            d = self._req({'cmd': 'winrate', 'time': float(time)}, timeout=15)
            return d.get('winrate')
        elif self._mode == 'local':
            return self._local.get_winrate(time=time)
        else:
            raise RuntimeError('引擎未就绪')


if __name__ == '__main__':
    # 自测: 空盘 + 当前棋局 (黑 D17 Q17 白 D4 K10 D3 -> 图像坐标)
    stones = [[0] * N for _ in range(N)]
    # 黑 (3,2)=D17, (16,2)=Q17, (3,17)=D2
    stones[2][3] = BLACK
    stones[2][16] = BLACK
    stones[17][3] = BLACK
    # 白 (3,15)=D4, (10,9)=K10, (3,16)=D3
    stones[15][3] = WHITE
    stones[9][10] = WHITE
    stones[16][3] = WHITE
    t0 = time.time()
    mv = select_move_katago(stones, WHITE, max_time=8.0)
    print(f'KataGo 选点: {mv} 用时 {time.time()-t0:.1f}s')

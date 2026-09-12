# -*- coding: utf-8 -*-
"""围棋 AI 一键启动器 (菜单式, 整合 KataGo 预加载)
- 启动/停止 controller + 实时看板
- 自动维护 KataGo 常驻服务 (预加载, 免冷启动)
- 调节随机思考时间下限/上限 (1~100s) / 轮询间隔等参数
用法: python go_launcher.py
"""
import os
import sys
import json
import time
import socket
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
# 配置单一真源: 档位/平台/读写全部来自 config_store (settings.json), 本模块与看板共用
from config_store import (  # noqa: E402
    CONFIG_PATH, LOG_FILE, KATA_LOG, LOCK_PATH, SERVICE_PORT,
    LEVELS, PLATFORMS, PLATFORM_LABELS, DEFAULT_CONFIG, load_config, save_config,
)

# 文件锁句柄 (保持打开期间锁有效)
_lock_file = None


def try_lock():
    """获取 launcher 互斥锁, 已有一个在跑则返回 False"""
    global _lock_file
    try:
        _lock_file = open(LOCK_PATH, 'w')
        import msvcrt
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (OSError, ImportError):
        if _lock_file:
            _lock_file.close()
            _lock_file = None
        return False


def release_lock():
    global _lock_file
    if _lock_file:
        try:
            _lock_file.close()
        except Exception:
            pass
        _lock_file = None
    try:
        os.unlink(LOCK_PATH)
    except Exception:
        pass


CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 便携化: 用当前解释器启动子进程 (谁运行 launcher 就用谁, 不再硬编码本机 venv 路径)
PY = sys.executable


# ---------- 配置 ----------
# load_config / save_config 由 config_store 提供 (settings.json 单一真源),
# 见文件头部的 import; 这里不再重复定义默认值与校验逻辑。


# ---------- 进程管理 ----------
_wmic_cache = {'t': 0, 'data': None}  # TTL 缓存: 进程查询慢 (4s+), 避免每次 HTTP 都查


def _invalidate_proc_cache():
    """进程集合发生变化(启动/杀进程)后立即失效缓存.

    否则启停判断会用到过期快照: 停止后立刻启动会误报"已在运行";
    启动后立刻停止会停不掉(新进程不在快照里), AI 会继续下棋."""
    _wmic_cache['t'] = 0
    _wmic_cache['data'] = None


def _wmic_list(cache_ttl=1.5):
    """返回 [(pid, cmdline), ...] 所有 python 进程.
    用 PowerShell CIM 查询 (wmic 在此环境输出混乱/有延迟, 会导致重复启动误判).
    带 TTL 缓存, 4s 内复用结果, 避免 HTTP API 每次卡 10s."""
    now = time.time()
    if _wmic_cache['data'] is not None and now - _wmic_cache['t'] < cache_ttl:
        return _wmic_cache['data']
    out = []
    try:
        script = (
            "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "ForEach-Object { [PSCustomObject]@{pid=$_.ProcessId; cmd=$_.CommandLine} }; "
            "$p | ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            capture_output=True, text=True, errors='ignore', timeout=20)
        txt = r.stdout.strip()
        if txt:
            data = json.loads(txt)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                pid = item.get('pid')
                if pid is not None:
                    out.append((int(pid), item.get('cmd') or ''))
    except Exception:
        pass
    _wmic_cache['t'] = time.time()
    _wmic_cache['data'] = out
    return out


def _listen(port):
    """检测端口是否在监听"""
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1.0):
            return True
    except OSError:
        return False


def status():
    procs = _wmic_list()
    ctrl = [p for p in procs if 'go_controller' in p[1]]
    watch = [p for p in procs if 'analysis_watch' in p[1]]
    if not ctrl:
        # 兜底: 进程枚举不可靠时, 用 controller 自己的单实例锁判断是否在跑。
        # 本机 wmic 返回空 (已实测), PowerShell 也可能偶发失败;
        # 一旦 _wmic_list 返空, 看门狗就会把"正在运行的 controller"误判为已退出,
        # 每 30s 重复拉起一个 -> 一堆僵尸。锁文件是文件系统级的, 不依赖子进程。
        lp = _controller_lock_pid()
        if lp:
            ctrl = [(lp, 'go_controller.py (lock)')]
    svc = _listen(SERVICE_PORT)
    return ctrl, watch, svc


def _controller_lock_pid():
    """读 _controller.lock: 锁内 PID 仍存活则返回该 PID, 否则 None。"""
    try:
        with open(os.path.join(BASE, '_controller.lock'), encoding='utf-8') as f:
            pid = int(json.load(f).get('pid'))
    except Exception:
        return None
    try:
        from kata_service import _pid_alive
        return pid if _pid_alive(pid) else None
    except Exception:
        return None


def is_running():
    ctrl, watch, svc = status()
    return bool(ctrl), bool(watch), svc, ctrl, watch


def kill_matching(keyword):
    procs = _wmic_list()
    killed = 0
    for pid, cmd in procs:
        if keyword in cmd:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10)
            killed += 1
    if not killed and keyword == 'go_controller':
        # 进程枚举失败时的兜底: 至少干掉持锁的那个 controller,
        # 否则「停止 AI」会点了没反应 (枚举返空 -> 以为没进程可杀)。
        lp = _controller_lock_pid()
        if lp:
            subprocess.run(['taskkill', '/F', '/PID', str(lp)],
                           capture_output=True, timeout=10)
            killed += 1
    if killed:
        _invalidate_proc_cache()
    return killed


def stop_all():
    n1 = kill_matching('go_controller')
    n2 = kill_matching('analysis_watch')
    # 停掉预加载服务 (按端口找 PID 更稳, 并保底杀 katago)
    preload_off = _stop_preload()
    subprocess.run(['taskkill', '/F', '/IM', 'katago.exe'],
                   capture_output=True, timeout=10)
    time.sleep(1)
    return n1, n2, preload_off


# ---------- KataGo 预加载服务 ----------
def _stop_preload():
    """按端口找出监听进程 PID 并结束"""
    try:
        r = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True,
            errors='ignore', timeout=10)
        pids = set()
        for line in r.stdout.splitlines():
            if f':{SERVICE_PORT}' in line and 'LISTENING' in line:
                parts = line.split()
                if parts:
                    last = parts[-1]
                    if last.isdigit():
                        pids.add(int(last))
        for pid in pids:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10)
        if pids:
            _invalidate_proc_cache()
        return len(pids)
    except Exception:
        return 0


def _kata_state():
    """KataGo 常驻服务状态: 'ready' | 'starting' | 'idle'."""
    if _listen(SERVICE_PORT):
        return 'ready'
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        from kata_service import service_state as _ss
        return _ss(SERVICE_PORT)
    except Exception:
        return 'idle'


_LAST_SPAWN = [0.0]


def start_preload(max_time):
    """确保 KataGo 常驻服务在跑. 返回 True 表示已就绪(或已在跑).

    注意: 冷启动期间端口尚未监听, 不能只靠端口判重, 否则反复点击会拉起多个引擎
    (每个 200MB+ GPU 显存, 且会双绑同一端口) -> 用锁文件认领 + 刚刚发起标记双重保护.
    """
    if _listen(SERVICE_PORT):
        print(f'[预加载] KataGo 服务已在运行 (端口 {SERVICE_PORT}), 复用免冷启动 ✓')
        return True
    state = _kata_state()
    if state == 'starting':
        print('[预加载] 检测到另一实例正在冷启动 KataGo, 等待其就绪 (不重复拉起引擎)...')
    elif time.time() - _LAST_SPAWN[0] < 120:
        print('[预加载] 刚刚已发起启动, 继续等待就绪 (不重复拉起引擎)...')
    else:
        print('[预加载] KataGo 服务未运行, 冷启动中 (仅首次 60-90s, 之后常驻)...')
        # 追加模式: 避免清空正在冷启动实例的日志 (曾用 'w' 截断导致进度丢失); 过大时才滚动
        try:
            big = os.path.getsize(KATA_LOG) > 300000
        except OSError:
            big = True
        with open(KATA_LOG, 'w' if big else 'a', encoding='utf-8') as f:
            subprocess.Popen(
                [PY, '-u', 'kata_service.py', '--port', str(SERVICE_PORT),
                 '--max-time', str(max_time)],
                cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        _invalidate_proc_cache()
        _LAST_SPAWN[0] = time.time()
    # 轮询等待就绪 (最多 180s)
    for i in range(60):
        time.sleep(3)
        if _listen(SERVICE_PORT):
            print(f'[预加载] KataGo 服务就绪 (等待 {(i+1)*3}s) ✓')
            _LAST_SPAWN[0] = 0.0
            return True
    print('[预加载] KataGo 服务启动超时, 请检查 kata_service.log')
    _LAST_SPAWN[0] = 0.0
    return False


# ---------- 启动 ----------
def start_ai(color, cfg, with_watch, mode='auto'):
    """启动 controller。mode: auto=AI 自动落子 | assist=只分析+玩家点选落子 | analyze=只分析不出手"""
    ctrl_r, watch_r, svc, ctrl_list, _ = is_running()
    if ctrl_r:
        print(f'!! controller 已在运行 (PID {[p[0] for p in ctrl_list]}), 先停止再启动')
        return False
    mode = mode if mode in ('auto', 'assist', 'analyze') else 'auto'
    tmin, tmax = cfg['tmin'], cfg['tmax']
    interval = cfg['interval']
    # 先确保预加载服务
    if not start_preload(max(tmin, tmax)):
        print('!! 预加载服务未就绪, 仍继续启动(controller 会回退本地引擎)')
    args = [PY, '-u', 'go_controller.py', '--color', color,
            '--tmin', str(tmin), '--tmax', str(tmax),
            '--interval', str(interval),
            '--platform', cfg.get('platform', 'tencent')]
    if mode == 'assist':
        args.append('--assist')          # 只给胜率/候选, 由玩家在看板点选落子
    elif mode == 'analyze':
        args.append('--analyze-only')    # 只分析, 永不出手
    with open(LOG_FILE, 'w', encoding='utf-8') as logf:
        subprocess.Popen(args, cwd=BASE, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW)
    # 子进程已继承句柄, 父进程这份必须关闭, 否则每次启动都泄漏一个文件句柄
    _invalidate_proc_cache()          # 新 controller 立即对后续 status 可见
    _mode_txt = {'assist': ' [辅助模式: AI 不出手, 看板点选落子]',
                 'analyze': ' [仅分析模式]'}.get(mode, '')
    print(f'[AI] 已启动: 执{"黑" if color=="black" else "白"}, 随机思考 {tmin:g}~{tmax:g}s, 轮询 {interval:g}s{_mode_txt}')
    print(f'     连接预加载服务后直接下棋, 无冷启动延迟; 日志: {LOG_FILE}')
    if with_watch:
        start_watch()
    return True


def start_watch():
    watch_r = is_running()[1]
    if watch_r:
        print('[看板] 已在运行')
        return
    subprocess.Popen([PY, 'analysis_watch.py'], cwd=BASE,
                     creationflags=CREATE_NO_WINDOW)
    time.sleep(2)
    print('[看板] 已启动: http://127.0.0.1:8123/analysis.html')


# ---------- 菜单 ----------
def ask_float(title, cur, lo=1.0, hi=100.0):
    while True:
        s = input(f'{title} (当前 {cur:g}s, {lo:g}-{hi:g}, 回车取消): ').strip()
        if not s:
            return None
        try:
            v = float(s)
            if lo <= v <= hi:
                return v
            print(f'  范围 {lo:g}-{hi:g}, 重输')
        except ValueError:
            print('  请输入数字')


def menu():
    cfg = load_config()
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        ctrl_r, watch_r, svc, ctrl_list, watch_list = is_running()
        color_cn = '黑' if cfg['color'] == 'black' else '白'
        svc_cn = '运行中' if svc else '停止'
        print('=' * 46)
        print('     围 棋 AI 一 键 启 动 器')
        print('=' * 46)
        print(f'  当前配置: 对手={PLATFORM_LABELS.get(cfg["platform"], cfg["platform"])} | 执{color_cn} | 随机思考 {cfg["tmin"]:g}~{cfg["tmax"]:g}s | 轮询 {cfg["interval"]:g}s | 看板 {"开" if cfg["watch"] else "关"}')
        print(f'  运行状态: controller [{"运行中" if ctrl_r else "停止"}]  看板 [{"运行中" if watch_r else "停止"}]  KataGo预加载 [{svc_cn}]')
        print('-' * 46)
        print('  [1] 启动 AI (执黑)')
        print('  [2] 启动 AI (执白)')
        print('  [3] 修改思考时间下限')
        print('  [4] 修改思考时间上限')
        print('  [5] 修改轮询间隔')
        print('  [6] 启动/重启 KataGo 预加载')
        print('  [7] 只看板 (不启动 AI)')
        print('  [8] 停止所有服务')
        print('  [9] 查看运行状态')
        print('  [A] 切换对手软件 (腾讯围棋 / 星阵围棋)')
        print('  [0] 退出')
        print('=' * 46)
        ch = input('  请选择: ').strip()
        if ch == '1':
            if start_ai('black', cfg, cfg['watch']):
                cfg['color'] = 'black'
                save_config(cfg)
            input('\n按回车返回菜单...')
        elif ch == '2':
            if start_ai('white', cfg, cfg['watch']):
                cfg['color'] = 'white'
                save_config(cfg)
            input('\n按回车返回菜单...')
        elif ch == '3':
            v = ask_float('  思考时间下限(秒)', cfg['tmin'], 1.0, 100.0)
            if v is not None and v <= cfg['tmax']:
                cfg['tmin'] = v
                save_config(cfg)
                print(f'  已保存: 下限 {v:g}s')
            elif v is not None:
                print(f'  下限 {v:g}s 超过上限 {cfg["tmax"]:g}s, 未保存')
            input('\n按回车返回菜单...')
        elif ch == '4':
            v = ask_float('  思考时间上限(秒)', cfg['tmax'], 1.0, 100.0)
            if v is not None and v >= cfg['tmin']:
                cfg['tmax'] = v
                save_config(cfg)
                print(f'  已保存: 上限 {v:g}s')
            elif v is not None:
                print(f'  上限 {v:g}s 低于下限 {cfg["tmin"]:g}s, 未保存')
            input('\n按回车返回菜单...')
        elif ch == '5':
            v = ask_float('  轮询间隔(秒)', cfg['interval'], 2.0, 30.0)
            if v:
                cfg['interval'] = v
                save_config(cfg)
                print(f'  已保存: 轮询 {v:g}s')
            input('\n按回车返回菜单...')
        elif ch == '6':
            if start_preload(max(cfg['tmin'], cfg['tmax'])):
                print('  KataGo 预加载已就绪 (或已在运行)')
            else:
                print('  KataGo 预加载启动失败, 请查看 kata_service.log')
            input('\n按回车返回菜单...')
        elif ch == '7':
            start_watch()
            input('\n按回车返回菜单...')
        elif ch == '8':
            n1, n2, nsvc = stop_all()
            print(f'  已停止: controller x{n1}, 看板 x{n2}, KataGo预加载服务 x{nsvc}')
            input('\n按回车返回菜单...')
        elif ch == '9':
            if ctrl_r:
                print(f'  controller 运行中: PID {[p[0] for p in ctrl_list]}')
                print(f'  最近日志: {LOG_FILE}')
            else:
                print('  controller: 停止')
            if watch_r:
                print(f'  看板运行中: PID {[p[0] for p in watch_list]}')
                print(f'  看板地址: http://127.0.0.1:8123/analysis.html')
            else:
                print('  看板: 停止')
            print(f'  KataGo 预加载 ({SERVICE_PORT}): {"运行中" if svc else "停止"}')
            input('\n按回车返回菜单...')
        elif ch == '0':
            print('再见')
            break
        elif ch.lower() == 'a':
            cur = cfg.get('platform', 'tencent')
            new_pf = 'xingzhen' if cur == 'tencent' else 'tencent'
            print(f'  对手软件: {PLATFORM_LABELS[cur]} -> {PLATFORM_LABELS[new_pf]}')
            cfg['platform'] = new_pf
            save_config(cfg)
            print(f'  已保存 (下次启动 AI 生效)')
            input('\n按回车返回菜单...')
        else:
            print('  无效选择')
            time.sleep(1)


if __name__ == '__main__':
    if not try_lock():
        print('!! 启动器已在运行 (锁文件 %s 被占用)' % LOCK_PATH)
        print('   如确认无启动器在跑, 删除该文件再试')
        input('按回车退出...')
        sys.exit(0)
    try:
        menu()
    except KeyboardInterrupt:
        print('\n已退出')
    finally:
        release_lock()

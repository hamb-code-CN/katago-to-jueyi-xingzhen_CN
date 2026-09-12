# -*- coding: utf-8 -*-
"""controller 单实例锁测试。

背景: 曾出现两个 go_controller 同时执子抢一块棋盘 —— 互相打断、
子数乱跳、胜率在 0% 与 98% 之间甩, 看起来就是"AI 突然变傻"。
锁必须满足:
  1) 空闲时可获取, 退出(或 atexit)后释放;
  2) 已有**存活**进程持锁 -> 后来者被拒;
  3) 持锁进程已死(陈旧锁) -> 后来者清理并接管, 避免崩溃后永久锁死。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.append(ROOT)      # append: 避免根目录残留的 test_*.py 抢在 tests/ 之前被 discover 到

import go_controller as gc  # noqa: E402


class TestSingletonLock(unittest.TestCase):
    def setUp(self):
        fd, self.lock = tempfile.mkstemp(prefix='_ctrl_test_', suffix='.lock')
        os.close(fd)
        os.remove(self.lock)                 # 从"不存在"开始
        self._orig = gc.CONTROLLER_LOCK
        gc.CONTROLLER_LOCK = self.lock       # 隔离, 绝不碰真实锁

    def tearDown(self):
        gc.CONTROLLER_LOCK = self._orig
        try:
            os.remove(self.lock)
        except OSError:
            pass

    def test_acquire_then_release(self):
        ok, pid = gc.acquire_controller_lock()
        self.assertTrue(ok)
        self.assertEqual(pid, os.getpid())
        self.assertTrue(os.path.exists(self.lock))
        gc.release_controller_lock()
        self.assertFalse(os.path.exists(self.lock))

    def test_second_blocked_by_live_holder(self):
        # 造一个真实存活的外部进程, 把它的 pid 写进锁文件
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            with open(self.lock, 'w', encoding='utf-8') as f:
                json.dump({'pid': proc.pid, 'ts': time.time(), 'color': None}, f)
            ok, owner = gc.acquire_controller_lock()
            self.assertFalse(ok, '有人持锁时不该拿到')
            self.assertEqual(owner, proc.pid)
        finally:
            proc.kill()
            proc.wait()

    def test_stale_lock_is_reclaimed(self):
        # 写一个必然不存在的 pid -> 视为陈旧锁, 应被清理后接管
        with open(self.lock, 'w', encoding='utf-8') as f:
            json.dump({'pid': 999999, 'ts': 0, 'color': None}, f)
        ok, pid = gc.acquire_controller_lock()
        self.assertTrue(ok, '陈旧锁应可接管')
        self.assertEqual(pid, os.getpid())
        gc.release_controller_lock()


class TestDashboardSingleBind(unittest.TestCase):
    """看板端口不可被两个进程同时绑定。

    Windows 的 SO_REUSEADDR 允许两个进程同时 bind 同一端口(Linux 不会),
    曾导致同时跑起两个看板 -> 两个看门狗 -> 两个 controller 抢棋盘。"""

    @unittest.skipUnless(os.name == 'nt', '该行为是 Windows 专有')
    def test_port_cannot_be_bound_twice(self):
        import functools as ft
        import analysis_watch as aw
        h = ft.partial(aw.NoCacheHandler, directory=aw.OUT_DIR)
        s1 = aw.DualStackServer(('::', 0), h)
        try:
            port = s1.server_address[1]
            with self.assertRaises(OSError):
                aw.DualStackServer(('::', port), h)
        finally:
            s1.server_close()


class TestStatusLockFallback(unittest.TestCase):
    """进程枚举返空时, status() 必须靠锁文件判断 controller 还在跑。

    本机 `wmic` 实测返回空。一旦枚举返空, 看门狗 `_supervise()` 就会把
    "正在运行的 controller" 误判成已退出, 每 30s 重复拉起一个,
    最终一堆僵尸同抢一块棋盘 (实测曾同时 11 个)。"""

    def setUp(self):
        import go_launcher as gl
        self.gl = gl
        self.dir = tempfile.mkdtemp(prefix='_gl_status_')
        self._saved = (gl.BASE, gl._wmic_list, gl._listen)
        gl.BASE = self.dir                       # 让 _controller_lock_pid 读临时目录
        gl._wmic_list = lambda cache_ttl=1.5: []  # 模拟枚举失败
        gl._listen = lambda port: False

    def tearDown(self):
        self.gl.BASE, self.gl._wmic_list, self.gl._listen = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_lock(self, pid):
        with open(os.path.join(self.dir, '_controller.lock'), 'w',
                  encoding='utf-8') as f:
            json.dump({'pid': pid, 'ts': time.time()}, f)

    def test_live_lock_pid_counts_as_running(self):
        self._write_lock(os.getpid())            # 本测试进程 = 存活
        ctrl, _watch, _svc = self.gl.status()
        self.assertTrue(ctrl, '枚举返空时仍应通过锁文件判定 controller 在跑')
        self.assertEqual(ctrl[0][0], os.getpid())

    def test_dead_lock_pid_is_ignored(self):
        self._write_lock(999999)                 # 必然不存在的 PID
        ctrl, _watch, _svc = self.gl.status()
        self.assertEqual(ctrl, [])


class TestRejectedControllerDoesNotHang(unittest.TestCase):
    """被单例锁拒绝的 controller 必须"有界退出", 不能卡在等回车。

    早期版本拒绝分支用 input('按回车退出...'): 看门狗拉起 controller 时 stdin
    继承了控制台 tty -> isatty() 为真 -> 进程永远卡在 input(), 既不下棋也不退出。
    本机因此同时堆了 11 个僵尸。用真实子进程跑一遍拒绝分支, 保证它一定结束。"""

    def test_rejected_process_exits(self):
        d = tempfile.mkdtemp(prefix='_ctrl_reject_')
        try:
            lockp = os.path.join(d, '_controller.lock')
            with open(lockp, 'w', encoding='utf-8') as f:
                json.dump({'pid': os.getpid(), 'ts': time.time()}, f)  # 存活者持锁
            env = dict(os.environ, GOAI_CONTROLLER_LOCK=lockp)
            proc = subprocess.Popen(
                [sys.executable, '-u', 'go_controller.py', '--once', '--color', 'white'],
                cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # stdin 不重定向: 交给当前终端 (正是当初 isatty=True 卡死的场景)
            try:
                out = proc.communicate(timeout=30)[0].decode('utf-8', 'ignore')
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail('被拒的 controller 未在 30s 内退出 (疑似又卡在 input)')
            self.assertIn('拒绝启动', out)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
# -*- coding: utf-8 -*-
"""重启 KataGo 服务 + AI controller (加载打劫修复后的新代码).
沿用当前运行参数: 执黑 / 每手 1.5~3.0s / interval 5s / 腾讯围棋.
看板 watch (8123) 保持不动.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import go_launcher as L

print('=== 重启 AI (打劫修复) ===', flush=True)

# 1) 停掉旧代码的 kata_service (8124) —— 旧版不认识 history 字段, 必须换新
n = L._stop_preload()
print('[1] 停止旧 kata_service x%d' % n, flush=True)
time.sleep(1.5)

# 2) 启动预加载(冷启动新版 KataGo, 可能 60-90s) + controller
cfg = {'tmin': 1.5, 'tmax': 3.0, 'interval': 5.0,
       'platform': 'tencent', 'watch': True}
ok = L.start_ai('black', cfg, with_watch=False)
print('[2] start_ai(执黑,1.5~3.0s,tencent) ->', ok, flush=True)
print('=== DONE ===', flush=True)

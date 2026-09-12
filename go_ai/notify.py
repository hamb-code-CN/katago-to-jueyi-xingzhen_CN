# -*- coding: utf-8 -*-
"""事件通知 (对局结束 / 引擎掉线 / 异常)。

设计要点:
- **默认关闭**: 只有 settings.json 里 notify.enabled=true 且 url 非空才会真的发请求,
  所以默认行为与升级前完全一致 (零副作用)。
- 只用标准库 urllib, 不引入 requests 依赖。
- 后台线程发送 + 短超时, 绝不阻塞对局主循环; 失败只写日志, 不抛异常。
- url 可以是任意接受 JSON POST 的地址。若填自己的 QQ bot 接口, 参考:
      {"event":"terminal","title":"...","text":"...","ts":...}
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
NOTIFY_LOG = os.path.join(BASE, 'notify.log')
LOG_MAX = 512 * 1024      # 超过则滚动, 避免长时间运行日志吃内存
TIMEOUT = 6.0


def _log(line):
    try:
        if os.path.exists(NOTIFY_LOG) and os.path.getsize(NOTIFY_LOG) > LOG_MAX:
            with open(NOTIFY_LOG, encoding='utf-8', errors='replace') as f:
                tail = f.read()[-LOG_MAX // 2:]
            with open(NOTIFY_LOG, 'w', encoding='utf-8') as f:
                f.write(tail)
        with open(NOTIFY_LOG, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
    except Exception:
        pass


def _cfg():
    """读取通知配置; 读不到就返回"关闭"配置 (不 import config_store 以免循环依赖)。"""
    try:
        p = os.path.join(BASE, 'settings.json')
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                n = (json.load(f) or {}).get('notify') or {}
            return {
                'enabled': bool(n.get('enabled', False)),
                'url': str(n.get('url') or '').strip(),
                'events': n.get('events') or [],
            }
    except Exception:
        pass
    return {'enabled': False, 'url': '', 'events': []}


def enabled(event=None, config=None):
    c = config or _cfg()
    if not c.get('enabled') or not c.get('url'):
        return False
    ev = c.get('events') or []
    return (not ev) or (event in ev)


def send(event, title, text='', config=None, timeout=TIMEOUT, block=False):
    """发一条通知。返回 True 表示已成功投递 (或按配置跳过时返回 False)。

    block=False (默认) 走后台线程, 立即返回; block=True 用于测试/退出前收尾。
    """
    c = config or _cfg()
    if not enabled(event, c):
        return False
    payload = {
        'event': event,
        'title': title,
        'text': text,
        'ts': time.time(),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'GoAI Auto-Player',
    }
    if block:
        return _post(c['url'], payload, timeout)
    threading.Thread(target=_post, args=(c['url'], payload, timeout), daemon=True).start()
    return True


def _post(url, payload, timeout=TIMEOUT):
    try:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8',
                     'User-Agent': 'GoAI-Auto-Player'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, 'status', 200)
        ok = 200 <= int(code) < 300
        _log('OK   %s -> %s %s' % (payload.get('event'), url, code))
        return ok
    except urllib.error.HTTPError as e:
        _log('FAIL %s -> %s HTTP %s' % (payload.get('event'), url, e.code))
        return False
    except Exception as e:
        _log('FAIL %s -> %s %r' % (payload.get('event'), url, e))
        return False


if __name__ == '__main__':
    print(json.dumps(_cfg(), ensure_ascii=False))

# -*- coding: utf-8 -*-
"""统一配置存储 (单一真源).

历史问题: 参数散落在 4 处 —— go_controller.Config 类默认值、命令行 argparse、
go_ai/launcher_config.json、看板 /api/config, 改一个参数要同时猜三处优先级,
已经出过"改了没生效"的坑。

现在规则只有一条:
    settings.json 是唯一权威, 代码里的 DEFAULT 只是缺失时的缺省值。
    go_launcher / analysis_watch / go_controller 全部通过本模块读写。

首次运行时若发现历史 launcher_config.json, 自动迁移成 settings.json(保留原文件)。
"""
import os
import json
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'settings.json')
LEGACY_PATH = os.path.join(BASE, 'launcher_config.json')   # 迁移来源(只读)
LOG_FILE = os.path.join(BASE, 'controller_launcher.log')
KATA_LOG = os.path.join(BASE, 'kata_service.log')
LOCK_PATH = os.path.join(BASE, '_launcher.lock')

SERVICE_PORT = 8124   # KataGo 常驻服务端口
WATCH_PORT = 8123     # 看板端口

# 智力档位 -> (思考下限, 思考上限) 秒. 决定 KataGo 每手搜索时间区间
LEVELS = {
    '入门新手': (0.5, 1.5),
    '初级':     (1.5, 3.0),
    '中级':     (3.0, 6.0),
    '高级':     (6.0, 12.0),
    '职业':     (12.0, 20.0),
}

# 对手软件 (棋盘来源)
PLATFORMS = ('tencent', 'xingzhen')
PLATFORM_LABELS = {'tencent': '腾讯围棋', 'xingzhen': '星阵围棋'}

VALID_COLORS = ('black', 'white')

# 落子方式: auto=AI 自动落子 | assist=AI 只分析, 玩家在看板点选落子 | analyze=只分析不出手
VALID_MODES = ('auto', 'assist', 'analyze')
MODE_LABELS = {'auto': 'AI 自动落子', 'assist': '辅助(玩家点选落子)', 'analyze': '仅分析不落子'}

# 全部已知键 (save 时据此裁剪; 嵌套组按字典合并)
DEFAULT_CONFIG = {
    # ---- 启动参数 (与旧 launcher_config.json 兼容) ----
    'tmin': 3.0,
    'tmax': 6.0,
    'interval': 5.0,
    'color': 'black',
    'mode': 'auto',          # auto | assist | analyze (见 VALID_MODES)
    'watch': True,
    'level': '中级',
    'platform': 'tencent',
    # ---- KataGo 搜索 ----
    'katago': {
        'max_time': 10.0,            # 未启用随机时的固定思考上限
        'time_random': True,         # 每手思考时间随机化(防 AI 特征)
        'playout_doubling_advantage': 0.0,  # >0 让 AI 变弱/放水, 0=全强度
        'num_search_threads': 0,     # 0=交给引擎按 CPU 自适应
    },
    # ---- 中盘低胜率延长思考 (原 WEIGHT_* 常量) ----
    'weights': {
        'enabled': True,
        'min_moves': 60,     # 触发门槛: 盘面总手数
        'winrate': 0.30,     # 我方胜率阈值 (0~1)
        'max_time': 20.0,    # 延长后思考上限
        'eval_time': 1.5,    # 预评估用时 (kata-raw-nn)
    },
    # ---- 终局识别与自动停止 ----
    'auto_stop': {
        'enabled': True,      # 识别到终局后自动停止控制器
        'stable_cycles': 12,  # 盘面连续 N 轮无变化 + 无行棋迹象 视为终局
        'export_sgf': True,   # 终局时自动导出 SGF
    },
    # ---- 事件通知 (默认关闭, 填 url 才启用) ----
    'notify': {
        'enabled': False,
        'url': '',            # 任意接受 JSON POST 的地址 (如自己的 QQ bot / 群机器人)
        'events': ['terminal', 'engine_down', 'error'],
    },
    # ---- SGF 导出 ----
    'sgf': {
        'dir': '',            # 空 = go_ai/games/
        'player_name': 'GoAI',
    },
    # ---- 看板看门狗 (controller 异常退出后自动重启) ----
    'watchdog': {
        'auto_restart': True,   # 主动"停止"/终局自动退出 不会触发重启
        'check_interval': 10,   # 检查周期(秒)
        'max_restarts': 5,      # 连续重启上限, 超过则停下并提示 (防崩溃循环)
    },
    # ---- 取图方式 (后台识别的关键) ----
    'capture': {
        # window: PrintWindow 抓窗口内容 (窗口被遮挡/最小化也能取图, 后台运行)
        # screen: 传统全屏截图 (需要窗口可见)
        # auto  : 先试窗口, 失败回落全屏
        'mode': 'auto',
        # 窗口取图是否水平翻转。部分客户端 PrintWindow 出来是镜像的。
        # 看板里能看到实际取图来源, 若数字棋盘左右反了就把这里改成 'off'。
        'mirror': 'auto',       # 'auto' | 'on' | 'off'
    },
    # ---- 看板棋盘显示 ----
    'dashboard': {
        'board_view': 'digital',   # digital=绘制棋盘(推荐) | photo=实拍截图
    },
}

_lock = threading.RLock()


def _deep_merge(base, patch):
    """递归合并: patch 里的 dict 与 base 同名 dict 合并, 其余覆盖。"""
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _pick_known(patch):
    """裁掉未知键 (防止外部写入垃圾字段), 嵌套组同样裁剪。"""
    out = {}
    for k, v in (patch or {}).items():
        if k not in DEFAULT_CONFIG:
            continue
        default_v = DEFAULT_CONFIG[k]
        if isinstance(default_v, dict):
            if isinstance(v, dict):
                out[k] = {kk: vv for kk, vv in v.items() if kk in default_v}
        else:
            out[k] = v
    return out


def _normalize(cfg):
    """校验 + 归一化。任何非法值都退回缺省, 保证下游不用再做防御。"""
    cfg = _deep_merge(DEFAULT_CONFIG, _pick_known(cfg))

    # 档位驱动: level 是内置档位时以档位为准; 否则视为自定义
    lv = cfg.get('level')
    if lv in LEVELS:
        cfg['tmin'], cfg['tmax'] = LEVELS[lv]
    else:
        cfg['level'] = '自定义'
    try:
        cfg['tmin'] = float(cfg['tmin'])
        cfg['tmax'] = float(cfg['tmax'])
        cfg['interval'] = float(cfg['interval'])
    except (TypeError, ValueError):
        cfg['tmin'], cfg['tmax'] = DEFAULT_CONFIG['tmin'], DEFAULT_CONFIG['tmax']
        cfg['interval'] = DEFAULT_CONFIG['interval']
    if cfg['tmin'] > cfg['tmax']:
        cfg['tmin'], cfg['tmax'] = cfg['tmax'], cfg['tmin']
    cfg['tmin'] = max(0.5, min(cfg['tmin'], 300.0))
    cfg['tmax'] = max(0.5, min(cfg['tmax'], 300.0))
    cfg['interval'] = max(1.0, min(cfg['interval'], 60.0))

    if cfg.get('color') not in VALID_COLORS:
        cfg['color'] = 'black'
    if cfg.get('mode') not in VALID_MODES:
        cfg['mode'] = 'auto'
    if cfg.get('platform') not in PLATFORMS:
        cfg['platform'] = 'tencent'
    cfg['watch'] = bool(cfg.get('watch', True))

    # KataGo 组
    kg = cfg['katago']
    try:
        kg['max_time'] = max(0.5, min(float(kg.get('max_time', 10.0)), 300.0))
    except (TypeError, ValueError):
        kg['max_time'] = 10.0
    try:
        kg['playout_doubling_advantage'] = max(0.0, min(
            float(kg.get('playout_doubling_advantage', 0.0)), 10.0))
    except (TypeError, ValueError):
        kg['playout_doubling_advantage'] = 0.0
    try:
        kg['num_search_threads'] = max(0, int(kg.get('num_search_threads', 0)))
    except (TypeError, ValueError):
        kg['num_search_threads'] = 0
    kg['time_random'] = bool(kg.get('time_random', True))

    # 权重组
    w = cfg['weights']
    try:
        w['min_moves'] = max(0, int(w.get('min_moves', 60)))
    except (TypeError, ValueError):
        w['min_moves'] = 60
    try:
        w['winrate'] = max(0.0, min(float(w.get('winrate', 0.30)), 1.0))
    except (TypeError, ValueError):
        w['winrate'] = 0.30
    try:
        w['max_time'] = max(1.0, min(float(w.get('max_time', 20.0)), 300.0))
    except (TypeError, ValueError):
        w['max_time'] = 20.0
    try:
        w['eval_time'] = max(0.3, min(float(w.get('eval_time', 1.5)), 30.0))
    except (TypeError, ValueError):
        w['eval_time'] = 1.5
    w['enabled'] = bool(w.get('enabled', True))

    # 自动停止组
    a = cfg['auto_stop']
    a['enabled'] = bool(a.get('enabled', True))
    a['export_sgf'] = bool(a.get('export_sgf', True))
    try:
        a['stable_cycles'] = max(3, min(int(a.get('stable_cycles', 12)), 600))
    except (TypeError, ValueError):
        a['stable_cycles'] = 12

    # 通知组
    n = cfg['notify']
    n['enabled'] = bool(n.get('enabled', False))
    n['url'] = str(n.get('url') or '').strip()
    ev = n.get('events')
    n['events'] = [str(x) for x in ev] if isinstance(ev, (list, tuple)) else list(
        DEFAULT_CONFIG['notify']['events'])

    # SGF 组
    s = cfg['sgf']
    s['dir'] = str(s.get('dir') or '').strip()
    s['player_name'] = str(s.get('player_name') or 'GoAI')

    # 看门狗组
    wd = cfg['watchdog']
    wd['auto_restart'] = bool(wd.get('auto_restart', True))
    try:
        wd['check_interval'] = max(3, min(int(wd.get('check_interval', 10)), 300))
    except (TypeError, ValueError):
        wd['check_interval'] = 10
    try:
        wd['max_restarts'] = max(0, min(int(wd.get('max_restarts', 5)), 100))
    except (TypeError, ValueError):
        wd['max_restarts'] = 5

    # 取图组
    cp = cfg['capture']
    if cp.get('mode') not in ('auto', 'window', 'screen'):
        cp['mode'] = 'auto'
    if cp.get('mirror') not in ('auto', 'on', 'off'):
        cp['mirror'] = 'auto'

    # 看板显示组
    db = cfg['dashboard']
    if db.get('board_view') not in ('digital', 'photo'):
        db['board_view'] = 'digital'
    return cfg


def _migrate_legacy():
    """首次运行: 把历史 launcher_config.json 迁移进 settings.json (原文件保留)。"""
    if os.path.exists(CONFIG_PATH) or not os.path.exists(LEGACY_PATH):
        return
    try:
        with open(LEGACY_PATH, encoding='utf-8') as f:
            old = json.load(f)
        if isinstance(old, dict) and old:
            save_config(_normalize(old))
            print(f'[config] 已从 {os.path.basename(LEGACY_PATH)} 迁移配置到 '
                  f'{os.path.basename(CONFIG_PATH)} (旧文件保留, 可手动删除)')
    except Exception as e:
        print(f'[config] 迁移历史配置失败: {e!r}')


def load_config():
    """读取配置 (带缺省值与归一化)。任何异常都返回可用缺省, 绝不抛。"""
    with _lock:
        _migrate_legacy()
        raw = {}
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, encoding='utf-8') as f:
                    raw = json.load(f)
        except Exception:
            raw = {}
        return _normalize(raw)


def save_config(cfg, patch=None):
    """保存配置。传 patch 时与当前文件内容深合并 (只覆盖给出的键)。"""
    with _lock:
        if patch:
            cfg = _deep_merge(cfg or load_config(), patch)
        clean = _normalize(cfg)
        unknown = [k for k in (cfg or {}) if k not in DEFAULT_CONFIG]
        if unknown:
            # 保留未知顶层键, 避免旧版本/插件写入的字段被静默丢弃
            out = _pick_known(clean)
            for k in unknown:
                out[k] = cfg[k]
            clean_out = out
        else:
            clean_out = _pick_known(clean)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(clean_out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)   # 原子替换: 前端不会读到半截 JSON
        return clean


def get(key, default=None):
    """读取单个键 (顶层)。"""
    return load_config().get(key, default)


def as_cli_defaults():
    """给 go_controller 用的 (tmin, tmax, interval, color, platform) 元组。"""
    c = load_config()
    return c['tmin'], c['tmax'], c['interval'], c['color'], c['platform']


if __name__ == '__main__':
    c = load_config()
    print(json.dumps(c, ensure_ascii=False, indent=2))

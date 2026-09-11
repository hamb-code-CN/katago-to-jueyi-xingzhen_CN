# -*- coding: utf-8 -*-
"""从 KataGo 日志解析最近一手的候选点/胜率, 绘制到棋盘截图上.
用法: python show_analysis.py [--watch N]  (N=每 N 秒刷新一次, 默认单次)
"""
import sys, os, re, glob, time, argparse, json
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE_DIR)  # 便携化: 相对本文件定位项目, 不再依赖本机绝对路径
import numpy as np
import cv2
import pyautogui
from go_vision import detect_board, refine_pts_local, find_go_window, find_browser_window, capture_go_window


def _resolve_log_dir():
    """KataGo 分析日志目录: gtp.cfg 中 logDir=gtp_logs 为相对路径, 落在 katago.exe
    所在目录 (opencl171/gtp_logs) 下. 与 katago_engine 共用同一引擎根目录解析,
    无任何本机绝对路径依赖."""
    from katago_engine import KATAGO_ROOT
    _cand = os.path.join(KATAGO_ROOT, 'opencl171', 'gtp_logs')
    return _cand if os.path.isdir(_cand) else _cand


LOG_DIR = _resolve_log_dir()


# ---------------- 内存优化: 尾部读取 / 日志轮转 ----------------
TAIL_SIZES = (256 * 1024, 4 * 1024 * 1024)   # 先试 256KB, 尾块不足再放大到 4MB


def read_tail_text(path, max_bytes):
    """只读文件末尾 max_bytes 字节 (内存恒定, 不随日志增长).
    返回 text; 若截断则丢掉首个不完整行."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    text = data.decode('utf-8', errors='ignore')
    if start > 0:
        nl = text.find('\n')
        text = text[nl + 1:] if nl >= 0 else ''
    return text


def prune_gtp_logs(keep=6, max_total_mb=256):
    """清理 gtp_logs: 只保留最新 keep 个; 目录总量超限时再从旧到新删.
    正在被 KataGo 写入的文件在 Windows 上删不掉, 失败跳过即可."""
    try:
        files = [os.path.join(LOG_DIR, n) for n in os.listdir(LOG_DIR)
                 if n.endswith('.log')]
    except OSError:
        return 0
    if not files:
        return 0
    files.sort(key=os.path.getmtime, reverse=True)
    removed = 0
    for p in files[keep:]:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    alive = [p for p in files[:keep] if os.path.exists(p)]
    total = sum(os.path.getsize(p) for p in alive)
    if total > max_total_mb * 1048576:
        for p in reversed(alive[1:]):          # 从旧到新删, 保留最新那个
            try:
                total -= os.path.getsize(p)
                os.remove(p)
                removed += 1
            except OSError:
                continue
            if total <= max_total_mb * 1048576:
                break
    return removed

OUT = os.path.join(_BASE_DIR, 'analysis_overlay.png')
GTP_COLS = 'ABCDEFGHJKLMNOPQRST'  # 围棋坐标无 I

# 固定回退棋盘 (窗口偶有偏移, detect_board 失败时用)
FALLBACK = {'x0': 13, 'y0': 316, 'x1': 735, 'y1': 1038, 'step': 38.0}
# 星阵围棋 (Edge 浏览器) 回退棋盘: 2026-08-31 实测 (浏览器窗口 (-8,0,1288,1398) 时棋盘位置)
FALLBACK_XZ = {'x0': 236, 'y0': 479, 'x1': 804, 'y1': 1047, 'step': 29.9}


def _current_platform():
    """读 launcher_config.json 的 platform 字段 ('tencent'/'xingzhen'), 失败默认 tencent"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'launcher_config.json'), encoding='utf-8') as f:
            return json.load(f).get('platform', 'tencent')
    except Exception:
        return 'tencent'


def _find_go_win(platform):
    """按平台找窗口: tencent=腾讯围棋原生窗口, xingzhen=Edge 浏览器窗口"""
    if platform == 'xingzhen':
        return find_browser_window()
    return find_go_window()

# 中国传统坐标: 1~19 -> 一~十九
_CN = '一二三四五六七八九十'


def cn_num(n):
    """1~19 -> 一~十九 (中国传统坐标用中文数字)"""
    if not 1 <= n <= 19:
        return str(n)
    if n <= 10:
        return _CN[n - 1]
    return '十' + _CN[n - 11]


def gtp_to_cn(pt):
    """GTP 坐标 (A3) -> 中国传统坐标 (一之三).
    X=列: 左起第几列(一~十九); Y=行: 下起第几行(GTP 行号即从底部数)."""
    m = re.match(r'^([A-T])(\d{1,2})$', pt.strip())
    if not m:
        return pt
    col = GTP_COLS.index(m.group(1)) + 1
    row = int(m.group(2))
    return f'{cn_num(col)}之{cn_num(row)}'


def _draw_cn(img_bgr, labels, pts, step):
    """PIL 中文标注 (cv2.putText 不支持中文):
    - 候选点标签: 传统坐标 + 胜率
    - 坐标轴: 底部 一~十九 (列, 左->右); 左侧 十九~一 (行, 上->下)
    labels: [(x, y, text, (b,g,r)), ...]
    """
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    f_lab = ImageFont.truetype(r'C:/Windows/Fonts/msyh.ttc', max(14, int(step * 0.42)))
    f_ax = ImageFont.truetype(r'C:/Windows/Fonts/msyh.ttc', max(16, int(step * 0.48)))
    h, w = img_bgr.shape[:2]
    ax_fill = (205, 213, 222)
    # 坐标轴先画 (标签后画盖在上面)
    yb = h - step * 0.95
    for j in range(19):
        d.text((int(pts[0, j, 0]), yb), cn_num(j + 1), font=f_ax, fill=ax_fill, anchor='mm')
    xl = step * 0.95
    for i in range(19):
        d.text((xl, int(pts[i, 0, 1])), cn_num(19 - i), font=f_ax, fill=ax_fill, anchor='mm')
    # 候选点标签
    for x, y, text, (b, g, r) in labels:
        d.text((x, y), text, font=f_lab, fill=(r, g, b), anchor='mm')
    img_bgr[...] = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def find_latest_log():
    logs = glob.glob(os.path.join(LOG_DIR, '*.log'))
    if not logs:
        return None
    logs.sort(key=os.path.getmtime)
    return logs[-1]


def find_latest_valid_log(max_try=3):
    """按时间倒序找最近一个含有效搜索数据的日志 (最多试最新 3 个)."""
    logs = glob.glob(os.path.join(LOG_DIR, '*.log'))
    logs.sort(key=os.path.getmtime, reverse=True)
    for log in logs[:max_try]:
        try:
            d = extract_last_search(log)
            if d and d['cands'] and d['root_win'] is not None:
                return log
        except Exception:
            continue
    return None


def parse_gtp_pt(pt):
    m = re.match(r'^([A-T])(\d{1,2})$', pt.strip())
    if not m:
        return None
    col = GTP_COLS.index(m.group(1))
    row = 19 - int(m.group(2))  # 第19行在顶部
    return row, col


def parse_block(block):
    """解析一块 'Time taken:' 之后的搜索输出"""
    m = re.search(r'Time taken:\s*([\d.]+)', block)
    t = float(m.group(1)) if m else 0.0
    side = 'B' if '---Black(^)---' in block else ('W' if '---White(^)---' in block else '?')
    cands = []
    pat = re.compile(
        r'^([A-T]\d{1,2})\s*:\s*T\s+-?[\d.]+c\s+W\s+(-?[\d.]+)c\s+S\s+-?[\d.]+c\s+\( ?([+-][\d.]+) L'
        r'.*?P\s+([\d.]+)%\s+WF\s+[\d.]+\s+PSV\s+\d+\s+N\s+(\d+)'
    )
    for line in block.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        rc = parse_gtp_pt(m.group(1))
        if not rc:
            continue
        cands.append({
            'pt': m.group(1), 'row': rc[0], 'col': rc[1],
            'win': (float(m.group(2)) / 100.0 + 1.0) / 2.0,
            'score': float(m.group(3)),
            'prior': float(m.group(4)) / 100.0,
            'visits': int(m.group(5)),
        })
    root = re.search(r'^:\s*T\s+-?[\d.]+c\s+W\s+(-?[\d.]+)c', block, re.M)
    root_win = (float(root.group(1)) / 100.0 + 1.0) / 2.0 if root else None
    # KataGo 眼中的棋盘子数 (从 MoveNum 棋盘图, 按行解析避免 CRLF 问题)
    nb = nw = None
    lines = block.splitlines()
    for li, ln in enumerate(lines):
        if 'MoveNum:' in ln:
            board_lines = lines[li + 2:li + 21]
            if len(board_lines) >= 19:
                bd = [list(l[3:22]) for l in board_lines[:19]]
                nb = sum(1 for rr in bd for c in rr if c in 'X@')
                nw = sum(1 for rr in bd for c in rr if c in 'O')
            break
    return {'time': t, 'side': side, 'root_win': root_win, 'cands': cands,
            'board_counts': (nb, nw)}


def extract_last_search(logpath, tail_bytes=None):
    """解析日志中最近一次完整搜索.

    只读文件尾部 (内存恒定, 不随日志大小增长); 尾部不足以覆盖最后一块搜索时,
    自动放大到 TAIL_SIZES 的下一档重试, 不做全量读取."""
    sizes = (tail_bytes,) if tail_bytes else TAIL_SIZES
    for sz in sizes:
        try:
            content = read_tail_text(logpath, sz)
        except OSError:
            return None
        if 'Time taken:' not in content:
            continue
        blocks = content.split('Time taken:')
        if len(blocks) < 2:
            continue
        for b in reversed(blocks[1:]):
            d = parse_block('Time taken:' + b)
            if d['cands']:
                return d
    return None


def get_board():
    """全屏截图 + 窗口锁定 (按平台: 腾讯原生窗口 / 星阵 Edge 浏览器窗口)."""
    platform = _current_platform()
    img = np.array(pyautogui.screenshot())
    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    det = detect_board(img_bgr, window_rect=_find_go_win(platform))
    if det is not None and 'pts' in det:
        det = refine_pts_local(img_bgr, det)
        return img_bgr, det
    return img_bgr, None


def fallback_pts(board):
    fb = FALLBACK_XZ if _current_platform() == 'xingzhen' else FALLBACK
    step = fb['step']
    x0, y0 = fb['x0'], fb['y0']
    pts = np.zeros((19, 19, 2), dtype=np.float64)
    for j in range(19):
        for i in range(19):
            pts[j, i] = (x0 + i * step, y0 + j * step)
    return {'pts': pts, 'step': step}


def _imwrite_unicode(path, img):
    """cv2.imwrite 不支持含中文的路径 (Windows), 用 imencode+tofile 兼容."""
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(path)
    return ok


def draw(show_osd=False, stale=False):
    """show_osd: 是否在图上画 OSD 信息条 (默认不画, 前端顶栏已显示状态, 图更纯净)
    stale: 屏幕棋盘与日志局面不一致(换对局)时, 不画候选点圈"""
    log = find_latest_log()
    if log is None:
        print('NO LOG'); return None
    data = extract_last_search(log)
    img_bgr, det = get_board()
    board = det if det is not None else fallback_pts(None)
    pts = board['pts']
    step = board['step']

    # 只裁剪棋盘区域: 四周各扩 M 步作边距, 把棋盘旁边也剪进来 (用户要求放大范围)
    M = 2.5  # 边距(格), 之前 0.9 太紧
    px0 = int(round(pts[0, 0, 0] - step * M))
    px1 = int(round(pts[18, 18, 0] + step * M))
    py0 = int(round(pts[0, 0, 1] - step * M))
    py1 = int(round(pts[18, 18, 1] + step * M))
    px0, py0 = max(px0, 0), max(py0, 0)
    img_bgr = img_bgr[py0:py1, px0:px1]
    pts = pts - np.array([px0, py0])  # 坐标平移到裁剪图

    if not data or not data['cands'] or stale:
        if stale:
            cv2.putText(img_bgr, 'NEW GAME - 等待 AI 计算', (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 220, 255), 2, cv2.LINE_AA)
        _imwrite_unicode(OUT, img_bgr)
        return OUT

    cands = sorted(data['cands'], key=lambda c: (-c['visits'], -c['win']))
    top = cands[:12]

    # 候选点圆圈 (cv2)
    r_px = int(step * 0.30)
    labels = []
    for k, c in enumerate(top):
        x = int(pts[c['row'], c['col'], 0])
        y = int(pts[c['row'], c['col'], 1])
        pct = c['win'] * 100.0
        if k == 0:
            color = (0, 0, 255)       # 最佳点 红色
            cv2.circle(img_bgr, (x, y), r_px + 2, color, 3)
            cv2.circle(img_bgr, (x, y), r_px + 9, color, 1)
        else:
            color = (0, 200, 0) if pct >= 50 else (200, 160, 0)
            cv2.circle(img_bgr, (x, y), r_px, color, 2)
        labels.append((x, y - r_px - 10, f'{c["pt"]} {pct:.1f}%', color))

    # 中文标注 (PIL): 候选点传统坐标 + 棋盘坐标轴
    _draw_cn(img_bgr, labels, pts, step)

    if show_osd:
        side_txt = '黑方' if data['side'] == 'B' else ('白方' if data['side'] == 'W' else '?')
        rw = data['root_win']
        b0 = top[0]
        line1 = f"轮 {side_txt} 行棋 | 行棋方胜率 {rw*100:.1f}% | 思考 {data['time']:.1f}s | 候选 {len(data['cands'])} 个"
        line2 = f"最佳: {b0['pt']} (胜率 {b0['win']*100:.1f}%, 搜索 {b0['visits']} 次)"
        h, w = img_bgr.shape[:2]
        cv2.rectangle(img_bgr, (10, 10), (w - 10, 84), (20, 20, 20), -1)
        cv2.putText(img_bgr, line1, (20, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img_bgr, line2, (20, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (80, 220, 255), 2, cv2.LINE_AA)

    _imwrite_unicode(OUT, img_bgr)
    return OUT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch', type=float, default=0, help='循环刷新间隔秒, 0=单次')
    args = ap.parse_args()
    if args.watch <= 0:
        p = draw()
        print('saved:', p)
        return
    while True:
        try:
            p = draw()
            if p:
                print(f'[{time.strftime("%H:%M:%S")}] saved {p}')
        except Exception as e:
            print('ERR', repr(e))
        time.sleep(args.watch)


if __name__ == '__main__':
    main()

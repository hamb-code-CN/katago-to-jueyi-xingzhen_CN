# -*- coding: utf-8 -*-
"""腾讯围棋 棋盘识别模块 v3: 木色 mask 找板 + 板内投影找线"""
import ctypes
from ctypes import wintypes

import cv2
import numpy as np

N = 19
EMPTY, BLACK, WHITE = 0, 1, 2

GO_WINDOW_TITLES = ('腾讯围棋', '对局', '19路')
# 浏览器窗口标题关键词 (用于星阵围棋等 Web 版)
BROWSER_WINDOW_TITLES = ('星阵围棋', '19x19', '围棋', 'Galaxy', 'GALAXY')


def find_browser_window():
    """枚举可见窗口, 返回浏览器窗口 rect (left, top, right, bottom) 或 None.
    匹配策略: 标题同时含浏览器标识 (Edge/Chrome) 和棋盘关键词 (星阵围棋/19x19).
    用于星阵围棋等 Web 版对手. 返回值与 find_go_window 同义, 可直接传入 detect_board()."""
    user32 = ctypes.windll.user32
    found = []

    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                # 必须是浏览器窗口 (含 Edge/Chrome) 且含棋盘关键词
                is_browser = any(b in title for b in ('Edge', 'Chrome', 'Mozilla', 'Brave', 'Opera'))
                has_go = any(k in title for k in BROWSER_WINDOW_TITLES)
                if is_browser and has_go:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append((hwnd, title, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc), 0)
    if not found:
        return None
    best = max(found, key=lambda f: (f[2][2] - f[2][0]) * (f[2][3] - f[2][1]))
    return best[2]


def find_go_window():
    """枚举可见窗口, 返回腾讯围棋窗口 rect (left, top, right, bottom) 或 None.
    窗口锁定: 无论窗口拖到哪/缩多大, 都能定位, 棋盘检测只在该窗口内进行."""
    user32 = ctypes.windll.user32
    found = []

    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if any(k in title for k in GO_WINDOW_TITLES):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append((hwnd, title, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc), 0)
    if not found:
        return None
    best = max(found, key=lambda f: (f[2][2] - f[2][0]) * (f[2][3] - f[2][1]))
    return best[2]


def capture_go_window():
    """用 PrintWindow 抓腾讯围棋窗口内容 (不依赖屏幕显示, 隐藏/最小化也能截).
    返回 (hwnd, rect, img_bgr) 或 (None, None, None).
    img_bgr 是窗口内容 BGR 数组 (已水平翻转消除镜像)."""
    import cv2
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    found = []

    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if any(k in buf.value for k in GO_WINDOW_TITLES):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append((hwnd, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc), 0)
    if not found:
        return None, None, None
    best = max(found, key=lambda f: (f[1][2] - f[1][0]) * (f[1][3] - f[1][1]))
    hwnd, rect = best
    l, t, r, b = rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return hwnd, rect, None
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, bmp)
    # PW_RENDERFULLCONTENT (2) 对 Chromium/Electron 渲染窗口有效
    ret = user32.PrintWindow(hwnd, hdc_mem, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                    ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long),
                    ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]
    bih = BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bih.biWidth = w; bih.biHeight = -h; bih.biPlanes = 1; bih.biBitCount = 32; bih.biCompression = 0
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(hdc_mem); user32.ReleaseDC(hwnd, hdc_win)
    if not ret:
        return hwnd, rect, None
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    img = arr[:, :, :3].copy()  # BGRA 前3通道就是 BGR 顺序, 不要翻转通道 (否则红蓝互换)
    img = cv2.flip(img, 1)  # 水平翻转消除镜像
    return hwnd, rect, img


def _wood_mask(roi):
    """提取棋盘区域 (木色/青蓝色), 返回二值 mask.
    腾讯围棋棋盘背景可能是暖木色 (HSV ~20,105,240) 或青蓝色 (HSV ~101,103,243),
    都属于暖-冷饱和的中亮度区域. 两类 mask 取并集, 再形态学填洞."""
    if roi is None or roi.size == 0:
        return np.zeros((1, 1), np.uint8)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 暖木色 (黄褐)
    m1 = cv2.inRange(hsv, (10, 60, 180), (35, 160, 255))
    # 青蓝色棋盘 (腾讯围棋另一个常见主题)
    m2 = cv2.inRange(hsv, (85, 80, 160), (120, 200, 255))
    mask = cv2.bitwise_or(m1, m2)
    k = np.ones((7, 7), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8), iterations=1)
    return mask


def detect_board(img, roi_x_max=1280, window_rect=None, debug=False):
    h, w = img.shape[:2]
    if window_rect is not None:
        # 窗口锁定: 只在腾讯围棋窗口内搜索棋盘
        ox, oy, ox1, oy1 = window_rect
        ox, oy = max(ox, 0), max(oy, 0)
        ox1, oy1 = min(ox1, w), min(oy1, h)
        # 收缩 ROI 排除窗口边框/阴影 (右 25px, 底 10px), 避免边框被误检为网格线/黑子
        ox1 -= 25
        oy1 -= 10
        if ox1 <= ox or oy1 <= oy:
            return None  # 窗口无效/最小化时 ROI 为空, 防止崩溃
        roi = img[oy:oy1, ox:ox1]
    else:
        ox, oy = 0, 0
        roi = img[:, :roi_x_max]
    hsv_roi = roi  # 同步

    # 1) 木色 mask
    mask = _wood_mask(roi)
    # 仅保留大块连通域
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    best = None
    for k in range(1, n):
        x, y, bw, bh, area = stats[k]
        if area < 50000:
            continue
        if bw < 300 or bh < 300:
            continue
        ratio = bw / bh
        if ratio < 0.75 or ratio > 1.33:
            continue
        # 排除过长的 (UI 工具条)
        if best is None or area > best[4]:
            best = (x, y, bw, bh, area)
    if best is None:
        if debug:
            cv2.imwrite('debug_mask.png', mask)
        return None
    bx, by, bw, bh, _ = best
    bx1, by1 = bx + bw, by + bh

    # 合理性校验: 腾讯围棋窗口整体木色背景会让连通域覆盖整个窗口(含下方按钮/历史),
    # 等距网格匹配可能错选下方按钮栏的横线作为棋盘. 真实棋盘高度 700-800px, 若连通域
    # 高度 > 850, 裁剪到上 70% 重检, 把按钮/历史/状态栏排除掉.
    if bh > 850:
        new_bh = int(bh * 0.7)
        cv2.rectangle(mask, (bx, by + new_bh), (bx1, by1), 0, -1)
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        best2 = None
        for k in range(1, n):
            x, y, bw2, bh2, area = stats[k]
            if area < 50000 or bw2 < 300 or bh2 < 300:
                continue
            ratio = bw2 / bh2
            if ratio < 0.75 or ratio > 1.33:
                continue
            if best2 is None or area > best2[4]:
                best2 = (x, y, bw2, bh2, area)
        if best2 is not None:
            bx, by, bw, bh, _ = best2
            bx1, by1 = bx + bw, by + bh

    # 2) 在木色区域内部找 19 条等距网格线
    sub = roi[by:by1, bx:bx1]
    # 腾讯围棋整体窗口背景是木色, wood_rect 包含下方按钮栏/历史/状态栏. 真实棋盘通常
    # 在 wood_rect 上半部分. 截取上 65% 作为搜索区域, 排除下方 UI 干扰.
    sub_search = sub[:int(sub.shape[0] * 0.65)]
    sub_gray = cv2.cvtColor(sub_search, cv2.COLOR_BGR2GRAY)
    _, binimg = cv2.threshold(sub_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # 形态学闭, 连接断线
    binimg = cv2.morphologyEx(binimg, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    vlines = _projection(binimg, axis=1)  # 行投影 -> 横线 y 位置
    hlines = _projection(binimg, axis=0)  # 列投影 -> 竖线 x 位置
    vx_rel = _fit_grid(hlines)  # vx 用竖线 x 数据
    hy_rel = _fit_grid(vlines)  # hy 用横线 y 数据
    if vx_rel is None or hy_rel is None:
        if debug:
            cv2.imwrite('debug_bin.png', binimg)
            cv2.imwrite('debug_mask.png', mask)
        return None
    step = vx_rel[1] - vx_rel[0]
    if not (15 < step < 80):
        return None

    # 转回全图坐标 (roi 偏移 ox,oy + 连通域偏移 bx,by + sub 内偏移)
    vx = [ox + bx + x for x in vx_rel]
    hy = [oy + by + y for y in hy_rel]
    pts = np.zeros((N, N, 2), dtype=np.float64)
    for i, x in enumerate(vx):
        for j, y in enumerate(hy):
            pts[j, i] = (x, y)

    margin = step / 2
    x0 = int(round(vx[0] - margin)); x1 = int(round(vx[-1] + margin))
    y0 = int(round(hy[0] - margin)); y1 = int(round(hy[-1] + margin))
    result = {'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
              'vx': vx, 'hy': hy, 'pts': pts, 'step': step,
              'wood_rect': (bx, by, bx1, by1)}
    # 合理性校验: 网格底部应接近木色连通域底部 (棋盘通常延伸到连通域下边缘附近).
    # 若 hy[-1] 距连通域底 > 3*step, 说明等距网格错选了中部(按钮栏/历史)的等距线.
    if (oy + by1) - hy[-1] > 3 * step:
        if debug:
            print(f'[detect] sanity FAIL: hy[-1]={hy[-1]:.0f} 距连通域底 {oy+by1-hy[-1]:.0f}px > 3*step')
        return None
    if debug:
        dbg = roi.copy()
        cv2.rectangle(dbg, (bx, by), (bx1, by1), (255, 0, 0), 2)
        for x in vx:
            cv2.line(dbg, (int(x), int(hy[0])), (int(x), int(hy[-1])), (0, 0, 255), 1)
        for y in hy:
            cv2.line(dbg, (int(vx[0]), int(y)), (int(vx[-1]), int(y)), (0, 0, 255), 1)
        for j in range(N):
            for i in range(N):
                cv2.circle(dbg, (int(pts[j, i, 0]), int(pts[j, i, 1])), 2, (0, 255, 0), -1)
        return result, dbg
    return result


def _projection(binimg, axis):
    """在二值图上按行/列累加, 返回聚类后的线位置"""
    proj = (binimg > 0).sum(axis=axis).astype(np.float64)
    L = binimg.shape[1 if axis == 1 else 0]
    thr = max(0.30 * L, 8)
    cand = np.where(proj > thr)[0]
    if len(cand) == 0:
        return []
    clusters = []
    cur = [cand[0]]
    for p in cand[1:]:
        if p - cur[-1] <= 3:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)
    positions = [int(c[np.argmax(proj[c])]) for c in clusters]
    return positions


def _fit_grid(positions, n=N, tol=3.0, prefer_top=True):
    """从检测到的网格线候选中拟合 19 条等距线.
    用匹配法(每条检测线作为候选起点, 选与最多线吻合的网格), 抗噪声线.
    prefer_top=True 时, 吻合数相同时偏好起点最靠上的网格, 避免误选下方UI栏的等距线."""
    if len(positions) < 3:
        return None
    pos = np.sort(np.asarray(positions, dtype=float))
    diffs = np.diff(pos)
    diffs = diffs[diffs > 0.5]
    if len(diffs) == 0:
        return None
    step = float(np.median(diffs))
    if step < 5:
        return None
    best_score, best_grid, best_p0 = -1, None, 1e18
    for s0 in np.arange(step - 1.0, step + 1.0, 0.1):
        s = max(s0, 5.0)
        for p0 in pos:
            grid = p0 + np.arange(n) * s
            dmin = np.abs(pos[:, None] - grid[None, :]).min(axis=1)
            score = int((dmin <= tol).sum())
            # 偏好: 吻合数高优先; 相同时起点靠上优先 (避免错选下方按钮栏)
            if score > best_score or (score == best_score and prefer_top and p0 < best_p0):
                best_score, best_grid, best_p0 = score, grid, p0
    if best_grid is None:
        return None
    return list(best_grid)


CAL_BOARD = {'x0': 33, 'y0': 336, 'x1': 727, 'y1': 1029, 'step': 38.5}  # 2026-08-26 校准 (归零+检测值)


def snap_to_cal(board, tol=50):
    """若检测棋盘位置与校准值接近 (<tol px), 用校准网格替换.
    避免 detect_board 在腾讯围棋窗口布局变化时把按钮栏/历史误认为棋盘.
    容差放宽到 50px 覆盖窗口轻微移动."""
    if board is None or 'pts' not in board:
        return board
    if abs(board['x0'] - CAL_BOARD['x0']) > tol or abs(board['y0'] - CAL_BOARD['y0']) > tol:
        return board  # 窗口真动了, 用检测值
    step = CAL_BOARD['step']
    vx = [CAL_BOARD['x0'] + i * step for i in range(N)]
    hy = [CAL_BOARD['y0'] + j * step for j in range(N)]
    pts = np.zeros((N, N, 2), dtype=np.float64)
    for j in range(N):
        for i in range(N):
            pts[j, i] = (vx[i], hy[j])
    nb = dict(board)
    nb.update({'vx': vx, 'hy': hy, 'pts': pts, 'step': step,
               'x0': CAL_BOARD['x0'], 'y0': CAL_BOARD['y0'],
               'x1': CAL_BOARD['x1'], 'y1': CAL_BOARD['y1']})
    return nb


CAL_Y_FRAC = 0.0  # 手动校准: 棋盘整体Y偏移(正=下移,负=上移, 按格距比例). 2026-08-25: 上移半格(-0.5)后用户反馈需下移半格 -> 归零


def _apply_cal_offset(board):
    """应用手动 Y 偏移校准 (按格距比例). 只移 y, 保持 x."""
    if not CAL_Y_FRAC or board is None or 'pts' not in board:
        return board
    step = board.get('step', 38.0)
    dy = step * CAL_Y_FRAC
    nb = dict(board)
    nb['pts'] = np.asarray(nb['pts'], dtype=np.float64) + np.array([0.0, dy])
    if 'hy' in nb:
        nb['hy'] = [v + dy for v in nb['hy']]
    if 'y0' in nb:
        nb['y0'] = int(round(nb['y0'] + dy))
    if 'y1' in nb:
        nb['y1'] = int(round(nb['y1'] + dy))
    return nb


def refine_pts(img, board):
    """棋子质心整体校正 (读盘/点击用, 稳定准确). 返回新 board."""
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except Exception:
        return _apply_cal_offset(board)
    pts = board['pts']
    step = board['step']
    r = int(step * 0.30)
    offs = []
    h, w = gray.shape[:2]
    for j in range(N):
        for i in range(N):
            x, y = int(pts[j, i, 0]), int(pts[j, i, 1])
            if x - r < 0 or y - r < 0 or x + r >= w or y + r >= h:
                continue
            p = gray[y - r:y + r + 1, x - r:x + r + 1]
            dark = p < 110
            light = p > 200
            if dark.sum() > 12:
                ys, xs = np.nonzero(dark)
            elif light.sum() > 12:
                ys, xs = np.nonzero(light)
            else:
                continue
            cx = x - r + xs.mean()
            cy = y - r + ys.mean()
            if abs(cx - x) < step * 0.35 and abs(cy - y) < step * 0.35:
                offs.append((cx - x, cy - y))
    if len(offs) >= 4:
        dx = float(np.median([o[0] for o in offs]))
        dy = float(np.median([o[1] for o in offs]))
        if abs(dx) > 0.4 or abs(dy) > 0.4:
            nb = dict(board)
            nb['pts'] = pts - np.array([dx, dy])
            nb['vx'] = [v - dx for v in board['vx']]
            nb['hy'] = [v - dy for v in board['hy']]
            nb['x0'] = int(round(board['x0'] - dx))
            nb['x1'] = int(round(board['x1'] - dx))
            nb['y0'] = int(round(board['y0'] - dy))
            nb['y1'] = int(round(board['y1'] - dy))
            return _apply_cal_offset(nb)
    return _apply_cal_offset(board)


def refine_pts_local(img, board):
    """整体校正 + 局部吸附 (看板标记绘制用, 圈更贴棋子).
    仅在需要视觉对齐的绘制场景使用, 读盘请用 refine_pts."""
    board = refine_pts(img, board)
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except Exception:
        return board
    pts = board['pts']
    step = board['step']
    new_pts = pts.copy()
    r2 = int(step * 0.42)
    max_shift = step * 0.18
    h, w = gray.shape[:2]
    for j in range(N):
        for i in range(N):
            x, y = int(new_pts[j, i, 0]), int(new_pts[j, i, 1])
            if x - r2 < 0 or y - r2 < 0 or x + r2 >= w or y + r2 >= h:
                continue
            p = gray[y - r2:y + r2 + 1, x - r2:x + r2 + 1]
            dark = p < 100
            light = p > 210
            if dark.sum() > 20:
                ys, xs = np.nonzero(dark)
            elif light.sum() > 20:
                ys, xs = np.nonzero(light)
            else:
                continue
            cx = x - r2 + xs.mean()
            cy = y - r2 + ys.mean()
            ddx, ddy = cx - x, cy - y
            if abs(ddx) < max_shift and abs(ddy) < max_shift:
                new_pts[j, i, 0] = cx
                new_pts[j, i, 1] = cy
    nb = dict(board)
    nb['pts'] = new_pts
    return nb


def read_board(img, board, debug=False):
    """读子: 交点 patch 均值法 (比连通域鲁棒, 不受棋子反光影响).
    返回 19x19 数组 (0空 1黑 2白). 调用方应先用 refine_pts 校正 board."""
    pts = board['pts']
    step = board['step']
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s_ch = hsv[:, :, 1]
    stones = np.zeros((N, N), dtype=np.int8)
    half = int(step * 0.45)  # 交点邻域半宽 (45% 步长, 不碰相邻交点/窗口边框)
    h, w = img.shape[:2]
    for j in range(N):
        for i in range(N):
            x, y = int(pts[j, i, 0]), int(pts[j, i, 1])
            x0, y0 = max(0, x - half), max(0, y - half)
            x1, y1 = min(w, x + half + 1), min(h, y + half + 1)
            if x1 - x0 < 6 or y1 - y0 < 6:
                continue
            p_g = gray[y0:y1, x0:x1]
            p_s = s_ch[y0:y1, x0:x1]
            mg = float(p_g.mean())
            ms = float(p_s.mean())
            if mg < 150:
                stones[j, i] = BLACK
            elif ms < 80:
                # 低饱和度 = 白子 (含带黑三角"最后一手"标记的白子,
                # 标记会拉低均值但饱和度仍低; 木色空点饱和度 ~108 不会误判)
                stones[j, i] = WHITE
    dbg = img.copy() if debug else None
    if debug:
        for j in range(N):
            for i in range(N):
                x, y = int(pts[j, i, 0]), int(pts[j, i, 1])
                col = {0: (0, 255, 0), 1: (0, 0, 255), 2: (0, 200, 255)}[int(stones[j, i])]
                cv2.circle(dbg, (x, y), int(step * 0.32), col, 2)
    return stones, dbg


def _wood_brightness(img, board):
    vx = board['vx']; hy = board['hy']
    samples = []
    for j in [5, 6, 12, 13]:
        for i in [5, 6, 12, 13]:
            x, y = int(round(vx[i])), int(round(hy[j]))
            p = img[y - 4:y + 5, x - 4:x + 5]
            if p.size == 0:
                continue
            samples.append(np.mean(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY)))
    return float(np.median(samples)) if samples else 140


def _wood_color(img, board):
    """取几个内部交点的 BGR 平均, 作为木色参考"""
    vx = board['vx']; hy = board['hy']
    samples = []
    for j in [5, 6, 12, 13]:
        for i in [5, 6, 12, 13]:
            x, y = int(round(vx[i])), int(round(hy[j]))
            r = int(max(3, board['step'] * 0.18))
            for dx, dy in [(r, r), (r, -r), (-r, r), (-r, -r)]:
                sx, sy = x + dx, y + dy
                if 0 <= sx < img.shape[1] and 0 <= sy < img.shape[0]:
                    samples.append(img[sy, sx])
    if not samples:
        return np.array([140, 200, 240], dtype=np.float32)
    return np.array(samples, dtype=np.float32).mean(axis=0)


def board_to_pixel(board, col, row):
    x, y = board['pts'][row, col]
    return int(round(x)), int(round(y))


def detect_turn_side(img, mid_x=391):
    """检测腾讯围棋界面 '黑方行棋/白方行棋' 印章 (朱砂红圆章).
    印章特征: 红色圆底 + 白字. 按列统计朱砂红像素密度找最大密集簇.
    cx < mid_x -> 黑方方行棋, cx > mid_x -> 白方行棋.
    子数法在预设局面(如"常见问题")会失效, 此函数可靠.
    返回 'B'/'W'/None.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # 朱砂红: H 0-12 或 170-180, S 60-170, V 150-240 (实测印章 HSV ~H5-11,S60-130,V150-240)
    m1 = cv2.inRange(hsv, (0, 60, 150), (12, 170, 240))
    m2 = cv2.inRange(hsv, (170, 60, 150), (180, 170, 240))
    mask = (m1 | m2).astype(np.uint8)
    # 只关注印章所在高度带 (窗口内 y 150-260)
    m = mask[150:260, :]
    # 形态学闭运算连接成块
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    col_sum = (m > 0).sum(axis=0)
    best = None
    start = 0
    in_cluster = False
    cur_sum = 0
    for x in range(len(col_sum)):
        if col_sum[x] > 8:
            if not in_cluster:
                start = x
                in_cluster = True
                cur_sum = 0
            cur_sum += int(col_sum[x])
        else:
            if in_cluster:
                cx = (start + x - 1) // 2
                if best is None or cur_sum > best[2]:
                    best = (cx, x - start, cur_sum)
                in_cluster = False
    if in_cluster:
        cx = (start + len(col_sum) - 1) // 2
        if best is None or cur_sum > best[2]:
            best = (cx, len(col_sum) - start, cur_sum)
    if best is None:
        return None
    return 'B' if best[0] < mid_x else 'W'

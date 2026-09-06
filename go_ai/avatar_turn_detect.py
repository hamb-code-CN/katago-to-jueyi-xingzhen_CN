# -*- coding: utf-8 -*-
"""星阵围棋头像高亮回合检测 (替代红章 OCR).
用头像下方/旁的'亮蓝色水滴'判断轮到谁下. 不用 OCR, 速度 <10ms.

UI 特征: 棋盘右上方面板, 左右两个圆形头像 (星阵鼠=黑方, hamb=白方).
'轮到该方走棋'时, 该方头像下方/外侧出现一个发光的亮蓝色水滴 (类似游戏'你的回合'提示).
不轮到他时, 头像下方无水滴. 倒计时在两个头像之间下方.

输入: img (全屏 BGR), window_rect (浏览器窗口)
输出: 'B' (黑方行棋) / 'W' (白方行棋) / None (未识别, 不在游戏中)
"""
import cv2
import numpy as np

# 蓝水滴 HSV 范围 (亮蓝/天蓝色, 含青色高光):
#   H: 100-135 (蓝/青), S: 50+ (有饱和度), V: 180+ (高亮)
#   暗水珠边沿 V 也可能低, 所以下限放到 150
#   实测水滴 size ~ 130-450 像素, 中心位置
BLUE_HUE = (95, 145)        # H 范围
BLUE_SAT_MIN = 50            # 饱和度下限 (避免把灰色误判)
BLUE_VAL_MIN = 150           # 亮度下限 (避免阴影区)

# 两个头像水滴搜索 ROI (相对窗口比例, x0/y0/x1/y1)
# 星阵围棋 UI: 棋盘上方右侧面板, 左头像=星阵鼠=黑方, 右头像=hamb=白方.
# 头像直径约 80-100px, 水滴在头像下方 100-150px (中间隔名字).
# 实测: 头像下方水滴中心约在 窗口 x=70%(黑) / x=90%(白), y=40%
BLACK_DROP_RATIO = (0.69, 0.36, 0.78, 0.43)   # 黑方 (星阵鼠) 头像水滴区
WHITE_DROP_RATIO = (0.85, 0.36, 0.94, 0.43)   # 白方 (hamb) 头像水滴区

# 单 ROI 内蓝色像素阈值 (水滴一般 100+ 像素, 噪声 < 30)
DROP_PIXEL_THRESHOLD = 50


def _blue_count(roi):
    """统计 ROI 内亮蓝色像素数"""
    if roi is None or roi.size == 0:
        return 0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (BLUE_HUE[0], BLUE_SAT_MIN, BLUE_VAL_MIN),
                             (BLUE_HUE[1], 255, 255))
    return int(mask.sum() / 255)


def detect_turn_side(img, window_rect=None):
    """识别'轮到哪方走棋'. img 全屏 BGR; window_rect=(ox,oy,ox1,oy1) 浏览器窗口.
    返回 'B' (黑方) / 'W' (白方) / None.
    None 表示: 窗口无效 / 两个 ROI 都没水滴 (可能不在游戏中) / 异常.
    """
    if window_rect is None:
        return None
    ox, oy, ox1, oy1 = window_rect
    bw, bh = max(ox1 - ox, 1), max(oy1 - oy, 1)
    H, W = img.shape[:2]

    results = {}
    for who, ratio in (('B', BLACK_DROP_RATIO), ('W', WHITE_DROP_RATIO)):
        rx0, ry0, rx1, ry1 = ratio
        x0 = max(0, ox + int(bw * rx0))
        y0 = max(0, oy + int(bh * ry0))
        x1 = min(W, ox + int(bw * rx1))
        y1 = min(H, oy + int(bh * ry1))
        if x1 <= x0 or y1 <= y0:
            results[who] = 0
            continue
        roi = img[y0:y1, x0:x1]
        results[who] = _blue_count(roi)

    b_cnt, w_cnt = results['B'], results['W']
    # 都没水滴 -> 不在游戏中
    if b_cnt < DROP_PIXEL_THRESHOLD and w_cnt < DROP_PIXEL_THRESHOLD:
        return None
    # 哪个有水滴 -> 轮到他
    if b_cnt > w_cnt and b_cnt >= DROP_PIXEL_THRESHOLD:
        return 'B'
    if w_cnt > b_cnt and w_cnt >= DROP_PIXEL_THRESHOLD:
        return 'W'
    return None


def is_my_turn(img, my_color, window_rect):
    """返回 True/False 表示是否轮到我方. None 表示无法判断 (交由子数法回退)."""
    side = detect_turn_side(img, window_rect)
    if side is None:
        return None
    return (side == 'B' and my_color == 1) or (side == 'W' and my_color == 2)


if __name__ == '__main__':
    import sys, time
    img = cv2.imread(sys.argv[1]) if len(sys.argv) > 1 else None
    if img is None:
        print('用法: python avatar_turn_detect.py <截图.png>')
        sys.exit(0)
    t0 = time.time()
    # 用全屏做假窗口 (相对坐标会失效, 仅演示)
    H, W = img.shape[:2]
    side = detect_turn_side(img, window_rect=(0, 0, W, H))
    print(f'检测: {side}  ({time.time()-t0:.3f}s)')

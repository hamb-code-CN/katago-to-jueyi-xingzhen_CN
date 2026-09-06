# -*- coding: utf-8 -*-
"""红章回合检测 - 用 RapidOCR 识别顶部栏 '黑方行棋/白方行棋' 印章.
红章区固定在全屏 (y197-265, x300-430), 不随黑白头像移动.
返回 'B'(黑方行棋) / 'W'(白方行棋) / None(未识别到).
RapidOCR 实例全程复用, 避免每步 1.5s 初始化.
"""
import os
import sys
import time
import threading

import cv2
import numpy as np

# RapidOCR 懒加载 + 单例
_ocr = None
_ocr_lock = threading.Lock()


def _get_ocr():
    global _ocr
    if _ocr is None:
        with _ocr_lock:
            if _ocr is None:
                from rapidocr_onnxruntime import RapidOCR
                _ocr = RapidOCR()
    return _ocr


# 红章搜索区域 (全屏坐标, 窗口在左上角时的实测值). 实测黑方/白方行棋时印章都在此区域.
SEAL_ROI = (300, 190, 440, 268)  # x0,y0,x1,y1
# 相对窗口比例 (窗口位置/大小变化时用比例定位, 更稳)
# 实测: 红章约在窗口宽 15%~55%, 高 17%~28% 区域
SEAL_RATIO = (0.15, 0.17, 0.55, 0.28)


def _red_mask(img):
    """提取顶部栏红色印章像素 (朱砂红: 两段H边界)"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 80, 80), (15, 255, 255))
    m2 = cv2.inRange(hsv, (165, 80, 80), (180, 255, 255))
    return (m1 | m2).astype(np.uint8)


def detect_turn_side(img, window_rect=None):
    """识别红章 '黑方行棋/白方行棋'. img 全屏 BGR.
    window_rect 为 (ox,oy,ox1,oy1) 窗口位置, 传了则按窗口比例定位印章 (窗口拖动/缩放也稳定).
    返回 'B'/'W'/None."""
    if window_rect:
        ox, oy, ox1, oy1 = window_rect
        w, h = max(ox1 - ox, 1), max(oy1 - oy, 1)
        rx0, ry0, rx1, ry1 = SEAL_RATIO
        x0 = ox + int(w * rx0)
        y0 = oy + int(h * ry0)
        x1 = ox + int(w * rx1)
        y1 = oy + int(h * ry1)
        # 越界保护
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, img.shape[1]), min(y1, img.shape[0])
        if x1 <= x0 or y1 <= y0:
            return None
    else:
        x0, y0, x1, y1 = SEAL_ROI
    roi = img[y0:y1, x0:x1]
    mask = _red_mask(roi)
    # 若区域内红色像素过少, 认为没有印章 (可能处于非回合/弹窗)
    if int(mask.sum()) < 200:
        return None
    # 直接用原始 ROI 放大 3x 供 OCR (预处理成黑字白底反而更难识别)
    big = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    ocr = _get_ocr()
    try:
        res, _ = ocr(big)
    except Exception:
        return None
    if not res:
        return None
    # 拼接识别文本, 找 keyword (容忍 OCR 单字误差)
    text = ''.join(r[1] for r in res)
    if '黑' in text:
        return 'B'
    if '白' in text:
        return 'W'
    return None


if __name__ == '__main__':
    # 自测
    img = cv2.imread(sys.argv[1]) if len(sys.argv) > 1 else None
    if img is None:
        print('用法: python seal_turn_detect.py <截图.png>')
        sys.exit(0)
    t0 = time.time()
    side = detect_turn_side(img)
    print(f'识别结果: {side}  ({time.time()-t0:.2f}s)')

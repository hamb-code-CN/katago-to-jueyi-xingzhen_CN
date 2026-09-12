# -*- coding: utf-8 -*-
"""视觉识别回归测试: 用**合成的棋盘图**固定住识别行为。

为什么需要: 识别链路的阈值(木色 HSV、黑白判据)历史上是手调出来的经验值,
改一行代码就可能悄悄破坏识别, 而"跑一局看看"成本太高。这里用一张程序生成的
19 路木色棋盘 (已知每颗子的位置) 做基准图, 任何识别退化都会立刻暴露。

不依赖屏幕/窗口, 只依赖 cv2 + numpy, 因此可以在 CI 里跑。
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2                                     # noqa: E402
import go_vision as gv                         # noqa: E402

WOOD_BGR = (142, 207, 240)      # HSV ≈ (20, 104, 240), 落在 _wood_mask 的暖木色区间
LINE_BGR = (90, 90, 90)
BG_BGR = (45, 45, 45)
STEP = 38.5
ORIGIN = 40.0                   # 第 0 条网格线的位置
WOOD_PAD = 20                   # 木色板比网格外扩的像素
BLACK_PTS = [(3, 3), (3, 15), (15, 3), (9, 4)]
WHITE_PTS = [(15, 15), (9, 9), (3, 9)]
IMG_W, IMG_H = 1280, 1400


def _vx(i):
    return ORIGIN + i * STEP


def make_board_image():
    """生成一张合成棋盘图 + 每颗子的 (col,row)。"""
    img = np.zeros((IMG_H, IMG_W, 3), dtype=np.uint8)
    img[:, :] = BG_BGR
    x0 = int(_vx(0) - WOOD_PAD)
    x1 = int(_vx(18) + WOOD_PAD)
    y0, y1 = x0, x1
    img[y0:y1, x0:x1] = WOOD_BGR
    # 19x19 网格线
    for i in range(19):
        x = int(round(_vx(i)))
        y = int(round(_vx(i)))
        cv2.line(img, (x, int(_vx(0))), (x, int(_vx(18))), LINE_BGR, 1)
        cv2.line(img, (int(_vx(0)), y), (int(_vx(18)), y), LINE_BGR, 1)
    # 棋子 (半径约 0.42*step)
    r = int(STEP * 0.42)
    for c, rw in BLACK_PTS:
        cv2.circle(img, (int(round(_vx(c))), int(round(_vx(rw)))), r, (25, 25, 25), -1)
    for c, rw in WHITE_PTS:
        cv2.circle(img, (int(round(_vx(c))), int(round(_vx(rw)))), r, (248, 248, 248), -1)
    return img


def fake_board():
    """按已知几何构造 detect_board 的输出结构 (等价于识别成功后的 board)。"""
    vx = [_vx(i) for i in range(19)]
    hy = [_vx(i) for i in range(19)]
    pts = np.zeros((19, 19, 2), dtype=np.float64)
    for j in range(19):
        for i in range(19):
            pts[j, i] = (vx[i], hy[j])
    return {'x0': int(vx[0]), 'y0': int(hy[0]), 'x1': int(vx[-1]), 'y1': int(hy[-1]),
            'vx': vx, 'hy': hy, 'pts': pts, 'step': STEP,
            'wood_rect': (20, 20, int(vx[18]) + 20, int(vx[18]) + 20)}


class TestAdaptiveThresholds(unittest.TestCase):
    def test_returns_sane_range(self):
        img = make_board_image()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        s_ch = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1]
        bt, wt = gv.auto_thresholds(img, fake_board(), gray=gray, s_ch=s_ch)
        self.assertTrue(60 <= bt <= 170, 'black threshold out of range: %s' % bt)
        self.assertTrue(40 <= wt <= 115, 'white threshold out of range: %s' % wt)
        # 合成木色灰度 ~200 -> 阈值应贴近历史经验值 150
        self.assertLess(abs(bt - gv.BLACK_GRAY_THR), 40)

    def test_falls_back_on_garbage(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)     # 全黑 -> 木色过暗
        b = fake_board()
        bt, wt = gv.auto_thresholds(img, b)
        self.assertEqual((bt, wt), (gv.BLACK_GRAY_THR, gv.WHITE_SAT_THR))

    def test_falls_back_on_tiny_sample(self):
        b = fake_board()
        b['pts'] = b['pts'][:1, :1]                        # 只有 1 个交点
        bt, wt = gv.auto_thresholds(np.zeros((50, 50, 3), np.uint8), b)
        self.assertEqual((bt, wt), (gv.BLACK_GRAY_THR, gv.WHITE_SAT_THR))


class TestWoodMask(unittest.TestCase):
    def test_finds_board_region(self):
        img = make_board_image()
        mask = gv._wood_mask(img)
        self.assertEqual(mask.shape, img.shape[:2])
        n, _lbl, stats, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
        areas = sorted((int(s[4]) for s in stats[1:]), reverse=True)
        self.assertTrue(areas and areas[0] > 50000,
                        '木色连通域太小: %s' % (areas[:3],))


class TestDetectBoard(unittest.TestCase):
    """detect_board 端到端: 合成图 -> 网格线定位。"""

    def test_detects_grid(self):
        img = make_board_image()
        board = gv.detect_board(img, roi_x_max=960)
        self.assertIsNotNone(board, 'detect_board 未能定位棋盘')
        self.assertEqual(len(board['vx']), 19)
        self.assertEqual(len(board['hy']), 19)
        self.assertLess(abs(board['step'] - STEP), 1.0)
        self.assertLess(abs(board['vx'][0] - _vx(0)), 6.0)
        self.assertLess(abs(board['hy'][0] - _vx(0)), 6.0)


class TestReadBoard(unittest.TestCase):
    """读子: 已知 4 黑 3 白, 必须全部读对且不误判。"""

    def test_reads_all_stones(self):
        img = make_board_image()
        stones, _dbg = gv.read_board(img, fake_board())
        self.assertEqual(stones.shape, (19, 19))
        for c, r in BLACK_PTS:
            self.assertEqual(stones[r, c], gv.BLACK, '黑子漏读 (%d,%d)' % (c, r))
        for c, r in WHITE_PTS:
            self.assertEqual(stones[r, c], gv.WHITE, '白子漏读 (%d,%d)' % (c, r))
        empties = int((stones == gv.EMPTY).sum())
        self.assertEqual(empties, 361 - len(BLACK_PTS) - len(WHITE_PTS))

    def test_explicit_thresholds_still_supported(self):
        img = make_board_image()
        stones, _ = gv.read_board(img, fake_board(),
                                  thresholds=(gv.BLACK_GRAY_THR, gv.WHITE_SAT_THR))
        for c, r in BLACK_PTS:
            self.assertEqual(stones[r, c], gv.BLACK)

    def test_dark_theme_still_readable(self):
        """深色主题 (木色偏暗) 下自适应阈值仍要能读对, 这是硬编码阈值做不到的。"""
        img = make_board_image()
        # 整体压暗到 60% (模拟暗色主题/低亮度)
        img = np.clip(img.astype(np.float32) * 0.6, 0, 255).astype(np.uint8)
        stones, _ = gv.read_board(img, fake_board())
        for c, r in BLACK_PTS:
            self.assertEqual(stones[r, c], gv.BLACK, '暗色主题下漏读黑子 (%d,%d)' % (c, r))
        for c, r in WHITE_PTS:
            self.assertEqual(stones[r, c], gv.WHITE, '暗色主题下漏读白子 (%d,%d)' % (c, r))


class TestLastMoveMarker(unittest.TestCase):
    """「最后一手」朱砂红标记不能把棋子读成空点。

    真实事故 (2026-09-12): 星阵在最后一手上画红色方块, 白子 patch 均值被拉成
    (225,153,140) 饱和度 0.35 -> 判为空点 -> 引擎认定该点可下, 反复点同一个已被
    占据的点, 115 轮一次都没落成。修法: 读子前剔除红色标记像素。
    """

    MARKER_BGR = (45, 30, 200)      # 朱砂红 (BGR)

    def _mark(self, img, c, r, size=None):
        """在 (c,r) 棋子中心画一个红方块, 模拟客户端的最后一手标记。"""
        x, y = int(round(_vx(c))), int(round(_vx(r)))
        s = size or max(3, int(STEP * 0.16))
        cv2.rectangle(img, (x - s, y - s), (x + s, y + s), self.MARKER_BGR, -1)
        return img

    def test_marker_mask_hits_red_only(self):
        img = make_board_image()
        self._mark(img, *WHITE_PTS[0])
        mask = gv.marker_red_mask(img)
        x, y = int(round(_vx(WHITE_PTS[0][0]))), int(round(_vx(WHITE_PTS[0][1])))
        self.assertTrue(mask[y, x], '红方块中心未被识别为标记')
        # 木色/黑白棋子都不该被当成标记
        ex, ey = int(round(_vx(10))), int(round(_vx(10)))
        self.assertFalse(mask[ey, ex], '木色空点被误判为红色标记')

    def test_marked_white_stone_still_white(self):
        for pt in WHITE_PTS:
            img = make_board_image()
            self._mark(img, pt[0], pt[1])
            stones, _ = gv.read_board(img, fake_board())
            self.assertEqual(stones[pt[1], pt[0]], gv.WHITE,
                             '带最后一手标记的白子被读错 (%d,%d)' % pt)

    def test_marked_black_stone_still_black(self):
        for pt in BLACK_PTS:
            img = make_board_image()
            self._mark(img, pt[0], pt[1])
            stones, _ = gv.read_board(img, fake_board())
            self.assertEqual(stones[pt[1], pt[0]], gv.BLACK,
                             '带最后一手标记的黑子被读错 (%d,%d)' % pt)

    def test_count_unchanged_with_marker(self):
        img = make_board_image()
        self._mark(img, *WHITE_PTS[0])
        stones, _ = gv.read_board(img, fake_board())
        self.assertEqual(int((stones == gv.BLACK).sum()), len(BLACK_PTS))
        self.assertEqual(int((stones == gv.WHITE).sum()), len(WHITE_PTS))

    def test_empty_point_never_marked_as_stone(self):
        """只有标记的红色像素不足以把空点读成棋子 (剔除后仍需是木色)。"""
        img = make_board_image()
        self._mark(img, 10, 10)
        stones, _ = gv.read_board(img, fake_board())
        self.assertEqual(stones[10, 10], gv.EMPTY)


class TestCaptureHelpers(unittest.TestCase):
    """后台取图的坐标换算: 窗口局部坐标 -> 屏幕绝对坐标 (点鼠标要用)。"""

    def _board(self):
        vx = [10.0 + i * 5 for i in range(19)]
        hy = [20.0 + i * 5 for i in range(19)]
        pts = np.zeros((19, 19, 2), dtype=np.float64)
        for j in range(19):
            for i in range(19):
                pts[j, i] = (vx[i], hy[j])
        return {'x0': int(vx[0]), 'y0': int(hy[0]), 'x1': int(vx[-1]), 'y1': int(hy[-1]),
                'vx': vx, 'hy': hy, 'pts': pts, 'step': 5.0, 'wood_rect': (1, 2, 3, 4)}

    def test_offset_board_shifts_everything(self):
        b = self._board()
        nb = gv.offset_board(b, 100, 50)
        self.assertAlmostEqual(nb['pts'][0, 0, 0], b['pts'][0, 0, 0] + 100)
        self.assertAlmostEqual(nb['pts'][3, 4, 1], b['pts'][3, 4, 1] + 50)
        self.assertEqual(nb['x0'], b['x0'] + 100)
        self.assertEqual(nb['y1'], b['y1'] + 50)
        self.assertEqual(nb['vx'][0], b['vx'][0] + 100)
        self.assertEqual(nb['hy'][0], b['hy'][0] + 50)
        self.assertEqual(nb['wood_rect'], (101, 52, 103, 54))
        # 原对象不被修改 (避免误用旧坐标)
        self.assertAlmostEqual(b['pts'][0, 0, 0], 10.0)

    def test_offset_zero_returns_same_object(self):
        b = self._board()
        self.assertIs(gv.offset_board(b, 0, 0), b)

    def test_offset_none_is_safe(self):
        self.assertIsNone(gv.offset_board(None, 10, 10))

    def test_grab_for_read_never_raises(self):
        """没有围棋窗口 / 截图不可用时应安静返回, 不能抛异常。"""
        img, origin, mode = gv.grab_for_read('tencent', prefer_window=True)
        self.assertIn(mode, ('window', 'screen', 'none'))
        self.assertEqual(len(origin), 2)
        if mode == 'none':
            self.assertIsNone(img)
        else:
            self.assertIsNotNone(img)


class TestSealTurnDetect(unittest.TestCase):
    """印章轮次识别: 印章位置不再写死, 缩放后仍能判断左右。"""

    def _make(self, seal_x, w=900, h=1000, scale=1.0):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:, :] = (60, 80, 100)
        cx, cy = seal_x, int(h * 0.12)
        ax, ay = int(22 * scale), int(18 * scale)
        # 朱砂红底 (BGR 90,100,180 -> HSV ≈ H3 S127 V180, 落在印章检测区间内)
        cv2.ellipse(img, (cx, cy), (ax, ay), 0, 0, 360, (90, 100, 180), -1)
        return img

    def test_left_seal_is_black(self):
        self.assertEqual(gv.detect_turn_side(self._make(200)), 'B')

    def test_right_seal_is_white(self):
        self.assertEqual(gv.detect_turn_side(self._make(700)), 'W')

    def test_works_after_resize(self):
        # 分辨率变大、印章等比放大 -> 旧实现 (y150:260 / mid_x=391) 会失效
        self.assertEqual(gv.detect_turn_side(self._make(500, w=1800, h=2000, scale=2.0)), 'B')
        self.assertEqual(gv.detect_turn_side(self._make(1300, w=1800, h=2000, scale=2.0)), 'W')

    def test_returns_none_without_seal(self):
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        img[:, :] = (60, 80, 100)
        self.assertIsNone(gv.detect_turn_side(img))


if __name__ == '__main__':
    unittest.main()

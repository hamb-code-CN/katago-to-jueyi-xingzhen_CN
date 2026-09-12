# 围棋 AI 自动对局系统 — 架构与运行逻辑文档

> 版本：2026-09-05（含星阵围棋平台适配 · 打劫处理机制 · 便携部署）
> 运行环境：Windows / Python 3.10~3.12 / KataGo OpenCL 1.7.1
> 便携包结构：本目录 `go_ai/`（代码）+ 同级 `katago/`（引擎与模型），代码自动按相对位置定位引擎，无任何本机绝对路径依赖。

---

## 一、系统定位

这是一套**纯视觉闭环的围棋自动对局系统**：它不读取任何客户端内部数据、不注入进程、不调用私有 API，而是完全通过「截图 → 图像识别 → AI 决策 → 模拟鼠标点击 → 再截图核对」的方式，在第三方围棋客户端上自动下棋。

这套设计带来两个关键特性：

| 特性 | 说明 |
|---|---|
| **客户端无关** | 只要能在屏幕上看见棋盘，就能下。已适配腾讯围棋（原生客户端）和星阵围棋（网页版）。新增平台只需加一个窗口定位 + 一个轮次识别策略 |
| **可观测** | 每一步的识别结果、决策、耗时、核对结论都落日志，并通过 Web 看板实时可视化 |

**核心难点**不在 AI（KataGo 本身很强），而在"感知"和"闭环可靠性"——如何让机器像人一样看懂屏幕上的棋盘、判断轮到谁走、确认自己确实点中了。

---

## 二、整体架构

系统分为五层，从底到顶：

```
┌─────────────────────────────────────────────────────────────────┐
│  ⑤ 展示 / 运维层                                                 │
│     analysis.html (前端看板)   go_launcher.py (菜单启动器)        │
│     analysis_watch.py (HTTP :8123 + 数据刷新)                     │
│     show_analysis.py (候选点标注图生成)                            │
└─────────────────────────────────────────────────────────────────┘
                              ↕ HTTP / 文件
┌─────────────────────────────────────────────────────────────────┐
│  ④ 控制层  go_controller.py                                      │
│     主循环编排：指令 → 截屏 → 读盘 → 判轮次 → 思考 →              │
│     校验 → 落子 → 核对 → 循环                                     │
└─────────────────────────────────────────────────────────────────┘
             ↕                              ↕
┌───────────────────────────┐  ┌───────────────────────────────────┐
│  ③ 决策层                  │  │  ② 感知层  go_vision.py            │
│  kata_service.py(:8124)   │  │  棋盘定位 / 棋子识别 / 网格校正      │
│  katago_engine.py (GTP)   │  │  seal_turn_detect.py   (腾讯 红章)  │
│  go_engine.py (本地回退)   │  │  avatar_turn_detect.py (星阵 水滴)  │
└───────────────────────────┘  └───────────────────────────────────┘
             ↕                              ↕
┌─────────────────────────────────────────────────────────────────┐
│  ① 外部世界                                                       │
│     KataGo 引擎进程  ·  腾讯围棋窗口 / Edge 浏览器(星阵)  ·  屏幕    │
└─────────────────────────────────────────────────────────────────┘
```

### 进程拓扑（运行时实际是 4 个进程）

| 进程 | 端口 | 职责 |
|---|---|---|
| `kata_service.py` | 127.0.0.1:8124 | KataGo 常驻服务，冷启动一次后长期复用 |
| `go_controller.py` | — | 对局主循环（截屏/决策/落子） |
| `analysis_watch.py` | 0.0.0.0:8123 | 看板后端，每 2s 刷新 `data.json` + `analysis_overlay.png` |
| `go_launcher.py` | — | 交互式菜单启动器（可选，也可用网页版） |

---

## 三、模块清单

| 文件 | 层 | 职责 |
|---|---|---|
| `go_vision.py` | 感知 | 窗口枚举定位、棋盘检测、网格拟合、棋子读取、坐标转换 |
| `avatar_turn_detect.py` | 感知 | **星阵围棋**轮次识别（头像蓝水滴 HSV 检测） |
| `seal_turn_detect.py` | 感知 | **腾讯围棋**轮次识别（红章 RapidOCR） |
| `katago_engine.py` | 决策 | KataGo GTP 封装 + 常驻服务 TCP 客户端 |
| `kata_service.py` | 决策 | KataGo 常驻服务进程（预加载，免冷启动） |
| `go_engine.py` | 决策 | 本地回退引擎（完整围棋规则 + 启发式 + 蒙特卡洛） |
| `go_controller.py` | 控制 | 主循环编排、思考时间控制、落子校验、闭环核对 |
| `analysis_watch.py` | 展示 | HTTP 服务、看板数据刷新、启动器 API |
| `show_analysis.py` | 展示 | 解析 KataGo 日志 → 绘制候选点标注图 |
| `analysis.html` | 展示 | 网页看板前端（棋盘 + 候选点列表 + 启动器面板） |
| `go_launcher.py` | 运维 | 菜单启动器、进程管理、配置持久化、智力档位 |

---

## 四、主循环：一次完整回合的 8 个阶段

`go_controller.main()` 每轮循环执行：

```
① 处理指令 ──── 读 _command.json（网页下发"重置/换色"），不重启进程
       ↓
② 截屏 ─────── pyautogui 全屏截图（每 interval 秒一次，默认 5s）
       ↓
③ 读棋盘 ───── 检测棋盘网格 → 识别 361 个交叉点状态（空/黑/白）
       ↓
④ 判轮次 ───── 优先 UI 视觉识别（红章/水滴），失败回退子数法
       ↓  （不是我方回合 → 回到 ② 等待）
⑤ 思考 ─────── 后台线程调 KataGo genmove，带超时保护
       ↓
⑥ 合法性校验 ── 空点 + 非自杀/非禁手？否则重截图重决策（最多 3 次）
       ↓
⑦ 落子 ─────── 坐标转屏幕像素 → pyautogui 点击
       ↓
⑧ 闭环核对 ─── 等 2.5s 动画稳定 → 重新截图 → 比对子数增量 → OK/FAIL
```

第 ⑧ 步是**可靠性设计的核心**：不假设点击一定成功，而是用"我方子数是否增加"来实证验证。

---

## 五、感知层详解

### 5.1 棋盘定位（`go_vision.detect_board`）

三级降级策略，保证各种环境下都能找到棋盘：

1. **窗口锁定**：先枚举可见窗口，找到目标客户端窗口 rect（腾讯按标题关键词 `腾讯围棋/对局/19路`；星阵按 `Edge/Chrome` + `星阵围棋/19x19/Galaxy`），把检测范围限制在窗口内，排除桌面其他区域干扰。
2. **木色掩膜 + 连通域**：在窗口内用 HSV 范围提取棋盘底色（暖木色 `H10-35,S60-160,V180-255` 与青蓝色 `H85-120,S80-200,V160-255` 取并集），形态学闭运算填洞后取最大连通域，约束条件：面积 ≥ 50000、宽高 ≥ 300、宽高比 0.75~1.33。
3. **等距网格拟合**：在候选区域内做 OTSU 二值化，用行/列投影找网格线，再用 `_fit_grid()` 拟合 19 条等距线（容差 3px，同分时优先选最靠上的网格，避免误选下方按钮栏）。步长须在 15~80px，最后校验"网格底部距连通域底不超过 3 倍步长"，防止错选 UI 元素。

**兜底**：检测失败时使用预标定的固定网格（腾讯 `CAL_BOARD`、星阵 `FALLBACK_XZ`）。

### 5.2 棋子识别（`read_board`）

采用**交点邻域统计法**，比连通域法更抗棋子反光：

- 取每个交点周围 45% 步长的方形 patch
- 灰度均值 < 150 → 判为**黑子**
- HSV 饱和度均值 < 80 → 判为**白子**（木色空点饱和度约 108，不会误判；带"最后一手"三角标记的白子仍可识别）
- **先剔除"最后一手"朱砂红标记像素**（`marker_red_mask`）：客户端会在最后一手上画红方块，
  标记盖住棋子中心会把 patch 均值拉红（实测白子从 `(213,209,191)` 变成 `(225,153,140)`，
  饱和度 0.35 远超白子阈值）→ 棋子被读成空点。剔除后剩余像素不足 40% 时退回整块统计（保守）。

**网格精校正**：`refine_pts()` 用已落子的质心反推整体偏移（需 ≥4 个有效样本），修正检测网格的系统性偏差；`refine_pts_local()` 在看板绘制场景额外做局部吸附，让标注圆圈更贴合棋子。

### 5.3 轮次识别（判断"轮到谁走"）

这是最容易出错的环节。系统为每个平台设计了不同策略，并保留子数法作为统一回退：

| 平台 | 主策略 | 原理 | 耗时 |
|---|---|---|---|
| **腾讯围棋** | 红章 OCR | 顶部栏显示"黑方行棋/白方行棋"朱砂红印章。先按窗口比例定位 `(0.15,0.17)-(0.55,0.28)`，红色像素 < 200 则跳过，再放大 3 倍交 RapidOCR 识别 | 首次 ~1.5s（加载模型），之后单例复用 |
| **星阵围棋** | 头像蓝水滴 | 轮到某方走时，该方头像下方出现发光蓝色水滴。在两个 ROI（黑 `(0.69,0.36)-(0.78,0.43)`、白 `(0.85,0.36)-(0.94,0.43)`）内统计 HSV `H95-145,S≥50,V≥150` 的像素数，超阈值 50 即判定 | **< 10ms** |
| 通用回退 | 子数法 | 黑子数 ≤ 白子数 → 轮到黑，否则轮到白 | 极快 |

**为什么需要视觉识别而不只用子数法？** 子数法在预设局面（死活题、让子棋、已有让子的对局）会失效，而 UI 上的行棋指示是权威信息源。

---

## 六、决策层详解

### 6.1 KataGo 引擎

- **模型**：`kata-b18c384nbt.bin.gz`（18 层 / 384 通道，约 40M 参数）
- **交互方式**：GTP 协议。两种运行模式：
  - **常驻服务模式（优先）**：`kata_service.py` 在 8124 端口常驻，controller 通过 TCP JSON 请求复用同一 KataGo 进程。**冷启动 60~90s 只发生一次**，之后 controller 重启无需重新加载。
  - **本地冷启动模式（回退）**：服务不在线时，controller 自己 `subprocess` 拉起 KataGo，等待 stderr 出现 "GTP ready"（最长 180s）。

- **棋盘同步**：`set_board()` 把识别到的 19×19 数组发给 KataGo。因该版本不支持 `place_free`/`set_stones`，只能用 `clear_board` + 交替 `play` 逐手重建（保证落子顺序合法）。

- **胜率查询**：用 `kata-raw-nn 0`（单次网络前馈，几十毫秒）而非 `kata-analyze`（流式输出无结束符，不适合同步读取）。注意其输出字段是 `whiteWin`，黑方胜率 = `1 - whiteWin`。

### 6.2 本地回退引擎（`go_engine.py`）

当 KataGo 不可用时（缺文件、启动失败）自动接管，保证系统不瘫痪：

- 完整围棋规则：气、提子、自杀禁手、打劫（含 ko 点判定与回提模拟）
- 启发式评分：提子 +12/子、救己方打吃 +8、攻击对方打吃 +5、气数 ×0.6、邻接已有子 ×0.4、靠近对方弱子
- 蒙特卡洛：对启发式 top-20 候选各做随机模拟（最多 2000 次/点），按 `0.5×启发分 + 1.0×胜率` 综合选点

### 6.3 思考时间控制

三层机制叠加：

1. **智力档位**（决定基础区间，见第十节）
2. **随机化**：每手在档位区间内随机取值，避免固定节奏暴露 AI 特征
3. **中盘低胜率权重**（`decide_think_time`）：当**总手数 ≥ 60** 且**我方胜率 < 30%** 时，先用 1.5s 快速评估胜率，若确认劣势则把思考时间延长到 `min(20s, max(原时间×2, 10s))`。设计意图是"劣势时多想想"。

> 实测触发样例（本局第 111 手）：
> `[权重] 中盘111手 我方胜率18.3%<30%, 延长思考 2.5s → 10.0s (上限20s)`

### 6.4 落子前合法性校验

KataGo 可能给出非法点（因 `set_board` 漏读某子导致视角不一致，或自杀/劫禁手）。校验逻辑：

```
若决策点 已占用 或 违反 go_engine.is_legal（自杀/劫禁手）
  → 重新截图 + 重读棋盘 + 重设 KataGo 局面 + 重新决策
  → 最多重试 4 轮；第 4 轮仍非法 → 本轮放弃落子（防死循环，见 6.5）
```

### 6.5 打劫（ko）处理机制

> 2026-09-05 新增。此前缺陷：对局中出现劫争时，AI 会**反复尝试在劫点落子**，
> 每次被客户端拒绝（非法回提）、棋盘不变、KataGo 看到同一局面又给同一劫点，形成死循环
> （实测连续 10+ 轮 `核对 FAIL +0/+0`）。

**根因（三层叠加）**：

1. **KataGo 不知道劫在哪**：`set_board` 用「按位置排序 + 黑白交替」play 重建棋盘，
   破坏了真实落子先后顺序 → KataGo 内部 ko 状态丢失 → 它认为劫点合法。
2. **本地校验拦不住**：`is_legal` 的 ko 判断写错（检查"落子后新产生的劫"==落子点，
   恒不成立）；且校验用静态棋盘快照构造，无历史 → `ko=None`。
3. **重试后无条件落子**：3 次重试循环结束无论是否合法都点击。

**修复方案**：

| 层 | 改动 | 文件 |
|---|---|---|
| 规则引擎 | `Board` 新增 `ko_color`（劫点约束方 = 被提方）；`is_legal` 改为检查本局面 `self.ko` | `go_engine.py` |
| 引擎同步 | `set_board(stones, history)`：有真实顺序时**按顺序逐手 play**（ko 正确），失败/缺失回退交替重建 | `katago_engine.py` |
| 常驻服务 | `set_board` 请求透传 `history` 字段 | `kata_service.py` |
| 主控制器 | 逐帧 diff 维护 `move_history`（真实落子顺序）；`_stones_to_board(history)` 重放还原 ko 供校验；所有 `set_board` 传 history；**重试 4 轮仍非法 → 放弃本轮落子** | `go_controller.py` |

**核心流程**：

```
每轮读盘 stones ──diff──> move_history [(col,row,color)...]
        │
        ├─ 一致性校验 history_matches(move_history, stones)
        │    不一致(中途启动/漏读) → 清空 → 退化为交替重建(无 ko 但有防死循环兜底)
        │
        ├─ set_board(stones, history) → KataGo 内部 ko 正确 → 劫争中主动找劫材
        │
        └─ _stones_to_board(stones, turn, history) → 本地 is_legal 能拦截劫点回提
             决策点非法 → 重截图重决策(最多4轮) → 仍非法 → 本轮放弃, 等局面变化
```

**验证**：`test_ko.py`（21 项：劫点产生/约束方/is_legal 拦截/找劫材解除/非劫提子不误判）
与 `test_ko_integration.py`（12 项：controller 完整链路）全部通过，可复用防回归。

> ⚠️ 生效条件：controller 需**从开局（空盘）启动**才能累积完整 `move_history`；
> 中途启动会自动检测到历史不完整并安全降级（不死循环，但不主动避劫）。

---

## 七、执行与闭环核对

**落子**：`board_to_pixel()` 把 (列,行) 转为屏幕像素 → `pyautogui.moveTo` + `click`。网格坐标在检测阶段已加上窗口偏移，为全屏绝对坐标。

**核对**（第 ⑧ 步）：

```python
time.sleep(2.5)          # 等落子动画稳定（刚落的子 0.6s 内识别不到）
重新截图 → 重新读盘 → 比对子数增量
ok = mine_delta > 0 or (mine_delta == 0 and opp_delta > 0)
```

第二种情况（我方子数未增但对方已应手）也判 OK——说明我方子其实落成了，只是动画被对方落子覆盖。

**可选确认按钮**：部分客户端落子后需点确认，通过 `--confirm x,y` 配置。

---

## 八、平台适配对照

| 维度 | 腾讯围棋 (`tencent`) | 星阵围棋 (`xingzhen`) |
|---|---|---|
| 载体 | 原生 Windows 客户端 | Edge 浏览器网页 (19x19.com) |
| 窗口定位 | `find_go_window()`：标题含 `腾讯围棋/对局/19路` | `find_browser_window()`：标题含浏览器标识 + `星阵围棋/19x19/Galaxy` |
| 轮次识别 | 红章 RapidOCR | 头像蓝水滴 HSV（<10ms，无需 OCR） |
| 棋盘标定 | 固定 `CAL_BOARD` + `snap_to_cal` | 实时 `detect_board` + `FALLBACK_XZ` |
| 启动参数 | `--platform tencent`（默认） | `--platform xingzhen` |

平台切换在网页看板「对手软件」下拉或启动器菜单 `[A]`，**下次启动 AI 生效**。

---

## 九、看板与服务层

### 数据流

```
KataGo 日志 (katago/opencl171/gtp_logs/*.log)
      ↓ show_analysis.extract_last_search()  解析最近一次完整搜索
      ↓
  data.json  ──────────→  前端 /data.json (2s 轮询)
      ↓                        ↓
  analysis_overlay.png ←── show_analysis.draw()  棋盘截图 + 候选点标注
```

### 看板能力（http://127.0.0.1:8123/analysis.html）

- 实时棋盘图：AI 推荐候选点（绿圈）、最佳点（红圈）、胜率标签、坐标轴
- 候选点列表：传统中文坐标（如"九之十六"）+ GTP 坐标 + 胜率条
- 网页版启动器：启动/停止 AI、KataGo 预加载、切换执黑/执白、智力档位、对手软件、重置对局
- 局域网/手机可访问（绑定 `::` IPv4+IPv6 双栈，防火墙已放行 8123）

### 关键 API

| 接口 | 功能 |
|---|---|
| `GET /api/status` | 各进程状态 + 配置 + 档位列表 |
| `POST /api/start` | 启动 AI（可传 color/tmin/tmax/interval） |
| `POST /api/stop` | 停止 AI / KataGo（看板自身保留） |
| `POST /api/preload` | 确保 KataGo 常驻服务在跑 |
| `POST /api/config` | 修改档位/时间/颜色 |
| `POST /api/reset` | **重开一局但不重启 AI 进程**（写 `_command.json`） |
| `POST /api/platform` | 切换对手软件 |

**"重置对局"的巧妙设计**：controller 每轮循环开头检查 `_command.json`，读到指令就在**不重启进程**的情况下切色并清空对局状态。用 `_CMD_SEEN` 内容指纹做幂等保护（防止删除失败时重复执行）。

---

## 十、智力档位

| 档位 | 每手思考时间 | 大致水平参考 |
|---|---|---|
| 入门新手 | 0.5 ~ 1.5s | 腾讯围棋 18k~15k |
| 初级 | 1.5 ~ 3.0s | 15k~10k |
| 中级 | 3.0 ~ 6.0s | 10k~5k |
| 高级 | 6.0 ~ 12.0s | 5k~1d |
| 职业 | 12.0 ~ 20.0s | 接近职业 |

> 注：思考时间只是最主要的影响因素，实际棋力还受随机性、中盘权重干预、KataGo 模型规模制约。手动微调时间后档位自动变为"自定义"。

---

## 十一、实测效果

数据来自 2026-09-05 实际对局日志（`controller_launcher.log`，腾讯围棋平台，执黑，初级档）：

| 指标 | 实测结果 |
|---|---|
| 落子核对成功率 | **58 / 58 = 100%**（0 次 FAIL） |
| 棋盘检测失败 | **0 次**（本局 59 个循环全部成功） |
| 单手耗时 | 思考 1.6~2.9s + 落子核对 ≈ **3.0~3.7s / 手** |
| 当前局面 | 第 115 手，黑 55 / 白 56，KataGo 判定黑方胜率 71.5% |
| 中盘权重 | 真实触发（111 手、胜率 18.3% → 思考 2.5s 延长至 10.0s） |

单次循环日志样例：

```
[56] t=1788619925 棋盘: 黑=54 白=55 (我方=黑)
[56] [权重] 中盘109手 胜率58.4%≥阈值, 正常思考
[56] >>> 我方行棋, 开始思考 (引擎=KataGo, 本手上限 2.2s, 延长=否)
[56] 决策: (3, 0) 用时 2.34s
[56] 本步已用 3.3s, 直接落子
[56] 已点击 (3,0) -> 屏幕 (704,487)
[56] 核对: 落子前 黑=54白=55 -> 落子后 黑=55白=56 (我方+1 对方+1) OK
```

**性能特征**：

- KataGo 冷启动 60~90s（首次），常驻服务复用后**零等待**
- 棋盘检测 + 棋子识别：实时（亚秒级）
- 星阵轮次识别：< 10ms；腾讯红章 OCR：单例复用后每次约数十毫秒

---

## 十二、容错机制清单

| 风险 | 应对机制 |
|---|---|
| KataGo 不可用 / 启动失败 | 自动回退本地启发式 + 蒙特卡洛引擎 |
| 常驻服务掉线 | `KataServiceClient` 自动回退本地冷启动 |
| 棋盘检测失败 | 使用预标定固定网格（`CAL_BOARD` / `FALLBACK_XZ`） |
| 轮次视觉识别失败 | 回退子数法 |
| 决策点非法（已占/自杀/劫） | 重截图重读重决策，最多 3 次 |
| 思考超时 | 后台线程 + `join(max(per_move_time+10, 32))` 超时保护，退一手 pass |
| 点击未生效 | 落子后截图核对，FAIL 时告警 |
| 指令文件删除失败 | `_CMD_SEEN` 内容指纹幂等保护 |
| 窗口被拖动/缩放 | 所有 ROI 按窗口比例定位 |
| 控制台 GBK 编码报错 | 启动即 `reconfigure(encoding='utf-8')`（曾因此导致 KataGo 连接误判） |
| 长时间运行内存/句柄 | 每轮循环结束更新 `prev_stones`，无累积状态 |

---

## 十三、运维要点与已知限制

### ⚠️ 改代码后必须重启对应进程

Python 模块加载后**不会热重载**。修改 `go_vision.py` / `show_analysis.py` 等被看板引用的模块后，必须重启 `analysis_watch.py` 才生效。

> 典型故障：2026-08-31 看板候选点标注错位，根因是看板后端在代码修改前启动，内存里仍是旧逻辑（用 `find_go_window()` 找到最小化腾讯窗口 → 检测失败 → 走错位 fallback）。重启后端即恢复。

### 已知限制

1. **棋盘重建开销**：KataGo 不支持 `place_free`，每次 `set_board` 需 `clear_board` + 逐手 `play` 重建，手数多时有开销。
2. **依赖可见窗口**：窗口最小化到托盘或完全被遮挡时无法截屏识别（腾讯平台有 `PrintWindow` 兜底，星阵浏览器无）。
3. **固定 ROI 依赖 UI 布局**：星阵/腾讯 UI 大改版会导致轮次识别 ROI 失效，需重新标定比例。
4. **`venv launcher` 两层进程现象**：非 bug，判断唯一性请以端口监听为准。
5. **外部网络访问受限**：局域网内 IPv4/IPv6 可访问看板，外网受光猫/运营商限制。

### 常用命令

```bash
# 网页看板（推荐）
#   http://127.0.0.1:8123/analysis.html

# 命令行启动器（菜单式）
python go_launcher.py

# 直接启动 AI（执黑，初级档，腾讯围棋）
python go_controller.py --color black --tmin 1.5 --tmax 3.0 --interval 5 --platform tencent

# 星阵围棋，仅分析不落子
python go_controller.py --platform xingzhen --analyze-only

# 单步测试（思考 + 落子后退出）
python go_controller.py --color white --once
```

---

## 十四、便携部署（迁移到其他电脑）

桌面提供 `围棋AI便携包.zip`，结构如下（**代码与 KataGo 平级**，代码自动按相对位置寻找引擎，无需改任何路径）：

```
围棋AI便携包/
├─ go_ai/                  # 全部 Python 代码 + analysis.html + launcher_config.json
│   └─ GO_AI_ARCHITECTURE.md   # 本文档
├─ katago/                 # KataGo 引擎 (与代码目录 ../katago 同构)
│   ├─ gtp.cfg             # logDir=gtp_logs (相对路径, 已便携化)
│   ├─ kata-b18c384nbt.bin.gz   # 模型 (~93MB)
│   └─ opencl171/
│       ├─ katago.exe      # + 运行所需 dll
│       └─ gtp_logs/       # 分析日志 (自动生成)
├─ requirements.txt        # Python 依赖
├─ 使用说明.md             # 部署步骤
└─ 启动围棋AI.bat          # 一键: 建 venv/装依赖 → 打开看板
```

### 在新电脑上部署（约 5 分钟）

```bat
1. 解压 zip 到任意目录 (例如 D:\GoAI 或 C:\GoAI)
2. 安装 Python 3.10~3.12 (勾选 Add to PATH)
3. 双击「启动围棋AI.bat」— 自动创建 venv 并安装依赖 (仅首次, 需联网)
4. 浏览器打开 http://127.0.0.1:8123/analysis.html
5. 在「对手软件」选择 腾讯围棋/星阵围棋 → 启动 AI
```

### 便携化原理（代码已内置）

- `katago_engine.py` 启动时按以下顺序定位引擎根目录（均无本机绝对路径）：
  1. 环境变量 `GOAI_KATAGO_ROOT`（可选，指定自定义引擎目录）
  2. `[go_ai]/../katago`（便携包标准结构）
- `show_analysis.py` 分析日志目录直接从 `katago_engine.KATAGO_ROOT` 推导
  （`../katago/opencl171/gtp_logs`）
- `go_launcher.py` 用 `sys.executable` 启动子进程（跟随当前解释器，不硬编码 venv 路径）
- `gtp.cfg` 的 `logDir = gtp_logs` 为相对路径（相对 katago.exe 所在目录）

> 若新电脑是 **NVIDIA 显卡**，KataGo 首次运行会重新做 OpenCL 调优缓存（自动，约 1~2 分钟）。
> 依赖清单：`opencv-python / numpy / pillow / pyautogui / rapidocr_onnxruntime`。

---

## 附录：关键参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| 屏幕分辨率 | 2560 × 1440 | `Config.SCREEN_W/H` |
| 棋盘搜索区 | x < 1280（屏幕左半） | `BOARD_ROI_X_MAX` |
| 思考硬上限 | 30s | `Config.THINK_MAX` |
| 截屏间隔 | 2~5s | `SCREENSHOT_INTERVAL` |
| 落子后核对延时 | 2.5s | controller 主循环 |
| 中盘权重门槛 | ≥60 手 且 胜率 < 30% | `WEIGHT_MIN_MOVES/WINRATE` |
| 权重延长上限 | 20s | `WEIGHT_MAX_TIME` |
| 看板端口 | 8123 | `analysis_watch.PORT` |
| KataGo 服务端口 | 8124 | `kata_service.DEFAULT_PORT` |
| 木色 HSV（暖） | (10,60,180)-(35,160,255) | `_wood_mask.m1` |
| 木色 HSV（青） | (85,80,160)-(120,200,255) | `_wood_mask.m2` |
| 黑子判定 | 灰度 < 150 | `read_board` |
| 白子判定 | 饱和度 < 80 | `read_board` |
| 蓝水滴 HSV | H95-145, S≥50, V≥150 | `avatar_turn_detect` |
| 红章 ROI 比例 | (0.15,0.17)-(0.55,0.28) | `SEAL_RATIO` |

---

## 十五、v1.0.3 变更（2026-09-12）

### 新增模块

| 模块 | 职责 |
| --- | --- |
| `config_store.py` | 配置单一真源：`settings.json` 读写 + 校验 + 首次从 `launcher_config.json` 迁移。`go_launcher` / `analysis_watch` / `go_controller` 全部经它取配置 |
| `game_state.py` | `TerminalDetector` 终局判定状态机（纯逻辑，无 GUI 依赖，可单测） |
| `sgf.py` | SGF 导出（真实手顺；无历史时退化为 `AB`/`AW` 摆盘） |
| `notify.py` | 事件通知（默认关闭；标准库 urllib，后台线程发送，失败只写日志） |
| `tests/` | 50 个回归用例（规则/劫/识别/配置/棋谱/终局/主循环冒烟），标准库 unittest |
| `tools/make_release.py` | 打包发布（只打 git 跟踪文件，可选 `--with-model`），本地与 CI 共用 |
| `.github/workflows/` | `ci.yml`（提交即跑测试+打包自检）、`release.yml`（打 tag 即发版） |

### 关键机制变更

1. **GTP I/O 重写**（`katago_engine.py`）
   - 旧：每发一条命令起临时线程 `readline()`；超时把线程存进 `_pending`，下次发送前逐个 join（单线程最长 45s）。
   - 新：进程启动时起 **1 个常驻 reader 线程**把 stdout 按行写入 `queue`；`_send` = 写命令 + 带超时取队列，读到空行即为一条完整响应。超时按**条数**记在 `_stale`，下次发送前最多花 3s 丢弃，不再阻塞整步。
   - `_send_multiline`（`kata-raw-nn`）走同一队列，去掉临时线程。

2. **KataGo 服务自愈**（`kata_service.py`）
   - 新增 `_health_loop()`：每 10s 检查 `proc.poll()`；进程死亡 -> `stop_engine()` + 重建，失败退避 30s。
   - `handle()` 新增 `status` / `score_lead` / `set_param`；`ping` 返回 `restarts` / `last_error`。
   - accept 循环不再在引擎缺失时 `break`（旧行为会让服务永久失效，看门狗无从恢复）。

3. **终局识别与收尾**（`game_state.TerminalDetector` + `go_controller.finish_game`）
   - 信号：`resign`（引擎认输）/ `two_passes`（我方连续虚着）/ `idle_N`（盘面连续 N 轮不变 **且** 有旁证：我方已 pass 或界面无行棋印章）。
   - 命中后：导出 SGF → 写 `game_result.json` → 发 `terminal` 通知 → 退出进程（`--no-auto-stop` 可关）。
   - `game_result.json` 同时是看板看门狗判断"终局主动退出"的依据。

4. **看板看门狗**（`analysis_watch._supervise()`，每 20s）
   - controller 异常消失 -> 自动 `start_ai()`（后台线程，避免阻塞看板循环）。
   - 排除三种情况：主动停止（120s 冷却）、终局退出（10 分钟内结果文件）、从未启动过；连续重启超过 `watchdog.max_restarts` 则停下。

5. **视觉阈值自适应**（`go_vision.py`）
   - `auto_thresholds()`：对 361 个交点取 patch 灰度直方图，**众数簇 = 木色**，再推
     `黑子灰度上限 = 0.78 × 木色灰度`、`白子饱和度上限 = 0.72 × 木色饱和`；木色过暗或样本不足时退回经验值 150/80。
   - `detect_turn_side()`：印章高度带改为"上部区域红像素行密度峰值向两侧扩展"，左右分界默认取画面中线，不再写死 `y150:260 / mid_x=391`。

6. **兜底引擎清理**（`go_engine.py`）
   - 删除 `play()` 中两处空操作分支（`pass`、`and False` 的永假循环）与 `final_score()` 的 `if False else`。
   - 选点加邻域剪枝 `near_candidates()`（默认距离 4，棋子≥300 或开局不过滤），`build_context()` 预算我方打吃点，避免对 361 点重复全盘扫描。
   - `random_playout()` 改为增量维护空点表（每 40 次落子重建一次，兼顾提子回填）。

### 参数速查（v1.0.3 更新）

| 项 | 值 | 位置 |
| --- | --- | --- |
| 配置文件 | `go_ai/settings.json` | `config_store.CONFIG_PATH` |
| 终局静止轮数 | 12 | `settings.json → auto_stop.stable_cycles` |
| 看门狗周期 / 重启上限 | 10s / 5 次 | `settings.json → watchdog` |
| 黑子灰度阈值 | 0.78 × 木色灰度（下限 60，上限 170） | `go_vision.auto_thresholds` |
| 白子饱和度阈值 | 0.72 × 木色饱和（下限 40，上限 115） | `go_vision.auto_thresholds` |
| 邻域剪枝半径 | 4 格 | `go_engine.near_candidates` |
| 棋谱目录 | `go_ai/games/` | `settings.json → sgf.dir` |

### 数字棋盘 + 后台取图（v1.0.3 追加）

**为什么要做**：原来看板显示的是"客户端截图 + 标注圈"（`analysis_overlay.png`），
受截图清晰度与遮挡影响，观感也差。现在前端直接用识别出的盘面 **Canvas 自绘**，
木纹底 + 渐变棋子 + 传统中文坐标 + 星位 + 最后一手红点 + 候选点圆环/胜率胶囊。

| 层 | 变更 |
| --- | --- |
| `analysis_watch.cur_board_snapshot()` | 识别当前盘面，返回 (stones, counts, mode)；进程内保存上一帧用于推算"最后一手" |
| `data.json` | 新增 `stones`(19×19 0/1/2)、`last_move`([[col,row],…])、`capture`('window'/'screen'/'none') |
| `analysis.html` | `drawDigitalBoard()` 绘制棋盘；`setView('digital'|'photo')` 切换视图（localStorage 记忆）；顶栏视图切换 |
| `/api/board` | 按需识别一次盘面并返回 stones（供外部调用/手动刷新） |

**后台取图链路**（`go_vision`）：

```
grab_for_read(platform, prefer_window, mirror)
  ├─ capture_window_for(platform, mirror)   # PrintWindow 抓窗口内容
  │     └─ _enum_windows(title_keys) → capture_window(hwnd, rect, mirror)
  └─ 回落: pyautogui 全屏截图
返回 (img, origin, mode)   # origin=(left,top) 供 offset_board() 换算屏幕绝对坐标
```

- **PrintWindow(PW_RENDERFULLCONTENT) 取的是窗口自己的渲染内容**，与屏幕是否被遮挡无关 ——
  这是"后台识别"能成立的原因；全屏截图只能拍到屏幕可见部分。
- 窗口图是**窗口局部坐标**，要模拟鼠标点击必须 `offset_board(board, rect.left, rect.top)` 加回原点。
- **镜像**：不同客户端 PrintWindow 结果是否左右反转不一致（实测 Chromium 系通常**不需要**翻转）。
  `capture.mirror` = auto/on/off，看板里一键切换；auto 时腾讯客户端按历史行为取 on、浏览器取 off。
- **边界（重要）**：自动落子仍要求窗口在前台 —— 点击是 `pyautogui` 模拟鼠标，
  窗口被遮挡时点击会命中别的程序。后台取图解决的是识别/看板不受遮挡，不是"后台也能下棋"。

---

## 十六、单实例保护（v1.0.3，2026-09-12）

### 起因：AI "突然变傻、乱下"

现场日志：胜率在 0% 与 98% 之间甩、同一局子数虚高（27 手读到 72 子）、轮次编号与时间戳非单调。
根因不是识别、也不是引擎，而是**同一台机器上跑了两套 AI 进程**，互相抢同一块棋盘：

```
两个 analysis_watch (看板)  ──>  各自 _supervise()  ──>  两个 go_controller
        ↑ 都绑在 8123                  ↑ 都以为对面死了             ↑ 都执黑点同一盘
```

而两个看板能同时起来的直接原因是 **Windows 的 `SO_REUSEADDR` 语义**：
与 Linux（只允许复用 TIME_WAIT）不同，Windows 上它允许**两个进程同时 bind 同一端口**。
`netstat` 实测两个 PID 同时 `LISTENING 0.0.0.0:8123`。

### 三层防护（任何一层单独都能挡住双开）

| 层 | 文件 | 做法 |
| --- | --- | --- |
| ① 看板端口独占 | `analysis_watch.DualStackServer` | `allow_reuse_address = (os.name != 'nt')`：Windows 上禁用 `SO_REUSEADDR`，第二个看板 `bind` 抛 `WinError 10048`，打印友好提示后退出（含 TIME_WAIT 的 3 次 1s 重试） |
| ② controller 单例锁 | `go_controller.acquire_controller_lock()` | `_controller.lock` 写入 `{pid, ts, color}`；`O_CREAT\|O_EXCL` 原子创建；启动时若锁属**存活**进程则拒绝启动（退出码 0），陈旧锁（进程已死）自动清理接管；`atexit` 释放 |
| ③ 终局误判兜底 | `game_state.TerminalDetector` | 见下 |

为什么锁要放在 **controller 内部**而不是各入口：启动路径有 bat、菜单启动器、看板按钮、看门狗自动重启四条，
只在某一个入口判断挡不住另一条。controller 自己是唯一能保证"同一时刻只有一个"的位置。

### 终局误判修复（顺带）

现象：开局前两手因**思考超时**退 `(-1,-1)`，被判成"双虚着"直接触发终局自动停止。

修复（`TerminalDetector`）：

1. **`forced_pass` 不算真虚着**：controller 在超时/引擎报错的兜底分支置 `mv_forced=True`，
   传给 `term.update(..., forced_pass=mv_forced)`；只有引擎**主动** `pass` 才累计 `my_passes`。
2. **盘面子数下限**：`two_passes` 还要求盘面总子数 ≥ `min_stones_two_passes`（默认 10），
   开局即便真双 pass 也不收工。`stones` 数不出子数时（测试桩只有 `tobytes()`）跳过该校验，
   保持纯状态机可测。

### 测试

`tests/test_singleton_lock.py`（新增，4 例）：

| 用例 | 断言 |
| --- | --- |
| `test_acquire_then_release` | 空闲可获取，释放后锁文件消失 |
| `test_second_blocked_by_live_holder` | 存活进程持锁 -> 后来者被拒且返回其 PID |
| `test_stale_lock_is_reclaimed` | 死亡 PID 的陈旧锁 -> 清理并接管 |
| `test_port_cannot_be_bound_twice` | Windows 上同一端口二次 bind 必失败 |

`tests/test_sgf_config.py` 新增 3 例：`test_forced_passes_do_not_count`、
`test_two_passes_blocked_on_empty_board`、`test_two_passes_fires_when_board_filled`。

**回归用例总数：62**（v1.0.3 为 50 → 55 → 62）。

### 运维提醒

- 同一台机器**只运行一个看板**。若看到 `[看板] 无法绑定端口 8123`，说明已有实例在跑，关掉多余窗口即可。
- `go_ai/_controller.lock`、`go_ai/_kata_service.lock` 均为运行期文件，已在 `.gitignore`。
- 若 controller 被 `taskkill /F` 强杀，锁会残留，但下次启动检测到 PID 已死会自动清理，无需手动删。

---

## 十七、辅助模式：AI 不出手，玩家点选落子（v1.0.3，2026-09-12）

需求：**关掉"AI 自动走子"**，只保留胜率分析与局面分析，落点由玩家自己选。

### 三种落子方式

`config_store.VALID_MODES` 是单一真源，看板下拉、启动器参数、controller 分支都读它：

| mode | 看板标签 | controller 参数 | 行为 |
| --- | --- | --- | --- |
| `auto` | AI 自动落子 | （无） | 原行为：AI 决策 + 自动点击落子 |
| `assist` | 辅助(玩家点选落子) | `--assist` | **AI 只算胜率/候选，绝不自己点**；玩家在看板点棋盘或候选点 → 帮他落子 |
| `analyze` | 仅分析不落子 | `--analyze-only` | 只评估、只更新看板，永不出手（纯围观） |

`--assist` 是加法：它包含 `--analyze-only` 的分析，只是额外接受玩家点选。

### 落子指令流（辅助模式）

```
看板点棋盘/点候选点
  → POST /api/pick {col,row}
  → analysis_watch.api_pick 写 go_ai/_command.json
       {"cmd":"play","col":c,"row":r,"ts":...}
  → controller 每轮 handle_command() 读到 -> 暂存 _PENDING_PLAY{pt,ts}
  → 本轮 assist 分支: 先算胜率/候选, 再取暂存点
       过期(>20s)作废 → assist_play() 校验 → 点击落子 → 等 2.2s 读帧并入历史
```

关键设计：

- **玩家点选同样过合法性校验**。`assist_play()` 自己把关：坐标范围、该点是否已有子、
  以及用 `_stones_to_board(..., history)` 还原的棋盘判 `is_legal()`（劫 / 自杀 / 禁手）。
  非法直接拒绝并在看板报原因，**不点鼠标**。玩家点错不会把客户端搞乱。
- **点选有效期 20s**（`PLAY_PICK_MAX_AGE`）。棋盘可能已经变了，过期点直接作废。
- **落子后仍走"读帧 + merge_move_history"**，保证真实的劫（ko）状态与手顺一致。
- 校验点放在**本轮拿到 board 之后**，因为落点像素坐标要等识别出棋盘才能算。

### 看板前端

| 元素 | 说明 |
| --- | --- |
| 启动器「落子方式」下拉 | 选 auto / assist / analyze，随 `/api/status` 回填 |
| 棋盘 `pickable` | assist 模式下鼠标变十字准星，悬停交点画十字准星高亮 |
| 候选行 `pickable` | assist 模式下候选点整行可点，点了就落子到该点 |
| `pickBadge` | 顶栏常驻提示「🎯 辅助模式：点棋盘 / 候选点落子」 |
| `boardHit()` | 用 `BD_GEO{pad,step,size}` 把鼠标像素反算回 (col,row)，命中半径 0.55×step |

非 assist 模式点棋盘只提示"启动 AI 时选「辅助模式」才能点选落子"，不会误发指令。

### 顺带修掉：看门狗"僵尸风暴"（严重）

现象：便携包里**同时存活 11 个 `go_controller`**，且每 ~30s 再新拉一个。

根因链（两个 bug 叠加）：

1. 旧版 `go_launcher._wmic_list()` 用 `wmic` 枚举进程，**本机实测 `wmic` 返回空**
   → `status()` 永远认为"controller 已退出" → 看门狗 `_supervise()` 每 30s 拉一个新的。
2. 单例锁生效后，新拉起的 controller 发现自己被拒，**旧代码在拒绝分支 `input('按回车退出...')`**；
   看门狗 `Popen` 继承控制台 `stdin`（`isatty()` 为真）→ **永远卡在 input()**，
   既不下棋也不退出 → 僵尸越堆越多。

修复：

| 位置 | 改动 |
| --- | --- |
| `go_controller.main()` 拒绝分支 | 去掉 `input()`，改 `time.sleep(3)` + `sys.exit(0)`：**有界退出**，最多停留 3s |
| `go_launcher.status()` | 进程枚举返空时**兜底读 `_controller.lock`**：锁内 PID 仍存活即判定在跑（文件系统级，不依赖子进程） |
| `go_launcher.kill_matching()` | 枚举返空时兜底 `taskkill` 掉**持锁的那个 PID**，否则「停止 AI」点了没反应 |
| `go_controller.CONTROLLER_LOCK` | 支持环境变量 `GOAI_CONTROLLER_LOCK` 覆盖，测试可隔离到临时路径 |

> 运维提醒：改完 `go_launcher.py` / `analysis_watch.py` **必须重启看板**。
> 正在运行的看板是把模块加载进内存的，改磁盘上的代码对它无效 —— 这次僵尸风暴
> 正是因为"跑了 40 分钟的旧看板 + 磁盘上的新代码"混搭。

### 测试

新增 `tests/test_assist_mode.py`（14 例：模式路由 / 启动参数 / `/api/pick` 写盘 /
玩家点选合法性拒绝 / 候选点击）与 `tests/test_ko.py`、`tests/test_ko_integration.py`
（原 `go_ai/test_ko*.py` 脚本迁移为标准 unittest，14 例）；
`tests/test_singleton_lock.py` 补 3 例（锁兜底判定 2 例 + 被拒进程有界退出 1 例）。

同时删除 `go_ai/` **根目录残留的 5 个 `test_*.py` 脚本**（`test_engine/test_ko/
test_ko_integration/test_setboard/test_setboard2`）—— 它们会被 `unittest discover`
当成 `tests/` 的同名模块，导致 `ImportError: 'test_engine' module incorrectly imported`。

**回归用例总数：94**（v1.0.3 为 50 → 55 → 62 → 91 → 94）。

## 十八、取图回落与「最后一手」红标误判（v1.0.3，2026-09-12）

### 两处真 bug（取图回落）

1. **`take_screenshot` 读的是类 `Config` 而非实例 `cfg`**：`main()` 里是 `cfg = Config()`，
   `apply_settings` 往**实例**写 `PLATFORM='xingzhen'`，而 `take_screenshot` 里取的是
   `getattr(Config, 'PLATFORM', ...)` → 永远拿到类默认 `'tencent'`。后果：星阵（浏览器平台）
   走的是"找腾讯围棋窗口"分支，**窗口取图这条路从未生效**，mirror 也永远是类默认值。
   修：`take_screenshot(path=None, cfg=None)` 显式收实例，所有调用点传 `cfg`；
   `tests/test_capture_fallback.py::TestCfgInstanceNotClass` 锁死。
2. **auto 模式只判"能不能拍"，不判"拍到的图能不能认出棋盘"**：弹窗盖住棋盘 → 全屏
   `detect_board` 返回 None → 空转。修：`read_board_frame(cfg, path)` —— 全屏认不出棋盘时
   自动改用窗口取图（`PrintWindow`，不受遮挡影响）重试；ROI 也要跟着换成窗口局部坐标
   （`GO_WINDOW=(0,0,w,h)`），否则棋盘会被裁到 ROI 外。

### 「最后一手」红标把棋子读成空点（死循环根因）

**现象**：AI 连续 115 轮决策同一个点 `(15,17)`、点击屏幕 `(699,1000)`，每一轮都
`核对 … (我方+0 对方+0) FAIL`，子数永远停在 `黑=14 白=13`（真实是 `黑14白14`）。

**排查**：逐点采样像素发现 —— 该点上其实是**对手的白子**，只是客户端在最后一手上画了
一个**朱砂红方块**。3×3 步长邻域内 66/289 像素是红标记，patch 均值从
`(213,209,191)`（纯白子）变成 `(225,153,140)`，饱和度 `0.35`（白子阈值约 `0.27`）→
判为空点。引擎在"空盘面"上算出该点是最佳点 → 点击已被占的点被客户端拒绝 → 子数不变
→ 下一轮读到同样的"空点" → **死循环**。

**修法（两层）**：

1. **视觉层（根因）**：`go_vision.marker_red_mask()` 识别朱砂红像素
   （`R-G>50 且 R-B>70 且 |G-B|<45`；木色 `G>B` 明显，不会被误剔），
   `_patch_stats()` 统计前剔除这些像素；剔除后剩余不足 40% 时退回整块（保守）。
   实测该点从"空点"恢复为**白子**，盘面计数 `黑14白14` 正确。
2. **控制层（双保险）**：`go_controller` 新增 `_FAILED_PTS` 黑名单 ——
   同一 `(col,row)` 连续 `PLAY_FAIL_PT_MAX(=2)` 次落子核对失败即拉黑，
   决策合法性校验里把"已被拉黑的点"视同非法，于是引擎会改下候选列表里的下一个点；
   任何一手核对成功说明链路恢复，立即清空黑名单。`reset`/`set_color` 也清空。

**回归用例新增 12 例（总数 121）**：`tests/test_vision.py::TestLastMoveMarker` 6 例
（红标掩码只命中红、带标记的白子/黑子仍读对、计数不变、只有红标的空点仍是空点）、
`tests/test_assist_mode.py::TestPlayFailBlacklist` 5 例。实测修复后连续 7 手全部
`核对 … OK`（此前 0 成功）。

### 日志噪音：历史退化提示

`move_history` 用于把真实落子顺序同步给引擎（劫状态正确）。但落子后要等 2.5s 才读帧，
此时对手往往已经应手、甚至把我方刚落的子提掉，一帧内向棋盘追加的两手（我方 + 对方）
**顺序无法确定**，重放必然对不上；中盘挂上时十几手的对局也常常对不上。这是**正常现象**，
旧版却每手打印两行「落子后历史不一致, 清空重来 / 落子历史与棋盘不一致, 清空重来」，
把真正的告警淹掉。

处理：**清空兜底逻辑不变**（不清空才危险，脏历史会喂给引擎），只把提示改成每局一次：
`_HISTORY_WARNED` + `warn_history_once(cycle, why)` 首次打印一行完整说明并注明后续自动静默，
之后完全不打印；`reset` / `set_color` 时 `reset_history_warning()` 复位，新一局可再提示一次。
回归用例 `tests/test_assist_mode.py::TestHistoryWarningOnce` 2 例（首次提示后静默、复位后可再提示）。

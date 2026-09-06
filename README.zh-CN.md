> **English version: [README.md](README.md)**

# 围棋 AI 自动对局系统（GoAI Auto-Player）

一个**纯视觉闭环**的围棋自动下棋机器人：不读取任何棋软内部数据、不注入进程、不调私有接口，
只靠"看屏幕"完成全部操作 —— **截图 → 棋盘识别 → KataGo 决策 → 模拟点击 → 再次截图核对**，
因此对任何能显示棋盘的围棋客户端都通用。

当前已适配两个平台：

| 平台 | 类型 | 轮次识别方式 |
|---|---|---|
| 腾讯围棋 | Windows 原生客户端 | 顶部红章 OCR（"黑方行棋/白方行棋"） |
| 星阵围棋（19x19.com / Galaxy） | Edge/Chrome 网页 | 头像蓝水滴 HSV 检测（<10ms，免 OCR） |

---

## 功能一览

-  **KataGo 决策**：本地常驻服务预加载，每手免冷启动；KataGo 不可用时自动回退内置轻量引擎
-  **棋力强**：默认权重即可轻松战胜星阵三星、绝艺九段等在线顶级 AI（受平台限时，由你设定每手思考时间决定深度）
-  **拟人节奏**：每手思考时间随机化（防 AI 特征）；中盘（≥60 手）胜率 <30% 时自动延长思考（最多 20s）
-  **打劫（ko）处理**：逐帧跟踪真实落子历史并同步给 KataGo，劫争中主动找劫材；本地二次合法性校验拦截非法回提
-  **非法点自动顺延**：候选点已占/劫/禁手时，自动改下 KataGo 本次搜索的**次优着法**，绝不反复重试同一非法点（无死循环）
-  **中途加入也能准确分析**：无完整落子历史时，用 KataGo 原生 `set_position` 快照摆盘，盘面与真实 100% 一致（仅缺接入瞬间的劫，会安全顺延），无需从开局监视
-  **实时看板**（http://127.0.0.1:8123/analysis.html）：
  - 棋盘标注图：AI 推荐候选点、最佳点、坐标轴
  - 候选点列表（传统中文坐标）—— 显示**落子后 AI 胜率**（按 AI 执子颜色自动换算）
  - **AI 胜率走势曲线图**（随对局推进，自动重置）
  - 网页版启动器：启动/停止/重开一局(进程不重启)/智力档位/对手软件切换
  - 局域网/手机可访问（IPv4 + IPv6 双栈）
-  **双平台自适应**：窗口拖动、缩放、最小化（腾讯走 PrintWindow）均能定位

---

## 目录结构

```
├─ go_ai/                Python 源码
│   ├─ go_controller.py      主控制器（截屏/决策/落子/核对 主循环）
│   ├─ go_vision.py          棋盘定位与棋子识别
│   ├─ go_engine.py          本地回退引擎（规则+启发式+蒙特卡洛）
│   ├─ katago_engine.py      KataGo GTP 封装 + 常驻服务客户端
│   ├─ kata_service.py       KataGo 常驻服务（端口 8124，免冷启动）
│   ├─ analysis_watch.py     看板后端（端口 8123）
│   ├─ analysis.html         看板前端
│   ├─ avatar_turn_detect.py 星阵：头像水滴回合检测
│   ├─ seal_turn_detect.py   腾讯：红章 OCR 回合检测
│   └─ GO_AI_ARCHITECTURE.md 中文架构文档
├─ katago/                KataGo 引擎目录（引擎二进制已随附；权重模型需下载，见下）
│   └─ opencl171/katago.exe
├─ ACKNOWLEDGMENTS.md     第三方 / AI 出处声明
├─ LICENSE                MIT 开源协议
├─ requirements.txt       Python 依赖
└─ 启动围棋AI.bat         Windows 一键启动
```

> ⚠️ **本开源仓库不含模型权重（~93MB）**，`katago/kata-*.bin.gz` 已被 `.gitignore` 排除。
> 请按下方说明自行下载 —— 这是 AI 棋力的来源，必须要有。

---

## 随包 AI 说明（仅用于测试）

完整离线包（zip）自带：

| 组件 | 内容 | 出处 |
|---|---|---|
| 引擎 | `katago/opencl171/katago.exe`（**KataGo v1.17.1** OpenCL 版） | [lightvector/KataGo](https://github.com/lightvector/KataGo)，作者 David J Wu，MIT License |
| 权重 | `katago/kata-b18c384nbt.bin.gz`（即官方 **kata1-b18c384nbt**，18 blocks / 384 channels） | 同上（KataGo 默认网络之一） |

> ⚠️ **该模型为较早期的中等强度网络，随包仅用于测试与体验，不代表 KataGo 的真实棋力上限。**
> 默认权重即可战胜多数普通玩家，但离"最强"还差得远。

想要更强棋力？到 [KataGo Releases](https://github.com/lightvector/KataGo/releases) 自行下载更新的网络
（如 `b28c512`、`b60`、最新 `kata1-*` 系列），放入 `katago\` 即可 —— 详见下文「模型下载与加载」。
（源码版 GitHub 仓库同样自带引擎、不含权重。）

---

## 环境要求

- Windows 10/11（x64）
- Python 3.10 ~ 3.12（安装时勾选 **Add Python to PATH**）
- 可用的 OpenCL 设备（NVIDIA/AMD/Intel 均可；NVIDIA 独显效果最好）
- **屏幕：最低 1920×1080（1080p）** —— 启动时自动实测分辨率，棋盘搜索区域随屏宽自适应（1080p → x<960，2K → x<1280，更宽屏幕按比例扩展）。已在 2K（2560×1440）实测通过。

---

## 快速开始

```
1. 下载本项目并解压
2. 下载 KataGo 模型权重（见下节），放入 katago\ 目录
3. 双击「启动围棋AI.bat」→ 首次自动建 venv 并安装依赖
4. 浏览器打开 http://127.0.0.1:8123/analysis.html
5. 打开你的围棋客户端（腾讯围棋或星阵网页）并开始一局
6. 看板选「对手软件」→ 选「智力档位」→ 点「执黑启动 / 执白启动」
```

启动后 AI 自动对局。**中途加入任何对局也能立即准确分析**（快照摆盘，见功能一览）；若想打劫处理最完整，从一局新棋开局即可。

---

## 模型下载与加载（重要）

### 下载

KataGo 网络权重（`.bin.gz` 格式）来自 KataGo 官方项目（出处与许可见 ACKNOWLEDGMENTS.md）：

- **GitHub Releases（推荐）**：https://github.com/lightvector/KataGo/releases
  在 Assets 中找 `.bin.gz` 权重文件，例如 `kata1-b18c384nbt-s9996604416-d4316597426.bin.gz`
  （文件名含义：`b18c384` = 18 层 / 384 通道；`b28c512` 等为更强更大的型号）
- 本项目随附（非开源仓库场景）的默认权重即为 **b18c384nbt**，下载同规格即可

> 想要更强？选 `b28c512` / `b30` 等更大权重即可，但需要更强的显卡（前馈更慢），
> 并建议同步使用 KataGo 最新版引擎（见"换更强模型"）。

### 放置

把下载好的 `.bin.gz` 放进 **`katago\`** 根目录即可，**文件名随意**。代码按以下优先级自动加载：

1. **环境变量 `GOAI_KATAGO_MODEL`** —— 最高优先级（绝对路径，或相对 katago 根的文件名）
2. **目录自动发现** —— 若 `katago\` 下恰好只有一个 `*.bin.gz`，自动采用（推荐：删掉旧的、只留一个新的）
3. **默认文件名** —— `katago\kata-b18c384nbt.bin.gz`

放置后需**重启常驻服务**使其生效：看板启动器面板 → 「KataGo 引擎预加载」→「启动 / 重启预加载」（或重启整个 AI）。

### 换更强模型

1. 下载新权重 → 放入 `katago\`（删旧，或设 `GOAI_KATAGO_MODEL` 指定）
2. 若新权重来自新版本 KataGo，同步替换 `katago\opencl171\katago.exe`（旧引擎可能读不了新格式模型）
3. 重启预加载服务
4. 大模型单次推理更慢 → 在看板把「智力档位」调高一档（更多思考时间），保持搜索深度

---

## 使用说明（看板）

| 功能 | 操作 |
|---|---|
| 选择对手软件 | 看板右上 ⚙ 启动器 →「对手软件」下拉（下次启动 AI 生效） |
| 智力档位 | 「智力档位」下拉：入门新手 ~ 职业，控制每手思考秒数 |
| 执黑/执白启动 | 「⚫ 执黑启动 / ⚪ 执白启动」 |
| 重开一局 | 「重置为执黑/执白」—— AI 进程不重启，立即清零对局数据 |
| 停止 | 「停止 AI / 停止 KataGo」 |
| 查看胜率曲线 | 右侧"AI 胜率走势"卡片（黑=金线，白=浅线） |

### 命令行启动（可选，不用看板）

```bat
:: 启动看板后端
venv\Scripts\python.exe go_ai\analysis_watch.py

:: 执黑 / 腾讯围棋 / 每手思考 1.5~3s
venv\Scripts\python.exe go_ai\go_controller.py --color black --tmin 1.5 --tmax 3.0 --interval 5 --platform tencent

:: 星阵围棋（网页版）执白
venv\Scripts\python.exe go_ai\go_controller.py --color white --platform xingzhen

:: 仅分析不落子（评估局面并更新看板）
venv\Scripts\python.exe go_ai\go_controller.py --platform xingzhen --analyze-only
```

---

## 常见问题

**Q：启动后提示找不到模型 / KataGo 不可用？**
A：模型未放入 `katago\`。下载 `.bin.gz` 放进去 → 重启预加载服务。模型缺失时系统会自动回退到本地轻量引擎（棋力弱，仅兜底）。

**Q：AI 卡在"对方行棋 / 非我方回合"不动？**
A：确认围棋客户端窗口在前台可见、已开始对局、看板选对了「对手软件」平台。

**Q：AI 中途加入（非空盘）一局能正常下吗？**
A：能。无完整落子历史时引擎自动改用 `set_position` 快照摆盘，盘面与真实完全一致；仅当加入瞬间恰好正打劫时，那一手缺劫点（会安全顺延到次优点，不卡死），之后完全正常。**从新局开局则劫处理最完整**。

**Q：为什么偶尔看到"历史不一致, 清空重来"日志？**
A：多帧之间棋盘识别抖动导致的正常降级提示（历史被清空后改用快照摆盘继续），不影响对局推进。

**Q：换模型后棋力没提升？**
A：先确认新模型已生效（重启服务后日志显示加载的模型路径）；并调高思考档位给足搜索时间。

---

## 原理简介

```
每个循环:
  ① 处理网页指令(_command.json)   重置/换色, 不重启进程
  ② 截图 → 检测棋盘(木色HSV+19线网格拟合) → 读 361 交点(空/黑/白)
  ③ 判轮次: 腾讯=红章OCR · 星阵=头像水滴 · 兜底=子数法
  ④ 轮到我方 → KataGo genmove(随机思考时长, 劣势自动延长)
  ⑤ 合法性校验(含劫): 最佳点非法 → 自动顺延本次搜索次优候选落子; 候选耗尽才放弃本轮
  ⑥ 点击落子 → 等动画 → 再次截图核对"我方子数+1"才收手
```

完整架构与数据流：见 `go_ai/GO_AI_ARCHITECTURE.md`

---

## 更新日志

### v1.0.0（2026-09-06）

首个公开发布版本。

- **核心**：视觉驱动围棋自动落子 —— 棋盘识别（网格精调 + 读子）、KataGo（GTP）决策引擎、鼠标点击与核对闭环；支持腾讯围棋、星阵围棋网页端
- **看板**：网页分析板（`http://127.0.0.1:8123/analysis.html`），实时标注棋盘、候选点、胜率；一键执黑/执白启动停止；**启动闸门** —— KataGo 预加载 / OpenCL 调优进度（`Tuning x/55`）可视化，引擎就绪后才进入看板
- **启动器**：`启动围棋AI.bat` 首次运行自动建 venv 并安装依赖
- **屏幕**：启动时自动实测分辨率，棋盘搜索区域随屏宽自适应（最低 1080p）
- **修复**：
  - 启动脚本改用 `goto` 标签结构（原嵌套括号块导致 cmd 解析失败、双击闪退）
  - `.gitattributes` 强制 `.bat/.cmd/.ps1` 为 CRLF 行尾
  - `cv2.imwrite` 改为 Unicode 安全写图（Windows 中文安装路径下静默失败）
  - GTP 流失步根治：超时命令的迟到响应在下一条命令前排空（引擎不再"假死"连环虚着）
  - `kata_service` 启动前探测 8124 端口（杜绝双引擎抢 GPU）

---

## AI 出处与开源协议

- **KataGo**（引擎与权重，真正的棋力所在）— [github.com/lightvector/KataGo](https://github.com/lightvector/KataGo)，作者 David J Wu，**MIT License**。详细出处与第三方依赖许可见 [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)
- **RapidOCR**（腾讯端红章识别）— Apache-2.0
- **本项目代码**（`go_ai/` 下的 Python）— [MIT License](LICENSE)

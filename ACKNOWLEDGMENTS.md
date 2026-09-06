# Acknowledgments / 第三方与 AI 出处声明

This project is an automation layer on top of third-party open-source AI engines and
libraries. We are grateful to their authors. This file lists every significant third-party
component bundled or required, with its origin and license.

本项目是基于第三方开源 AI 引擎与库的自动化外壳，以下列出来源与许可。

## Bundled components / 随包分发组件

| Component | What it is | Origin | License |
|---|---|---|---|
| **KataGo** (`katago/`) | Go engine & analysis — the actual AI that chooses moves | [github.com/lightvector/KataGo](https://github.com/lightvector/KataGo) by **David J Wu (lightvector)** | [MIT](https://github.com/lightvector/KataGo/blob/master/LICENSE) |
| **kata-b18c384nbt.bin.gz** (model) | KataGo network weights (18 blocks / 384 channels) | Released by the KataGo project in its official model releases | MIT (same as KataGo) |

> The KataGo engine binary and network weights are **not** written by this project’s
> authors. They keep their own copyright/license (MIT, © KataGo project). When you
> redistribute the whole folder, please keep this attribution.
>
> 说明：`katago/` 目录下的引擎与权重为 KataGo 项目产物，版权归 KataGo 作者所有，按 MIT 许可使用与再分发，请保留本声明。

## Runtime dependencies / 运行期依赖 (installed via requirements.txt)

| Library | Used for | License |
|---|---|---|
| [KataGo](https://github.com/lightvector/KataGo) | move selection / winrate analysis | MIT |
| [opencv-python](https://github.com/opencv/opencv-python) | board image processing | Apache-2.0 |
| [numpy](https://numpy.org/) | array computation | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | drawing text/overlays (Chinese fonts) | HPND (MIT-style) |
| [pyautogui](https://github.com/asweigart/pyautogui) | screen capture & mouse control | BSD-3-Clause |
| [RapidOCR (rapidocr_onnxruntime)](https://github.com/RapidAI/RapidOCR) | OCR of the “black/white to move” seal on Tencent client | Apache-2.0 |

## Rules engine fallback / 本地规则引擎 (own code, go_engine.py)

`go_engine.py` is this project’s own lightweight rules + heuristic + Monte-Carlo engine,
used only when KataGo is unavailable. It is original code released under this project’s
MIT license.

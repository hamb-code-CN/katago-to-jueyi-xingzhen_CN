# GoAI Auto-Player — Vision-Based AI Opponent for Online Go Clients

> **中文版说明见 [README.zh-CN.md](README.zh-CN.md)**

A **fully vision-driven Go (Weiqi/Baduk) auto-playing robot**. It reads **no internal game data**, injects **no processes**, and calls **no private APIs** — everything is done by *watching the screen*:

```
screenshot → board recognition → KataGo decision → simulated click → screenshot verification
```

Because of that, it works with **any Go client that can display a board**. Currently adapted to two platforms:

| Platform | Type | Turn detection |
|---|---|---|
| Tencent Weiqi (腾讯围棋) | Windows native client | Red-seal OCR ("Black/White to move") |
| Xingzhen Weiqi (星阵围棋, 19x19.com / Galaxy) | Edge/Chrome web page | Avatar water-drop HSV detection (<10ms, no OCR) |

---

## Features

-  **KataGo decision engine**: persistent local service with preloaded model — no cold start per move; falls back to a built-in lightweight engine when KataGo is unavailable
-  **Strong play**: the default network already beats top online AIs (Xingzhen 3-star, Jueyi 9-dan, etc.) — depth is bounded by the per-move thinking time *you* set
-  **Human-like rhythm**: randomized thinking time per move (anti-bot-detection); when losing badly mid-game (≥60 moves, win-rate <30%) it automatically extends thinking (up to 20s)
-  **Ko handling**: tracks the true move history frame-by-frame and syncs it to KataGo; actively looks for ko threats; a local legality check blocks illegal recaptures
-  **Illegal-move auto-fallback**: if the best candidate is occupied / ko / forbidden, it automatically plays KataGo's **next-best candidate from the same search** — never retries the same illegal point (no infinite loops)
-  **Join mid-game with full accuracy**: without complete move history it uses KataGo's native `set_position` snapshot — board matches reality 100% (only the ko point at the join instant is missing, which is safely skipped), no need to watch from move 1
-  **Live dashboard** (http://127.0.0.1:8123/analysis.html):
  - Board overlay: recommended candidate points, best move, coordinate axes
  - Candidate list (traditional Chinese coordinates) — shows **post-move AI win-rate** (auto-converted to the AI's color)
  - **AI win-rate trend chart** (auto-resets on a new game)
  - Web launcher: start/stop, restart a game (no process restart), strength level, opponent platform switch
  - Reachable over LAN / mobile (IPv4 + IPv6)
-  **Dual-platform adaptation**: handles window move, resize and minimize (PrintWindow path on Tencent) for reliable capture

---

## Directory layout

```
├─ go_ai/                Python source
│   ├─ go_controller.py      Main controller (capture / decide / play / verify loop)
│   ├─ go_vision.py          Board locating & stone recognition
│   ├─ go_engine.py          Local fallback engine (rules + heuristics + Monte Carlo)
│   ├─ katago_engine.py      KataGo GTP wrapper + resident-service client
│   ├─ kata_service.py       KataGo resident service (port 8124, no cold start)
│   ├─ analysis_watch.py     Dashboard backend (port 8123)
│   ├─ analysis.html         Dashboard frontend
│   ├─ avatar_turn_detect.py Xingzhen: avatar water-drop turn detection
│   ├─ seal_turn_detect.py   Tencent: red-seal OCR turn detection
│   └─ GO_AI_ARCHITECTURE.md Chinese architecture doc
├─ katago/                KataGo engine dir (engine binary included; model must be downloaded, see below)
│   └─ opencl171/katago.exe
├─ ACKNOWLEDGMENTS.md     Third-party / AI attribution
├─ LICENSE                MIT License
├─ requirements.txt       Python dependencies
└─ 启动围棋AI.bat         Windows one-click launcher
```

> ⚠️ **This open-source repo does NOT bundle the model weights (~93MB)** — `katago/kata-*.bin.gz` is excluded by `.gitignore`.
> Download them as described below — they are the actual source of playing strength and are required.

---

## Bundled AI (for testing only)

The full offline package (zip) ships with:

| Component | What | Source |
|---|---|---|
| Engine | `katago/opencl171/katago.exe` (**KataGo v1.17.1**, OpenCL build) | [lightvector/KataGo](https://github.com/lightvector/KataGo) by David J Wu, MIT License |
| Weights | `katago/kata-b18c384nbt.bin.gz` (official **kata1-b18c384nbt**, 18 blocks / 384 channels) | same (one of KataGo's default nets) |

> ⚠️ **This bundled model is an older, mid-tier network and is included FOR TESTING AND EXPERIENCE ONLY —
> it does NOT represent KataGo's full strength.** It already beats most casual players, but it is far from the best.

For serious strength, download a newer net yourself (`b28c512`, `b60`, latest `kata1-*` releases) from
[KataGo Releases](https://github.com/lightvector/KataGo/releases) and drop it into `katago\` —
see "Model download & loading" below. (The source-only GitHub repo likewise ships the engine but no weights.)

---

## Requirements

- Windows 10/11 (x64)
- Python 3.10–3.12 (check **Add Python to PATH** during install)
- A working OpenCL device (NVIDIA / AMD / Intel; dedicated NVIDIA GPU works best)
- **Screen: 1920×1080 (1080p) minimum** — resolution is auto-detected at startup; the board search region adapts to the screen width (1080p → x<960, 2K → x<1280, wider screens scale proportionally). Verified on 2K (2560×1440).

---

## Quick start

```
1. Clone or download this project and extract
2. Download the KataGo model weights (see below) into the katago\ directory
3. Double-click 启动围棋AI.bat → first run creates a venv and installs dependencies
4. Open http://127.0.0.1:8123/analysis.html in a browser
5. Start a game in your Go client (Tencent Weiqi or Xingzhen web)
6. On the dashboard pick the platform → pick a strength level → click Start Black / Start White
```

The AI then plays automatically. **You can also join an already-running game at any point** (snapshot board placement, see Features). For the most complete ko handling, start from an empty board.

---

## Model download & loading (important)

### Download

KataGo network weights (`.bin.gz`) come from the official KataGo project (attribution & license in ACKNOWLEDGMENTS.md):

- **GitHub Releases (recommended)**: https://github.com/lightvector/KataGo/releases — look for `.bin.gz` weights in Assets, e.g. `kata1-b18c384nbt-s9996604416-d4316597426.bin.gz`
  (name breakdown: `b18c384` = 18 blocks / 384 channels; `b28c512` etc. are stronger but heavier)
- The default model shipped in the full offline package (not this repo) is **b18c384nbt** — download the same variant if you want parity

> Want more strength? Pick a bigger net (`b28c512` / `b30`...). It needs a beefier GPU (slower feedforward), and we suggest pairing it with the newest KataGo engine (see below).

### Placement

Drop the downloaded `.bin.gz` into the **`katago\`** root — **the filename doesn't matter**. Auto-loading priority:

1. **Env var `GOAI_KATAGO_MODEL`** — highest priority (absolute path, or a filename relative to the katago root)
2. **Directory auto-discovery** — if exactly one `*.bin.gz` exists in `katago\`, it is used automatically (recommended: remove old, keep just one)
3. **Default filename** — `katago\kata-b18c384nbt.bin.gz`

Restart the resident service afterwards: dashboard launcher panel → "KataGo engine preload" → "Start / Restart preload" (or restart the whole AI).

### Switching to a stronger model

1. Download the new weights → place into `katago\` (delete the old one, or point `GOAI_KATAGO_MODEL` at it)
2. If the weights come from a newer KataGo release, replace `katago\opencl171\katago.exe` too (old engines may not read new model formats)
3. Restart the preload service
4. Bigger nets are slower per playout → raise the strength level on the dashboard (more thinking time) to keep search depth

---

## Dashboard usage

| Action | How |
|---|---|
| Choose opponent platform | Dashboard ⚙ launcher (top-right) → "Opponent platform" dropdown (applies on next AI start) |
| Strength level | "Strength" dropdown: Beginner ~ Professional — controls seconds of thinking per move |
| Start as black/white | "Start Black / Start White" buttons |
| Restart a game | "Reset as Black/White" — clears game data instantly without restarting the AI process |
| Stop | "Stop AI / Stop KataGo" |
| Win-rate trend | "AI Win-rate" card on the right (black = gold line, white = light line) |

### CLI launch (optional, without the dashboard)

```bat
:: Start the dashboard backend
venv\Scripts\python.exe go_ai\analysis_watch.py

:: Play black on Tencent Weiqi, 1.5~3s per move
venv\Scripts\python.exe go_ai\go_controller.py --color black --tmin 1.5 --tmax 3.0 --interval 5 --platform tencent

:: Play white on Xingzhen (web)
venv\Scripts\python.exe go_ai\go_controller.py --color white --platform xingzhen

:: Analyze only (evaluate the position and update the dashboard, no moves)
venv\Scripts\python.exe go_ai\go_controller.py --platform xingzhen --analyze-only
```

---

## FAQ

**Q: "Model not found / KataGo unavailable" at startup?**
A: No model in `katago\`. Download a `.bin.gz`, put it there, restart the preload service. If the model is missing the system falls back to the local lightweight engine (weak, safety net only).

**Q: AI stuck at "opponent's turn / not my move"?**
A: Make sure the Go client window is visible in the foreground, the game has started, and the correct platform is selected on the dashboard.

**Q: Can the AI join a mid-game (non-empty board)?**
A: Yes. Without complete move history the engine automatically switches to `set_position` snapshot placement — the board matches reality exactly. The only edge case is joining exactly during a ko fight: that one ko point is unavailable (it safely falls back to the next-best point, no stall); afterwards everything is fully normal. **Starting from an empty board gives the most complete ko handling.**

**Q: Why do I sometimes see "history inconsistent, clearing and restarting" in the log?**
A: A benign degradation notice caused by board-recognition jitter across frames (history is cleared and play continues via snapshot placement). It does not affect the game flow.

**Q: Switched models but strength didn't improve?**
A: Confirm the new model is active (the log shows the loaded model path after service restart), and give it more thinking time via the strength level.

---

## How it works

```
each loop:
  ① handle web commands (_command.json)     reset / color switch, no process restart
  ② screenshot → detect board (wood-color HSV + 19-line grid fit) → read 361 intersections (empty/black/white)
  ③ detect turn: Tencent = red-seal OCR · Xingzhen = avatar water-drop · fallback = stone-count heuristic
  ④ our turn → KataGo genmove (random thinking time; extends automatically when losing)
  ⑤ legality check (incl. ko): if the best point is illegal → auto-fall back to the next-best candidate
     from the same search; only gives up the round when all candidates are exhausted
  ⑥ click to play → wait for animation → screenshot again to verify "our stone count +1" before moving on
```

Full architecture & data flow: see `go_ai/GO_AI_ARCHITECTURE.md`

---

## Changelog

### v1.0.0 (2026-09-06)

First public release.

- **Core**: screen-vision Go auto-player — board recognition (grid refinement + stone reading), KataGo (GTP) decision engine, mouse click & confirm loop; supports Tencent Weiqi (Fox) and Xingzhen web clients
- **Dashboard**: web analysis board (`http://127.0.0.1:8123/analysis.html`) with board overlay, top candidates, win-rate; one-click start/stop per color; **startup gate** — KataGo preload / OpenCL tuning progress (`Tuning x/55`) is visualized, dashboard unlocks when the engine is ready
- **Launcher**: `启动围棋AI.bat` auto-creates venv and installs dependencies on first run
- **Screen**: resolution auto-detection at startup; board search region scales with screen width (min 1080p)
- **Fixes**:
  - launcher script rewritten with `goto` labels (nested parenthesized blocks caused cmd parse failure & instant exit)
  - `.gitattributes` forces CRLF for `.bat/.cmd/.ps1`
  - `cv2.imwrite` replaced with Unicode-safe writing (non-ASCII install paths on Windows silently failed)
  - GTP stream de-sync fix: late responses from timed-out commands are drained before the next command (engine no longer "freeze" into consecutive passes)
  - `kata_service` checks port 8124 before starting (prevents duplicate engines fighting over the GPU)

---

## Attribution & license

- **KataGo** (engine & weights — the real playing strength) — [github.com/lightvector/KataGo](https://github.com/lightvector/KataGo) by David J Wu, **MIT License**. Detailed attribution and third-party dependency licenses: [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)
- **RapidOCR** (red-seal OCR on the Tencent client) — Apache-2.0
- **This project's code** (Python under `go_ai/`) — [MIT License](LICENSE)

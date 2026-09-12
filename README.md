# GoAI Auto-Player — Vision-Based AI Opponent for Online Go Clients

> **中文版说明见 [README.zh-CN.md](README.zh-CN.md)**

> ⚠️ **For Go AI technical research and local testing ONLY. Any automated play, playing on someone's behalf, unattended botting, rank boosting or score farming on online Go platforms is strictly prohibited** — see [Disclaimer & Acceptable Use](#-disclaimer--acceptable-use) below.

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

## ⚠️ Disclaimer & Acceptable Use

This project (`katago-to-jueyi-xingzhen_CN`) is provided **for Go AI technical research, study and local testing only**.

### Prohibited uses

You must NOT use this project for:

- automated play, playing on someone else's behalf, unattended/bot play, rank boosting, score farming or cheating on any online Go platform or matchmaking app;
- any competitive setting — official or unofficial tournaments, dan/kyu ranking matches, ranked ladders, championships;
- evading platform anti-cheat detection, disguising automated play as human, or fabricating game records;
- any other purpose that violates laws, platform terms of service, tournament rules or public decency.

### Risk disclosure

This tool can capture the screen, recognize the board, call KataGo for decisions, simulate clicks and use human-like thinking time — it **can objectively be abused for cheating in online games**. Users should be aware that:

- online platforms generally prohibit AI assistance or automated play; violations may lead to **account bans, voided results, permanent restrictions**;
- using AI in official competitions may breach professional discipline and lead to **suspension, revoked rank, voided results**;
- in serious cases, it may entail **legal liability**.

### Liability

Users bear **sole responsibility** for all consequences of using, modifying, distributing or abusing this project, including but not limited to account bans, tournament sanctions, professional penalties, civil damages and criminal liability. The developers, contributors and distributors **accept no liability for any misuse or any loss arising from it**.

### Technology neutrality

This project is open-sourced for technical research only and **does not constitute authorization, encouragement or support for any cheating, rule-breaking or unlawful use**. Technology neutrality is not a waiver of responsibility: users may not rely on "learning purposes only" to circumvent platform rules or legal obligations.

**If you do not agree to these restrictions, stop using, copying, modifying or distributing this project immediately.**

This notice does not replace the liability terms in [LICENSE](LICENSE). Where this notice conflicts with applicable laws, platform rules or tournament rules, those shall prevail.

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
  - Web launcher: start/stop, restart a game (no process restart), strength level, opponent platform switch, SGF export, notification settings
  - Reachable over LAN / mobile (IPv4 + IPv6)
-  **Dual-platform adaptation**: handles window move, resize and minimize (PrintWindow path on Tencent) for reliable capture
-  **End-of-game detection & auto wrap-up** (v1.0.3): engine resignation / two consecutive passes / board unchanged for N cycles *and* no turn indicator — any of these ends the game and triggers **automatic SGF export + notification + stopping the AI**
-  **SGF export** (v1.0.3): exported in true move order (setup-only when history is missing); one click from the dashboard, or automatic on game end into `go_ai/games/`
-  **Event notifications** (v1.0.3, off by default): POST a JSON payload to your own URL on game end / engine down / thinking error
-  **Digital board dashboard** (v1.0.3): the frontend **draws the board itself** on a canvas
  (wood grain, gradient stones, Chinese coordinates, candidate points with win-rate pills, red
  last-move marker) instead of relying on screenshots; one-click switch back to 「实拍」 for the raw capture
-  **Background recognition** (v1.0.3): board reading uses **PrintWindow window capture** by default,
  so it still works when the client is covered or minimized — no need to keep the board in front
  (`capture.mode`: auto/window/screen; mirror can be flipped from the dashboard)
-  **Self-healing** (v1.0.3): ① the KataGo service watchdog rebuilds the engine if the process dies; ② the dashboard watchdog restarts the controller if it dies unexpectedly (intentional stops and end-of-game exits do not trigger it)
-  **Adaptive recognition thresholds** (v1.0.3): black/white classification is derived from sampled wood color — no code edits when you change theme, monitor or display scaling
-  **Single source of truth for config** (v1.0.3): everything lives in `go_ai/settings.json`; CLI > config file > built-in defaults
-  **Assist mode: AI plays nothing, you pick the move** (v1.0.3): choose **Assist** as the "move mode" in the dashboard and the AI only reports win-rate + candidate moves. You decide where to play — **click an intersection on the board**, or **click a candidate row** — and it clicks that move for you in the client. Your picks go through the same legality check (occupied / ko / suicide / forbidden), so bad clicks are rejected with a reason instead of scrambling the client. A separate **Analyze-only** mode just watches and never plays
-  **Single-instance protection** (v1.0.3): only one AI may own the board per machine, plus a fix for a watchdog "zombie storm" that used to spawn a new AI every 30 s when process enumeration failed
-  **Regression tests** (v1.0.3): 121 cases (rules/ko/vision/config/SGF/end-game/singleton-lock/assist-mode/loop smoke), one command, run automatically in CI

---

## Directory layout

```
├─ go_ai/                Python source
│   ├─ go_controller.py      Main controller (capture / decide / play / verify / wrap-up)
│   ├─ go_vision.py          Board locating & stone recognition (adaptive thresholds)
│   ├─ go_engine.py          Local fallback engine (rules + heuristics + Monte Carlo)
│   ├─ game_state.py         End-of-game state machine (pure logic, unit-tested)
│   ├─ katago_engine.py      KataGo GTP wrapper + resident-service client
│   ├─ kata_service.py       KataGo resident service (port 8124, self-healing)
│   ├─ config_store.py       Single config source (settings.json load/save/validate)
│   ├─ sgf.py                SGF export
│   ├─ notify.py             Event notifications (off by default)
│   ├─ analysis_watch.py     Dashboard backend (port 8123 + controller watchdog)
│   ├─ analysis.html         Dashboard frontend
│   ├─ tests/                Regression tests (stdlib unittest, no extra deps)
│   ├─ avatar_turn_detect.py Xingzhen: avatar water-drop turn detection
│   ├─ seal_turn_detect.py   Tencent: red-seal OCR turn detection
│   └─ GO_AI_ARCHITECTURE.md Chinese architecture doc
├─ tools/make_release.py Release packaging script (shared by local & CI)
├─ katago/                KataGo engine dir (engine binary included; model must be downloaded, see below)
│   └─ opencl171/katago.exe
├─ ACKNOWLEDGMENTS.md     Third-party / AI attribution
├─ LICENSE                MIT License
├─ requirements.txt       Python dependencies (pinned versions)
├─ requirements-flexible.txt  Loose ranges (for setting up on a new machine)
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

:: Assist mode (AI only reports win-rate/candidates; you click the board or a candidate to play)
venv\Scripts\python.exe go_ai\go_controller.py --color black --platform xingzhen --assist
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

### v1.0.3 (2026-09-12)

**New**

- **End-of-game detection & auto wrap-up**: three independent signals (engine resignation / two consecutive of our passes / board unchanged for N cycles *and* no turn indicator) end the game and trigger SGF export + notification + stopping the AI. The idle rule requires a corroborating hint, so an opponent thinking for a long time is never mistaken for the end of the game.
- **SGF export**: true move order (falls back to a setup-only snapshot when history is missing); one click from the dashboard, automatic on game end into `go_ai/games/`, plus `game_result.json` shown on the dashboard.
- **Event notifications** (off by default): set a URL under `notify` in `settings.json` and receive a JSON POST on game end / engine down / thinking error.
- **KataGo service self-healing**: a health-check thread rebuilds the engine if the process dies (OOM, GPU driver restart, ...). Previously the engine was created once at startup, so a crash left the service permanently unusable.
- **Dashboard controller watchdog**: restarts the controller if it dies unexpectedly; intentional stops (120s cooldown) and end-of-game exits (result file within 10 min) do not trigger it, and it gives up after a restart limit to avoid crash loops.
- **Adaptive recognition thresholds**: black/white thresholds are derived from sampled board (wood) color — matching the legacy 150/80 on the current setup but surviving theme/monitor/scaling changes. Seal-based turn detection no longer hardcodes `y150:260 / mid_x=391`.
- **Tunable handicap & threads**: `katago.playout_doubling_advantage` (>0 weakens the AI), `katago.num_search_threads`.
- **Single source of truth for config**: all parameters in `go_ai/settings.json` (auto-migrated from `launcher_config.json` on first run, old file kept). CLI > config > defaults.
- **Mid-game think-time extension is configurable** (`weights` group) instead of hardcoded constants.

**UI**

- **Dashboard restyled to a white + beige light theme** (rice-paper feel): white top bar with a
  wood-tone hairline, warm beige gradient behind the board, rounded cards with soft warm shadows,
  the best candidate row highlighted in a beige-gold gradient, and the win-rate chart recoloured
  (the old theme drew the white-side curve in pure white, which is invisible on a light background —
  it now uses warm grey with a subtle stroke). The cold-start gate card was restyled to match.

- **Digital board (drawn in the browser)** — the board is rendered on a `<canvas>`: wood-grain
  background, radially shaded stones with drop shadows, traditional Chinese coordinates, star points,
  a **red last-move marker**, and candidate rings coloured by win-rate with percentage pills.
  No stitched screenshot needed — lighter and much clearer. Toggle 「数字棋盘 / 实拍」 in the top bar
  (choice remembered locally). Backend `data.json` gained `stones` (19×19), `last_move`, `capture`.
- **Background recognition** — board reading now goes through **PrintWindow window capture**
  (`go_vision.grab_for_read()`), so it still works when the client window is covered or minimized,
  and no longer depends on a foreground full-screen grab; it falls back to full-screen capture when
  the window isn't available. New `capture.mode` (auto/window/screen) and `capture.mirror`
  (auto/on/off); the dashboard shows the live capture source and can flip the mirror in one click.
  ⚠️ Auto-play still needs the window in front (clicks are simulated mouse input) — background
  capture primarily keeps the **dashboard / recognition** working regardless of occlusion.

**Stability / performance**

- **GTP I/O rewritten**: the old "spawn a thread per command, stash timed-out threads in `_pending` and join them later (up to 45s)" scheme is replaced by **one persistent reader thread + queue**, with stale responses discarded by count. The race is gone by design and timeouts no longer contaminate later commands.
- **Pure logic extracted** (`game_state.py`) so it can be tested without a display.
- **Fallback engine cleanup + speedup**: removed no-op branches in `play()` and dead code in `final_score()`; candidate generation now prunes to the neighbourhood of existing stones instead of evaluating all 361 points.

**Engineering**

- **Regression tests**: 121 cases (rules/capture/ko/suicide, SGF, config validation, notifications, end-game state machine, synthetic-board recognition, singleton lock, assist mode, controller loop smoke), stdlib `unittest`, no extra dependencies:
  ```
  cd go_ai && python -m unittest discover -s tests -v
  ```
- **GitHub Actions**: `ci.yml` runs syntax check + tests + packaging self-check on every push; `release.yml` releases on tag push (tests → build → SHA256 → GitHub Release).
- **Scripted packaging**: `python tools/make_release.py [--with-model]` — only git-tracked files, so venvs, debug logs and caches no longer leak into the zip.
- **Auto-generated release notes**: `python tools/release_notes.py --version v1.0.3` extracts the matching section **from this README's changelog** (both languages), so release notes and docs can never drift apart.
- **Pinned dependencies**: exact versions in `requirements.txt`, with `requirements-flexible.txt` for new machines.
- **Cleanup**: dead code in `go_engine`, unused imports in `go_controller`.

**Fixes**

- **Two hard failures in the release pipeline (the first v1.0.3 release died on both)**:
  - The syntax-check step ran `python -m py_compile go_ai/*.py`, but CI's default shell is pwsh, which does **not** expand globs — the literal `go_ai/*.py` was passed as a filename, aborting with `[Errno 22] Invalid argument`. Now uses `python -m compileall -q go_ai tools` under `bash`.
  - The packaging script printed Chinese (`打包完成: …`) to the runner's **cp1252** console, raising `UnicodeEncodeError` and failing the job. The script now forces UTF-8 on `stdout`/`stderr` at startup.
  (Both only bite on GitHub's Windows runners — a local UTF-8 console is perfectly happy, hence "all green locally, all red in CI".)
- **The "last move" red mark read as an empty point → infinite loop (major)**: some clients draw a vermilion square on the last move. It covers the stone centre, tinting the sampled patch red (white-stone saturation rises from 0.10 to 0.35), so the stone was classified as an **empty point**. The engine then treated that point as the best available move and clicked an occupied intersection over and over — the client rejected it, the stone count never changed, and the next cycle read the same "empty" point. In a real run this spun for 115 cycles without a single successful move.
  - Root-cause fix: **discard vermilion marker pixels before counting** (`go_vision.marker_red_mask`), falling back to the plain patch when too much would be removed. The point now reads as a white stone and the counts are correct.
  - Safety net: after **2 consecutive failed move verifications** at the same intersection it is blacklisted and the engine's next candidate is used instead, so the same point is never retried forever. Any successful verification clears the blacklist.
- **Window capture never worked on Xingzhen (browser platform)**: the screenshot helper read a **class** attribute instead of the live config **instance**, so the platform check always saw the default value and took the "find the Tencent Go window" branch — window capture and mirror settings were effectively dead. Now the instance is passed explicitly.
- **Window capture no longer a matter of luck**: previously it only fell back to window capture when the screenshot itself failed; with the board covered by a popup the shot succeeds but **the board is unrecognisable**, so the loop idled. It now falls back to `PrintWindow` window capture (unaffected by occlusion) in that case too.
- **Log noise**: 2.5s after playing, the opponent has usually already answered or captured, so the order of the two new stones in a single frame is unknowable and replay can never match — this is normal, but the old build printed «history mismatch, resetting» **twice per move**. Now it explains itself once per game and then stays silent (the reset fallback itself is unchanged).

### v1.0.2 (2026-09-11)

- **Fix (major)**: memory kept growing during long sessions — the dashboard read the **entire** KataGo log into memory every 2 seconds, while that log grows with every move (hundreds of MB are possible), so memory only ever went up. The dashboard now reads only the **tail** (256KB cap), making memory independent of log size: measured **+2MB** for a 202MB log (vs **+165MB** for a 73MB log before). Whole stack now sits at roughly **330MB** resident
- Added gtp log rotation (newest 6 kept, 256MB directory cap), pruned at startup and periodically
- **Fix**: "Start KataGo preload / play as black" blocked the HTTP request for the whole cold start (up to 180s), so the button looked dead; now it runs in the background and returns immediately
- **Fix**: a controller could be re-spawned by the background start thread after you pressed stop (orphan process); and a stale process snapshot could make "stop" miss a just-started controller (the AI would keep playing). Both fixed
- Memory tuning: KataGo NN cache halved (`nnCacheSizePowerOfTwo = 19`), search threads 16 → 8, daemon-thread HTTP server
- Misc: bounded engine stderr queue, fixed a launcher log-handle leak
- Bonus: dashboard polling latency dropped from 214s to 9s

### v1.0.1 (2026-09-11)

- **Fix**: clicking "Start KataGo preload" several times during cold start used to spawn **multiple engines** (200MB+ VRAM each, and on Windows they could double-bind the same port so requests landed on a random engine). A PID-based exclusive lock now claims the startup right at second 0, the socket is bound exclusively, and the dashboard gate shows "cold start" without offering the button again
- Preload log is now appended instead of truncated (truncation wiped the in-flight cold-start progress)
- **Docs**: added a "Disclaimer & Acceptable Use" section

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

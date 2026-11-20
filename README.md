# Usage Harvester

Spends Claude Code and Codex usage you would otherwise lose at the weekly reset on a backlog of tasks, using only the end of each 5-hour session.

Windows only. Needs [Claude Code](https://claude.com/claude-code), the [Codex](https://developers.openai.com/codex) CLI or desktop app, or both, installed and logged in. Each agent you have is harvested on its own limits.

## Install

1. Download `UsageHarvester-<version>-windows-x64.zip` from [Releases](https://github.com/LucasLonegro/UsageWindowHarvester/releases) and extract it, e.g. to `%LOCALAPPDATA%\Programs\UsageHarvester`.
2. Add that folder to your `PATH` so `hv` works anywhere.
3. Run `Harvester.exe` (or `hv app`); `hv autostart on` starts it at every login. The executables are unsigned, so SmartScreen may ask you to confirm the first run.
4. Optional: `hv skill install` installs the backlog skill for every agent you have. Type `/backlog` in Claude Code or `$backlog` in Codex to hand the current task to the backlog.

From source instead: Python 3.11+ on Windows, put this folder on `PATH` and use `hv.cmd`; `hv app` starts `app.pyw`.

## Desktop app

- Header: one button per agent with its mode (watching, harvest, burn, paused). The selected agent's reason, cards, estimates, timeline, week chart and runs fill the window.
- Session and Week cards: usage, a lighter band for where your current pace ends up, and markers for the session cap, even weekly pace and the weekly reserve.
- Estimates: what a session is worth, spare sessions, when harvesting should start, what would go unused, what has been harvested.
- Timeline: gantt of session usage, harvester mode, your activity and every task run (8h, 24h, 2 days or the whole week). Hover for details, click a run to open its task.
- Week: weekly usage over the week against even pace, with the projection to reset and the share that would go unused.
- Runs and Queue: the selected agent's run history, and the whole backlog with the agent each task is routed to. Double-click for a task's prompt and result; the Queue tab can move tasks to the top, retry or remove them.
- Closing the window minimizes it; Quit stops running tasks and exits.

## Commands

```
hv                     dashboard per agent: mode, session/week meters, estimates, last 8h timeline; then the queue
hv status -s           the same in one line
hv watch [-n SECS]     live dashboard
hv gantt [-H HOURS] [-A HOURS] [-w]   timeline of sessions, harvester modes and task runs (-w: whole week)
hv week                weekly usage chart: used, even pace, projection at your pace
hv runs [-n N]         run history with length, turns and estimated weekly share
                       status, watch, gantt, week and runs take -p claude|codex for one agent
hv add "prompt" [-d DIR] [--via any|claude|codex] [-p PRIORITY] [-m MODEL] [-t "Edit Bash(npm test:*)"] [--mode acceptEdits]
hv add -f prompt.md    prompt from a file
hv skill install [claude] [codex]   install the backlog skill
hv ls [-a]             tasks (-a includes done)
hv show ID             prompt and result
hv top ID | rm ID | retry ID
hv pause | resume | now
hv config [KEY [VALUE]]
hv app | autostart on|off
```

Data lives in `~/.usage-harvester` (override with `HV_HOME`): `backlog.json`, `config.json`, `results/ID.md`, `logs/`, `history.jsonl` (one line per agent per check, kept 9 days), `runs.jsonl` (one line per task run).

### Routing tasks

`--via` picks the agent: `claude`, `codex`, or `any` (the default, set by `default_route`), which goes to whichever agent starts burning first. Once a task has run, it stays on that agent so a stopped run can resume its own session.

- Claude Code runs `claude -p` with the task's permission mode (`acceptEdits` by default) and `-t` tools.
- Codex runs `codex exec` in the `codex_sandbox` sandbox (`workspace-write` by default: edits and commands in the task folder, no network) and never asks for approval. `--mode plan` makes it read-only; `--mode bypassPermissions` turns the sandbox off. `-t` is ignored.
- `-m` is passed to whichever agent runs the task, so give a model only together with a matching `--via`.

### Reading the dashboard

- Session bar: `│` is the cap tasks stop at, `▒` is where your current pace ends the session.
- Week bar: `┆` is even pace (the share of the week elapsed), `▒` your projected use by reset, `│` the weekly reserve.
- "Harvest from" estimates when spare sessions reach `slack_margin_windows`: at your average pace this week, and at the earliest if you stopped using Claude now.
- "Would go unused" is the projected leftover at your pace, as sessions and as hours of task time at the learned burn rate.
- Weekly share per run is an estimate: run length × burn rate × weekly share of a session. Values marked "(guess)" are still the config defaults; they are replaced once enough usage has been observed.
- Timeline rows: session % sparkline (`╌` is the rest of the open session), harvester mode (`·` watching, `─` harvest waiting, `█` burning, `░` planned burn), when you were active, and one row per task run.

## How it decides

- **c** = weekly % one full session is worth (learned from your usage).
- **spare sessions** = (what the current session has left + sessions that fit before the weekly reset) − weekly % left, in sessions.
- Above `slack_margin_windows` spare sessions it only watches. At or below, it harvests.
- While harvesting it waits until the session's last stretch: the time it needs to use what is left at the learned burn rate, × `tail_safety`, at least `min_tail_minutes`. Then it runs tasks until `session_reserve_pct` is left, and in the last `final_minutes` it uses all of it.
- Each agent is planned separately from its own limits, with its own learned **c** and burn rate.
- Whenever you use an agent (any of its transcripts that isn't one of the harvester's changed in the last `idle_minutes`), it stops that agent's tasks. For Codex this covers the CLI, the IDE extension and the desktop app. Stopped tasks resume their own session later.
- With no session open while harvesting, it sends one tiny message to open one (`start_window_ping`): Haiku for Claude Code, low reasoning effort for Codex.

## Building and releasing

`./build.ps1 -Version 0.1.0` builds `Harvester.exe` and `hv.exe` with PyInstaller into `build\exe` and zips them with this README, the license and the skill into `dist\`. Pass `-Python path\to\python.exe` to build with a specific interpreter or virtual environment.

Pushing a tag that starts with `v` runs `.github/workflows/release.yml` on GitHub: it builds on Windows, smoke-tests `hv.exe`, and publishes a release with the zip and generated notes. Tags with a hyphen (`v0.2.0-beta.1`) become prereleases.

```
git tag v0.1.0
git push upstream v0.1.0
```

Running the workflow by hand (Actions → Release → Run workflow) builds the zip as a downloadable artifact without publishing a release.

## Usage source

Codex: each check asks `codex app-server` for the account's rate limits (`account/rateLimits/read`), the same numbers `/status` shows. It makes no model call. If `codex` is not on `PATH`, the CLI bundled with the Codex desktop app is used.

Claude Code: each check runs `claude -p /usage` and reads the "Current session" and "Current week (all models)" lines. In print mode that command makes no model call, costs nothing, and keeps the CLI login fresh. If the output format changes, the app shows the error. It then falls back to a snapshot from your status line, if you add this to `statusline.py` after it parses its input `d`:

```python
import subprocess, json; subprocess.run(["hv", "snapshot"], input=json.dumps(d), text=True)
```

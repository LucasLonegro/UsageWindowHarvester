import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import core

WEEK_S = 7 * 86400
LABEL_W = 14
SPARK = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"
MARK = {"queued": "·", "running": "▶", "paused": "‖", "done": "✓", "failed": "✗"}
STATUS_COLOR = {"running": "36", "done": "32", "paused": "33", "failed": "31"}
MODE_COLOR = {"watching": "90", "harvest": "33", "burn": "35", "paused": "90"}
MODE_GLYPH = {"watching": "·", "harvest": "─", "burn": "█", "paused": "‖"}
MODE_RANK = {"burn": 3, "harvest": 2, "paused": 1, "watching": 0}


class Style:
    def __init__(self, on):
        self.on = on

    def __call__(self, text, code=""):
        return f"\x1b[{code}m{text}\x1b[0m" if self.on and code and text else text


def enable_vt():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 4)
    except (AttributeError, OSError):
        pass


def make_style():
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if on:
        enable_vt()
    return Style(on)


def term_width():
    return max(64, min(160, shutil.get_terminal_size((100, 30)).columns - 1))


def join(cells, s):
    out, run, code = [], [], None
    for ch, c in cells:
        if c != code and run:
            out.append(s("".join(run), code))
            run = []
        code = c
        run.append(ch)
    if run:
        out.append(s("".join(run), code))
    return "".join(out)


def short(text, n=60):
    text = " ".join(w for w in text.split() if w.strip("#"))
    return text if len(text) <= n else text[: n - 1] + "…"


def wrap(parts, width, indent, s, sep=" · ", code="90"):
    lines, cur = [], ""
    for part in parts:
        cand = cur + sep + part if cur else part
        if cur and indent + len(cand) > width:
            lines.append(cur)
            cur = part
        else:
            cur = cand
    return [" " * indent + s(line, code) for line in lines + [cur]]


def ago(secs):
    return "<1m" if secs < 60 else core.fmt_dur(secs)


def when(ts):
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%a %H:%M")


def task_line(t, width, s):
    prio = f"p{t['priority']}" if t["priority"] else ""
    left = f" {t['id']:>3} {prio:<3} "
    folder = f"[{Path(t['dir']).name}]"
    note = f"  ({short(t['note'], 40)})" if t.get("note") else ""
    room = max(20, width - 1 - len(left) - len(folder) - 2 - len(note))
    mark = s(MARK.get(t["status"], "?"), STATUS_COLOR.get(t["status"], ""))
    return f"{mark}{left}{short(t['prompt'], room)}  {s(folder, '90')}{s(note, '90')}"


# Data

def load(now=None):
    now = now or time.time()
    tasks = core.load_tasks()
    runs = core.read_jsonl("runs.jsonl", key="end")
    for t in tasks:
        if t["status"] == "running" and t.get("started"):
            runs.append({"task": t["id"], "run": t["runs"], "start": t["started"], "end": None,
                         "status": "running"})
    return {"now": now, "cfg": core.load_config(), "st": core.read_json(core.path("state.json"), {}),
            "tasks": tasks, "runs": runs, "hist": core.read_jsonl("history.jsonl")}


def harvested(v, since, e):
    secs, n = 0.0, 0
    for r in v["runs"]:
        a, b = r["start"], r["end"] or v["now"]
        if b > since:
            secs += b - max(a, since)
            n += 1
    sess = secs / 60 * e["rate"]
    return {"secs": secs, "runs": n, "session_pct": sess, "week_pct": sess * e["c"] / 100}


def estimate(v):
    now, cfg, st = v["now"], v["cfg"], v["st"]
    learned = st.get("learned", {})
    e = {"c": learned.get("window_weekly_pct") or cfg["window_weekly_pct"],
         "c_learned": bool(learned.get("window_weekly_pct")),
         "rate": learned.get("burn_pct_per_min") or cfg["burn_pct_per_min"],
         "rate_learned": bool(learned.get("burn_pct_per_min")),
         "par": max(1, cfg["max_parallel"]), "margin": cfg["slack_margin_windows"]}
    u = st.get("usage")
    if not u:
        return e
    S, Ts, W, Tw = u["session_pct"], u["session_reset"], u["weekly_pct"], u["weekly_reset"]
    if not (Ts and Ts > now):
        S, Ts = 0.0, None
    if not (Tw and Tw > now):
        W, Tw = 0.0, None
    e.update(S=S, Ts=Ts, W=W, Tw=Tw)
    c, rate = e["c"], e["rate"]

    if Ts:
        left = Ts - now
        cap = 100.0 if left <= cfg["final_minutes"] * 60 else 100.0 - cfg["session_reserve_pct"]
        fill = max(0.0, cap - S) / (rate * e["par"]) * 60
        tail = max(cfg["min_tail_minutes"] * 60, fill * cfg["tail_safety"])
        e.update(session_left=left, cap=cap, fill_s=fill, burn_at=Ts - tail)
        elapsed = core.WINDOW_S - left
        if elapsed >= 900:
            e["session_proj"] = min(100.0, S / elapsed * core.WINDOW_S)
    if not Tw:
        return e

    start = Tw - WEEK_S
    elapsed = max(60.0, now - start)
    h = harvested(v, start, e)
    # The user's own pace: weekly use minus what backlog runs likely spent.
    pace = max(0.0, W - h["week_pct"]) / elapsed
    current = c * (100 - S) / 100 if Ts else 0.0
    future = max(0.0, Tw - (Ts or now)) / core.WINDOW_S
    slack = (current + c * future - (100 - W)) / c
    proj = min(100.0, W + pace * (Tw - now))
    e.update(week_start=start, week_left=Tw - now, even=min(100.0, elapsed / WEEK_S * 100),
             harvested=h, pace=pace, proj_end=proj, unused=100 - proj,
             sessions_left=current / c + future, slack=slack)
    e["unused_sessions"] = e["unused"] / c
    e["unused_task_s"] = e["unused"] / (rate * c / 100) * 60

    gap = slack - e["margin"]
    if gap <= 0:
        e["eta_idle"] = e["eta_pace"] = now
    else:
        # Spare sessions shrink one per 5h, and grow as you spend the week.
        e["eta_idle"] = min(Tw, now + gap * core.WINDOW_S)
        shrink = 1 / core.WINDOW_S - pace / c
        eta = now + gap / shrink if shrink > 0 else None
        e["eta_pace"] = eta if eta and eta < Tw else None
    return e


# Views

def header(v, width, s):
    st, now = v["st"], v["now"]
    clock = datetime.fromtimestamp(now).strftime("%a %d %b %H:%M")
    if not st:
        app, code = "○ app never ran · hv app", "31"
    elif core.app_alive(st):
        app, code = f"● app running · checked {ago(now - st.get('updated', now))} ago", "32"
    else:
        app, code = f"○ app not running · data {ago(now - st.get('updated', 0))} old · hv app", "31"
    title = "Usage Harvester"
    pad = max(2, width - len(title) - len(app) - len(clock) - 5)
    lines = [s(title, "1") + " " * pad + s(app, code) + s("  ·  " + clock, "90")]
    d = st.get("decision") or {}
    mode = "paused" if core.path("paused").exists() else d.get("mode")
    if mode:
        reason = d.get("reason", "") if mode == d.get("mode") else "paused by you"
        if st.get("user_active") and mode != "paused":
            reason += s(" · you're active", "34")
        lines.append(s(f" {mode.upper()} ", "7;1;" + MODE_COLOR.get(mode, "37")) + "  " + reason)
    if st.get("error"):
        lines.append(s("error: " + st["error"], "31"))
    return lines


def meter(width, pct, proj=None, marks=()):
    cells = []
    unit = 100 / width
    for i in range(width):
        frac = (pct - i * unit) / unit
        if frac >= 1:
            cells.append(["█", "36"])
        elif frac > 0:
            cells.append([EIGHTHS[max(1, int(frac * 8))], "36"])
        elif proj is not None and i * unit < proj:
            cells.append(["▒", "33"])
        else:
            cells.append(["░", "90"])
    for p, ch in marks:
        cells[min(width - 1, max(0, int(p / unit)))] = [ch, "1;37"]
    return cells


def meters(v, e, width, s):
    if "S" not in e:
        return [s("no usage data yet · the app records it every few minutes", "90")]
    cfg = v["cfg"]
    bw = max(16, width - 9 - 37)
    lines = []
    if e["Ts"]:
        bar = meter(bw, e["S"], e.get("session_proj"), [(e["cap"], "│")])
        right = f"{e['S']:>4.0f}%  resets {core.fmt_clock(e['Ts'])} · {core.fmt_dur(e['session_left'])} left"
        parts = [f"│ cap {e['cap']:.0f}%"]
        if "session_proj" in e:
            parts.append(f"▒ your pace → {e['session_proj']:.0f}%")
        parts.append(f"tasks fill to cap in {core.fmt_dur(e['fill_s'])}")
        window = f"{core.fmt_clock(max(e['burn_at'], v['now']))}–{core.fmt_clock(e['Ts'])}"
        harvesting = (v["st"].get("decision") or {}).get("mode") in ("harvest", "burn")
        parts.append(f"burn window {window}" if harvesting else f"if harvesting, would burn {window}")
    else:
        bar = meter(bw, 0)
        right = "   0%  no session open"
        parts = ["the harvester opens one when it needs to" if cfg["start_window_ping"]
                 else "the next one starts with your next message"]
    lines += [f"{'Session':<9}" + join(bar, s) + right] + wrap(parts, width, 9, s)
    if e["Tw"]:
        reserve = 100 - cfg["weekly_reserve_pct"]
        bar = meter(bw, e["W"], e["proj_end"], [(e["even"], "┆"), (reserve, "│")])
        right = f"{e['W']:>4.0f}%  resets {core.fmt_clock(e['Tw'])} · {core.fmt_dur(e['week_left'])} left"
        sub = [f"┆ even pace {e['even']:.0f}%",
               f"▒ your pace {e['pace'] * 86400:.1f}%/day → {e['proj_end']:.0f}% by reset", f"│ reserve {reserve}%"]
        lines += [f"{'Week':<9}" + join(bar, s) + right] + wrap(sub, width, 9, s)
    return lines


def estimate_rows(e):
    if "slack" not in e:
        return []
    g = lambda learned: "" if learned else " (guess)"
    if e["slack"] <= e["margin"]:
        eta = ["now"]
    else:
        eta = [f"~{core.fmt_clock(e['eta_pace'])} at your pace" if e["eta_pace"] else "not before reset at your pace",
               f"{core.fmt_clock(e['eta_idle'])} at the earliest, if you stop using Claude"]
    if e["unused"] >= 0.5:
        unused = [f"{e['unused']:.0f}% of week", f"≈ {e['unused_sessions']:.1f} sessions",
                  f"≈ {core.fmt_dur(e['unused_task_s'])} of task time", "(without the harvester)"]
    else:
        unused = ["none", "you're on pace to use the whole week"]
    h = e["harvested"]
    return [
        ("1 session", [f"≈ {e['c']:.1f}% of week{g(e['c_learned'])}",
                       f"a task burns ≈ {e['rate']:.2f}% of a session/min{g(e['rate_learned'])}"]),
        ("sessions left", [f"{e['sessions_left']:.1f} until reset", f"spare {e['slack']:+.1f}",
                           f"harvest when spare ≤ {e['margin']:g}"]),
        ("harvest from", eta),
        ("would go unused", unused),
        ("harvested", [f"{h['runs']} runs this week", core.fmt_dur(h["secs"]), f"≈ {h['week_pct']:.1f}% of week"]),
    ]


def estimates_block(e, width, s):
    rows = estimate_rows(e)
    if not rows:
        return []
    lines = [s("Estimates", "1")]
    for key, parts in rows:
        body = wrap(parts, width, 18, s, code="")
        lines += [s(f"  {key:<16}", "90") + body[0][18:]] + body[1:]
    return lines


def tick_cols(start, end, cols):
    step = (end - start) / cols
    every = next((h for h in (1, 2, 3, 6, 12, 24) if h * 3600 / step >= 6), 24)
    t = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    out = []
    while t.timestamp() < end:
        if t.hour % every == 0:
            label = t.strftime("%a") if t.hour == 0 else f"{t.hour}h"
            out.append((int((t.timestamp() - start) / step), label))
        t += timedelta(hours=1)
    return out


def timeline(v, e, start, end, width, s, max_tasks=6, title="Timeline"):
    now, cfg = v["now"], v["cfg"]
    cols = max(10, width - LABEL_W)
    step = (end - start) / cols
    col = lambda t: int((t - start) / step)
    now_c = col(now) if start <= now < end else None
    last_c = cols - 1 if now_c is None else now_c
    ticks = tick_cols(start, end, cols)

    def blank():
        row = [[" ", ""] for _ in range(cols)]
        for c, _ in ticks:
            row[c] = ["·", "90"]
        if now_c is not None:
            row[now_c] = ["┊", "33"]
        return row

    def paint(row, a, b, ch, code):
        for i in range(max(0, col(a)), min(cols - 1, col(b)) + 1):
            row[i] = [ch, code]

    label_row = [" "] * cols
    free = 0
    for c, lab in ticks:
        if c >= free and c + len(lab) <= cols:
            label_row[c:c + len(lab)] = list(lab)
            free = c + len(lab) + 1
    axis = [["─", "90"] for _ in range(cols)]
    for c, _ in ticks:
        axis[c] = ["┬", "90"]
    if now_c is not None:
        axis[now_c] = ["▼", "1;33"]
    lines = [s(f"{title:<{LABEL_W}}"[:LABEL_W], "1") + "".join(label_row),
             " " * LABEL_W + join(axis, s)]

    gap = 3 * cfg["poll_seconds"] + 60
    hist = [h for h in v["hist"] if start - gap <= h["t"] < end]
    per_col = [[] for _ in range(cols)]
    carry = None
    for h in hist:
        if h["t"] < start:
            carry = h
        elif col(h["t"]) < cols:
            per_col[col(h["t"])].append(h)
    sess, mode, you = blank(), blank(), blank()
    for i in range(last_c + 1):
        t0 = start + i * step
        recs = per_col[i]
        if recs:
            carry = recs[-1]
        elif carry and t0 - carry["t"] <= gap:
            recs = [carry]
        else:
            carry = None
        if not recs:
            continue
        open_s = [h["S"] for h in recs if h.get("Ts") and h["Ts"] > t0]
        if open_s:
            sess[i] = [SPARK[min(7, int(max(open_s) / 100 * 8))], "36"]
        m = max((h["m"] for h in recs), key=lambda x: MODE_RANK.get(x, 0))
        mode[i] = [MODE_GLYPH.get(m, "?"), MODE_COLOR.get(m, "")]
        if any(h.get("a") for h in recs):
            you[i] = ["█", "34"]

    if now_c is not None and e.get("Ts"):
        paint(sess, now + step, min(e["Ts"], end - 1), "╌", "2;36")
        mode_now = (v["st"].get("decision") or {}).get("mode")
        if mode_now in ("harvest", "burn") and e.get("burn_at"):
            paint(mode, max(e["burn_at"], now + step), min(e["Ts"], end - 1), "░", "35")
    for key, ch in (("eta_idle", "◇"), ("eta_pace", "◆")):
        t = e.get(key)
        if now_c is not None and t and now < t < end and col(t) > now_c:
            mode[col(t)] = [ch, "33"]

    row = lambda name, cells: s(f"{name:<{LABEL_W}}"[:LABEL_W], "90") + join(cells, s)
    lines += [row("session %", sess), row("harvester", mode), row("you active", you)]

    runs = [r for r in v["runs"] if (r["end"] or now) >= start and r["start"] < end]
    by_task = {}
    for r in runs:
        by_task.setdefault(r["task"], []).append(r)
    order = sorted(by_task, key=lambda tid: max((r["end"] or now) for r in by_task[tid]))
    hidden = order[:-max_tasks] if len(order) > max_tasks else []
    shown = sorted(order[len(hidden):], key=lambda tid: min(r["start"] for r in by_task[tid]))
    prompts = {t["id"]: t["prompt"] for t in v["tasks"]}
    for tid in shown:
        cells = blank()
        for r in by_task[tid]:
            code = STATUS_COLOR.get(r["status"], "")
            a, b = max(r["start"], start), min(r["end"] or now, end - 1)
            paint(cells, a, b, "█", code)
            if col(b) > col(a):
                cells[min(cols - 1, col(b))] = [MARK.get(r["status"], "?"), code]
        lines.append(row(f"#{tid} {short(prompts.get(tid, '(removed)'), LABEL_W)}", cells))
    if hidden:
        lines.append(s(f"{'':<{LABEL_W}}+{len(hidden)} more tasks · hv gantt -H for a longer range", "90"))
    if not runs:
        lines.append(s(f"{'':<{LABEL_W}}no backlog runs in this range", "90"))
    lines += wrap(["· watching", "─ harvest, waiting", "█ burning", "░ planned burn",
                   "◆ harvest starts at your pace", "◇ earliest"], width, LABEL_W, s, sep="  ")
    if runs:
        lines.append(s(f"{'':<{LABEL_W}}tasks: ", "90") + " ".join(
            s(f"{MARK[k]} {k}", STATUS_COLOR[k]) for k in ("running", "done", "paused", "failed")))
    return lines


def backlog_block(v, width, s, n=5):
    tasks = v["tasks"]
    queue = core.queue_order(tasks)
    running = [t for t in tasks if t["status"] == "running"]
    counts = Counter(t["status"] for t in tasks)
    summary = " · ".join(f"{counts[k]} {k}" for k in ("running", "paused", "queued", "failed", "done") if counts[k])
    lines = [s("Backlog", "1") + "  " + s(summary or "empty · add with hv add or /backlog", "90")]
    lines += [task_line(t, width, s) for t in running + queue[:n]]
    if len(queue) > n:
        lines.append(s(f"  … {len(queue) - n} more · hv ls", "90"))
    return lines


def render_status(v, width, s):
    e = estimate(v)
    now = v["now"]
    out = header(v, width, s) + [""] + meters(v, e, width, s) + [""]
    est = estimates_block(e, width, s)
    if est:
        out += est + [""]
    out += timeline(v, e, now - 8 * 3600, now + 4 * 3600, width, s, max_tasks=4, title="Last 8h") + [""]
    out += backlog_block(v, width, s)
    out.append(s("more: hv gantt · hv week · hv runs · hv watch", "90"))
    return "\n".join(out)


def render_short(v, s):
    e = estimate(v)
    st = v["st"]
    mode = "paused" if core.path("paused").exists() else (st.get("decision") or {}).get("mode", "?")
    parts = [s(mode.upper(), MODE_COLOR.get(mode, ""))]
    if "S" in e:
        parts.append(f"session {e['S']:.0f}%" + (f" ↺{core.fmt_dur(e['session_left'])}" if e["Ts"] else ""))
        parts.append(f"week {e['W']:.0f}%" + (f" ↺{core.fmt_dur(e['week_left'])}" if e["Tw"] else ""))
    if "slack" in e:
        parts.append(f"spare {e['slack']:+.1f}")
    counts = Counter(t["status"] for t in v["tasks"])
    parts.append(f"{counts['queued'] + counts['paused']} queued")
    if counts["running"]:
        parts.append(f"{counts['running']} running")
    if st and not core.app_alive(st):
        parts.append(s("app off", "31"))
    return " · ".join(parts)


def render_gantt(v, width, s, hours=24, ahead=6, week=False):
    e = estimate(v)
    now = v["now"]
    if week and e.get("Tw"):
        start, end, title = e["Tw"] - WEEK_S, e["Tw"], "This week"
    else:
        start, end, title = now - hours * 3600, now + ahead * 3600, f"Last {hours:g}h"
    return "\n".join(timeline(v, e, start, end, width, s, max_tasks=30, title=title))


def render_week(v, width, s, rows=10):
    e = estimate(v)
    Tw = e.get("Tw")
    if not Tw:
        return "no weekly data yet"
    now, start = v["now"], Tw - WEEK_S
    cols = max(20, width - 6)
    step = WEEK_S / cols
    col = lambda t: int((t - start) / step)
    now_c = min(cols - 1, col(now))
    vals, burn = [None] * cols, [False] * cols
    for h in v["hist"]:
        if h.get("Tw") and abs(h["Tw"] - Tw) < 3600 and start <= h["t"] < Tw:
            c = col(h["t"])
            vals[c] = max(vals[c] or 0, h["W"])
            burn[c] = burn[c] or h.get("m") == "burn"
    seen = None
    carried = [False] * cols
    for c in range(now_c + 1):
        if vals[c] is None and seen is not None:
            vals[c], carried[c] = seen, True
        elif vals[c] is not None:
            seen = vals[c]
    vals[now_c] = e["W"]
    proj = [None] * cols
    for c in range(now_c + 1, cols):
        proj[c] = min(100.0, e["W"] + e["pace"] * (start + (c + 0.5) * step - now))

    def cell(val, lo):
        if val >= lo + 10:
            return "█"
        if val > lo:
            return SPARK[min(7, int((val - lo) / 10 * 8))]
        return None

    lines = [s("Weekly usage", "1") + s(f"  resets {core.fmt_clock(Tw)} · {core.fmt_dur(Tw - now)} left", "90")]
    for r in range(rows - 1, -1, -1):
        lo = r * 100 / rows
        cells = []
        for c in range(cols):
            even = (c + 0.5) / cols * 100
            ch, code = None, ""
            if vals[c] is not None:
                ch = cell(vals[c], lo)
                code = "35" if burn[c] else ("2;36" if carried[c] else "36")
            elif proj[c] is not None:
                ch = cell(proj[c], lo)
                ch, code = ("▒" if ch == "█" else ch), "33"
            if ch is None:
                ch, code = ("·", "90") if lo <= even < lo + 100 / rows else (" ", "")
            cells.append([ch, code])
        label = {rows - 1: "100%", rows // 2 - 1: " 50%"}.get(r, "")
        lines.append(s(f"{label:>4}", "90") + s("│", "90") + join(cells, s))
    axis = [["─", "90"] for _ in range(cols)]
    labels = [" "] * cols
    day = datetime.fromtimestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < Tw:
        c = col(day.timestamp())
        if 0 <= c < cols:
            axis[c] = ["┴", "90"]
        mid = col(day.timestamp() + 43200) - 1
        if 0 <= mid and mid + 3 <= cols:
            labels[mid:mid + 3] = list(day.strftime("%a"))
        day += timedelta(days=1)
    axis[now_c] = ["▲", "1;33"]
    lines.append(s("    └", "90") + join(axis, s))
    lines.append("     " + s("".join(labels), "90"))
    lines.append("")
    lines.append(s("     █ used  ", "36") + s("█ harvester burning  ", "35") + s("▒ your pace  ", "33")
                 + s("· even pace  ▲ now", "90"))
    if "unused" in e:
        lines.append(f"     used {e['W']:.0f}% · even pace {e['even']:.0f}% · your pace {e['pace'] * 86400:.1f}%/day"
                     f" → {e['proj_end']:.0f}% at reset · would go unused {e['unused']:.0f}%"
                     f" ≈ {e['unused_sessions']:.1f} sessions")
    return "\n".join(lines)


def render_runs(v, width, s, limit=20):
    e = estimate(v)
    now = v["now"]
    prompts = {t["id"]: t["prompt"] for t in v["tasks"]}
    runs = sorted(v["runs"], key=lambda r: r["start"], reverse=True)
    if not runs:
        return "no backlog runs yet"
    per_min = e["rate"] * e["c"] / 100
    head = f"{'task':>5} {'run':>3}  {'started':<9} {'length':>6}  {'status':<9} {'turns':>5} {'≈week':>6}  prompt"
    lines = [s(head, "90")]
    for r in runs[:limit]:
        secs = (r["end"] or now) - r["start"]
        status = s(f"{MARK.get(r['status'], '?')} {r['status']:<7}", STATUS_COLOR.get(r["status"], ""))
        turns = r.get("turns")
        room = max(10, width - len(head) + len("prompt"))
        lines.append(f"{'#' + str(r['task']):>5} {r.get('run') or '-':>3}  {when(r['start']):<9} {core.fmt_dur(secs):>6}  "
                     f"{status} {turns if turns is not None else '-':>5} {secs / 60 * per_min:>5.1f}%  "
                     f"{short(prompts.get(r['task'], '(removed)'), room)}")
    if len(runs) > limit:
        lines.append(s(f"… {len(runs) - limit} older · hv runs -n {len(runs)}", "90"))
    lines.append("")
    for name, since in (("this week", e.get("week_start")), ("all time", 0)):
        if since is None:
            continue
        h = harvested(v, since, e)
        lines.append(f"{name:<10} {h['runs']} runs · {core.fmt_dur(h['secs'])} · ≈ {h['week_pct']:.1f}% of a week")
    lines.append(s("≈week = run length × learned burn rate × weekly share of a session", "90"))
    return "\n".join(lines)

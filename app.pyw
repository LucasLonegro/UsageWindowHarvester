import os
import socket
import sys
import threading
import time
import tkinter as tk
import zlib
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402
import views  # noqa: E402

BG, PANEL, FG, MUTED, TRACK, GRID = "#141417", "#1e1e23", "#e8e8ea", "#8a8a93", "#2a2a31", "#26262c"
ACCENT, ACCENT_DIM, AMBER, RED, YOU = "#7aa2f7", "#2c3550", "#e0a43a", "#e06c6c", "#9d7cd8"
TIP_BG = "#2f2f37"
MODE_COLOR = {"watching": "#8a8a93", "harvest": "#e0a43a", "burn": "#4cc38a", "paused": "#6f8bd6"}
MODE_BAND = {"watching": ("#3a3a42", 3), "harvest": ("#e0a43a", 8), "burn": ("#4cc38a", 16), "paused": ("#6f8bd6", 6)}
STATUS_COLOR = {"running": "#4fb3d9", "done": "#4cc38a", "paused": "#e0a43a", "failed": "#e06c6c", "queued": MUTED}
FONT, SMALL, TINY = ("Segoe UI", 10), ("Segoe UI", 9), ("Segoe UI", 8)
BOLD, TITLE = ("Segoe UI Semibold", 10), ("Segoe UI Semibold", 13)
MONO = ("Cascadia Mono", 9)
WIDTH = 780
RANGES = {"8h": (8, 4), "24h": (24, 6), "2 days": (48, 6), "Week": None}


def single_instance():
    # One instance per data folder; the default folder keeps the fixed port.
    port = 47613 + (zlib.crc32(str(core.HOME).lower().encode()) % 1000 + 1 if os.environ.get("HV_HOME") else 0)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        sys.exit(0)
    return s


def label(parent, text="", fg=FG, bg=BG, font=SMALL, **kw):
    return tk.Label(parent, text=text, fg=fg, bg=bg, font=font, anchor="w", justify="left", **kw)


def button(parent, text, cmd, bg=PANEL):
    return tk.Button(parent, text=text, command=cmd, bg=bg, fg=FG, activebackground=TRACK,
                     activeforeground=FG, relief="flat", bd=0, padx=10, pady=4, font=SMALL, cursor="hand2")


def ticks(start, end, px):
    every = next((h for h in (1, 2, 3, 6, 12, 24) if h * 3600 / (end - start) * px >= 46), 24)
    t = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t.timestamp() < end:
        if t.hour % every == 0:
            yield t.timestamp(), t.strftime("%a %d") if t.hour == 0 else t.strftime("%H:%M")
        t += timedelta(hours=1)


def intervals(hist, key, gap, poll, now):
    """Merges consecutive checks with the same key into [start, end, key] spans."""
    out = []
    for i, h in enumerate(hist):
        nxt = hist[i + 1]["t"] if i + 1 < len(hist) else None
        end = nxt if nxt is not None and nxt - h["t"] <= gap else min(h["t"] + poll, now)
        k = key(h)
        if out and out[-1][2] == k and abs(out[-1][1] - h["t"]) < 1:
            out[-1][1] = end
        else:
            out.append([h["t"], end, k])
    return out


class Tooltip:
    def __init__(self, canvas):
        self.c = canvas

    def bind(self, tag, text, click=None):
        self.c.tag_bind(tag, "<Enter>", lambda ev: self.show(ev, text))
        self.c.tag_bind(tag, "<Leave>", lambda _: self.c.delete("tip"))
        if click:
            self.c.tag_bind(tag, "<Button-1>", lambda _: click())
            self.c.tag_bind(tag, "<Enter>", lambda _: self.c.config(cursor="hand2"), add="+")
            self.c.tag_bind(tag, "<Leave>", lambda _: self.c.config(cursor=""), add="+")

    def show(self, ev, text):
        self.c.delete("tip")
        t = self.c.create_text(ev.x + 14, ev.y + 14, text=text, fill=FG, font=SMALL, anchor="nw", tags="tip")
        x0, y0, x1, y1 = self.c.bbox(t)
        dx = min(0, self.c.winfo_width() - x1 - 8)
        dy = -(y1 - y0) - 32 if y1 > self.c.winfo_height() - 4 else 0
        self.c.move(t, dx, dy)
        x0, y0, x1, y1 = self.c.bbox(t)
        r = self.c.create_rectangle(x0 - 7, y0 - 5, x1 + 7, y1 + 5, fill=TIP_BG, outline=TRACK, tags="tip")
        self.c.tag_lower(r, t)


class Meter(tk.Frame):
    H = 14

    def __init__(self, parent, title):
        super().__init__(parent, bg=PANEL, padx=14, pady=12)
        top = tk.Frame(self, bg=PANEL)
        top.pack(fill="x")
        self.value = label(top, title, bg=PANEL, font=BOLD)
        self.value.pack(side="left")
        self.right = label(top, fg=MUTED, bg=PANEL)
        self.right.pack(side="right")
        self.canvas = tk.Canvas(self, height=self.H + 14, width=300, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="x", pady=(8, 2))
        self.detail = label(self, fg=MUTED, bg=PANEL, wraplength=320)
        self.detail.pack(fill="x")
        self.title = title
        self.args = None
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.bind("<Configure>", lambda ev: self.detail.config(wraplength=max(100, ev.width - 30)))

    def set(self, pct, proj, marks, right, detail):
        self.value.config(text=f"{self.title}  {pct:.0f}%")
        self.right.config(text=right)
        self.detail.config(text=detail)
        self.args = (pct, proj, marks)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        if w < 10 or not self.args:
            return
        pct, proj, marks = self.args
        x = lambda p: max(0.0, min(100.0, p)) / 100 * w
        c.create_rectangle(0, 0, w, self.H, fill=TRACK, width=0)
        if proj and proj > pct:
            c.create_rectangle(x(pct), 0, x(proj), self.H, fill=ACCENT_DIM, width=0)
        c.create_rectangle(0, 0, x(pct), self.H, fill=ACCENT, width=0)
        taken = []
        for p, color, text, dashed in marks:
            kw = {"dash": (2, 2)} if dashed else {}
            c.create_line(x(p), 0, x(p), self.H, fill=color, width=2, **kw)
            if text:
                anchor = "ne" if x(p) > w - 30 else ("nw" if x(p) < 30 else "n")
                t = c.create_text(x(p), self.H + 1, text=text, fill=color, font=TINY, anchor=anchor)
                x0, _, x1, _ = c.bbox(t)
                if any(x0 < b + 4 and x1 > a - 4 for a, b in taken):
                    c.delete(t)
                else:
                    taken.append((x0, x1))


class App:
    def __init__(self):
        self.logs = deque(maxlen=4)
        self.engine = core.Engine(log=self.log)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.engine.loop, args=(self.stop_event,), daemon=True)
        self.v_all = self.v = self.e = None
        self.prov = None
        self.agent_btns = {}
        self.sig = None
        self.loaded = 0
        self.tab = "Timeline"
        self.range = "24h"

        self.root = tk.Tk()
        self.root.title(f"Usage Harvester {core.VERSION}")
        self.root.configure(bg=BG, padx=18, pady=16)
        self.root.minsize(640, 0)
        self.root.protocol("WM_DELETE_WINDOW", self.root.iconify)
        self.icon = tk.PhotoImage(width=16, height=16)
        self.icon.put("#4cc38a", to=(2, 2, 14, 14))
        self.root.iconphoto(True, self.icon)
        self.style_ttk()

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x")
        label(head, "Harvester", font=TITLE).pack(side="left")
        self.agent_bar = tk.Frame(head, bg=BG)
        self.agent_bar.pack(side="left", padx=(14, 0))
        self.checked = label(head, fg=MUTED)
        self.checked.pack(side="right")
        self.pill = tk.Label(head, bg=BG, font=("Segoe UI Semibold", 9), padx=8, pady=1)
        self.pill.pack(side="right", padx=10)
        self.reason = label(self.root, fg=MUTED, wraplength=WIDTH - 40)
        self.reason.pack(fill="x", pady=(2, 12))

        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1, uniform="card")
        self.session = Meter(cards, "Session")
        self.session.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.week = Meter(cards, "Week")
        self.week.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        est = tk.Frame(self.root, bg=PANEL, padx=14, pady=10)
        est.pack(fill="x", pady=(10, 0))
        label(est, "Estimates", bg=PANEL, font=BOLD).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        est.columnconfigure(1, weight=1)
        self.est_rows = []
        for i in range(5):
            k = label(est, fg=MUTED, bg=PANEL)
            k.grid(row=i + 1, column=0, sticky="nw", padx=(0, 14))
            val = label(est, bg=PANEL, wraplength=WIDTH - 200)
            val.grid(row=i + 1, column=1, sticky="w")
            self.est_rows.append((k, val))

        tabs = tk.Frame(self.root, bg=BG)
        tabs.pack(fill="x", pady=(14, 0))
        self.tab_btns = {}
        for name in ("Timeline", "Week", "Runs", "Queue"):
            b = button(tabs, name, lambda n=name: self.show_tab(n))
            b.pack(side="left", padx=(0, 4))
            self.tab_btns[name] = b
        self.range_bar = tk.Frame(tabs, bg=BG)
        self.range_btns = {}
        for name in reversed(RANGES):
            b = tk.Button(self.range_bar, text=name, command=lambda n=name: self.set_range(n), relief="flat", bd=0,
                          padx=7, pady=2, font=TINY, cursor="hand2", activebackground=TRACK, activeforeground=FG)
            b.pack(side="right", padx=(3, 0))
            self.range_btns[name] = b

        self.body = tk.Frame(self.root, bg=PANEL, padx=10, pady=10)
        self.body.pack(fill="both", expand=True)
        self.pages = {
            "Timeline": self.build_timeline(), "Week": self.build_week(),
            "Runs": self.build_runs(), "Queue": self.build_queue(),
        }

        self.logbox = label(self.root, fg=MUTED, wraplength=WIDTH - 40)
        self.logbox.pack(fill="x", pady=(8, 10))
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x")
        self.pause_btn = button(bar, "Pause", self.toggle_pause)
        self.pause_btn.pack(side="left")
        button(bar, "Check", self.check_now).pack(side="left", padx=6)
        button(bar, "Settings", self.settings).pack(side="left")
        button(bar, "Folder", lambda: os.startfile(core.HOME)).pack(side="left", padx=6)
        button(bar, "Quit", self.quit).pack(side="right")
        self.show_tab(self.tab)
        self.set_range(self.range)

    def style_ttk(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, bordercolor=PANEL,
                     borderwidth=0, rowheight=22, font=SMALL)
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat", borderwidth=0,
                     font=SMALL)
        st.map("Treeview", background=[("selected", TRACK)], foreground=[("selected", FG)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.map("Treeview.Heading", background=[("active", PANEL)])
        st.configure("Vertical.TScrollbar", background=TRACK, troughcolor=PANEL, bordercolor=PANEL,
                     arrowcolor=MUTED, lightcolor=TRACK, darkcolor=TRACK, gripcount=0)
        st.map("Vertical.TScrollbar", background=[("disabled", PANEL), ("active", "#3a3a42"), ("!disabled", TRACK)],
               arrowcolor=[("disabled", TRACK)])

    # Pages

    def build_timeline(self):
        f = tk.Frame(self.body, bg=PANEL)
        self.tl = tk.Canvas(f, width=WIDTH - 56, height=200, bg=PANEL, highlightthickness=0)
        self.tl.pack(fill="both", expand=True)
        self.tl_tip = Tooltip(self.tl)
        self.tl.bind("<Configure>", lambda _: self.draw_timeline())
        return f

    def build_week(self):
        f = tk.Frame(self.body, bg=PANEL)
        self.wk = tk.Canvas(f, width=WIDTH - 56, height=280, bg=PANEL, highlightthickness=0)
        self.wk.pack(fill="both", expand=True)
        self.wk_tip = Tooltip(self.wk)
        self.wk_summary = label(f, fg=MUTED, bg=PANEL, wraplength=WIDTH - 60)
        self.wk_summary.pack(fill="x", pady=(6, 0))
        self.wk.bind("<Configure>", lambda _: self.draw_week())
        return f

    def tree(self, parent, cols, height=10):
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        tv = ttk.Treeview(wrap, columns=[c for c, *_ in cols], show="headings", height=height, selectmode="browse")
        for c, title, width, anchor in cols:
            tv.heading(c, text=title, anchor=anchor)
            tv.column(c, width=width, anchor=anchor, stretch=c == "prompt")
        for status, color in STATUS_COLOR.items():
            tv.tag_configure(status, foreground=color)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    def build_runs(self):
        f = tk.Frame(self.body, bg=PANEL)
        self.runs_tv = self.tree(f, [("task", "task", 50, "e"), ("run", "run", 36, "e"),
                                     ("started", "started", 80, "w"), ("length", "length", 60, "e"),
                                     ("status", "status", 80, "w"), ("turns", "turns", 50, "e"),
                                     ("week", "≈ week", 60, "e"), ("prompt", "prompt", 300, "w")])
        self.runs_tv.bind("<Double-1>", lambda _: self.open_selected(self.runs_tv, by_tag=True))
        self.runs_total = label(f, fg=MUTED, bg=PANEL)
        self.runs_total.pack(fill="x", pady=(6, 0))
        return f

    def build_queue(self):
        f = tk.Frame(self.body, bg=PANEL)
        self.queue_tv = self.tree(f, [("id", "#", 40, "e"), ("status", "status", 80, "w"), ("prio", "prio", 40, "e"),
                                      ("agent", "agent", 56, "w"), ("folder", "folder", 100, "w"),
                                      ("prompt", "prompt", 300, "w"), ("note", "note", 130, "w")])
        self.queue_tv.bind("<Double-1>", lambda _: self.open_selected(self.queue_tv))
        bar = tk.Frame(f, bg=PANEL)
        bar.pack(fill="x", pady=(8, 0))
        for text, fn in (("Show", self.q_show), ("Move to top", self.q_top), ("Retry", self.q_retry),
                         ("Remove", self.q_remove)):
            button(bar, text, fn, bg=TRACK).pack(side="left", padx=(0, 6))
        label(bar, "double-click a row for its prompt and result", fg=MUTED, bg=PANEL).pack(side="right")
        return f

    def show_tab(self, name):
        self.tab = name
        for n, page in self.pages.items():
            page.pack_forget()
            self.tab_btns[n].config(bg=TRACK if n == name else BG, fg=FG if n == name else MUTED)
        self.pages[name].pack(fill="both", expand=True)
        if name == "Timeline":
            self.range_bar.pack(side="right")
        else:
            self.range_bar.pack_forget()
        self.draw_tab()

    def set_range(self, name):
        self.range = name
        for n, b in self.range_btns.items():
            b.config(bg=TRACK if n == name else BG, fg=FG if n == name else MUTED)
        self.draw_timeline()

    def draw_tab(self):
        {"Timeline": self.draw_timeline, "Week": self.draw_week,
         "Runs": self.fill_runs, "Queue": self.fill_queue}[self.tab]()

    # Data

    def reload(self):
        self.v_all = views.load()
        provs = self.v_all["providers"]
        if self.prov not in provs:
            self.prov = provs[0]
        if list(self.agent_btns) != provs:
            self.build_agents(provs)
        self.v = views.select(self.v_all, self.prov)
        self.e = views.estimate(self.v)
        self.loaded = time.time()
        self.update_meters()
        self.update_estimates()
        self.draw_tab()

    def build_agents(self, provs):
        for b in self.agent_btns.values():
            b.destroy()
        self.agent_btns = {}
        for name in provs:
            b = tk.Button(self.agent_bar, command=lambda n=name: self.set_agent(n), relief="flat", bd=0, padx=10,
                          pady=3, font=SMALL, cursor="hand2", activebackground=TRACK, activeforeground=FG)
            b.pack(side="left", padx=(0, 4))
            self.agent_btns[name] = b
        self.paint_agents()

    def paint_agents(self):
        for name, b in self.agent_btns.items():
            mode = self.mode(name)
            on = name == self.prov
            b.config(text=f"{core.PROVIDERS[name].label} · {mode}", bg=TRACK if on else BG,
                     fg=FG if on else MODE_COLOR.get(mode, MUTED))

    def set_agent(self, name):
        self.prov = name
        self.sig = None
        self.refresh_header()
        self.reload()

    def pstate(self, name=None):
        return (self.engine.state.get("providers") or {}).get(name or self.prov) or {}

    def mode(self, name=None):
        if core.path("paused").exists():
            return "paused"
        return (self.pstate(name).get("decision") or {}).get("mode", "starting")

    def update_meters(self):
        e, v = self.e, self.v
        if "S" not in e:
            self.session.set(0, None, [], "", "waiting for the first usage check")
            self.week.set(0, None, [], "", "")
            return
        cfg = v["cfg"]
        if e["Ts"]:
            parts = []
            if "session_proj" in e:
                parts.append(f"lighter: your pace → {e['session_proj']:.0f}%")
            parts.append(f"tasks fill to the cap in {core.fmt_dur(e['fill_s'])}")
            window = f"{core.fmt_clock(max(e['burn_at'], v['now']))}–{core.fmt_clock(e['Ts'])}"
            parts.append(f"burn window {window}" if self.mode() in ("harvest", "burn")
                         else f"if harvesting, would burn {window}")
            self.session.set(e["S"], e.get("session_proj"), [(e["cap"], FG, "cap", False)],
                             f"resets {core.fmt_clock(e['Ts'])} · {core.fmt_dur(e['session_left'])} left",
                             " · ".join(parts))
        else:
            self.session.set(0, None, [], "no session open",
                             "the harvester opens one when it needs to" if cfg["start_window_ping"]
                             else "the next one starts with your next message")
        if e["Tw"]:
            reserve = 100 - cfg["weekly_reserve_pct"]
            marks = [(e["even"], MUTED, "even pace", True),
                     (reserve, FG, "reserve", False)]
            self.week.set(e["W"], e["proj_end"], marks,
                          f"resets {core.fmt_clock(e['Tw'])} · {core.fmt_dur(e['week_left'])} left",
                          f"lighter: your pace {e['pace'] * 86400:.1f}%/day → {e['proj_end']:.0f}% by reset")
        else:
            self.week.set(0, None, [], "no weekly window", "")

    def update_estimates(self):
        rows = views.estimate_rows(self.e)
        for i, (k, val) in enumerate(self.est_rows):
            if i < len(rows):
                k.config(text=rows[i][0])
                val.config(text=" · ".join(rows[i][1]))
            else:
                k.config(text="" if rows or i else "waiting for the first usage check")
                val.config(text="")

    # Timeline

    def bounds(self):
        now = self.v["now"]
        r = RANGES[self.range]
        if r is None and self.e.get("Tw"):
            return self.e["Tw"] - views.WEEK_S, self.e["Tw"]
        back, ahead = r or (168, 0)
        return now - back * 3600, now + ahead * 3600

    def draw_timeline(self):
        c = self.tl
        if not self.v or self.tab != "Timeline":
            return
        c.delete("all")
        W = c.winfo_width()
        if W < 200:
            return
        v, e = self.v, self.e
        now, cfg = v["now"], v["cfg"]
        start, end = self.bounds()
        LW, TOP, ROW = 128, 22, 24
        x0, x1 = LW, W - 8
        X = lambda t: x0 + (min(max(t, start), end) - start) / (end - start) * (x1 - x0)
        gap, poll = 3 * cfg["poll_seconds"] + 60, cfg["poll_seconds"]
        prompts = {t["id"]: t["prompt"] for t in v["tasks"]}
        per_min = e["rate"] * e["c"] / 100

        runs = [r for r in v["runs"] if (r["end"] or now) >= start and r["start"] < end]
        by_task = {}
        for r in runs:
            by_task.setdefault(r["task"], []).append(r)
        recent = sorted(by_task, key=lambda tid: max((r["end"] or now) for r in by_task[tid]))[-14:]
        shown = sorted(recent, key=lambda tid: min(r["start"] for r in by_task[tid]))
        names = ["Session %", "Harvester", "You active"] + [f"#{tid} {views.short(prompts.get(tid, '(removed)'), 18)}"
                                                           for tid in shown]
        bottom = TOP + len(names) * ROW
        row_y = lambda i: (TOP + i * ROW, TOP + (i + 1) * ROW)

        now_x = X(now) if start <= now < end else None
        for t, text in ticks(start, end, x1 - x0):
            c.create_line(X(t), TOP - 3, X(t), bottom, fill=GRID)
            if now_x is None or not -22 < X(t) - now_x < 40:
                c.create_text(X(t), TOP - 6, text=text, fill=MUTED, font=TINY, anchor="s")
        for i, name in enumerate(names):
            y0, y1 = row_y(i)
            c.create_text(4, (y0 + y1) / 2, text=name, fill=MUTED if i < 3 else FG, font=SMALL, anchor="w")
            c.create_line(x0, y1, x1, y1, fill=GRID)
        c.create_line(x0, TOP, x1, TOP, fill=GRID)

        hist = [h for h in v["hist"] if start - gap <= h["t"] < end]
        y0, y1 = row_y(0)
        top, base = y0 + 3, y1 - 2
        Y = lambda pct: base - min(100, pct) / 100 * (base - top)
        segs = []
        for i, h in enumerate(hist):
            if not (h.get("Ts") and h["Ts"] > h["t"]):
                continue
            nxt = hist[i + 1]["t"] if i + 1 < len(hist) else None
            stop = nxt if nxt is not None and nxt - h["t"] <= gap else min(h["t"] + poll, now)
            if segs and segs[-1]["Ts"] == h["Ts"] and abs(segs[-1]["pts"][-1][0] - h["t"]) < 1:
                segs[-1]["pts"].append((stop, h["S"]))
                segs[-1]["first"].append((h["t"], h["S"]))
            else:
                segs.append({"Ts": h["Ts"], "pts": [(stop, h["S"])], "first": [(h["t"], h["S"])]})
        for sg in segs:
            pts = []
            for (a, sa), (b, _) in zip(sg["first"], sg["pts"]):
                pts += [(X(a), Y(sa)), (X(b), Y(sa))]
            if pts[-1][0] - pts[0][0] < 1:
                continue
            c.create_polygon([(pts[0][0], base)] + pts + [(pts[-1][0], base)], fill=ACCENT_DIM, outline="")
            c.create_line(pts, fill=ACCENT, width=1.5)
            if pts[-1][0] - pts[0][0] > 30:
                c.create_text(pts[-1][0] - 2, top - 1, text=f"{sg['first'][-1][1]:.0f}%", fill=ACCENT,
                              font=TINY, anchor="ne")
        if e.get("Ts") and start <= now < end:
            c.create_line(X(now), Y(e["S"]), X(e["Ts"]), Y(e["S"]), fill=ACCENT, dash=(3, 3))
            c.create_line(X(e["Ts"]), top, X(e["Ts"]), base, fill=ACCENT)
            tag = "sess_reset"
            c.create_rectangle(X(e["Ts"]) - 4, top, X(e["Ts"]) + 4, base, fill="", outline="", tags=tag)
            self.tl_tip.bind(tag, f"session resets {core.fmt_clock(e['Ts'])}")

        y0, y1 = row_y(1)
        mid = (y0 + y1) / 2
        for a, b, m in intervals(hist, lambda h: h.get("m"), gap, poll, now):
            color, h = MODE_BAND.get(m, (MUTED, 4))
            tag = f"mode{a}"
            c.create_rectangle(X(a), mid - h / 2, max(X(b), X(a) + 1), mid + h / 2, fill=color, width=0, tags=tag)
            self.tl_tip.bind(tag, f"{m} · {views.when(a)}–{views.when(b)} · {core.fmt_dur(b - a)}")
        if self.mode() in ("harvest", "burn") and e.get("burn_at") and e.get("Ts") and start <= now < end:
            a = max(e["burn_at"], now)
            c.create_rectangle(X(a), mid - 8, X(e["Ts"]), mid + 8, outline=MODE_COLOR["burn"], dash=(3, 2),
                               tags="planned")
            self.tl_tip.bind("planned", f"planned burn {core.fmt_clock(a)}–{core.fmt_clock(e['Ts'])}")
        for key, fill, text in (("eta_idle", "", f"earliest harvest start, if you stop using {e['label']}"),
                                ("eta_pace", AMBER, "harvest starts at your pace")):
            t = e.get(key)
            if t and now < t < end:
                x = X(t)
                c.create_polygon(x, mid - 7, x + 6, mid, x, mid + 7, x - 6, mid, fill=fill, outline=AMBER,
                                 width=1.5, tags=key)
                self.tl_tip.bind(key, f"{text}\n{core.fmt_clock(t)}")

        y0, y1 = row_y(2)
        mid = (y0 + y1) / 2
        for a, b, active in intervals(hist, lambda h: bool(h.get("a")), gap, poll, now):
            if active:
                c.create_rectangle(X(a), mid - 4, max(X(b), X(a) + 2), mid + 4, fill=YOU, width=0)

        for i, tid in enumerate(shown):
            y0, y1 = row_y(3 + i)
            for j, r in enumerate(by_task[tid]):
                a, b = r["start"], r["end"] or now
                color = STATUS_COLOR.get(r["status"], MUTED)
                tag = f"run{tid}_{j}"
                c.create_rectangle(X(a), y0 + 5, max(X(b), X(a) + 3), y1 - 5, fill=color, width=0, tags=tag)
                if X(b) - X(a) > 18:
                    c.create_text(X(b) - 4, (y0 + y1) / 2, text=views.MARK.get(r["status"], ""), fill=BG,
                                  font=TINY, anchor="e", tags=tag)
                turns = r.get("turns")
                info = (f"#{tid} run {r.get('run') or '-'} · {r['status']}\n"
                        f"{views.when(a)}–{'now' if not r['end'] else views.when(b)} · {core.fmt_dur(b - a)}"
                        + (f" · {turns} turns" if turns is not None else "")
                        + f"\n≈ {(b - a) / 60 * per_min:.1f}% of week\n{views.short(prompts.get(tid, '(removed)'), 70)}")
                self.tl_tip.bind(tag, info, click=lambda t=tid: self.show_task(t))

        if start <= now < end:
            c.create_line(X(now), TOP - 3, X(now), bottom, fill=AMBER, width=1.5)
            c.create_text(X(now) + 3, TOP - 6, text="now", fill=AMBER, font=TINY, anchor="sw")
        if not runs:
            c.create_text(x0 + 4, bottom + 4, text="no backlog runs in this range", fill=MUTED, font=SMALL, anchor="nw")
        legend_end = self.legend(c, bottom + 24, [
            ("band", MODE_BAND["watching"][0], "watching"), ("band", AMBER, "harvest, waiting"),
            ("band", MODE_COLOR["burn"], "burning"), ("outline", MODE_COLOR["burn"], "planned burn"),
            ("diamond", AMBER, "harvest starts"), ("band", YOU, "you active"),
            ("band", STATUS_COLOR["done"], "done"), ("band", STATUS_COLOR["paused"], "paused"),
            ("band", STATUS_COLOR["failed"], "failed"), ("band", STATUS_COLOR["running"], "running")])
        if int(c.cget("height")) != legend_end + 4:
            c.config(height=legend_end + 4)

    def legend(self, c, y, items):
        x, W = 4, c.winfo_width()
        for kind, color, text in items:
            if x + 16 + len(text) * 6 > W:
                x, y = 4, y + 18
            if kind == "outline":
                c.create_rectangle(x, y - 4, x + 12, y + 4, outline=color, dash=(2, 2))
            elif kind == "diamond":
                c.create_polygon(x + 6, y - 5, x + 11, y, x + 6, y + 5, x + 1, y, fill=color, outline="")
            else:
                c.create_rectangle(x, y - 4, x + 12, y + 4, fill=color, width=0)
            t = c.create_text(x + 16, y, text=text, fill=MUTED, font=TINY, anchor="w")
            x = c.bbox(t)[2] + 12
        return y + 10

    # Week

    def draw_week(self):
        c = self.wk
        if not self.v or self.tab != "Week":
            return
        c.delete("all")
        W, H = c.winfo_width(), 280
        if W < 200:
            return
        v, e = self.v, self.e
        Tw = e.get("Tw")
        if not Tw:
            c.create_text(W / 2, H / 2, text="no weekly data yet", fill=MUTED, font=FONT)
            self.wk_summary.config(text="")
            return
        now, start = v["now"], Tw - views.WEEK_S
        x0, x1, y0, y1 = 40, W - 84, 12, H - 50
        X = lambda t: x0 + (t - start) / views.WEEK_S * (x1 - x0)
        Y = lambda p: y1 - min(100, max(0, p)) / 100 * (y1 - y0)

        for p in (0, 25, 50, 75, 100):
            c.create_line(x0, Y(p), x1, Y(p), fill=GRID)
            c.create_text(x0 - 6, Y(p), text=f"{p}%", fill=MUTED, font=TINY, anchor="e")
        day = datetime.fromtimestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() < Tw:
            ts = day.timestamp()
            if ts > start:
                c.create_line(X(ts), y0, X(ts), y1, fill=GRID)
            mid = ts + 43200
            if start < mid < Tw:
                c.create_text(X(mid), y1 + 12, text=day.strftime("%a %d"), fill=MUTED, font=TINY)
            day += timedelta(days=1)
        c.create_line(X(start), Y(0), X(Tw), Y(100), fill=MUTED, dash=(4, 4))
        reserve = 100 - v["cfg"]["weekly_reserve_pct"]
        c.create_line(x0, Y(reserve), x1, Y(reserve), fill=TRACK, dash=(2, 4))

        hist = [h for h in v["hist"] if h.get("Tw") and abs(h["Tw"] - Tw) < 3600 and start <= h["t"] <= now]
        pts = [(h["t"], h["W"], h.get("m")) for h in hist] + [(now, e["W"], self.mode())]
        if len(pts) > 1:
            xy = [(X(t), Y(w)) for t, w, _ in pts]
            c.create_polygon([(xy[0][0], Y(0))] + xy + [(xy[-1][0], Y(0))], fill=ACCENT_DIM, outline="")
            c.create_line(xy, fill=ACCENT, width=2)
            for (a, b), (_, _, m) in zip(zip(xy, xy[1:]), pts):
                if m == "burn":
                    c.create_line(a, b, fill=MODE_COLOR["burn"], width=3)
        nx, ny = X(now), Y(e["W"])
        c.create_line(nx, y0, nx, y1, fill=AMBER)
        c.create_text(nx + 3, y0, text="now", fill=AMBER, font=TINY, anchor="nw")
        px, py = X(Tw), Y(e["proj_end"])
        c.create_line(nx, ny, px, py, fill=AMBER, dash=(5, 3), width=2)
        c.create_oval(nx - 4, ny - 4, nx + 4, ny + 4, fill=ACCENT, outline=PANEL, tags="now_pt")
        self.wk_tip.bind("now_pt", f"used {e['W']:.0f}% · even pace {e['even']:.0f}%")
        c.create_text(px + 8, py, text=f"{e['proj_end']:.0f}% projected", fill=AMBER, font=TINY, anchor="w")
        if e["unused"] >= 1:
            c.create_line(px + 4, Y(100), px + 4, py - 6, fill=RED, width=2)
            c.create_text(px + 8, (Y(100) + py) / 2, text=f"{e['unused']:.0f}% unused", fill=RED, font=TINY,
                          anchor="w")
        self.legend(c, H - 12, [("band", ACCENT, "used"), ("band", MODE_COLOR["burn"], "harvester burning"),
                               ("band", AMBER, "your pace, projected"), ("band", MUTED, "even pace (dashed)")])
        self.wk_summary.config(text=(
            f"used {e['W']:.0f}% · even pace {e['even']:.0f}% · your pace {e['pace'] * 86400:.1f}%/day → "
            f"{e['proj_end']:.0f}% at reset · would go unused {e['unused']:.0f}% ≈ "
            f"{e['unused_sessions']:.1f} sessions ≈ {core.fmt_dur(e['unused_task_s'])} of task time"))

    # Tables

    def refill(self, tv, rows):
        sel = tv.selection()
        tv.delete(*tv.get_children())
        for iid, values, tag in rows:
            tv.insert("", "end", iid=iid, values=values, tags=(tag,))
        keep = [i for i in sel if tv.exists(i)]
        if keep:
            tv.selection_set(keep)

    def fill_runs(self):
        if not self.v:
            return
        v, e = self.v, self.e
        now = v["now"]
        prompts = {t["id"]: t["prompt"] for t in v["tasks"]}
        per_min = e["rate"] * e["c"] / 100
        rows = []
        for i, r in enumerate(sorted(v["runs"], key=lambda r: r["start"], reverse=True)[:300]):
            secs = (r["end"] or now) - r["start"]
            turns = r.get("turns")
            rows.append((f"r{i}_{r['task']}", (f"#{r['task']}", r.get("run") or "-", views.when(r["start"]),
                                                core.fmt_dur(secs), f"{views.MARK.get(r['status'], '')} {r['status']}",
                                                "-" if turns is None else turns, f"{secs / 60 * per_min:.1f}%",
                                                views.short(prompts.get(r["task"], "(removed)"), 120)), r["status"]))
        self.refill(self.runs_tv, rows)
        parts = []
        for name, since in (("this week", e.get("week_start")), ("all time", 0)):
            if since is not None:
                h = views.harvested(v, since, e)
                parts.append(f"{name}: {h['runs']} runs · {core.fmt_dur(h['secs'])} · ≈ {h['week_pct']:.1f}% of a week")
        self.runs_total.config(text="   ".join(parts) + "   · double-click a run for its task")

    def fill_queue(self):
        if not self.v:
            return
        tasks = self.v["tasks"]
        order = ([t for t in tasks if t["status"] == "running"] + core.queue_order(tasks)
                 + [t for t in tasks if t["status"] == "failed"]
                 + sorted((t for t in tasks if t["status"] == "done"), key=lambda t: -t.get("updated", 0)))
        self.refill(self.queue_tv, [
            (str(t["id"]), (t["id"], f"{views.MARK.get(t['status'], '')} {t['status']}", t["priority"] or "",
                            core.route(t), Path(t["dir"]).name, views.short(t["prompt"], 120),
                            views.short(t.get("note") or "", 60)),
             t["status"]) for t in order])

    def selected_task(self, tv=None, by_tag=False):
        tv = tv or self.queue_tv
        sel = tv.selection()
        if not sel:
            return None
        return int(sel[0].rsplit("_", 1)[1]) if by_tag else int(sel[0])

    def open_selected(self, tv, by_tag=False):
        tid = self.selected_task(tv, by_tag)
        if tid is not None:
            self.show_task(tid)

    def edit_selected(self, fn):
        tid = self.selected_task()
        if tid is None:
            return
        def apply(tasks):
            for t in tasks:
                if t["id"] == tid:
                    fn(tasks, t)
                    return
        core.update_tasks(apply)
        self.sig = None

    def q_show(self):
        self.open_selected(self.queue_tv)

    def q_top(self):
        self.edit_selected(lambda tasks, t: t.update(priority=max(x["priority"] for x in tasks) + 1))

    def q_retry(self):
        self.edit_selected(lambda tasks, t: core.reset_task(t))

    def q_remove(self):
        tid = self.selected_task()
        if tid is not None and messagebox.askyesno("Remove task", f"Remove task #{tid} from the backlog?",
                                                   parent=self.root):
            self.edit_selected(lambda tasks, t: tasks.remove(t))

    def show_task(self, tid):
        t = next((t for t in core.load_tasks() if t["id"] == tid), None)
        if not t:
            messagebox.showinfo("Task", f"Task #{tid} is no longer in the backlog.", parent=self.root)
            return
        win = tk.Toplevel(self.root, bg=BG, padx=12, pady=12)
        win.title(f"Task #{tid}")
        txt = tk.Text(win, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", font=MONO, wrap="word",
                      width=96, height=32, padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        meta = [f"#{t['id']} · {t['status']} · runs {t['runs']} · {core.route(t)} · {t['dir']}"]
        meta += [f"{k}: {t[k]}" for k in ("model", "permission_mode", "tools", "note", "session_id") if t.get(k)]
        txt.insert("end", "\n".join(meta) + "\n\n" + t["prompt"])
        res = core.path("results") / f"{t['id']}.md"
        if res.exists():
            txt.insert("end", "\n\n--- result ---\n\n" + res.read_text(encoding="utf-8"))
        txt.config(state="disabled")

    # Controls

    def log(self, msg):
        self.logs.append(f"{time.strftime('%H:%M')}  {msg}")

    def toggle_pause(self):
        p = core.path("paused")
        if p.exists():
            p.unlink()
        else:
            p.touch()
        self.check_now()

    def check_now(self):
        self.engine.kick.set()

    def quit(self):
        self.root.title("Usage Harvester · stopping tasks…")
        self.stop_event.set()
        self.root.after(100, self._wait_quit)

    def _wait_quit(self):
        if self.thread.is_alive():
            self.root.after(200, self._wait_quit)
        else:
            self.root.destroy()

    def settings(self):
        win = tk.Toplevel(self.root, bg=BG, padx=16, pady=14)
        win.title("Settings")
        win.resizable(False, False)
        cfg = core.load_config()
        fields = {}
        for row, key in enumerate(core.DEFAULTS):
            tk.Label(win, text=key, bg=BG, fg=FG, font=SMALL, anchor="w").grid(row=row, column=0, sticky="w")
            if isinstance(core.DEFAULTS[key], bool):
                var = tk.BooleanVar(value=cfg[key])
                tk.Checkbutton(win, variable=var, bg=BG, activebackground=BG, selectcolor=PANEL,
                               bd=0).grid(row=row, column=1, sticky="w", padx=8)
            else:
                var = tk.StringVar(value=str(cfg[key]))
                tk.Entry(win, textvariable=var, width=14, bg=PANEL, fg=FG, insertbackground=FG,
                         relief="flat", font=SMALL).grid(row=row, column=1, sticky="w", padx=8, pady=2)
            tk.Label(win, text=core.CONFIG_HELP[key], bg=BG, fg=MUTED, font=SMALL,
                     anchor="w").grid(row=row, column=2, sticky="w")
            fields[key] = var
        status = tk.Label(win, bg=BG, fg=RED, font=SMALL)
        status.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(10, 0))

        def save():
            new = dict(cfg)
            for key, var in fields.items():
                try:
                    new[key] = core.coerce(key, var.get())
                except ValueError:
                    status.config(text=f"bad value for {key}")
                    return
            core.save_config(new)
            self.check_now()
            win.destroy()
        button(win, "Save", save).grid(row=len(fields), column=2, sticky="e", pady=(10, 0))

    def refresh_header(self):
        s, ps = self.engine.state, self.pstate()
        mode = self.mode()
        self.pill.config(text=mode, fg=BG, bg=MODE_COLOR.get(mode, MUTED))
        self.pause_btn.config(text="Resume" if mode == "paused" else "Pause")
        reason = "paused by you" if mode == "paused" else (ps.get("decision") or {}).get("reason", "checking usage…")
        if ps.get("user_active") and mode != "paused":
            reason += " · you're active"
        error = ps.get("error") or s.get("error")
        self.reason.config(text=error or reason, fg=RED if error else MUTED)
        self.paint_agents()
        return s.get("updated")

    def refresh(self):
        updated = self.refresh_header()
        self.checked.config(text=f"checked {views.ago(time.time() - updated)} ago" if updated else "first check…")
        self.logbox.config(text="\n".join(self.logs))
        try:
            sig = (updated, core.path("backlog.json").stat().st_mtime if core.path("backlog.json").exists() else 0,
                   core.path("paused").exists())
        except OSError:
            sig = None
        if sig != self.sig or time.time() - self.loaded > 30:
            self.sig = sig
            self.reload()
        self.root.after(1000, self.refresh)

    def run(self):
        self.thread.start()
        self.refresh()
        self.root.mainloop()


if __name__ == "__main__":
    _lock = single_instance()
    App().run()

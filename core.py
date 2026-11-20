import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from _version import VERSION
except ImportError:
    VERSION = "dev"

HOME = Path(os.environ.get("HV_HOME") or Path.home() / ".usage-harvester")
CLAUDE_DIR = Path.home() / ".claude"
WINDOW_S = 5 * 3600
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RESUME_PROMPT = "Continue the task where you left off. If it is already complete, summarize the result."

DEFAULTS = {
    "poll_seconds": 120,
    "max_parallel": 1,
    "window_weekly_pct": 10.0,
    "burn_pct_per_min": 1.0,
    "slack_margin_windows": 1.0,
    "tail_safety": 1.5,
    "min_tail_minutes": 20,
    "session_reserve_pct": 3,
    "weekly_reserve_pct": 2,
    "final_minutes": 10,
    "idle_minutes": 5,
    "start_window_ping": True,
    "ping_model": "haiku",
    "default_model": "",
    "default_permission_mode": "acceptEdits",
    "claude_cmd": "claude",
}

CONFIG_HELP = {
    "poll_seconds": "seconds between usage checks",
    "max_parallel": "backlog tasks running at once",
    "window_weekly_pct": "starting guess: weekly % one full session is worth",
    "burn_pct_per_min": "starting guess: session % per minute per task",
    "slack_margin_windows": "harvest once spare sessions drop to this",
    "tail_safety": "start burning this many times earlier than needed",
    "min_tail_minutes": "never start burning earlier than this before reset",
    "session_reserve_pct": "session % left for you until the final minutes",
    "weekly_reserve_pct": "weekly % left for you until the last session",
    "final_minutes": "minutes before reset when the reserve is released",
    "idle_minutes": "you count as active this long after a Claude message",
    "start_window_ping": "send a tiny message to open a session when none is running",
    "ping_model": "model for that message",
    "default_model": "model for tasks without one (blank = Claude Code default)",
    "default_permission_mode": "permission mode for tasks without one",
    "claude_cmd": "Claude Code executable",
}


def path(name):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name


def read_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p, data):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, p)


def append_jsonl(name, rec):
    with open(path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def read_jsonl(name, since=0, key="t"):
    try:
        lines = path(name).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get(key) or 0) >= since:
            out.append(rec)
    return out


def trim_jsonl(name, days, key="t", max_bytes=1_000_000):
    p = path(name)
    if p.exists() and p.stat().st_size > max_bytes:
        keep = read_jsonl(name, time.time() - days * 86400, key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep), encoding="utf-8")
        os.replace(tmp, p)


class FileLock:
    def __init__(self, p):
        self.p = Path(p)

    def __enter__(self):
        deadline = time.time() + 10
        while True:
            try:
                self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > 30:
                        self.p.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"{self.p} is locked")
                time.sleep(0.05)

    def __exit__(self, *_):
        os.close(self.fd)
        self.p.unlink(missing_ok=True)


def load_config():
    return {**DEFAULTS, **read_json(path("config.json"), {})}


def save_config(cfg):
    write_json(path("config.json"), {k: cfg[k] for k in DEFAULTS if k in cfg})


def coerce(key, raw):
    kind = type(DEFAULTS[key])
    if kind is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return kind(raw)


# Backlog

def load_tasks():
    return read_json(path("backlog.json"), [])


def update_tasks(fn):
    with FileLock(path("backlog.lock")):
        tasks = load_tasks()
        out = fn(tasks)
        write_json(path("backlog.json"), tasks)
        return out


def add_task(prompt, cwd, model="", permission_mode="", tools="", priority=0):
    def fn(tasks):
        task = {
            "id": max((t["id"] for t in tasks), default=0) + 1,
            "prompt": prompt,
            "dir": str(cwd),
            "model": model,
            "permission_mode": permission_mode,
            "tools": tools,
            "priority": priority,
            "status": "queued",
            "session_id": None,
            "runs": 0,
            "pid": None,
            "note": "",
            "created": time.time(),
            "updated": time.time(),
        }
        tasks.append(task)
        return task
    return update_tasks(fn)


def queue_order(tasks):
    pending = [t for t in tasks if t["status"] in ("paused", "queued")]
    return sorted(pending, key=lambda t: (t["status"] != "paused", -t["priority"], t["id"]))


# Usage

class UsageError(Exception):
    pass


def parse_ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def parse_usage(d, source):
    def window(key):
        w = d.get(key) or {}
        pct = w.get("utilization", w.get("used_percentage"))
        return (None if pct is None else float(pct)), parse_ts(w.get("resets_at"))
    s, s_reset = window("five_hour")
    w, w_reset = window("seven_day")
    return {"session_pct": s or 0.0, "session_reset": s_reset,
            "weekly_pct": w or 0.0, "weekly_reset": w_reset,
            "source": source, "at": time.time()}


USAGE_LINE = re.compile(
    r"Current (session|week \(all models\)):\s*(\d+(?:\.\d+)?)% used"
    r"(?:[^\n]*?resets\s+([^(\n]+?)\s*(?:\(([^)\n]+)\))?\s*$)?", re.M)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def parse_reset(text, tz):
    m = re.match(r"(?:([A-Za-z]{3})\w*\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text.strip(), re.I)
    if not m:
        return None
    mon, day, hour, minute, ampm = m.groups()
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    now = datetime.now(zone)
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    when = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if mon:
        when = when.replace(month=MONTHS.index(mon.lower()) + 1, day=int(day))
        if when < now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    elif when < now:
        when += timedelta(days=1)
    return when.timestamp()


def parse_usage_text(text):
    found = {kind: (float(pct), parse_reset(reset, tz) if reset else None)
             for kind, pct, reset, tz in USAGE_LINE.findall(text)}
    if "session" not in found or "week (all models)" not in found:
        raise UsageError("unexpected /usage output: " + " ".join(text.split())[:160])
    (s, s_reset), (w, w_reset) = found["session"], found["week (all models)"]
    return {"session_pct": s, "session_reset": s_reset, "weekly_pct": w, "weekly_reset": w_reset,
            "source": "cli", "at": time.time()}


HOST_ENV = re.compile(r"^(CLAUDECODE|CLAUDE_(?!CONFIG_DIR$).*|ANTHROPIC_BASE_URL|USE_LOCAL_OAUTH|USE_STAGING_OAUTH)$")


def clean_env():
    """Drops variables a parent Claude session sets, so children use the saved CLI login."""
    return {k: v for k, v in os.environ.items() if not HOST_ENV.match(k)}


def fetch_cli_usage():
    # Print-mode /usage runs locally: no model call, and it refreshes the CLI login.
    try:
        r = subprocess.run([claude_exe(load_config()), "-p", "--output-format", "json",
                            "--no-session-persistence"], input="/usage", cwd=str(path("")),
                           capture_output=True, text=True, encoding="utf-8", timeout=90,
                           env=clean_env(), creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UsageError(f"could not run claude: {e}")
    try:
        text = json.loads(r.stdout.strip().splitlines()[-1]).get("result") or ""
    except (json.JSONDecodeError, IndexError):
        raise UsageError("claude /usage failed: " + " ".join((r.stderr or r.stdout).split())[:160])
    return parse_usage_text(text)


def save_snapshot(statusline_input):
    rl = statusline_input.get("rate_limits")
    if isinstance(rl, dict) and rl:
        write_json(path("snapshot.json"), parse_usage(rl, "statusline"))


def read_snapshot(max_age=1800):
    u = read_json(path("snapshot.json"), None)
    if not u or time.time() - u["at"] > max_age:
        return None
    now = time.time()
    if u["session_reset"] and u["session_reset"] <= now:
        u["session_pct"], u["session_reset"] = 0.0, None
    if u["weekly_reset"] and u["weekly_reset"] <= now:
        u["weekly_pct"], u["weekly_reset"] = 0.0, None
    return u


def get_usage():
    try:
        return fetch_cli_usage()
    except UsageError as e:
        snap = read_snapshot()
        if snap:
            return snap
        raise e


# Planner

def plan(now, u, cfg, learned, n_running, user_active, paused):
    c = learned.get("window_weekly_pct") or cfg["window_weekly_pct"]
    rate = learned.get("burn_pct_per_min") or cfg["burn_pct_per_min"]
    S, Ts = u["session_pct"], u["session_reset"]
    W, Tw = u["weekly_pct"], u["weekly_reset"]
    window_open = Ts is not None and Ts > now
    if not window_open:
        S = 0.0
    d = {"mode": "watching", "action": "none", "reason": "", "slack_windows": None,
         "burn_at": None, "c": c, "rate": rate}

    def stop_or_none(mode, reason):
        d.update(mode=mode, reason=reason, action="stop" if n_running else "none")
        return d

    if paused:
        return stop_or_none("paused", "paused by you")
    if Tw is None or Tw <= now:
        return stop_or_none("watching", "weekly window has not started")

    remaining = max(0.0, 100.0 - W)
    current = c * (100.0 - S) / 100.0 if window_open else 0.0
    future = max(0.0, Tw - (Ts if window_open else now)) / WINDOW_S
    slack = (current + c * future - remaining) / c
    d["slack_windows"] = round(slack, 2)

    if remaining <= cfg["weekly_reserve_pct"] and Tw - now > WINDOW_S:
        return stop_or_none("watching", "weekly usage nearly spent")
    if slack > cfg["slack_margin_windows"]:
        return stop_or_none("watching", f"{slack:.1f} spare sessions before weekly reset")

    d["mode"] = "harvest"
    if not window_open:
        if user_active:
            d["reason"] = "no session open; you're active"
        elif cfg["start_window_ping"]:
            d.update(action="ping", reason="no session open; opening one")
        else:
            d["reason"] = "no session open"
        return d

    minutes_left = (Ts - now) / 60
    session_cap = 100.0 if minutes_left <= cfg["final_minutes"] else 100.0 - cfg["session_reserve_pct"]
    weekly_cap = 100.0 if Tw - now <= WINDOW_S else 100.0 - cfg["weekly_reserve_pct"]
    need = max(0.0, session_cap - S) / (rate * max(1, cfg["max_parallel"]))
    tail = max(cfg["min_tail_minutes"], need * cfg["tail_safety"])
    d["burn_at"] = Ts - tail * 60

    if minutes_left > tail:
        return stop_or_none("harvest", "waiting for the end of this session")
    if user_active:
        return stop_or_none("harvest", "you're active; stepping aside")
    if S >= session_cap:
        return stop_or_none("harvest", "session reserve reached")
    if W >= weekly_cap:
        return stop_or_none("harvest", "weekly reserve reached")
    d.update(mode="burn", action="start", reason=f"burning the last {minutes_left:.0f} min")
    return d


def learn(learned, u, now, n_running, user_active):
    """Updates the weekly-%-per-session ratio and the bot's burn rate in place."""
    key = (round(u["session_reset"] or 0, -2), round(u["weekly_reset"] or 0, -2))
    S, W = u["session_pct"], u["weekly_pct"]

    a = learned.get("ratio_anchor")
    if not a or tuple(a["key"]) != key or S < a["S"] or W < a["W"]:
        learned["ratio_anchor"] = {"key": key, "S": S, "W": W}
    elif S - a["S"] >= 8:
        sample = (W - a["W"]) / (S - a["S"]) * 100
        if 0.3 <= sample <= 100:
            old = learned.get("window_weekly_pct")
            learned["window_weekly_pct"] = round(sample if old is None else 0.7 * old + 0.3 * sample, 2)
        learned["ratio_anchor"] = {"key": key, "S": S, "W": W}

    r = learned.get("rate_anchor")
    if n_running == 0 or user_active:
        learned.pop("rate_anchor", None)
    elif not r or tuple(r["key"]) != key or r["n"] != n_running or S < r["S"]:
        learned["rate_anchor"] = {"key": key, "S": S, "t": now, "n": n_running}
    elif now - r["t"] >= 600 or S - r["S"] >= 5:
        sample = (S - r["S"]) / ((now - r["t"]) / 60) / n_running
        if sample > 0:
            old = learned.get("burn_pct_per_min")
            learned["burn_pct_per_min"] = round(sample if old is None else 0.8 * old + 0.2 * sample, 3)
        learned["rate_anchor"] = {"key": key, "S": S, "t": now, "n": n_running}


def user_active(bot_ids, minutes):
    cutoff = time.time() - minutes * 60
    for dirpath, dirs, files in os.walk(CLAUDE_DIR / "projects"):
        if any(b in dirpath for b in bot_ids):
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".jsonl") and f[:-6] not in bot_ids:
                try:
                    if os.path.getmtime(os.path.join(dirpath, f)) >= cutoff:
                        return True
                except OSError:
                    pass
    return False


# Runner

def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def is_claude_process(pid):
    if not pid:
        return False
    if os.name != "nt":
        return Path(f"/proc/{pid}").exists()
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout.lower()
    return "claude" in out or "node" in out


def claude_exe(cfg):
    return shutil.which(cfg["claude_cmd"]) or cfg["claude_cmd"]


class Runner:
    def __init__(self):
        self.procs = {}

    def start(self, task, cfg):
        resume = bool(task.get("session_id")) and task["runs"] > 0
        sid = task.get("session_id") or str(uuid.uuid4())
        run = task["runs"] + 1
        logs = path("logs")
        logs.mkdir(exist_ok=True)
        prompt_file = logs / f"{task['id']}-{run}.prompt.txt"
        prompt_file.write_text(RESUME_PROMPT if resume else task["prompt"], encoding="utf-8")
        args = [claude_exe(cfg), "-p", "--output-format", "json"]
        args += ["--resume", sid] if resume else ["--session-id", sid]
        if task.get("model") or cfg["default_model"]:
            args += ["--model", task.get("model") or cfg["default_model"]]
        if task.get("permission_mode") or cfg["default_permission_mode"]:
            args += ["--permission-mode", task.get("permission_mode") or cfg["default_permission_mode"]]
        if task.get("tools"):
            args += ["--allowedTools", task["tools"]]
        out_path = logs / f"{task['id']}-{run}.json"
        err_path = logs / f"{task['id']}-{run}.log"
        with open(prompt_file, "rb") as stdin, open(out_path, "wb") as out, open(err_path, "wb") as err:
            p = subprocess.Popen(args, cwd=task["dir"], stdin=stdin, stdout=out, stderr=err,
                                 env=clean_env(), creationflags=NO_WINDOW)
        self.procs[task["id"]] = {"p": p, "sid": sid, "out": out_path, "err": err_path,
                                  "killed": False, "started": time.time()}
        return sid, run, p.pid

    def stop(self, task_id):
        info = self.procs.get(task_id)
        if info and info["p"].poll() is None:
            info["killed"] = True
            kill_tree(info["p"].pid)
            try:
                info["p"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def stop_all(self):
        for tid in list(self.procs):
            self.stop(tid)

    def running(self):
        return [tid for tid, i in self.procs.items() if i["p"].poll() is None]

    def reap(self):
        """Returns {task_id: outcome dict} for exited processes."""
        done = {}
        for tid, info in list(self.procs.items()):
            code = info["p"].poll()
            if code is None:
                continue
            del self.procs[tid]
            raw = info["out"].read_text(encoding="utf-8", errors="replace").strip()
            err = info["err"].read_text(encoding="utf-8", errors="replace").strip()
            try:
                res = json.loads(raw.splitlines()[-1]) if raw else {}
            except json.JSONDecodeError:
                res = {}
            text = res.get("result") or ""
            blob = (raw + "\n" + err).lower()
            if info["killed"]:
                status, note, text = "paused", "stopped by harvester", ""
            elif code == 0 and not res.get("is_error"):
                status, note = "done", ""
            elif "limit" in blob:
                status, note, text = "paused", "hit usage limit", ""
            else:
                status, note, text = "failed", (text or err or raw)[-300:] or f"exit code {code}", ""
            done[tid] = {"status": status, "note": note, "text": text, "start": info["started"],
                         "end": time.time(), "turns": res.get("num_turns")}
        return done


# Engine

class Engine:
    def __init__(self, log=None):
        self.runner = Runner()
        self.log = log or (lambda msg: None)
        self.state = read_json(path("state.json"), {})
        self.state.setdefault("learned", {})
        self.state.setdefault("bot_sessions", [])
        self.kick = threading.Event()
        self.lock = threading.Lock()
        self._recover()

    def _recover(self):
        last_seen = self.state.get("updated") or time.time()

        def fn(tasks):
            for t in tasks:
                if t["status"] == "running":
                    if is_claude_process(t.get("pid")):
                        kill_tree(t["pid"])
                    t.update(status="paused", pid=None, note="harvester restarted", updated=time.time())
                    if t.get("started"):
                        append_jsonl("runs.jsonl", {"task": t["id"], "run": t["runs"], "start": t["started"],
                                                    "end": max(t["started"], last_seen), "status": "paused"})
        update_tasks(fn)

    def _bot(self, sid):
        ids = self.state["bot_sessions"]
        if sid not in ids:
            ids.append(sid)
            del ids[:-300]

    def _ping(self, cfg):
        if time.time() - self.state.get("last_ping", 0) < 600:
            return
        self.state["last_ping"] = time.time()
        try:
            subprocess.run([claude_exe(cfg), "-p", "--model", cfg["ping_model"], "--no-session-persistence"],
                           input="Reply with just OK.", cwd=str(HOME), capture_output=True, text=True,
                           encoding="utf-8", timeout=180, env=clean_env(), creationflags=NO_WINDOW)
            self.log("opened a new session")
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"ping failed: {e}")

    def _finish(self, results):
        if not results:
            return
        res_dir = path("results")
        res_dir.mkdir(exist_ok=True)

        def fn(tasks):
            for t in tasks:
                r = results.get(t["id"])
                if r:
                    t.update(status=r["status"], note=r["note"], pid=None, updated=time.time())
                    if r["text"]:
                        (res_dir / f"{t['id']}.md").write_text(r["text"], encoding="utf-8")
                    append_jsonl("runs.jsonl", {"task": t["id"], "run": t["runs"], "start": round(r["start"]),
                                                "end": round(r["end"]), "status": r["status"], "turns": r["turns"]})
                    self.log(f"#{t['id']} {r['status']}" + (f": {r['note']}" if r["note"] else ""))
        update_tasks(fn)

    def _start_next(self, cfg):
        def fn(tasks):
            busy = set(self.runner.running())
            for t in queue_order(tasks):
                if len(busy) >= cfg["max_parallel"]:
                    break
                if t["id"] in busy:
                    continue
                if not Path(t["dir"]).is_dir():
                    t.update(status="failed", note=f"folder not found: {t['dir']}")
                    continue
                sid, run, pid = self.runner.start(t, cfg)
                self._bot(sid)
                t.update(status="running", session_id=sid, runs=run, pid=pid, note="",
                         started=round(time.time()), updated=time.time())
                busy.add(t["id"])
                self.log(f"#{t['id']} started")
        update_tasks(fn)

    def stop_running(self):
        self.runner.stop_all()
        self._finish(self.runner.reap())

    def tick(self):
        with self.lock:
            cfg = load_config()
            now = time.time()
            self._finish(self.runner.reap())
            n = len(self.runner.running())
            try:
                u = get_usage()
                self.state["error"] = ""
            except UsageError as e:
                self.state["error"] = str(e)
                self.state["updated"] = now
                write_json(path("state.json"), self.state)
                return
            active = user_active(set(self.state["bot_sessions"]), cfg["idle_minutes"])
            learn(self.state["learned"], u, now, n, active)
            paused = path("paused").exists()
            d = plan(now, u, cfg, self.state["learned"], n, active, paused)
            if d["action"] == "stop":
                self.log(f"stopping tasks: {d['reason']}")
                self.stop_running()
            elif d["action"] == "start":
                self._start_next(cfg)
                if not self.runner.running():
                    d["reason"] = "backlog empty"
            elif d["action"] == "ping":
                self._ping(cfg)
            self.state.update(usage=u, decision=d, user_active=active, updated=now,
                              running=self.runner.running())
            write_json(path("state.json"), self.state)
            ts = lambda v: round(v) if v else None
            append_jsonl("history.jsonl", {"t": round(now), "S": u["session_pct"], "W": u["weekly_pct"],
                                           "Ts": ts(u["session_reset"]), "Tw": ts(u["weekly_reset"]),
                                           "m": d["mode"], "n": len(self.runner.running()), "a": active})
            trim_jsonl("history.jsonl", days=9)

    def loop(self, stop_event):
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {e}"
                self.log(self.state["error"])
            cfg = load_config()
            wait = min(60, cfg["poll_seconds"]) if self.runner.running() else cfg["poll_seconds"]
            deadline = time.time() + wait
            while time.time() < deadline and not stop_event.is_set():
                if self.kick.is_set() or path("kick").exists():
                    self.kick.clear()
                    path("kick").unlink(missing_ok=True)
                    break
                stop_event.wait(1)
        self.stop_running()


# Formatting

def fmt_dur(seconds):
    if seconds is None:
        return "-"
    m = max(0, int(seconds // 60))
    if m >= 1440:
        return f"{m // 1440}d{m % 1440 // 60}h"
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_clock(ts):
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if ts - time.time() < 86400 else dt.strftime("%a %H:%M")


def app_alive(state):
    cfg = load_config()
    return time.time() - state.get("updated", 0) < 3 * cfg["poll_seconds"] + 30

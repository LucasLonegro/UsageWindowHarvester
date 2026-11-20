import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import core
import views

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).parent if FROZEN else Path(__file__).parent
APP = HERE / ("Harvester.exe" if FROZEN else "app.pyw")
STARTUP_LNK = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/Usage Harvester.lnk"
def cmd_status(a):
    s = views.make_style()
    v = views.load()
    if a.short:
        print(views.render_short(v, s))
    else:
        print(views.render_status(v, views.term_width(), s))


def cmd_watch(a):
    s = views.make_style()
    views.enable_vt()
    esc = lambda code: "\x1b[" + code
    sys.stdout.write(esc("?25l"))
    try:
        while True:
            body = views.render_status(views.load(), views.term_width(), s)
            body += "\n" + s(f"refreshing every {a.interval}s · Ctrl+C to exit", "90")
            lines = [line + esc("K") for line in body.split("\n")]
            sys.stdout.write(esc("H") + "\n".join(lines) + esc("J"))
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(esc("?25h") + "\n")


def cmd_gantt(a):
    print(views.render_gantt(views.load(), views.term_width(), views.make_style(), a.hours, a.ahead, a.week))


def cmd_week(_):
    print(views.render_week(views.load(), views.term_width(), views.make_style()))


def cmd_runs(a):
    print(views.render_runs(views.load(), views.term_width(), views.make_style(), a.n))


def cmd_add(a):
    prompt = Path(a.file).read_text(encoding="utf-8") if a.file else " ".join(a.prompt)
    if not prompt.strip():
        sys.exit("empty prompt")
    folder = Path(a.dir or os.getcwd()).resolve()
    if not folder.is_dir():
        sys.exit(f"not a folder: {folder}")
    t = core.add_task(prompt.strip(), folder, a.model or "", a.mode or "", a.tools or "", a.priority)
    print(f"added #{t['id']} in {folder}")


def cmd_ls(a):
    tasks = core.load_tasks()
    if not a.all:
        tasks = [t for t in tasks if t["status"] not in ("done",)]
    s, width = views.make_style(), views.term_width()
    for t in sorted(tasks, key=lambda t: t["id"]):
        print(views.task_line(t, width, s))
    if not tasks:
        print("backlog empty")


def edit_tasks(ids, fn, verb):
    ids = set(ids)

    def apply(tasks):
        hit = [t for t in tasks if t["id"] in ids]
        for t in hit:
            fn(tasks, t)
        return [t["id"] for t in hit]
    hit = core.update_tasks(apply)
    missing = ids - set(hit)
    if hit:
        print(f"{verb} " + " ".join(f"#{i}" for i in hit))
    if missing:
        print("not found: " + " ".join(f"#{i}" for i in sorted(missing)))


def cmd_rm(a):
    def fn(tasks, t):
        if t["status"] == "running":
            print(f"#{t['id']} is running; it stops at the next check")
        tasks.remove(t)
    edit_tasks(a.ids, fn, "removed")


def cmd_top(a):
    def fn(tasks, t):
        t["priority"] = max(x["priority"] for x in tasks) + 1
    edit_tasks(a.ids, fn, "moved to top:")


def cmd_retry(a):
    def fn(tasks, t):
        t.update(status="queued", note="", session_id=None, runs=0)
    edit_tasks(a.ids, fn, "requeued")


def cmd_show(a):
    t = next((t for t in core.load_tasks() if t["id"] == a.id), None)
    if not t:
        sys.exit(f"#{a.id} not found")
    print(f"#{t['id']} {t['status']} · runs {t['runs']} · {t['dir']}")
    for k in ("model", "permission_mode", "tools", "note", "session_id"):
        if t.get(k):
            print(f"{k}: {t[k]}")
    print("\n" + t["prompt"])
    res = core.path("results") / f"{t['id']}.md"
    if res.exists():
        print("\n--- result ---\n" + res.read_text(encoding="utf-8"))


def cmd_pause(_):
    core.path("paused").touch()
    core.path("kick").touch()
    print("paused · running tasks stop at the next check")


def cmd_resume(_):
    core.path("paused").unlink(missing_ok=True)
    core.path("kick").touch()
    print("resumed")


def cmd_now(_):
    core.path("kick").touch()
    print("check requested")


def app_command():
    if FROZEN:
        return [str(APP)]
    w = Path(sys.executable).with_name("pythonw.exe")
    return [str(w if w.exists() else Path(sys.executable)), str(APP)]


def cmd_app(_):
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | core.NO_WINDOW
    subprocess.Popen(app_command(), cwd=str(HERE), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("app started")


def cmd_autostart(a):
    if a.state == "off":
        STARTUP_LNK.unlink(missing_ok=True)
        print("autostart off")
        return
    q = lambda s: str(s).replace("'", "''")
    target, *args = app_command()
    arguments = " ".join(f'"{a}"' for a in args)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(STARTUP_LNK)}');"
          f"$s.TargetPath='{q(target)}';$s.Arguments='{q(arguments)}';"
          f"$s.WorkingDirectory='{q(HERE)}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"autostart on · {STARTUP_LNK}")


def cmd_config(a):
    cfg = core.load_config()
    if a.key is None:
        for k in core.DEFAULTS:
            print(f"{k:<24} {str(cfg[k]):<12} {core.CONFIG_HELP[k]}")
        return
    if a.key not in core.DEFAULTS:
        sys.exit(f"unknown key: {a.key}")
    if a.value is None:
        print(cfg[a.key])
        return
    cfg[a.key] = core.coerce(a.key, a.value)
    core.save_config(cfg)
    print(f"{a.key} = {cfg[a.key]}")


def cmd_snapshot(_):
    try:
        core.save_snapshot(json.load(sys.stdin))
    except (json.JSONDecodeError, OSError):
        pass


def main():
    p = argparse.ArgumentParser(prog="hv", description="Spend leftover Claude usage on a backlog.")
    p.add_argument("--version", action="version", version=f"hv {core.VERSION}")
    sub = p.add_subparsers(dest="cmd")
    add = sub.add_parser("add", help="add a task")
    add.add_argument("prompt", nargs="*")
    add.add_argument("-f", "--file", help="read the prompt from a file")
    add.add_argument("-d", "--dir", help="folder to run in (default: current)")
    add.add_argument("-m", "--model")
    add.add_argument("-p", "--priority", type=int, default=0)
    add.add_argument("-t", "--tools", help='allowed tools, e.g. "Edit Bash(npm test:*)"')
    add.add_argument("--mode", help="permission mode, e.g. acceptEdits")
    add.set_defaults(fn=cmd_add)
    ls = sub.add_parser("ls", help="list tasks")
    ls.add_argument("-a", "--all", action="store_true", help="include done")
    ls.set_defaults(fn=cmd_ls)
    for name, fn, h in (("rm", cmd_rm, "remove tasks"), ("top", cmd_top, "move tasks to the front"),
                        ("retry", cmd_retry, "requeue tasks from scratch")):
        sp = sub.add_parser(name, help=h)
        sp.add_argument("ids", nargs="+", type=int)
        sp.set_defaults(fn=fn)
    show = sub.add_parser("show", help="show a task and its result")
    show.add_argument("id", type=int)
    show.set_defaults(fn=cmd_show)
    status = sub.add_parser("status", help="dashboard: usage, estimates, timeline, queue")
    status.add_argument("-s", "--short", action="store_true", help="one line")
    status.set_defaults(fn=cmd_status, short=False)
    watch = sub.add_parser("watch", help="live dashboard")
    watch.add_argument("-n", "--interval", type=int, default=5, help="seconds between redraws")
    watch.set_defaults(fn=cmd_watch)
    gantt = sub.add_parser("gantt", help="timeline of sessions, harvester modes and task runs")
    gantt.add_argument("-H", "--hours", type=float, default=24, help="hours back (default 24)")
    gantt.add_argument("-A", "--ahead", type=float, default=6, help="hours ahead (default 6)")
    gantt.add_argument("-w", "--week", action="store_true", help="whole weekly window")
    gantt.set_defaults(fn=cmd_gantt)
    sub.add_parser("week", help="weekly usage chart with pace and projection").set_defaults(fn=cmd_week)
    runs = sub.add_parser("runs", help="task run history")
    runs.add_argument("-n", type=int, default=20, help="how many (default 20)")
    runs.set_defaults(fn=cmd_runs)
    for name, fn, h in (("pause", cmd_pause, "stop harvesting"),
                        ("resume", cmd_resume, "resume harvesting"), ("now", cmd_now, "check usage now"),
                        ("app", cmd_app, "start the desktop app")):
        sub.add_parser(name, help=h).set_defaults(fn=fn)
    auto = sub.add_parser("autostart", help="start the app when you log in")
    auto.add_argument("state", choices=["on", "off"])
    auto.set_defaults(fn=cmd_autostart)
    conf = sub.add_parser("config", help="show or set settings")
    conf.add_argument("key", nargs="?")
    conf.add_argument("value", nargs="?")
    conf.set_defaults(fn=cmd_config)
    sub.add_parser("snapshot").set_defaults(fn=cmd_snapshot)
    a = p.parse_args()
    if not getattr(a, "fn", None):
        a.fn, a.short = cmd_status, False
    a.fn(a)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

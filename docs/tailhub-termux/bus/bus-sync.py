#!/usr/bin/env python3
"""허브 감사로그(messages.jsonl)를 비공개 agent-docs 의 bus-log/YYYY-MM-DD.md 로 미러링하고 변경 시 커밋·푸시.
원문 그대로 올린다(마스킹 없음, 사용자 결정). 30초 주기."""
import datetime
import json
import pathlib
import re
import subprocess
import time

H = pathlib.Path.home()
SRC = H / "logs/agent-bus/messages.jsonl"
REPO = H / "agent-docs"
STATE = H / ".config/agent-bus/.synced_seq"
def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)


def cycle():
    if not SRC.exists():
        return
    last = int(STATE.read_text()) if STATE.exists() else 0
    new = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("op") == "msg" and m["seq"] > last:
            new.append(m)
    if not new:
        return
    (REPO / "bus-log").mkdir(exist_ok=True)
    for m in new:
        d = datetime.datetime.fromtimestamp(m["ts"])
        f = REPO / "bus-log" / ("%s.md" % d.strftime("%Y-%m-%d"))
        if not f.exists():
            f.write_text("# Bus log %s\n\n" % d.strftime("%Y-%m-%d"), encoding="utf-8")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("### %s `%s` -> `%s` (seq %s, hop %s)\n\n%s\n\n" % (
                d.strftime("%H:%M:%S"), m["from"], m["to"], m["seq"], m["hop"], m["text"]))
    git("pull", "--rebase", "-q", "origin", "main")
    git("add", "-A", "bus-log")
    msg = "bus-log: +%d messages (seq %d)" % (len(new), new[-1]["seq"])
    if git("commit", "-q", "-m", msg).returncode == 0:
        if git("push", "-q", "origin", "main").returncode == 0:
            STATE.write_text(str(new[-1]["seq"]))
    else:
        STATE.write_text(str(new[-1]["seq"]))


while True:
    try:
        cycle()
    except Exception as e:
        print("sync error", e, flush=True)
    time.sleep(30)

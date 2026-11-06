#!/usr/bin/env bash
# ==============================================================================
# bin/chain-registry.sh — 에이전트 체인 자가 복원 매니페스트(recovery.json) 및
#                         전역 파이프라인 레지스트리(runtime/registry.json) 관리기
# ==============================================================================
# 사용법:
#   bin/chain-registry.sh sync [세션명|런타임경로]  런타임 recovery.json & recover-self.sh 갱신 및 전역 registry.json 동기화
#   bin/chain-registry.sh json [--all]              ts / ts-tui 파이프라인용 최신 JSON 출력 (자동 sync 포함)
#   bin/chain-registry.sh get <세션명>              특정 세션의 복원 JSON 객체 반환
#   bin/chain-registry.sh retire <세션명>           세션 정리(retired=true) 및 registry.json 갱신
#   bin/chain-registry.sh unretire <세션명>         세션 복원(retired=false) 및 registry.json 갱신
# ==============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_FILE="$HERE/runtime/registry.json"

cmd="${1:-json}"
shift || true

python3 - "$HERE" "$REGISTRY_FILE" "$cmd" "$@" <<'PY'
import os
import sys
import glob
import json
import shlex
import subprocess
from datetime import datetime, timezone, timedelta

HERE = sys.argv[1]
REGISTRY_FILE = sys.argv[2]
CMD = sys.argv[3] if len(sys.argv) > 3 else "json"
ARGS = sys.argv[4:]

KST = timezone(timedelta(hours=9))

def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")

def read_first_line(path):
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except Exception:
        return ""

def parse_env_file(path):
    res = {}
    if not path or not os.path.isfile(path):
        return res
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                res[k.strip()] = v.strip().strip("\"'")
    except Exception:
        pass
    return res

def is_pid_alive(pid_val):
    if not pid_val:
        return None, False
    try:
        pid = int(str(pid_val).strip())
        os.kill(pid, 0)
        return pid, True
    except Exception:
        return None, False

def check_pid_file(path):
    return is_pid_alive(read_first_line(path))

def get_active_tmux_sessions():
    res = {}
    try:
        p = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}"],
            capture_output=True, text=True
        )
        if p.returncode == 0:
            for line in p.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 4:
                    res[parts[0]] = {
                        "windows": int(parts[1]) if parts[1].isdigit() else 0,
                        "attached": (int(parts[2]) > 0) if parts[2].isdigit() else False,
                        "created": int(parts[3]) if parts[3].isdigit() else 0,
                    }
    except Exception:
        pass
    return res

def find_claude_live_info(session_name):
    """tmux 세션의 sonnet 창에서 실행 중인 Claude 프로세스 및 ~/.claude/sessions/<pid>.json 조회"""
    info = {
        "claude_alive": False,
        "claude_pid": None,
        "claude_status": None,
        "waiting_for": None,
        "supervisor_session_id": None,
        "bridge_session_id": None,
        "remote_control_url": None,
    }
    try:
        p = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{session_name}:sonnet", "#{pane_pid}|#{pane_current_command}"],
            capture_output=True, text=True
        )
        if p.returncode != 0 or not p.stdout.strip():
            return info
        parts = p.stdout.strip().split("|", 1)
        ppid = parts[0].strip()
        fg_cmd = parts[1].strip() if len(parts) > 1 else ""
        cpid = None
        if ppid.isdigit():
            pg = subprocess.run(["pgrep", "-P", ppid, "-f", "claude"], capture_output=True, text=True)
            pids = [x.strip() for x in pg.stdout.splitlines() if x.strip().isdigit()]
            if pids:
                cpid = int(pids[0])
            elif fg_cmd == "claude":
                cpid = int(ppid)

        if cpid:
            info["claude_alive"] = True
            info["claude_pid"] = cpid
            info["claude_status"] = "running"
            s_file = os.path.join(os.path.expanduser("~"), ".claude", "sessions", f"{cpid}.json")
            if os.path.isfile(s_file):
                with open(s_file, "r", encoding="utf-8", errors="ignore") as f:
                    d = json.load(f)
                info["claude_status"] = d.get("status", "idle")
                info["waiting_for"] = d.get("waitingFor")
                if d.get("sessionId"):
                    info["supervisor_session_id"] = d.get("sessionId")
                if d.get("bridgeSessionId"):
                    info["bridge_session_id"] = d.get("bridgeSessionId")
                    info["remote_control_url"] = f"https://claude.ai/code/{d['bridgeSessionId']}"
    except Exception:
        pass
    return info

def find_transcript_session_id(project_dir, runtime_dir):
    if not project_dir or not runtime_dir:
        return None
    slug = project_dir.replace("/", "-").replace(".", "-").replace("_", "-")
    proj_claude_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", slug)
    if not os.path.isdir(proj_claude_dir):
        return None
    needle = f"sonnet-event-wait.sh stop {runtime_dir}"
    try:
        jsonls = glob.glob(os.path.join(proj_claude_dir, "*.jsonl"))
        jsonls.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        for jf in jsonls:
            try:
                with open(jf, "r", encoding="utf-8", errors="ignore") as f:
                    if needle in f.read():
                        return os.path.basename(jf)[:-6]
            except Exception:
                continue
    except Exception:
        pass
    return None

def discover_chain_runtimes():
    """
    모든 체인 런타임과 세션 매핑을 동적으로 탐색.
    기준:
      1) instances/*.env
      2) runtime/*/session_name 또는 runtime/*/recovery.json
    동일 session_name 에 여러 런타임이 있으면:
      - 현재 살아있는 tmux 세션의 ACC_RUNTIME 환경변수 일치 우선
      - instances/<session>.env 의 ACC_RUNTIME 우선
      - 가장 최근 수정된(mtime) 런타임 우선
    """
    tmux_sessions = get_active_tmux_sessions()
    candidates = {}  # session_name -> list of runtime_dirs
    env_by_session = {}

    for env_path in glob.glob(os.path.join(HERE, "instances", "*.env")):
        s_name = os.path.basename(env_path)[:-4]
        if not s_name:
            continue
        env_data = parse_env_file(env_path)
        env_by_session[s_name] = (env_path, env_data)
        rt = env_data.get("ACC_RUNTIME", "")
        if rt and os.path.isdir(rt):
            candidates.setdefault(s_name, []).append(os.path.abspath(rt))

    for rt_dir in glob.glob(os.path.join(HERE, "runtime", "*")):
        if not os.path.isdir(rt_dir):
            continue
        rt_abs = os.path.abspath(rt_dir)
        s_name = read_first_line(os.path.join(rt_abs, "session_name"))
        if not s_name:
            rec_file = os.path.join(rt_abs, "recovery.json")
            if os.path.isfile(rec_file):
                try:
                    with open(rec_file, "r", encoding="utf-8") as f:
                        s_name = json.load(f).get("session_name", "")
                except Exception:
                    pass
        if not s_name:
            continue
        if rt_abs not in candidates.setdefault(s_name, []):
            candidates[s_name].append(rt_abs)

    resolved = {}
    for s_name, rt_list in candidates.items():
        if not rt_list:
            continue
        chosen = None
        # 1) 현재 살아있는 tmux ACC_RUNTIME 확인
        if s_name in tmux_sessions:
            try:
                p = subprocess.run(["tmux", "show-environment", "-t", s_name, "ACC_RUNTIME"], capture_output=True, text=True)
                for line in p.stdout.splitlines():
                    if line.startswith("ACC_RUNTIME="):
                        val = os.path.abspath(line.split("=", 1)[1].strip())
                        if os.path.isdir(val):
                            chosen = val
                            break
            except Exception:
                pass
        # 2) instances/<s_name>.env 의 ACC_RUNTIME 확인
        if not chosen and s_name in env_by_session:
            rt_env = env_by_session[s_name][1].get("ACC_RUNTIME", "")
            if rt_env and os.path.isdir(rt_env):
                chosen = os.path.abspath(rt_env)
        # 3) retired 아닌 최신 런타임 우선
        if not chosen:
            non_retired = [r for r in rt_list if not os.path.isfile(os.path.join(r, "retired"))]
            pool = non_retired if non_retired else rt_list
            pool.sort(key=lambda r: os.path.getmtime(r), reverse=True)
            chosen = pool[0]

        resolved[s_name] = chosen

    return resolved, env_by_session, tmux_sessions

def sync_one_session(s_name, rt_dir, env_by_session, tmux_sessions):
    rec_path = os.path.join(rt_dir, "recovery.json")
    prev = {}
    if os.path.isfile(rec_path):
        try:
            with open(rec_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}

    cfg_env = parse_env_file(os.path.join(HERE, "config.env"))
    inst_path, inst_env = env_by_session.get(s_name, (None, {}))

    project_dir = inst_env.get("PROJECT_DIR") or prev.get("project_dir") or cfg_env.get("PROJECT_DIR") or "/mnt/c/Users/rerun/llm-wiki"
    work_dir = inst_env.get("WORK_DIR") or prev.get("work_dir") or cfg_env.get("WORK_DIR") or project_dir
    handoff_file = inst_env.get("HANDOFF_FILE") or prev.get("handoff_file") or cfg_env.get("HANDOFF_FILE") or ""
    sonnet_cmd = inst_env.get("SONNET_CMD") or cfg_env.get("SONNET_CMD") or "claude --model sonnet --remote-control"

    retired = os.path.isfile(os.path.join(rt_dir, "retired"))
    tmux_info = tmux_sessions.get(s_name)
    tmux_alive = tmux_info is not None

    live_claude = find_claude_live_info(s_name) if tmux_alive else {
        "claude_alive": False,
        "claude_pid": None,
        "claude_status": None,
        "waiting_for": None,
        "supervisor_session_id": None,
        "bridge_session_id": None,
        "remote_control_url": None,
    }

    sid = (
        live_claude.get("supervisor_session_id")
        or read_first_line(os.path.join(rt_dir, "supervisor_session_id"))
        or prev.get("supervisor_session_id")
        or find_transcript_session_id(project_dir, rt_dir)
    )
    if sid:
        try:
            with open(os.path.join(rt_dir, "supervisor_session_id"), "w", encoding="utf-8") as f:
                f.write(sid + "\n")
        except Exception:
            pass

    rc_url = (
        live_claude.get("remote_control_url")
        or read_first_line(os.path.join(rt_dir, "remote_control_url"))
        or prev.get("remote_control_url")
    )
    if rc_url:
        try:
            with open(os.path.join(rt_dir, "remote_control_url"), "w", encoding="utf-8") as f:
                f.write(rc_url + "\n")
        except Exception:
            pass

    bridge_id = live_claude.get("bridge_session_id") or prev.get("bridge_session_id")
    if not bridge_id and rc_url and "session_" in rc_url:
        bridge_id = rc_url.rsplit("/", 1)[-1]

    w_pid, w_alive = check_pid_file(os.path.join(rt_dir, "watchdog.pid"))
    l_pid, l_alive = check_pid_file(os.path.join(rt_dir, "listener.pid"))

    pending_dir = os.path.join(rt_dir, "events", "pending")
    pending_cnt = len(os.listdir(pending_dir)) if os.path.isdir(pending_dir) else 0

    busy_workers = []
    workers_dir = os.path.join(rt_dir, "workers")
    if os.path.isdir(workers_dir):
        for bf in sorted(glob.glob(os.path.join(workers_dir, "*.busy"))):
            b_name = os.path.basename(bf)[:-5]
            tid = read_first_line(bf)
            busy_workers.append(f"{b_name} ({tid})" if tid else b_name)

    recover_self_sh = os.path.join(rt_dir, "recover-self.sh")
    resume_arg = f"--resume {shlex.quote(sid)}" if sid else ""
    settings_path = os.path.join(rt_dir, "claude-bridge.settings.json")
    brief_path = os.path.join(rt_dir, "session_brief.md")
    lite = (inst_env.get("CHAIN_MODE") == "lite") or os.path.isfile(os.path.join(rt_dir, "lite"))
    resume_cmd = (
        f"export ACC_RUNTIME={shlex.quote(rt_dir)} SESSION_NAME={shlex.quote(s_name)}; "
        f"{sonnet_cmd} {resume_arg}".strip()
    ) if lite else (
        f"export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 "
        f"ACC_RUNTIME={shlex.quote(rt_dir)} "
        f"ACC_TEMPLATE_ROOT={shlex.quote(HERE)} "
        f"SESSION_NAME={shlex.quote(s_name)}; "
        f"{sonnet_cmd} {resume_arg} "
        f"--settings {shlex.quote(settings_path)} "
        f"--add-dir {shlex.quote(HERE)} "
        f"--append-system-prompt \"$(cat {shlex.quote(brief_path)})\""
    ).strip()

    # 자가 복원 스크립트(recover-self.sh) 생성/갱신
    try:
        with open(recover_self_sh, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write(f"# Auto-generated self-recovery script for {s_name}\n")
            f.write(f"exec {shlex.quote(os.path.join(HERE, 'bin', 'chain-recover.sh'))} {shlex.quote(s_name)} \"$@\"\n")
        os.chmod(recover_self_sh, 0o755)
    except Exception:
        pass

    record = {
        "session_name": s_name,
        "runtime_dir": rt_dir,
        "instance_file": inst_path,
        "project_dir": project_dir,
        "work_dir": work_dir,
        "handoff_file": handoff_file,
        "mode": "lite" if lite else "full",
        "retired": retired,
        "tmux_alive": tmux_alive,
        "tmux_windows": tmux_info["windows"] if tmux_info else 0,
        "tmux_attached": tmux_info["attached"] if tmux_info else False,
        "tmux_created": tmux_info["created"] if tmux_info else 0,
        "claude_alive": live_claude["claude_alive"],
        "claude_pid": live_claude["claude_pid"],
        "claude_status": live_claude["claude_status"],
        "waiting_for": live_claude["waiting_for"],
        "supervisor_session_id": sid,
        "bridge_session_id": bridge_id,
        "remote_control_url": rc_url,
        "watchdog_alive": w_alive,
        "watchdog_pid": w_pid,
        "listener_alive": l_alive,
        "listener_pid": l_pid,
        "pending_events": pending_cnt,
        "busy_workers": busy_workers,
        "resume_cmd": resume_cmd,
        "recover_script": recover_self_sh,
        "updated_at": now_iso(),
    }

    try:
        tmp_rec = f"{rec_path}.tmp.{os.getpid()}"
        with open(tmp_rec, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_rec, rec_path)
    except Exception:
        pass

    return record

def sync_all(include_retired=False):
    resolved, env_by_session, tmux_sessions = discover_chain_runtimes()
    records = []
    for s_name in sorted(resolved.keys()):
        rt_dir = resolved[s_name]
        is_ret = os.path.isfile(os.path.join(rt_dir, "retired"))
        if not include_retired and is_ret and s_name not in tmux_sessions:
            continue
        rec = sync_one_session(s_name, rt_dir, env_by_session, tmux_sessions)
        if not include_retired and rec["retired"] and not rec["tmux_alive"]:
            continue
        records.append(rec)

    payload = {
        "updated_at": now_iso(),
        "template_root": HERE,
        "sessions": records,
    }
    try:
        os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
        tmp_reg = f"{REGISTRY_FILE}.tmp.{os.getpid()}"
        with open(tmp_reg, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_reg, REGISTRY_FILE)
    except Exception:
        pass
    return payload

if CMD in ("sync", "json", "list"):
    inc_all = ("--all" in ARGS)
    payload = sync_all(include_retired=inc_all)
    if CMD in ("json", "list"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
elif CMD == "get":
    target = ARGS[0] if ARGS else ""
    payload = sync_all(include_retired=True)
    for s in payload.get("sessions", []):
        if s["session_name"] == target or os.path.basename(s["runtime_dir"]) == target:
            print(json.dumps(s, ensure_ascii=False, indent=2))
            sys.exit(0)
    sys.exit(70)
elif CMD in ("retire", "unretire"):
    target = ARGS[0] if ARGS else ""
    if not target:
        sys.exit(64)
    chain_bin = os.path.join(HERE, "bin", "chain")
    subprocess.run([chain_bin, CMD, target])
    sync_all(include_retired=False)
else:
    print(f"Unknown command: {CMD}", file=sys.stderr)
    sys.exit(64)
PY

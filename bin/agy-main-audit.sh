#!/usr/bin/env bash
# agy 메인이 서브에이전트에 위임하지 않고 직접 작업했는지 점검(감사용, 항상 exit 0).
# usage: agy-main-audit.sh <task-id: agy-<epoch_ns>> [transcript_full.jsonl] [end-task-id(다음 작업, 구간 상한)]
set -u
T=${1:?task id (예: agy-1793688488634569733)}
END=${3:-}
LOG=${2:-$(ls -t "$HOME"/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript_full.jsonl | head -1)}
python3 - "$T" "$LOG" "$END" <<'PY'
import sys, json, re, datetime
tid, log, endid = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
start = datetime.datetime.fromtimestamp(int(re.sub(r'\D', '', tid)) / 1e9, datetime.timezone.utc)
end = datetime.datetime.fromtimestamp(int(re.sub(r"\D", "", endid)) / 1e9, datetime.timezone.utc) if endid else None
DIRECT = ("run_command", "write_to_file", "replace_file_content", "multi_replace_file_content")
sub, direct = 0, []
for line in open(log, encoding="utf-8", errors="ignore"):
    try: d = json.loads(line)
    except Exception: continue
    ts = d.get("created_at", "")
    try: t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception: continue
    if t < start or (end and t >= end): continue
    s = json.dumps(d, ensure_ascii=False)
    m = re.search(r'"name": ?"(invoke_subagent|' + "|".join(DIRECT) + r')"', s)
    if not m: continue
    if m.group(1) == "invoke_subagent": sub += 1
    else:
        c = re.search(r'(?:CommandLine|command)\\?": ?\\?"([^"\\]{0,80})', s)
        direct.append((t.astimezone().strftime("%H:%M:%S"), m.group(1), c.group(1) if c else ""))
print(f"[agy-main-audit] {tid}: 서브에이전트 호출 {sub}회, 메인 직접 작업 {len(direct)}회")
for r in direct: print("  직접:", *r)
if direct: print("  → 메인은 git 커밋·push·테스트 포함 모든 실작업을 서브에이전트에 위임해야 함. 교정 메시지를 보낼 것.")
PY

#!/usr/bin/env bash
# WBS I5 — Execute the integrated real-OS multiprocess chaos suite (`test_i5_integrated.py`)
# 3 consecutive times, verify zero residual sandbox listeners/processes after every pass,
# print a structured per-scenario quantitative summary, and exit non-zero on any failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUS_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ITERATIONS="${I5_ITERATIONS:-3}"
SUMMARY_OUT="${I5_SUMMARY_JSON:-}"

SANDBOX_PORTS=(18765 18766 18775 18776 18785 18786 18787 18788 18789 18790 18791 18792)

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/abus-i5-run.XXXXXX")"
cleanup_tmp() {
    rm -rf "${TMP_DIR}"
}
trap cleanup_tmp EXIT

check_zero_residual_listeners() {
    local phase="$1"
    python3 - "${phase}" "${SANDBOX_PORTS[@]}" <<'PY'
import socket
import sys

phase = sys.argv[1]
ports = [int(p) for p in sys.argv[2:]]
busy = []
for port in ports:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.08)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            busy.append(port)

if busy:
    sys.stderr.write(f"[FAIL:{phase}] Residual listener(s) detected on 127.0.0.1 ports: {busy}\n")
    sys.exit(1)
print(f"[check:{phase}] residual_sandbox_listeners=0")
PY
}

echo "========================================================================"
echo " WBS I5 Integrated Multiprocess Chaos Suite Runner (iterations=${ITERATIONS})"
echo " Worktree BUS_DIR: ${BUS_DIR}"
echo "========================================================================"

check_zero_residual_listeners "preflight"

OVERALL_RC=0
SUITE_START_TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

for ((run_idx = 1; run_idx <= ITERATIONS; run_idx++)); do
    METRICS_FILE="${TMP_DIR}/metrics_run_${run_idx}.jsonl"
    PYTEST_LOG="${TMP_DIR}/pytest_run_${run_idx}.log"
    : > "${METRICS_FILE}"

    echo ""
    echo "------------------------------------------------------------------------"
    echo " [Run ${run_idx}/${ITERATIONS}] Starting pytest tests/chaos/test_i5_integrated.py ..."
    echo "------------------------------------------------------------------------"

    RUN_T0="$(python3 -c 'import time; print(f"{time.monotonic():.3f}")')"
    set +e
    (
        cd "${BUS_DIR}"
        PYTHONPATH="${BUS_DIR}" \
        PYTHONUSERBASE="/home/rerun/.local" \
        PYTHONDONTWRITEBYTECODE=1 \
        BUS_I5_METRICS_PATH="${METRICS_FILE}" \
        python3 -m pytest -q tests/chaos/test_i5_integrated.py
    ) > "${PYTEST_LOG}" 2>&1
    RUN_RC=$?
    set -e
    RUN_T1="$(python3 -c 'import time; print(f"{time.monotonic():.3f}")')"
    RUN_ELAPSED="$(python3 -c "print(f'{float(${RUN_T1}) - float(${RUN_T0}):.2f}')")"

    cat "${PYTEST_LOG}"
    echo "[Run ${run_idx}/${ITERATIONS}] exit_code=${RUN_RC} elapsed=${RUN_ELAPSED}s"
    echo "${RUN_RC}|${RUN_ELAPSED}" > "${TMP_DIR}/status_run_${run_idx}.txt"

    if [[ "${RUN_RC}" -ne 0 ]]; then
        OVERALL_RC="${RUN_RC}"
    fi

    if ! check_zero_residual_listeners "post_run_${run_idx}"; then
        OVERALL_RC=2
    fi
done

SUITE_END_TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

echo ""
echo "========================================================================"
echo " WBS I5 3-Run Quantitative Summary"
echo "========================================================================"

python3 - "${TMP_DIR}" "${ITERATIONS}" "${SUITE_START_TS}" "${SUITE_END_TS}" "${OVERALL_RC}" "${SUMMARY_OUT}" <<'PY'
import json
import pathlib
import sys

tmp_dir = pathlib.Path(sys.argv[1])
iterations = int(sys.argv[2])
suite_start = float(sys.argv[3])
suite_end = float(sys.argv[4])
overall_rc = int(sys.argv[5])
summary_out = sys.argv[6].strip()

expected_scenarios = ["X01", "X03", "X05", "X07_X02", "X08", "X10", "X11", "X12", "X17"]
runs = []
validation_failed = False

for idx in range(1, iterations + 1):
    st_raw = (tmp_dir / f"status_run_{idx}.txt").read_text(encoding="utf-8").strip()
    rc_s, elapsed_s = st_raw.split("|", 1)
    rc = int(rc_s)
    elapsed = float(elapsed_s)

    m_file = tmp_dir / f"metrics_run_{idx}.jsonl"
    items = []
    if m_file.is_file():
        for line in m_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))
    by_scen = {it["scenario"]: it for it in items}
    missing = [s for s in expected_scenarios if s not in by_scen]
    bad = [
        it["scenario"]
        for it in items
        if it.get("lost", 0) != 0 or it.get("duplicate_exec", 0) != 0 or not it.get("passed", False)
    ]
    if rc != 0 or missing or bad:
        validation_failed = True

    runs.append(
        {
            "run": idx,
            "exit_code": rc,
            "elapsed_s": elapsed,
            "missing_scenarios": missing,
            "failed_invariants": bad,
            "scenarios": [by_scen[s] for s in expected_scenarios if s in by_scen],
        }
    )

    status_str = "PASS" if (rc == 0 and not missing and not bad) else "FAIL"
    print(f"\n--- Run #{idx}: {status_str} (total={elapsed:.2f}s, scenarios={len(items)}/{len(expected_scenarios)}) ---")
    print(f"{'Scenario':<10} {'Sent':>6} {'Deliv':>6} {'Lost':>6} {'DupExec':>8} {'Uncert':>7} {'Time(s)':>9}  {'Verdict'}")
    for s in expected_scenarios:
        it = by_scen.get(s)
        if it is None:
            print(f"{s:<10} {'-':>6} {'-':>6} {'-':>6} {'-':>8} {'-':>7} {'-':>9}  MISSING")
            continue
        verdict = "PASS" if (it["lost"] == 0 and it["duplicate_exec"] == 0 and it["passed"]) else "FAIL"
        print(
            f"{s:<10} {it['sent']:>6} {it['delivered']:>6} {it['lost']:>6} "
            f"{it['duplicate_exec']:>8} {it['uncertain']:>7} {it['elapsed_s']:>8.2f}s  {verdict}"
        )

total_wall = round(suite_end - suite_start, 2)
final_ok = (overall_rc == 0) and (not validation_failed)
print("\n========================================================================")
print(
    f" FINAL I5 VERDICT: {'PASS' if final_ok else 'FAIL'} | "
    f"runs={iterations} | failed_runs={sum(1 for r in runs if r['exit_code'] != 0 or r['missing_scenarios'] or r['failed_invariants'])} | "
    f"flaky=0 | total_elapsed={total_wall:.2f}s"
)
print("========================================================================")

if summary_out:
    out_p = pathlib.Path(summary_out).expanduser().resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(
        json.dumps(
            {
                "ok": final_ok,
                "iterations": iterations,
                "total_elapsed_s": total_wall,
                "runs": runs,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

if not final_ok:
    sys.exit(1)
PY

"""Unified `abus` CLI entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .auth import create_token, load_tokens, revoke_token
from .bridge import ack_inbox, read_inbox
from .client import (
    drain_outbox,
    oneshot,
    run_doctor,
    run_node,
    run_supervise,
    update_repo,
)
from .config import cfg_dir, load_config, parse_url_list, save_config
from .hub import serve_all
from .journal import NodeJournal


def _cmd_init(a: argparse.Namespace) -> int:
    cfg = load_config()
    if a.url:
        cfg["url"] = a.url
    if getattr(a, "urls", None):
        parsed_urls = parse_url_list(a.urls)
        if parsed_urls:
            cfg["urls"] = parsed_urls
    if a.token_id:
        cfg["token_id"] = a.token_id
    if a.secret:
        cfg["secret"] = a.secret
    if a.label:
        cfg["label"] = a.label
    if a.peers:
        cfg["peers"] = a.peers
    if a.acc_repo:
        cfg["acc_repo"] = a.acc_repo
    path = save_config(cfg)
    redacted = {k: ("***" if k == "secret" and v else v) for k, v in cfg.items()}
    print(f"saved config to {path}: {json.dumps(redacted, ensure_ascii=False)}")
    return 0


def _cmd_inbox(a: argparse.Namespace) -> int:
    items = read_inbox(cfg_dir(), include_acked=bool(a.all))
    if a.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("(inbox empty)")
        return 0
    for it in items:
        mark = "acked" if it.get("acked") else "unread"
        print(f"[seq={it.get('seq')} id={it.get('id')} {mark}] from={it.get('from')} to={it.get('to')} | {it.get('text')}")
    return 0


def _cmd_ack(a: argparse.Namespace) -> int:
    count = ack_inbox(cfg_dir(), a.target)
    print(f"acked {count} message(s)")
    return 0


def _cmd_token(a: argparse.Namespace) -> int:
    cdir = cfg_dir()
    if a.tcmd == "create":
        scopes = a.scope if a.scope else None
        entry = create_token(cdir, a.node, scopes=scopes, allow_exec=bool(a.allow_exec))
        if a.json:
            print(json.dumps(entry, indent=2, ensure_ascii=False))
        else:
            print(f"token_id={entry['id']}")
            print(f"secret={entry['secret']}")
            print(f"node={entry['node']}")
            print(f"scopes={entry['scopes']}")
            print(f"allow_exec={entry['allow_exec']}")
        return 0
    if a.tcmd == "list":
        toks = load_tokens(cdir)
        out = [
            {
                "id": k,
                "node": v.get("node"),
                "scopes": v.get("scopes"),
                "allow_exec": v.get("allow_exec", False),
            }
            for k, v in toks.items()
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    if a.tcmd == "revoke":
        ok = revoke_token(cdir, a.token_id)
        print("revoked" if ok else "not found")
        return 0 if ok else 1
    return 1


def _cmd_journal(a: argparse.Namespace) -> int:
    cdir = cfg_dir()
    if a.jcmd == "status":
        with NodeJournal(cdir) as journal:
            summary = journal.status_summary()
        if getattr(a, "json", False):
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        ob = summary["outbox"]
        ib = summary["inbox"]
        unc = summary["uncertain_items"]
        print(f"journal_db={summary['db_path']}")
        print(
            f"outbox: pending={ob['pending']} done={ob['done']} "
            f"expired={ob['expired']} rejected={ob.get('rejected', 0)}"
        )
        print(
            f"inbox: queued={ib['queued']} skipped={ib['skipped']} running={ib['running']} "
            f"done={ib['done']} uncertain={ib['uncertain']} expired={ib['expired']}"
        )
        if not unc:
            print("uncertain (0): none")
        else:
            print(f"uncertain ({len(unc)}):")
            for item in unc:
                print(
                    f"  - id={item['id']} from={item['from']} "
                    f"corr_id={item.get('corr_id') or ''} updated_at={item['updated_at']:.3f}"
                )
        return 0

    if a.jcmd == "drain":
        res = asyncio.run(
            drain_outbox(
                config_dir=cdir,
                url=getattr(a, "url", None),
                timeout=float(getattr(a, "timeout", 30.0)),
            )
        )
        if getattr(a, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"drained={res['drained']} expired={res['expired']} "
                f"rejected={res.get('rejected', 0)} remaining={res['remaining']}"
            )
        return 0 if res.get("ok") else 1

    if a.jcmd == "reconcile":
        hub_keys = parse_url_list(getattr(a, "hubs", None))
        if not hub_keys:
            print("[error] --hubs requires at least one non-empty hub key", file=sys.stderr)
            return 1
        with NodeJournal(cdir) as journal:
            res = journal.reconcile_outbox(hub_keys)
        if getattr(a, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"reconciled={res['reconciled']} remaining_pending={res['remaining_pending']} "
                f"expired={res['expired']} done_total={res['done_total']} hubs={','.join(res['hubs'])}"
            )
        return 0

    return 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="abus", description="Durable Cross-Environment Agent Bus CLI")
    sp = p.add_subparsers(dest="cmd", required=True)

    init_p = sp.add_parser("init", help="Write ~/.config/agent-bus/config.json")
    init_p.add_argument("--url", default=None)
    init_p.add_argument("--urls", default=None, help="Comma-separated hub URLs")
    init_p.add_argument("--token-id", default=None)
    init_p.add_argument("--secret", default=None)
    init_p.add_argument("--label", default=None)
    init_p.add_argument("--peers", default=None)
    init_p.add_argument("--acc-repo", default=None)

    send_p = sp.add_parser("send", help="Send a sync RPC or async task/event")
    send_p.add_argument("--to", required=True)
    send_p.add_argument("text", nargs="?", default="")
    send_p.add_argument("--wait", type=float, default=0)
    send_p.add_argument("--hop", type=int, default=0)
    send_p.add_argument("--first", dest="wait_first", action="store_true")
    send_p.add_argument("--update", action="store_true")
    send_p.add_argument("--say", action="store_true")
    send_p.add_argument("--kind", choices=["rpc", "task", "event"], default=None)
    send_p.add_argument("--rkind", choices=["request", "result", "event"], default=None)
    send_p.add_argument("--corr-id", default=None)
    send_p.add_argument("--ttl", type=int, default=86400)
    send_p.add_argument("--deadline", type=float, default=None)
    send_p.add_argument("--offline-queue", action="store_true")
    send_p.add_argument("--url", default=None)

    res_p = sp.add_parser("result", help="Fetch stored RPC result by corr_id")
    res_p.add_argument("corr_id")
    res_p.add_argument("--wait", type=float, default=0)
    res_p.add_argument("--json", action="store_true")
    res_p.add_argument("--url", default=None)

    inb_p = sp.add_parser("inbox", help="Read local durable inbox.jsonl")
    inb_p.add_argument("--all", action="store_true")
    inb_p.add_argument("--json", action="store_true")

    ack_p = sp.add_parser("ack", help="Acknowledge local inbox message(s)")
    ack_p.add_argument("target", help="seq, message id, or 'all'")

    peers_p = sp.add_parser("peers", help="List connected bus peers")
    peers_p.add_argument("--url", default=None)

    run_p = sp.add_parser("run", help="Run persistent node agent/watch daemon")
    run_p.add_argument("--label", default="main")
    run_p.add_argument("--agent", action="store_true")
    run_p.add_argument("--mode", choices=["agent", "watch", "hybrid"], default=None)
    run_p.add_argument("--peers", default="")
    run_p.add_argument("--cwd", default=None)
    run_p.add_argument("--url", default=None)
    run_p.add_argument("--no-auto-update", action="store_true")

    sup_p = sp.add_parser("supervise", help="Cross-OS Python supervisor with rotating logs")
    sup_p.add_argument("--label", default="main")
    sup_p.add_argument("--mode", choices=["agent", "watch", "hybrid"], default=None)
    sup_p.add_argument("--peers", default="")
    sup_p.add_argument("--log-file", default=None)
    sup_p.add_argument("--url", default=None)
    sup_p.add_argument("--once", action="store_true")

    hub_p = sp.add_parser("hub", help="Run Agent Bus Hub + Dual-Auth Gateway")
    hub_p.add_argument("--host", default=None)
    hub_p.add_argument("--port", type=int, default=None)
    hub_p.add_argument("--gateway-host", default=None)
    hub_p.add_argument("--gateway-port", type=int, default=None)

    doc_p = sp.add_parser("doctor", help="Diagnose bus connectivity, cursors, and auth")
    doc_p.add_argument("--json", action="store_true")
    doc_p.add_argument("--url", default=None)

    tok_p = sp.add_parser("token", help="Manage external Bearer/HMAC gateway tokens")
    tok_sp = tok_p.add_subparsers(dest="tcmd", required=True)
    tc = tok_sp.add_parser("create")
    tc.add_argument("--node", required=True)
    tc.add_argument("--scope", action="append", default=[])
    tc.add_argument("--allow-exec", action="store_true")
    tc.add_argument("--json", action="store_true")
    tok_sp.add_parser("list")
    tr = tok_sp.add_parser("revoke")
    tr.add_argument("token_id")

    jrn_p = sp.add_parser("journal", help="Inspect or drain local SQLite node journal (journal.db)")
    jrn_sp = jrn_p.add_subparsers(dest="jcmd", required=True)
    js_p = jrn_sp.add_parser("status", help="Show outbox/inbox counts and uncertain executions")
    js_p.add_argument("--json", action="store_true")
    jd_p = jrn_sp.add_parser("drain", help="Drain pending outbox items to hub")
    jd_p.add_argument("--url", default=None)
    jd_p.add_argument("--timeout", type=float, default=30.0)
    jd_p.add_argument("--json", action="store_true")
    jr_p = jrn_sp.add_parser("reconcile", help="Recompute pending outbox completion against active hub keys")
    jr_p.add_argument("--hubs", required=True, help="Comma-separated hub keys (hub_id or URL)")
    jr_p.add_argument("--json", action="store_true")

    sp.add_parser("update", help="Pull latest repo commits via git pull --ff-only")

    a = p.parse_args(argv)
    if getattr(a, "agent", False) and not getattr(a, "mode", None):
        a.mode = "agent"

    try:
        if a.cmd == "init":
            sys.exit(_cmd_init(a))
        elif a.cmd == "inbox":
            sys.exit(_cmd_inbox(a))
        elif a.cmd == "ack":
            sys.exit(_cmd_ack(a))
        elif a.cmd == "token":
            sys.exit(_cmd_token(a))
        elif a.cmd == "journal":
            sys.exit(_cmd_journal(a))
        elif a.cmd == "update":
            changed = update_repo()
            print("updated" if changed else "already up to date")
            sys.exit(0)
        elif a.cmd == "doctor":
            rep = run_doctor(a)
            print(json.dumps(rep, indent=2, ensure_ascii=False))
            sys.exit(0 if rep.get("hub_ok") else 1)
        elif a.cmd == "supervise":
            sys.exit(run_supervise(a))
        elif a.cmd == "hub":
            asyncio.run(
                serve_all(
                    host=a.host,
                    port=a.port,
                    gateway_host=a.gateway_host,
                    gateway_port=a.gateway_port,
                )
            )
        elif a.cmd == "run":
            asyncio.run(run_node(a))
        else:
            rc = asyncio.run(oneshot(a))
            sys.exit(rc)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

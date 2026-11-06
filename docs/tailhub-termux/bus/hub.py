#!/usr/bin/env python3
"""Tailnet Agent Bus 허브 (별형 토폴로지, 1개 노드에서 실행).
- 신원: 접속 IP 를 `tailscale whois` 로 노드명 확인 (자기신고 name 은 label 로만 사용)
- 허용 노드: ~/.config/agent-bus/allow.txt (한 줄에 노드명, 접속마다 다시 읽음 -> 재시작 불필요)
- 메시지: {op:send,id,to,text,hop}  to = 노드명 | 노드/라벨 | *   (hop > MAX_HOP 이면 폐기: 루프 차단)
- 오프라인 큐: 재접속 시 hello 의 since(마지막 seq) 이후 메시지 재전송. 보존 24h / 최대 500건
- 감사로그: ~/logs/agent-bus/messages.jsonl, 노드당 분당 20건 제한, 본문 16KB 제한
호환: 구 ws/client.py(hello={auth,name}, 메시지={text,to}) 도 그대로 접속된다.
"""
import asyncio, collections, json, os, pathlib, subprocess, time, uuid
import websockets

HOME = pathlib.Path.home()
CFG = HOME / ".config/agent-bus"
LOGD = HOME / "logs/agent-bus"
HOST = os.environ.get("BUS_HOST", "100.87.35.41")
PORT = int(os.environ.get("BUS_PORT", "8765"))
MAX_HOP, MAX_TEXT, RATE_PER_MIN = 3, 16384, 20
QUEUE_TTL, QUEUE_MAX = 86400, 500
ALIASES = {"pc": "cat-food", "termux": "z-fold8", "phone": "z-fold8", "claw8": "msi"}
DEFAULT_ALLOW = {"cat-food", "z-fold8"}

PEERS = {}                      # "node/label" -> ws
QUEUE = collections.deque(maxlen=QUEUE_MAX)
SEEN = collections.deque(maxlen=2000)
SEQ = 0
RATE = collections.defaultdict(list)
_WHO = {}

def allowed():
    p = CFG / "allow.txt"
    if not p.exists():
        CFG.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(sorted(DEFAULT_ALLOW)) + "\n")
    return {l.strip().lower() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")}

def whois_sync(ip):
    r = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True, text=True, timeout=5)
    n = json.loads(r.stdout)["Node"]
    return (n.get("ComputedName") or n.get("Name", "")).split(".")[0].lower()

async def who(ip):
    c = _WHO.get(ip)
    if c and time.time() - c[1] < 60: return c[0]
    try: node = await asyncio.get_running_loop().run_in_executor(None, whois_sync, ip)
    except Exception: node = ""
    _WHO[ip] = (node, time.time())
    return node

def resolve(to):
    to = (to or "*").strip().lower()
    node, _, label = to.partition("/")
    return ALIASES.get(node, node) + (("/" + label) if label else "")

def matches(msg, node, key):
    to = msg["to"]
    if to == "*": return msg["fnode"] != node
    return to in (node, key)

def log(msg):
    LOGD.mkdir(parents=True, exist_ok=True)
    with open(LOGD / "messages.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")

def load_queue():
    global SEQ
    p = LOGD / "messages.jsonl"
    if not p.exists(): return
    now = time.time()
    for line in p.read_text(encoding="utf-8").splitlines()[-QUEUE_MAX * 2:]:
        try: m = json.loads(line)
        except Exception: continue
        SEQ = max(SEQ, m.get("seq", 0))
        if now - m.get("ts", 0) < QUEUE_TTL and m.get("op") == "msg": QUEUE.append(m)

def sysmsg(text, **kw):
    return json.dumps({"op": "sys", "from": "relay", "text": text, **kw}, ensure_ascii=False)

async def handler(ws):
    global SEQ
    key = None
    try:
        ip = (ws.remote_address or ("",))[0]
        node = await who(ip)
        if not node or node not in allowed():
            await ws.close(4003, "node not allowed"); return
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        label = "".join(c for c in str(hello.get("label") or hello.get("name") or "main") if c.isalnum() or c in "-_")[:24] or "main"
        key = f"{node}/{label}"
        since = int(hello.get("since", 0) or 0)
        if key in PEERS: await PEERS[key].close(4002, "replaced")
        PEERS[key] = ws
        await ws.send(sysmsg(f"connected as {key}; peers={sorted(PEERS)}", you=key, peers=sorted(PEERS), seq=SEQ))
        now = time.time()
        for m in list(QUEUE):
            if m["seq"] > since and now - m["ts"] < QUEUE_TTL and matches(m, node, key):
                await ws.send(json.dumps({**m, "replay": True}, ensure_ascii=False))
        async for raw in ws:
            m = json.loads(raw)
            op = m.get("op", "send")
            if op == "peers":
                await ws.send(sysmsg("peers=" + ",".join(sorted(PEERS)), peers=sorted(PEERS))); continue
            if op != "send": continue
            t = time.time(); RATE[node] = [x for x in RATE[node] if t - x < 60]
            text = str(m.get("text", ""))
            if len(RATE[node]) >= RATE_PER_MIN: await ws.send(sysmsg("rate limit exceeded", error="rate")); continue
            if len(text) > MAX_TEXT: await ws.send(sysmsg("text too large", error="size")); continue
            hop = int(m.get("hop", 0) or 0)
            if hop > MAX_HOP: await ws.send(sysmsg(f"dropped: hop {hop} > {MAX_HOP} (loop guard)", error="hop")); continue
            mid = str(m.get("id") or uuid.uuid4().hex[:12])
            if mid in SEEN: continue
            SEEN.append(mid); RATE[node].append(t)
            SEQ += 1
            msg = {"op": "msg", "seq": SEQ, "id": mid, "from": key, "fnode": node,
                   "to": resolve(m.get("to")), "text": text, "hop": hop, "ts": t}
            QUEUE.append(msg); log(msg)
            out = json.dumps(msg, ensure_ascii=False); delivered = []
            for k, w in list(PEERS.items()):
                if k != key and matches(msg, k.split("/")[0], k):
                    try: await w.send(out); delivered.append(k)
                    except Exception: pass
            dl = delivered or "queued"
            await ws.send(sysmsg(f"ack seq={SEQ} delivered={dl}", ack=mid, seq=SEQ, delivered=delivered))
    except Exception:
        pass
    finally:
        if key and PEERS.get(key) is ws: del PEERS[key]

async def main():
    load_queue()
    async with websockets.serve(handler, HOST, PORT, max_size=1 << 20, ping_interval=20, ping_timeout=20):
        print(f"agent-bus hub ws://{HOST}:{PORT} allow={sorted(allowed())} seq={SEQ}", flush=True)
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

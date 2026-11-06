#!/usr/bin/env python3
"""간단한 WebSocket 릴레이: 같은 토큰을 가진 클라이언트끼리 메시지를 중계한다.
  python3 relay.py [--host 100.87.35.41] [--port 8765]
토큰: 환경변수 WS_TOKEN 또는 ~/.config/ws-relay/token (없으면 생성)
프로토콜(JSON): 첫 메시지 {"auth":TOKEN,"name":NAME} -> 이후 {"text":...,"to":NAME(선택)}
"""
import argparse, asyncio, json, os, pathlib, secrets
import websockets

def load_token():
    t = os.environ.get("WS_TOKEN")
    if t: return t
    p = pathlib.Path.home()/".config/ws-relay/token"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(24)); p.chmod(0o600)
    return p.read_text().strip()

TOKEN = load_token()
# Tailscale 이 인증한 기기(소스 IP)는 토큰 없이 허용. 그 외는 토큰 필수.
TRUSTED = set(filter(None, os.environ.get("WS_TRUSTED_IPS", "100.94.216.66").split(",")))
PEERS = {}  # name -> ws

async def handler(ws):
    name = None
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        peer_ip = (ws.remote_address or ("",))[0]
        if peer_ip not in TRUSTED and not secrets.compare_digest(str(hello.get("auth", "")), TOKEN):
            await ws.close(4001, "bad token"); return
        name = str(hello.get("name") or "anon")[:32]
        if name in PEERS: await PEERS[name].close(4002, "replaced")
        PEERS[name] = ws
        await ws.send(json.dumps({"from": "relay", "text": f"connected as {name}; peers={sorted(PEERS)}"}))
        async for raw in ws:
            m = json.loads(raw)
            out = json.dumps({"from": name, "text": str(m.get("text", ""))})
            to = m.get("to")
            targets = [PEERS[to]] if to in PEERS else [w for n, w in PEERS.items() if n != name]
            await asyncio.gather(*(w.send(out) for w in targets), return_exceptions=True)
    except Exception:
        pass
    finally:
        if name and PEERS.get(name) is ws: del PEERS[name]

async def main(host, port):
    async with websockets.serve(handler, host, port, max_size=1<<20):
        print(f"relay listening ws://{host}:{port}  (token: ~/.config/ws-relay/token)", flush=True)
        await asyncio.Future()

if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--host", default="100.87.35.41"); a.add_argument("--port", type=int, default=8765)
    a = a.parse_args(); asyncio.run(main(a.host, a.port))

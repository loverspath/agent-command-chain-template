#!/usr/bin/env python3
"""릴레이 클라이언트. chat 모드(기본): 터미널 입출력. agent 모드: 받은 메시지를 읽기전용 claude -p 로 처리해 회신.
  python3 client.py --url ws://100.87.35.41:8765 --name termux [--agent] [--to NAME]
토큰: WS_TOKEN 환경변수 또는 ~/.config/ws-relay/token
"""
import argparse, asyncio, json, os, pathlib, subprocess, sys
import websockets

def token():
    if os.environ.get("WS_TOKEN"): return os.environ["WS_TOKEN"]
    p = pathlib.Path.home()/".config/ws-relay/token"
    return p.read_text().strip() if p.exists() else ""  # 신뢰 IP(Tailscale)는 토큰 불필요

def ask_claude(prompt):
    r = subprocess.run(["claude", "-p", prompt, "--allowedTools", "Read,Grep,Glob"],
                       capture_output=True, text=True, timeout=300,
                       cwd=str(pathlib.Path.home()))  # Termux claude 는 ./.local/share/claude/setdns.js 상대경로 preload 를 쓰므로 항상 홈에서 실행
    return (r.stdout or r.stderr).strip()[:8000]

async def run(a):
    async with websockets.connect(a.url) as ws:
        await ws.send(json.dumps({"auth": token(), "name": a.name}))
        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                print("[%s] %s" % (m["from"], m["text"]), flush=True)
                if a.agent and m["from"] != "relay":
                    loop = asyncio.get_running_loop()
                    ans = await loop.run_in_executor(None, ask_claude, m["text"])
                    await ws.send(json.dumps({"text": ans, "to": m["from"]}))
        async def writer():
            loop = asyncio.get_running_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line: break
                msg = {"text": line.strip()}
                if a.to: msg["to"] = a.to
                await ws.send(json.dumps(msg))
        tasks = [reader()] + ([] if a.agent else [writer()])
        await asyncio.wait([asyncio.ensure_future(t) for t in tasks], return_when=asyncio.FIRST_COMPLETED)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://100.87.35.41:8765"); p.add_argument("--name", default="client")
    p.add_argument("--agent", action="store_true"); p.add_argument("--to")
    asyncio.run(run(p.parse_args()))

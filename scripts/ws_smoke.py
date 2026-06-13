from __future__ import annotations

import asyncio
import json

import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765/v1/realtime") as ws:
        first = json.loads(await ws.recv())
        print("recv:", first["type"])
        await ws.send(json.dumps({"type": "session.update", "session": {"instructions": "smoke"}}))
        second = json.loads(await ws.recv())
        print("recv:", second["type"])


if __name__ == "__main__":
    asyncio.run(main())

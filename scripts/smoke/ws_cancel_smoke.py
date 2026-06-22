from __future__ import annotations

import asyncio
import json

import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765/v1/realtime") as ws:
        print("recv:", json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type": "session.update", "session": {"instructions": "cancel smoke"}}))
        print("recv:", json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type": "response.create"}))

        saw_delta = False
        cancelled = False
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            print("recv:", event["type"])
            if event["type"] == "error":
                print("error:", event.get("error"))
                raise SystemExit(1)
            if event["type"] == "response.output_audio.delta":
                saw_delta = True
                await ws.send(json.dumps({"type": "response.cancel"}))
            if event["type"] == "response.done":
                cancelled = event.get("response", {}).get("status") == "cancelled"
                break

        extra_delta = False
        try:
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.25))
                print("recv:", event["type"])
                if event["type"] == "response.output_audio.delta":
                    extra_delta = True
                    break
        except asyncio.TimeoutError:
            pass

        print("saw_delta:", saw_delta)
        print("cancelled:", cancelled)
        print("extra_delta_after_cancel:", extra_delta)
        if not saw_delta or not cancelled or extra_delta:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import base64
import json
import math
import time

import websockets


def safe_print(label: str, value) -> None:
    text = f"{label} {value}"
    print(text.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace"))


def pcm_tone(sample_rate: int, duration_s: float, amplitude: float = 0.18) -> bytes:
    frames = int(sample_rate * duration_s)
    out = bytearray()
    for i in range(frames):
        sample = int(math.sin(2 * math.pi * 440 * i / sample_rate) * amplitude * 32767)
        out.extend(sample.to_bytes(2, "little", signed=True))
    return bytes(out)


def pcm_silence(sample_rate: int, duration_s: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * duration_s)


async def send_audio(ws, raw: bytes, sample_rate: int, chunk_ms: int = 40) -> None:
    bytes_per_chunk = int(sample_rate * chunk_ms / 1000) * 2
    for i in range(0, len(raw), bytes_per_chunk):
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(raw[i : i + bytes_per_chunk]).decode("ascii"),
                }
            )
        )
        await asyncio.sleep(chunk_ms / 1000)


async def main() -> None:
    sample_rate = 16000
    async with websockets.connect("ws://127.0.0.1:8765/v1/realtime") as ws:
        print("recv:", json.loads(await ws.recv())["type"])
        await ws.send(json.dumps({"type": "session.update", "session": {"instructions": "smoke"}}))
        print("recv:", json.loads(await ws.recv())["type"])
        start = time.perf_counter()
        await send_audio(ws, pcm_tone(sample_rate, 0.7) + pcm_silence(sample_rate, 0.9), sample_rate)

        audio_deltas = 0
        first_audio_at = None
        speech_stopped_at = None
        transcript = None
        assistant = None
        deadline = asyncio.get_running_loop().time() + 240
        while asyncio.get_running_loop().time() < deadline:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
            print("recv:", event["type"])
            if event["type"] == "error":
                print("error:", event.get("error"))
                raise SystemExit(1)
            if event["type"] == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript")
            if event["type"] == "input_audio_buffer.speech_stopped":
                speech_stopped_at = time.perf_counter()
            if event["type"] == "response.output_audio.delta":
                audio_deltas += 1
                if first_audio_at is None:
                    first_audio_at = time.perf_counter()
            if event["type"] == "response.output_audio_transcript.done":
                assistant = event.get("transcript")
            if event["type"] == "response.done":
                break

        safe_print("transcript:", transcript)
        safe_print("assistant:", assistant)
        print("audio_deltas:", audio_deltas)
        if first_audio_at is not None:
            print("first_audio_ms:", int((first_audio_at - start) * 1000))
        if first_audio_at is not None and speech_stopped_at is not None:
            print("post_speech_first_audio_ms:", int((first_audio_at - speech_stopped_at) * 1000))
        print("total_ms:", int((time.perf_counter() - start) * 1000))
        if not transcript or not assistant or audio_deltas == 0:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Telnyx standalone STT latency test.

Streams an audio file to the Telnyx STT WebSocket and reports
time-to-first-partial, time-to-final, and total wall-clock latency.

Default: benchmarks Deepgram nova-3 and flux side-by-side.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlencode

import websockets

WS_URL = "wss://api.telnyx.com/v2/speech-to-text/transcription"
CHUNK_BYTES = 2048


@dataclass
class Result:
    engine: str
    model: Optional[str]
    audio_seconds: float
    ttfp_ms: Optional[float]
    ttf_final_ms: Optional[float]
    total_ms: float
    transcript: str
    error: Optional[str] = None


def audio_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


async def stream_one(path: str, engine: str, model: Optional[str], api_key: str) -> Result:
    params = {"transcription_engine": engine, "input_format": "wav"}
    if model:
        params["transcription_model"] = model
    url = f"{WS_URL}?{urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"}

    duration = audio_duration(path)
    label = f"{engine}" + (f"/{model}" if model else "")

    t_start = time.perf_counter()
    ttfp: Optional[float] = None
    ttf_final: Optional[float] = None
    transcript_parts: list[str] = []
    error: Optional[str] = None

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:

            async def send_audio() -> None:
                with open(path, "rb") as f:
                    while chunk := f.read(CHUNK_BYTES):
                        await ws.send(chunk)
                await ws.send(json.dumps({"type": "eof"}))

            send_task = asyncio.create_task(send_audio())

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    error = "timeout waiting for response"
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("error"):
                    error = str(msg["error"])
                    break

                if "transcript" in msg:
                    now_ms = (time.perf_counter() - t_start) * 1000
                    if ttfp is None:
                        ttfp = now_ms
                    if msg.get("is_final"):
                        ttf_final = now_ms
                        text = msg.get("transcript", "").strip()
                        if text:
                            transcript_parts.append(text)
                        if send_task.done():
                            break

            await send_task
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    total_ms = (time.perf_counter() - t_start) * 1000
    return Result(
        engine=engine,
        model=model,
        audio_seconds=round(duration, 3),
        ttfp_ms=round(ttfp, 1) if ttfp else None,
        ttf_final_ms=round(ttf_final, 1) if ttf_final else None,
        total_ms=round(total_ms, 1),
        transcript=" ".join(transcript_parts),
        error=error,
    )


def print_summary(results: list[Result]) -> None:
    print()
    print(f"{'engine/model':<24} {'audio':>7} {'TTFP':>9} {'TTF-final':>11} {'total':>9}")
    print("-" * 64)
    for r in results:
        label = r.engine + (f"/{r.model}" if r.model else "")
        ttfp = f"{r.ttfp_ms:.0f}ms" if r.ttfp_ms is not None else "—"
        ttff = f"{r.ttf_final_ms:.0f}ms" if r.ttf_final_ms is not None else "—"
        print(f"{label:<24} {r.audio_seconds:>6.2f}s {ttfp:>9} {ttff:>11} {r.total_ms:>7.0f}ms")
        if r.error:
            print(f"  ERROR: {r.error}")
    print()


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        print("error: TELNYX_API_KEY not set", file=sys.stderr)
        return 2

    if not os.path.isfile(args.audio):
        print(f"error: audio file not found: {args.audio}", file=sys.stderr)
        return 2

    if args.engine:
        runs = [(args.engine, args.model)]
    else:
        runs = [("Deepgram", "nova-3"), ("Deepgram", "flux")]

    results: list[Result] = []
    for engine, model in runs:
        results.append(await stream_one(args.audio, engine, model, api_key))

    print_summary(results)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))

    return 1 if any(r.error for r in results) else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Telnyx standalone STT latency test")
    p.add_argument("--audio", default="samples/sample.wav", help="path to WAV file (default: samples/sample.wav)")
    p.add_argument("--engine", help="single engine to test (Telnyx, Deepgram, Google, Azure). Default: nova-3+flux sweep")
    p.add_argument("--model", help="model name (Deepgram only: nova-2, nova-3, flux)")
    p.add_argument("--json", action="store_true", help="print results as JSON after summary")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()

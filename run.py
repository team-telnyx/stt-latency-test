#!/usr/bin/env python3
"""Telnyx standalone STT latency test.

Streams an audio file to the Telnyx STT WebSocket and reports
streaming and finalization latency.

Default: benchmarks Deepgram nova-3 and flux side-by-side.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
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
    rtt_ms: Optional[float]
    ttf_interim_ms: Optional[float]
    ttf_final_ms: Optional[float]
    ttf_last_final_ms: Optional[float]
    total_ms: float
    transcript: str
    realtime: bool
    error: Optional[str] = None
    finals_count: int = 0


def audio_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


async def stream_one(
    path: str,
    engine: str,
    model: Optional[str],
    api_key: str,
    realtime: bool,
    prewarm_ms: int = 0,
    strip_wav_header: bool = False,
) -> Result:
    # Prewarm and header-strip both require raw PCM (linear16) because
    # input_format=wav makes the server reject any non-RIFF leading bytes.
    use_raw_pcm = strip_wav_header or prewarm_ms > 0
    params: dict[str, str] = {
        "transcription_engine": engine,
        "interim_results": "true",
    }
    if use_raw_pcm:
        params["input_format"] = "linear16"
        params["sample_rate"] = "16000"
        # When in raw mode we always skip the WAV header, regardless of flag,
        # because we declared raw PCM input.
        strip_wav_header = True
    else:
        params["input_format"] = "wav"
    if model:
        params["transcription_model"] = model
    url = f"{WS_URL}?{urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"}

    duration = audio_duration(path)

    # Pace audio to match real-time playback when --realtime is set.
    # CHUNK_BYTES=2048 / 32000 bytes-per-sec (16kHz mono s16) = 64ms per chunk.
    chunk_seconds = CHUNK_BYTES / 32000.0

    t_start = time.perf_counter()
    ttf_interim: Optional[float] = None
    ttf_first_final: Optional[float] = None
    ttf_last_final: Optional[float] = None
    transcript_parts: list[str] = []
    finals_count = 0
    error: Optional[str] = None

    rtt_ms: Optional[float] = None

    try:
        async with websockets.connect(url, additional_headers=headers, close_timeout=1) as ws:

            # Probe WebSocket RTT before streaming so we can subtract network cost
            # from the latency metrics. Median of 3 ping/pongs.
            samples: list[float] = []
            for _ in range(3):
                t0 = time.perf_counter()
                pong_waiter = await ws.ping()
                try:
                    await asyncio.wait_for(pong_waiter, timeout=5.0)
                    samples.append((time.perf_counter() - t0) * 1000)
                except asyncio.TimeoutError:
                    break
            if samples:
                samples.sort()
                rtt_ms = samples[len(samples) // 2]

            # Optional prewarm: send N ms of silence so the upstream connection
            # and Deepgram's VAD/model are warm before the real audio begins.
            # The clock starts AFTER prewarm so prewarm doesn't poison metrics.
            if prewarm_ms > 0:
                silence_bytes = (prewarm_ms * 32)  # 32 bytes per ms at 16kHz mono s16
                silence = b"\x00" * silence_bytes
                # Send in chunks at realtime pace so VAD treats it like background
                offset = 0
                while offset < len(silence):
                    await ws.send(silence[offset:offset + CHUNK_BYTES])
                    offset += CHUNK_BYTES
                    if realtime:
                        await asyncio.sleep(chunk_seconds)

            async def send_audio() -> None:
                with open(path, "rb") as f:
                    if strip_wav_header:
                        # Skip the 44-byte RIFF/fmt/data header; send raw PCM only
                        f.read(44)
                    while chunk := f.read(CHUNK_BYTES):
                        await ws.send(chunk)
                        if realtime:
                            await asyncio.sleep(chunk_seconds)
                # Tell Telnyx we're done — finalize remaining audio immediately
                await ws.send(json.dumps({"type": "CloseStream"}))

            # Reset clock now — metrics below measure from "first real-audio byte sent"
            t_start = time.perf_counter()
            send_task = asyncio.create_task(send_audio())

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    error = "timeout waiting for response"
                    break
                except websockets.ConnectionClosed:
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
                    is_final = bool(msg.get("is_final"))
                    if not is_final and ttf_interim is None:
                        ttf_interim = now_ms
                    if is_final:
                        finals_count += 1
                        if ttf_first_final is None:
                            ttf_first_final = now_ms
                        ttf_last_final = now_ms
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
        rtt_ms=round(rtt_ms, 1) if rtt_ms is not None else None,
        ttf_interim_ms=round(ttf_interim, 1) if ttf_interim is not None else None,
        ttf_final_ms=round(ttf_first_final, 1) if ttf_first_final is not None else None,
        ttf_last_final_ms=round(ttf_last_final, 1) if ttf_last_final is not None else None,
        total_ms=round(total_ms, 1),
        transcript=" ".join(transcript_parts),
        realtime=realtime,
        finals_count=finals_count,
        error=error,
    )


def fmt(ms: Optional[float]) -> str:
    return f"{ms:.0f}ms" if ms is not None else "—"


def adj(ms: Optional[float], rtt: Optional[float]) -> str:
    if ms is None or rtt is None:
        return "—"
    return f"{max(ms - rtt, 0):.0f}ms"


def print_summary(results: list[Result]) -> None:
    print()
    header = (
        f"{'engine/model':<22} {'audio':>7} {'RTT':>7} "
        f"{'first-int':>10} {'first-fin':>10} {'last-fin':>10} {'total':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        label = r.engine + (f"/{r.model}" if r.model else "")
        print(
            f"{label:<22} {r.audio_seconds:>6.2f}s {fmt(r.rtt_ms):>7} "
            f"{fmt(r.ttf_interim_ms):>10} {fmt(r.ttf_final_ms):>10} "
            f"{fmt(r.ttf_last_final_ms):>10} {r.total_ms:>7.0f}ms"
        )
        # Network-adjusted view: subtract one RTT to approximate service-only latency
        print(
            f"{'  (− RTT)':<22} {'':>7} {'':>7} "
            f"{adj(r.ttf_interim_ms, r.rtt_ms):>10} "
            f"{adj(r.ttf_final_ms, r.rtt_ms):>10} "
            f"{adj(r.ttf_last_final_ms, r.rtt_ms):>10}"
        )
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
        results.append(await stream_one(
            args.audio, engine, model, api_key, args.realtime,
            prewarm_ms=args.prewarm_ms, strip_wav_header=args.strip_wav_header,
        ))

    print_summary(results)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))

    return 1 if any(r.error for r in results) else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Telnyx standalone STT latency test")
    p.add_argument("--audio", default="samples/sample.wav", help="path to WAV file (default: samples/sample.wav)")
    p.add_argument("--engine", help="single engine to test (Telnyx, Deepgram, Google, Azure). Default: nova-3+flux sweep")
    p.add_argument("--model", help="model name (Deepgram only: nova-2, nova-3, flux)")
    p.add_argument("--realtime", action="store_true", help="pace audio at 1x to simulate a live mic")
    p.add_argument("--prewarm-ms", type=int, default=0, help="send N ms of silence before real audio to warm upstream (default: 0)")
    p.add_argument("--strip-wav-header", action="store_true", help="skip the 44-byte WAV header so only raw PCM is sent")
    p.add_argument("--json", action="store_true", help="print results as JSON after summary")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()

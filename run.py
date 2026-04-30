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

from rich.console import Console, Group
from rich.text import Text

_console = Console()

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
    show_stream: bool = False,
    on_stream=None,
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
                    text = msg.get("transcript", "").strip()
                    if not is_final and ttf_interim is None:
                        ttf_interim = now_ms
                    if text:
                        if on_stream is not None:
                            on_stream(is_final, text)
                        elif show_stream:
                            label = "final:   " if is_final else "interim: "
                            print(f"    {label} {text}", file=sys.stderr)
                    if is_final:
                        finals_count += 1
                        if ttf_first_final is None:
                            ttf_first_final = now_ms
                        ttf_last_final = now_ms
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


def eou_ms(r: Result) -> Optional[float]:
    """End-of-utterance latency: time from end-of-audio to final transcript."""
    if r.ttf_last_final_ms is None:
        return None
    return max(r.ttf_last_final_ms - r.audio_seconds * 1000.0, 0)


def clock_b_first_final_ms(r: Result) -> Optional[float]:
    """Clock B for first final: finalization delay after first interim emitted.

    We don't have sentence-boundary annotations, so we approximate the
    'lag from audio event' for the first final as the gap between first
    interim and first final — i.e. how long the engine sat on the partial
    before committing it.
    """
    if r.ttf_final_ms is None or r.ttf_interim_ms is None:
        return None
    return max(r.ttf_final_ms - r.ttf_interim_ms, 0)


RULE = "═" * 60
H1_RULE_LEN = 60

_CURRENT_ITER: dict = {"idx": 0}

# Color through-line:
#   bold yellow = EOU (the metric that matters)
#   bold cyan   = first-int / TTFT and model names in result tables
#   cyan        = model names in iteration scroll
#   dim         = secondary metrics, prefixes, explainer paragraphs
#   bold        = numbers that matter (EOU values in tables, ▸ section headers)
#   green       = ok status
#   red         = fail / errors

# Telnyx brand green from the SVG mark.
TELNYX_GREEN = "#00E3AA"

S_RULE = TELNYX_GREEN
S_SECTION = f"bold {TELNYX_GREEN}"
S_DIM = "dim"
S_BOLD = "bold"
S_EOU = "bold yellow"
S_FIRST_INT = "bold cyan"
S_MODEL_RES = "bold cyan"
S_MODEL_ITER = "cyan"
S_OK = "green"
S_FAIL = "bold red"
S_PARAGRAPH = "white"  # primary text — was implicit terminal default; now explicit white


def _h1(title: str) -> None:
    bar = "═" * H1_RULE_LEN
    pad = (H1_RULE_LEN - 6 - len(title)) // 2
    middle = "═══" + " " * pad + title + " " * (H1_RULE_LEN - 6 - pad - len(title)) + "═══"
    _console.print(Text(bar, style=S_RULE))
    _console.print(Text(middle, style=S_SECTION))
    _console.print(Text(bar, style=S_RULE))


def _section(title: str) -> None:
    _console.print(Text(RULE, style=S_RULE))
    _console.print(Text(f"  {title}", style=S_SECTION))
    _console.print(Text(RULE, style=S_RULE))


def _preamble() -> None:
    _section("WHAT YOU'RE ABOUT TO SEE — READ THIS FIRST")
    _console.print()
    _console.print(Text("▸ The metric that matters", style=S_BOLD))
    _console.print()
    p1 = Text("  Voice agents feel slow because of ONE number: ")
    p1.append("EOU latency", style=S_EOU)
    p1.append(" — the\n  dead air between when the user stops talking and when the\n  transcript locks. That's the only latency your users actually feel.")
    _console.print(p1)
    _console.print()
    _console.print(Text("▸ The marketing number", style=S_BOLD))
    _console.print()
    p2 = Text("  ")
    p2.append("TTFT (first-int)", style=S_FIRST_INT)
    p2.append(" is how fast the first word appears as you talk.\n  It tells you the pipe is alive but doesn't predict conversation\n  feel. We report it but don't optimize for it.")
    _console.print(p2)
    _console.print()
    _console.print(Text("▸ The two columns", style=S_BOLD))
    _console.print()
    p3 = Text("  ")
    p3.append("wall-clock", style=S_BOLD)
    p3.append("    The raw measurement. Stopwatch from when audio starts\n                 flowing until the transcript locks. Includes your\n                 network round-trip.")
    _console.print(p3)
    _console.print()
    p4 = Text("  ")
    p4.append("service-only", style=S_BOLD)
    p4.append("  An estimate of the engine alone. We approximate it by\n                 subtracting one measured RTT from the wall-clock number.\n                 It's not perfect — a more rigorous test would inject\n                 timestamps into the audio sample itself — but it's close\n                 enough to compare engines fairly across regions.")
    _console.print(p4)
    _console.print()


def _legend(verbose: bool) -> None:
    _section("LEGEND")
    eou_t = Text("  ")
    eou_t.append("EOU (\"End of Utterance\")", style=S_EOU)
    _console.print(eou_t)
    _console.print(Text("    The dead air after the user stops talking.", style=S_PARAGRAPH))
    _console.print(Text("    This is the number that decides how fast your bot replies.", style=S_PARAGRAPH))
    _console.print()
    fi_t = Text("  ")
    fi_t.append("first-int (\"first interim\", a.k.a. TTFT or Time To First Token)", style=S_FIRST_INT)
    _console.print(fi_t)
    _console.print(Text("    The first guess the engine ships after audio starts.", style=S_PARAGRAPH))
    _console.print(Text("    Comes back fast. Don't optimize for it.", style=S_PARAGRAPH))
    _console.print()
    total_t = Text("  ")
    total_t.append("total", style=S_BOLD)
    total_t.append("       End-to-end duration of the run.")
    _console.print(total_t)
    _console.print(Text("    Sanity check, not a comparison metric.", style=S_PARAGRAPH))
    _console.print()
    if verbose:
        _console.print(Text("  first-final time from audio-started → first final transcript"))
        _console.print(Text("  last-final  time from audio-started → last final transcript"))
        _console.print()
    rtt_t = Text("  ")
    rtt_t.append("RTT", style=S_BOLD)
    rtt_t.append(" (\"Round-Trip Time\")")
    _console.print(rtt_t)
    _console.print(Text("    Network latency between your machine and Telnyx.", style=S_PARAGRAPH))
    _console.print(Text("    We subtract one RTT from wall-clock to get service-only.", style=S_PARAGRAPH))
    _console.print()
    p_t = Text("  ")
    p_t.append("p50 / p95", style=S_BOLD)
    p_t.append("   The median (p50) and the tail (p95). Half your runs")
    _console.print(p_t)
    _console.print(Text("              beat p50; 5% are slower than p95. p50 tells you what"))
    _console.print(Text("              normal feels like; p95 tells you how bad the bad days get."))
    if not verbose:
        _console.print()
        _console.print(Text("  Tip: pass --verbose to include first-final and last-final.", style=S_DIM))


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        print("error: TELNYX_API_KEY not set", file=sys.stderr)
        return 2

    if not os.path.isfile(args.audio):
        print(f"error: audio file not found: {args.audio}", file=sys.stderr)
        return 2

    if args.engine:
        configs = [(args.engine, args.model)]
    else:
        configs = [("Deepgram", "nova-3"), ("Deepgram", "flux")]

    duration = audio_duration(args.audio)
    _h1("TELNYX DEEPGRAM STT LATENCY BENCHMARK")
    _console.print()
    _section("TEST CONFIGURATION")

    def _cfg(label: str, value: str) -> None:
        t = Text("  ")
        t.append(label, style=S_DIM)
        t.append(value)
        _console.print(t)

    _cfg("Audio:      ", f"{args.audio} ({duration:.2f} seconds)")
    if args.spoken:
        _cfg("Says:       ", f"\"{args.spoken}\"")
    _cfg("Iterations: ", f"{args.runs} per model")
    if args.prewarm_ms > 0:
        _cfg("Pre-warm:   ", f"{args.prewarm_ms}ms of silence to warm the connection")
    else:
        _cfg("Pre-warm:   ", "none (cold start)")
    _cfg("Pacing:     ", "realtime (1x) to simulate a live mic")
    _console.print()
    _preamble()
    _legend(args.verbose)
    _console.print()
    _section("RUNNING")
    _console.print()
    for line in [
        "  Each iteration runs both models back-to-back: nova-3, then flux.",
        "  We do this so network jitter affects both equally — if your Wi-Fi",
        "  blips, both models see it. Iteration 1 streams the live interim/",
        "  final transcripts so you can see what the engine is hearing.",
        "  Iterations 2+ show metrics only.",
    ]:
        _console.print(Text(line, style=S_PARAGRAPH))
    _console.print()

    all_results: list[Result] = []
    for run_idx in range(args.runs):
        _CURRENT_ITER["idx"] = run_idx + 1
        for engine, model in configs:
            model_label = model if model else (engine.lower())
            max_idx_len = len(f"[{args.runs}/{args.runs}]")
            idx_str = f"[{run_idx + 1}/{args.runs}]".rjust(max_idx_len)
            demo = run_idx == 0

            if demo:
                t = Text("  ")
                t.append(idx_str + " ", style=S_DIM)
                t.append(f"{model_label:<8}", style=S_MODEL_ITER)
                t.append(" running...", style=S_DIM)
                _console.print(t)

            stream_cb = None
            if demo:
                def _on_stream(is_final: bool, text: str) -> None:
                    label = "final:   " if is_final else "interim: "
                    line = Text("    ")
                    line.append(label, style=S_DIM)
                    line.append(text)
                    _console.print(line)
                stream_cb = _on_stream

            r = await stream_one(
                args.audio, engine, model, api_key, True,
                prewarm_ms=args.prewarm_ms, strip_wav_header=args.strip_wav_header,
                on_stream=stream_cb,
            )
            all_results.append(r)

            eou = eou_ms(r)
            eou_str = f"{eou:.0f}ms" if eou is not None else "—"
            fi_str = f"{r.ttf_interim_ms:.0f}ms" if r.ttf_interim_ms is not None else "—"
            if demo:
                line = Text("    ")
                line.append("metrics:  ", style=S_DIM)
                line.append("EOU ", style=S_EOU)
                line.append(eou_str, style=S_EOU)
                line.append("   first-int ", style=S_DIM)
                line.append(fi_str, style=S_DIM)
                _console.print(line)
                _console.print()
            else:
                eou_str_pad = f"{eou:>5.0f}ms" if eou is not None else "    —  "
                fi_str_pad = f"{r.ttf_interim_ms:>5.0f}ms" if r.ttf_interim_ms is not None else "    —  "
                line = Text("  ")
                line.append(idx_str + " ", style=S_DIM)
                line.append(f"{model_label:<8}", style=S_MODEL_ITER)
                if not r.error:
                    line.append("   ✓    ", style=S_OK)
                else:
                    line.append("   ✗    ", style=S_FAIL)
                line.append("EOU ", style=S_EOU)
                line.append(eou_str_pad, style=S_EOU)
                line.append("   first-int ", style=S_DIM)
                line.append(fi_str_pad, style=S_DIM)
                _console.print(line)

    _console.print()
    _console.print(Text("  Transcripts captured:", style=S_DIM))
    for engine, model in configs:
        label = model if model else engine.lower()
        rs = [r for r in all_results if r.engine == engine and r.model == model and not r.error]
        transcripts = [r.transcript.strip() for r in rs if r.transcript.strip()]
        if not transcripts:
            _console.print(Text(f"    {label:<10} (no transcript captured)", style=S_DIM))
            continue
        unique = set(transcripts)
        canonical = max(unique, key=lambda t: transcripts.count(t))
        agree = transcripts.count(canonical)
        _console.print(Text(f"    {label:<10} {agree}/{len(rs)} agreed: \"{canonical}\""))
        if len(unique) > 1:
            _console.print(Text(f"               note: {len(unique) - 1} iteration(s) returned different text — see --json", style=S_DIM))

    print_aggregate(all_results, configs, args.verbose)

    if args.json:
        print(json.dumps([asdict(r) for r in all_results], indent=2))

    return 1 if any(r.error for r in all_results) else 0


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, p50, p95, stddev) of non-empty values."""
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0, 0.0)
    s = sorted(values)
    mean = sum(values) / n
    p50 = s[n // 2]
    p95 = s[min(n - 1, int(n * 0.95))]
    variance = sum((v - mean) ** 2 for v in values) / n
    return (mean, p50, p95, variance ** 0.5)


def print_aggregate(results: list[Result], configs: list[tuple[str, Optional[str]]], verbose: bool) -> None:
    _console.print()
    _section("RESULTS")
    _console.print()
    for line in [
        "  Both columns shown side-by-side keeps us honest. Wall-clock is the",
        "  full number including your network — service-only is what we estimate",
        "  the engine alone is doing. You see both, you do the math.",
    ]:
        _console.print(Text(line, style=S_PARAGRAPH))
    _console.print()
    for engine, model in configs:
        rs = [r for r in results if r.engine == engine and r.model == model]
        ok = [r for r in rs if not r.error]
        label = model if model else engine.lower()

        def wall(attr: str) -> list[float]:
            return [getattr(r, attr) for r in ok if getattr(r, attr) is not None]

        def service(attr: str) -> list[float]:
            out: list[float] = []
            for r in ok:
                v = getattr(r, attr)
                if v is not None and r.rtt_ms is not None:
                    out.append(max(v - r.rtt_ms, 0))
            return out

        eou_wall = [v for v in (eou_ms(r) for r in ok) if v is not None]
        eou_service = [
            max(v - r.rtt_ms, 0)
            for r, v in ((r, eou_ms(r)) for r in ok)
            if v is not None and r.rtt_ms is not None
        ]
        rtt_vals = [r.rtt_ms for r in ok if r.rtt_ms is not None]

        rows: list[tuple[str, list[float], Optional[list[float]]]] = [
            ("EOU", eou_wall, eou_service),
            ("first-int", wall("ttf_interim_ms"), service("ttf_interim_ms")),
            ("total", [r.total_ms for r in ok], service("total_ms")),
        ]
        if verbose:
            rows.append(("first-final", wall("ttf_final_ms"), service("ttf_final_ms")))
            rows.append(("last-final", wall("ttf_last_final_ms"), service("ttf_last_final_ms")))
        rows.append(("RTT", rtt_vals, None))

        label_t = Text("  ")
        label_t.append(label, style=S_MODEL_RES)
        _console.print(label_t)
        _console.print(Text(f"  ({len(ok)}/{len(rs)} iterations)", style=S_DIM))
        _console.print()

        header = Text(f"  {'':<11} ")
        header.append(f"{'service-only (- RTT)':<38}", style=S_DIM)
        header.append("  wall-clock", style=S_DIM)
        _console.print(header)

        underline = Text(f"  {'':<11} ")
        underline.append(f"{'-' * 20:<38}", style=S_DIM)
        underline.append("  " + "-" * 10, style=S_DIM)
        _console.print(underline)

        for metric_name, vals, svc in rows:
            if not vals:
                _console.print(Text(f"  {metric_name:<11} no data", style=S_DIM))
                continue
            mean, p50, p95, _sd = _stats(vals)
            is_eou = (metric_name == "EOU")
            metric_style = S_EOU if is_eou else S_DIM
            num_style = S_BOLD if is_eou else S_DIM
            line = Text("  ")
            line.append(f"{metric_name:<11} ", style=metric_style)
            if svc:
                s_mean, s_p50, s_p95, _ = _stats(svc)
                svc_str = f"mean {s_mean:>4.0f}ms  p50 {s_p50:>4.0f}ms  p95 {s_p95:>4.0f}ms"
                line.append(f"{svc_str:<38}", style=num_style)
            else:
                line.append(" " * 38, style=num_style)
            wall_str = f"  mean {mean:>4.0f}ms  p50 {p50:>4.0f}ms  p95 {p95:>4.0f}ms"
            line.append(wall_str, style=num_style)
            _console.print(line)
        _console.print()


def main() -> None:
    p = argparse.ArgumentParser(description="Telnyx standalone STT latency test")
    p.add_argument("--audio", default="samples/sample.wav", help="path to WAV file (default: samples/sample.wav)")
    p.add_argument("--spoken", default="Hello, my name is Jon and I'm testing speech recognition.", help="text spoken in the audio file, displayed in the test configuration")
    p.add_argument("--engine", help="single engine to test (Telnyx, Deepgram, Google, Azure). Default: nova-3+flux sweep")
    p.add_argument("--model", help="model name (Deepgram only: nova-2, nova-3, flux)")
    p.add_argument("--prewarm-ms", type=int, default=1000, help="send N ms of silence before real audio to warm the upstream connection + Deepgram VAD/model. Default 1000ms reflects the warmed-state latency a real voice agent experiences. Set to 0 to measure cold-start.")
    p.add_argument("--strip-wav-header", action="store_true", help="skip the 44-byte WAV header so only raw PCM is sent")
    p.add_argument("--runs", type=int, default=1, help="number of times to run each (engine, model) — reports mean/p50/p95/stddev (default: 1)")
    p.add_argument("--verbose", action="store_true", help="include first-final and last-final in the report (off by default)")
    p.add_argument("--json", action="store_true", help="print results as JSON after summary")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        idx = _CURRENT_ITER["idx"]
        if idx and args.runs > 1:
            print(file=sys.stderr)
            print(f"Interrupted at iteration {idx}/{args.runs}. Partial results "
                  "discarded — re-run to get a full benchmark.", file=sys.stderr)
        else:
            print(file=sys.stderr)
            print("Interrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

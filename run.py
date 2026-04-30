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
from typing import Callable, Optional
from urllib.parse import urlencode

import websockets

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

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
    on_stream: Optional[Callable[[bool, str], None]] = None,
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


def _h1(title: str) -> None:
    bar = "═" * H1_RULE_LEN
    inner = " " * H1_RULE_LEN
    pad = (H1_RULE_LEN - 6 - len(title)) // 2
    middle = "═══" + " " * pad + title + " " * (H1_RULE_LEN - 6 - pad - len(title)) + "═══"
    print(bar)
    print(middle)
    print(bar)


def _section(title: str) -> None:
    print(RULE)
    print(f"  {title}")
    print(RULE)


def _preamble() -> None:
    _section("WHAT YOU'RE ABOUT TO SEE — READ THIS FIRST")
    print()
    print("▸ The metric that matters")
    print()
    print("  Voice agents feel slow because of ONE number: EOU latency — the")
    print("  dead air between when the user stops talking and when the")
    print("  transcript locks. That's the only latency your users actually feel.")
    print()
    print("▸ The marketing number")
    print()
    print("  TTFT (first-int) is how fast the first word appears as you talk.")
    print("  It tells you the pipe is alive but doesn't predict conversation")
    print("  feel. We report it but don't optimize for it.")
    print()
    print("▸ The two columns")
    print()
    print("  wall-clock     The raw measurement. Stopwatch from when audio starts")
    print("                 flowing until the transcript locks. Includes your")
    print("                 network round-trip.")
    print()
    print("  service-only   An estimate of the engine alone. We approximate it by")
    print("                 subtracting one measured RTT from the wall-clock number.")
    print("                 It's not perfect — a more rigorous test would inject")
    print("                 timestamps into the audio sample itself — but it's close")
    print("                 enough to compare engines fairly across regions.")
    print()


def _legend(verbose: bool) -> None:
    _section("LEGEND")
    print("  EOU (\"End of Utterance\")")
    print("              The dead air after the user stops talking.")
    print("              This is the number that decides how fast your bot replies.")
    print()
    print("  first-int (\"first interim\", a.k.a. TTFT or Time To First Token)")
    print("              The first guess the engine ships after audio starts.")
    print("              Comes back fast. Don't optimize for it.")
    print()
    print("  total       End-to-end duration of the run.")
    print("              Sanity check, not a comparison metric.")
    print()
    if verbose:
        print("  first-final time from audio-started → first final transcript")
        print("  last-final  time from audio-started → last final transcript")
        print()
    print("  RTT (\"Round-Trip Time\")")
    print("              Network latency between your machine and Telnyx.")
    print("              We subtract one RTT from wall-clock to get service-only.")
    print()
    print("  p50 / p95   The median (p50) and the tail (p95). Half your runs")
    print("              beat p50; 5% are slower than p95. p50 tells you what")
    print("              normal feels like; p95 tells you how bad the bad days get.")
    if not verbose:
        print()
        print("  Tip: pass --verbose to include first-final and last-final.")


def print_summary(results: list[Result], verbose: bool) -> None:
    print()
    print("Results (service-only | wall-clock)")
    print()
    for r in results:
        label = r.engine + (f"/{r.model}" if r.model else "")
        eou = eou_ms(r)
        print(f"  {label}: EOU {adj(eou, r.rtt_ms)} | {fmt(eou)}  "
              f"first-int {adj(r.ttf_interim_ms, r.rtt_ms)} | {fmt(r.ttf_interim_ms)}  "
              f"RTT {fmt(r.rtt_ms)}")
        if r.error:
            print(f"    ERROR: {r.error}")
    print()
    _legend(verbose)
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
        configs = [(args.engine, args.model)]
    else:
        configs = [("Deepgram", "nova-3"), ("Deepgram", "flux")]

    use_rich = sys.stdout.isatty() and not args.no_rich

    if use_rich:
        console = Console()
        all_results, exit_code = await _run_with_rich(args, configs, api_key, console)
        if args.json:
            print(json.dumps([asdict(r) for r in all_results], indent=2))
        return exit_code

    if args.runs > 1:
        duration = audio_duration(args.audio)
        _h1("DEEPGRAM STT LATENCY BENCHMARK")
        print()
        _section("TEST CONFIGURATION")
        print(f"  Audio:      {args.audio} ({duration:.2f} seconds)")
        if args.spoken:
            print(f"  Says:       \"{args.spoken}\"")
        print(f"  Iterations: {args.runs} per model")
        if args.prewarm_ms > 0:
            print(f"  Pre-warm:   {args.prewarm_ms}ms of silence to warm the connection")
        else:
            print("  Pre-warm:   none (cold start)")
        if args.realtime:
            print("  Pacing:     realtime (1x) to simulate a live mic")
        else:
            print("  Pacing:     as fast as possible (batch mode)")
        print()
        _preamble()
        _legend(args.verbose)
        print()
        _section("RUNNING")
        print()
        print("  Each iteration runs both models back-to-back: nova-3, then flux.")
        print("  We do this so network jitter affects both equally — if your Wi-Fi")
        print("  blips, both models see it. Iteration 1 streams the live interim/")
        print("  final transcripts so you can see what the engine is hearing.")
        print("  Iterations 2+ show metrics only.")
        print()

    all_results: list[Result] = []
    for run_idx in range(args.runs):
        _CURRENT_ITER["idx"] = run_idx + 1
        for engine, model in configs:
            model_label = model if model else (engine.lower())
            rw = len(str(args.runs))
            idx_str = f"[{run_idx + 1:>{rw}}/{args.runs}]"
            demo = run_idx == 0 and args.runs > 1

            if demo:
                print(f"  {idx_str} {model_label:<8} running...", file=sys.stderr)

            r = await stream_one(
                args.audio, engine, model, api_key, args.realtime,
                prewarm_ms=args.prewarm_ms, strip_wav_header=args.strip_wav_header,
                show_stream=demo,
            )
            all_results.append(r)

            if args.runs > 1:
                marker = "ok  " if not r.error else "fail"
                eou = eou_ms(r)
                eou_str = f"{eou:>5.0f}ms" if eou is not None else "    —  "
                fi_str = f"{r.ttf_interim_ms:>5.0f}ms" if r.ttf_interim_ms is not None else "    —  "
                if demo:
                    print(f"    metrics:  EOU {eou_str.strip()}   first-int {fi_str.strip()}",
                          file=sys.stderr)
                    print(file=sys.stderr)
                else:
                    print(f"  {idx_str} {model_label:<8} {marker}  "
                          f"EOU {eou_str}   first-int {fi_str}",
                          file=sys.stderr)

    if args.runs > 1:
        print()
        print("  Transcripts captured:")
        for engine, model in configs:
            label = model if model else engine.lower()
            rs = [r for r in all_results if r.engine == engine and r.model == model and not r.error]
            transcripts = [r.transcript.strip() for r in rs if r.transcript.strip()]
            if not transcripts:
                print(f"    {label:<10} (no transcript captured)")
                continue
            unique = set(transcripts)
            canonical = max(unique, key=lambda t: transcripts.count(t))
            agree = transcripts.count(canonical)
            print(f"    {label:<10} {agree}/{len(rs)} agreed: \"{canonical}\"")
            if len(unique) > 1:
                print(f"               note: {len(unique) - 1} iteration(s) returned different text — see --json")

    if args.runs > 1:
        print_aggregate(all_results, configs, args.verbose)
    else:
        print_summary(all_results, args.verbose)

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
    print()
    _section("RESULTS")
    print()
    print("  Both columns shown side-by-side keeps us honest. Wall-clock is the")
    print("  full number including your network — service-only is what we estimate")
    print("  the engine alone is doing. You see both, you do the math.")
    print()
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

        print(f"  {label}")
        print(f"  ({len(ok)}/{len(rs)} iterations)")
        print()
        print(f"  {'':<11} {'service-only (- RTT)':<38}  wall-clock")
        for metric_name, vals, svc in rows:
            if not vals:
                print(f"  {metric_name:<11} no data")
                continue
            mean, p50, p95, _sd = _stats(vals)
            wall_col = f"mean {mean:>4.0f}ms  p50 {p50:>4.0f}ms  p95 {p95:>4.0f}ms"
            if svc:
                s_mean, s_p50, s_p95, _ = _stats(svc)
                svc_col = f"mean {s_mean:>4.0f}ms  p50 {s_p50:>4.0f}ms  p95 {s_p95:>4.0f}ms"
                line = f"  {metric_name:<11} {svc_col:<38}  {wall_col}"
            else:
                line = f"  {metric_name:<11} {'':<38}  {wall_col}"
            print(line)
        print()


# ─────────────────────────────────────────────────────────────────
# Rich (TTY) UI
# ─────────────────────────────────────────────────────────────────

# Color through-line for the spec:
#   bold yellow = EOU (the metric that matters)
#   bold cyan   = first-int / TTFT and model names in result tables
#   cyan        = model names in iteration scroll
#   dim         = secondary metrics, prefixes, explainer paragraphs
#   bold        = numbers that matter
#   green       = ok
#   red         = fail / errors

S_RULE = "bright_white"
S_SECTION = "bold bright_white"
S_DIM = "dim"
S_BOLD = "bold"
S_EOU = "bold yellow"
S_FIRST_INT = "bold cyan"
S_MODEL_RES = "bold cyan"
S_MODEL_ITER = "cyan"
S_OK = "green"
S_FAIL = "bold red"


def _r_rule() -> Text:
    return Text(RULE, style=S_RULE)


def _r_section(title: str) -> Group:
    return Group(_r_rule(), Text(f"  {title}", style=S_SECTION), _r_rule())


def _r_h1(title: str) -> Group:
    bar = "═" * H1_RULE_LEN
    pad = (H1_RULE_LEN - 6 - len(title)) // 2
    middle = "═══" + " " * pad + title + " " * (H1_RULE_LEN - 6 - pad - len(title)) + "═══"
    return Group(
        Text(bar, style=S_RULE),
        Text(middle, style=S_SECTION),
        Text(bar, style=S_RULE),
    )


def _r_test_config(args: argparse.Namespace, duration: float) -> Group:
    rows = []
    rows.append(_r_section("TEST CONFIGURATION"))
    def _row(label: str, value: str) -> Text:
        t = Text("  ")
        t.append(label, style=S_DIM)
        t.append(value)
        return t
    rows.append(_row(f"Audio:      ", f"{args.audio} ({duration:.2f} seconds)"))
    if args.spoken:
        rows.append(_row("Says:       ", f"\"{args.spoken}\""))
    rows.append(_row("Iterations: ", f"{args.runs} per model"))
    if args.prewarm_ms > 0:
        rows.append(_row("Pre-warm:   ", f"{args.prewarm_ms}ms of silence to warm the connection"))
    else:
        rows.append(_row("Pre-warm:   ", "none (cold start)"))
    if args.realtime:
        rows.append(_row("Pacing:     ", "realtime (1x) to simulate a live mic"))
    else:
        rows.append(_row("Pacing:     ", "as fast as possible (batch mode)"))
    return Group(*rows)


def _r_preamble() -> Group:
    out = []
    out.append(_r_section("WHAT YOU'RE ABOUT TO SEE — READ THIS FIRST"))
    out.append(Text(""))
    out.append(Text("▸ The metric that matters", style=S_BOLD))
    out.append(Text(""))
    p1 = Text("  Voice agents feel slow because of ONE number: ")
    p1.append("EOU latency", style=S_EOU)
    p1.append(" — the\n  dead air between when the user stops talking and when the\n  transcript locks. That's the only latency your users actually feel.")
    out.append(p1)
    out.append(Text(""))
    out.append(Text("▸ The marketing number", style=S_BOLD))
    out.append(Text(""))
    p2 = Text("  ")
    p2.append("TTFT (first-int)", style=S_FIRST_INT)
    p2.append(" is how fast the first word appears as you talk.\n  It tells you the pipe is alive but doesn't predict conversation\n  feel. We report it but don't optimize for it.")
    out.append(p2)
    out.append(Text(""))
    out.append(Text("▸ The two columns", style=S_BOLD))
    out.append(Text(""))
    p3 = Text("  ")
    p3.append("wall-clock", style=S_BOLD)
    p3.append("    The raw measurement. Stopwatch from when audio starts\n                 flowing until the transcript locks. Includes your\n                 network round-trip.")
    out.append(p3)
    out.append(Text(""))
    p4 = Text("  ")
    p4.append("service-only", style=S_BOLD)
    p4.append("  An estimate of the engine alone. We approximate it by\n                 subtracting one measured RTT from the wall-clock number.\n                 It's not perfect — a more rigorous test would inject\n                 timestamps into the audio sample itself — but it's close\n                 enough to compare engines fairly across regions.")
    out.append(p4)
    return Group(*out)


def _r_legend(verbose: bool) -> Group:
    out = []
    out.append(_r_section("LEGEND"))
    eou_t = Text("  ")
    eou_t.append("EOU", style=S_EOU)
    eou_t.append(" (\"End of Utterance\")")
    out.append(eou_t)
    out.append(Text("    The dead air after the user stops talking.", style=S_DIM))
    out.append(Text("    This is the number that decides how fast your bot replies.", style=S_DIM))
    out.append(Text(""))
    fi_t = Text("  ")
    fi_t.append("first-int", style=S_FIRST_INT)
    fi_t.append(" (\"first interim\", a.k.a. TTFT or Time To First Token)")
    out.append(fi_t)
    out.append(Text("    The first guess the engine ships after audio starts.", style=S_DIM))
    out.append(Text("    Comes back fast. Don't optimize for it.", style=S_DIM))
    out.append(Text(""))
    total_t = Text("  ")
    total_t.append("total", style=S_BOLD)
    total_t.append("       End-to-end duration of the run.")
    out.append(total_t)
    out.append(Text("    Sanity check, not a comparison metric.", style=S_DIM))
    out.append(Text(""))
    if verbose:
        out.append(Text("  first-final time from audio-started → first final transcript"))
        out.append(Text("  last-final  time from audio-started → last final transcript"))
        out.append(Text(""))
    rtt_t = Text("  ")
    rtt_t.append("RTT", style=S_BOLD)
    rtt_t.append(" (\"Round-Trip Time\")")
    out.append(rtt_t)
    out.append(Text("    Network latency between your machine and Telnyx.", style=S_DIM))
    out.append(Text("    We subtract one RTT from wall-clock to get service-only.", style=S_DIM))
    out.append(Text(""))
    p_t = Text("  ")
    p_t.append("p50 / p95", style=S_BOLD)
    p_t.append("   The median (p50) and the tail (p95). Half your runs")
    out.append(p_t)
    out.append(Text("              beat p50; 5% are slower than p95. p50 tells you what"))
    out.append(Text("              normal feels like; p95 tells you how bad the bad days get."))
    if not verbose:
        out.append(Text(""))
        out.append(Text("  Tip: pass --verbose to include first-final and last-final.", style=S_DIM))
    return Group(*out)


def _r_running_intro() -> Group:
    out = []
    out.append(_r_section("RUNNING"))
    out.append(Text(""))
    out.append(Text("  Each iteration runs both models back-to-back: nova-3, then flux.", style=S_DIM))
    out.append(Text("  We do this so network jitter affects both equally — if your Wi-Fi", style=S_DIM))
    out.append(Text("  blips, both models see it. Iteration 1 streams the live interim/", style=S_DIM))
    out.append(Text("  final transcripts so you can see what the engine is hearing.", style=S_DIM))
    out.append(Text("  Iterations 2+ show metrics only.", style=S_DIM))
    out.append(Text(""))
    return Group(*out)


def _r_iter_header(idx_str: str, model_label: str) -> Text:
    t = Text("  ")
    t.append(idx_str + " ", style=S_DIM)
    t.append(f"{model_label:<8}", style=S_MODEL_ITER)
    t.append(" running...", style=S_DIM)
    return t


def _r_iter_stream_line(is_final: bool, text: str) -> Text:
    label = "final:   " if is_final else "interim: "
    out = Text("    ")
    out.append(label, style=S_DIM)
    out.append(text)
    return out


def _r_iter_metrics(eou: Optional[float], fi: Optional[float]) -> Text:
    eou_str = f"{eou:.0f}ms" if eou is not None else "—"
    fi_str = f"{fi:.0f}ms" if fi is not None else "—"
    out = Text("    ")
    out.append("metrics:  ", style=S_DIM)
    out.append("EOU ", style=S_EOU)
    out.append(eou_str, style=S_EOU)
    out.append("   first-int ", style=S_DIM)
    out.append(fi_str, style=S_DIM)
    return out


def _r_iter_compact(idx_str: str, model_label: str, ok: bool, eou: Optional[float], fi: Optional[float]) -> Text:
    eou_str = f"{eou:>5.0f}ms" if eou is not None else "    —  "
    fi_str = f"{fi:>5.0f}ms" if fi is not None else "    —  "
    out = Text("  ")
    out.append(idx_str + " ", style=S_DIM)
    out.append(f"{model_label:<8}", style=S_MODEL_ITER)
    if ok:
        out.append("   ok    ", style=S_OK)
    else:
        out.append("   fail  ", style=S_FAIL)
    out.append("EOU ", style=S_EOU)
    out.append(eou_str, style=S_EOU)
    out.append("   first-int ", style=S_DIM)
    out.append(fi_str, style=S_DIM)
    return out


def _r_transcripts(all_results: list, configs: list) -> Group:
    out = []
    out.append(Text(""))
    out.append(Text("  Transcripts captured:", style=S_DIM))
    for engine, model in configs:
        label = model if model else engine.lower()
        rs = [r for r in all_results if r.engine == engine and r.model == model and not r.error]
        transcripts = [r.transcript.strip() for r in rs if r.transcript.strip()]
        if not transcripts:
            out.append(Text(f"    {label:<10} (no transcript captured)"))
            continue
        unique = set(transcripts)
        canonical = max(unique, key=lambda t: transcripts.count(t))
        agree = transcripts.count(canonical)
        out.append(Text(f"    {label:<10} {agree}/{len(rs)} agreed: \"{canonical}\""))
        if len(unique) > 1:
            out.append(Text(f"               note: {len(unique) - 1} iteration(s) returned different text — see --json", style=S_DIM))
    return Group(*out)


def _r_results(all_results: list, configs: list, verbose: bool) -> Group:
    out = []
    out.append(Text(""))
    out.append(_r_section("RESULTS"))
    out.append(Text("  Both columns shown side-by-side keeps us honest. Wall-clock is the", style=S_DIM))
    out.append(Text("  full number including your network — service-only is what we estimate", style=S_DIM))
    out.append(Text("  the engine alone is doing. You see both, you do the math.", style=S_DIM))
    out.append(Text(""))

    for engine, model in configs:
        rs = [r for r in all_results if r.engine == engine and r.model == model]
        ok = [r for r in rs if not r.error]
        label = model if model else engine.lower()

        def wall(attr: str) -> list:
            return [getattr(r, attr) for r in ok if getattr(r, attr) is not None]

        def service(attr: str) -> list:
            vals = []
            for r in ok:
                v = getattr(r, attr)
                if v is not None and r.rtt_ms is not None:
                    vals.append(max(v - r.rtt_ms, 0))
            return vals

        eou_wall = [v for v in (eou_ms(r) for r in ok) if v is not None]
        eou_service = [
            max(v - r.rtt_ms, 0)
            for r, v in ((r, eou_ms(r)) for r in ok)
            if v is not None and r.rtt_ms is not None
        ]
        rtt_vals = [r.rtt_ms for r in ok if r.rtt_ms is not None]

        rows = [
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
        out.append(label_t)
        out.append(Text(f"  ({len(ok)}/{len(rs)} iterations)", style=S_DIM))
        out.append(Text(""))

        header = Text(f"  {'':<11} ")
        header.append(f"{'service-only (- RTT)':<38}", style=S_DIM)
        header.append("  wall-clock", style=S_DIM)
        out.append(header)

        for metric_name, vals, svc in rows:
            if not vals:
                out.append(Text(f"  {metric_name:<11} no data", style=S_DIM))
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
            out.append(line)
        out.append(Text(""))

    return Group(*out)


async def _run_with_rich(args: argparse.Namespace, configs: list, api_key: str, console: Console) -> tuple:
    duration = audio_duration(args.audio)

    # Clear terminal so the UI begins fresh from the top.
    console.clear()

    top = Group(
        _r_h1("DEEPGRAM STT LATENCY BENCHMARK"),
        Text(""),
        _r_test_config(args, duration),
        Text(""),
        _r_preamble(),
        Text(""),
        _r_legend(args.verbose),
    )
    top_panel = Panel(top, border_style=S_RULE, padding=(0, 1))

    bottom_lines: list = [_r_running_intro()]

    def render() -> Group:
        bottom_panel = Panel(Group(*bottom_lines), border_style=S_RULE, padding=(0, 1))
        return Group(top_panel, bottom_panel)

    all_results: list = []
    with Live(render(), console=console, refresh_per_second=8, transient=False, screen=False) as live:
        def refresh_bottom():
            live.update(render())
        for run_idx in range(args.runs):
            _CURRENT_ITER["idx"] = run_idx + 1
            for engine, model in configs:
                model_label = model if model else (engine.lower())
                rw = len(str(args.runs))
                idx_str = f"[{run_idx + 1:>{rw}}/{args.runs}]"
                demo = run_idx == 0

                if demo:
                    bottom_lines.append(_r_iter_header(idx_str, model_label))
                    refresh_bottom()

                stream_cb = None
                if demo:
                    def make_cb():
                        def cb(is_final: bool, text: str) -> None:
                            bottom_lines.append(_r_iter_stream_line(is_final, text))
                            refresh_bottom()
                        return cb
                    stream_cb = make_cb()

                r = await stream_one(
                    args.audio, engine, model, api_key, args.realtime,
                    prewarm_ms=args.prewarm_ms, strip_wav_header=args.strip_wav_header,
                    on_stream=stream_cb,
                )
                all_results.append(r)

                eou = eou_ms(r)
                if demo:
                    bottom_lines.append(_r_iter_metrics(eou, r.ttf_interim_ms))
                    bottom_lines.append(Text(""))
                else:
                    bottom_lines.append(_r_iter_compact(idx_str, model_label, not r.error, eou, r.ttf_interim_ms))
                refresh_bottom()

        bottom_lines.append(_r_transcripts(all_results, configs))
        bottom_lines.append(_r_results(all_results, configs, args.verbose))
        refresh_bottom()
        live.refresh()

    return all_results, 1 if any(r.error for r in all_results) else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Telnyx standalone STT latency test")
    p.add_argument("--audio", default="samples/sample.wav", help="path to WAV file (default: samples/sample.wav)")
    p.add_argument("--spoken", default="Hello, my name is Jon and I'm testing speech recognition.", help="text spoken in the audio file, displayed in the test configuration")
    p.add_argument("--engine", help="single engine to test (Telnyx, Deepgram, Google, Azure). Default: nova-3+flux sweep")
    p.add_argument("--model", help="model name (Deepgram only: nova-2, nova-3, flux)")
    p.add_argument("--realtime", action="store_true", help="pace audio at 1x to simulate a live mic")
    p.add_argument("--prewarm-ms", type=int, default=1000, help="send N ms of silence before real audio to warm the upstream connection + Deepgram VAD/model. Default 1000ms reflects the warmed-state latency a real voice agent experiences. Set to 0 to measure cold-start.")
    p.add_argument("--strip-wav-header", action="store_true", help="skip the 44-byte WAV header so only raw PCM is sent")
    p.add_argument("--runs", type=int, default=1, help="number of times to run each (engine, model) — reports mean/p50/p95/stddev (default: 1)")
    p.add_argument("--verbose", action="store_true", help="include first-final and last-final in the report (off by default)")
    p.add_argument("--json", action="store_true", help="print results as JSON after summary")
    p.add_argument("--no-rich", action="store_true", help="disable the live two-panel UI; print linearly even when stdout is a TTY")
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

<h1><a href="https://portal.telnyx.com"><img src="assets/telnyx-mark.svg" width="40" height="42" alt="Telnyx" align="top"></a> Telnyx STT Latency Test</h1>

Measure how fast Telnyx STT actually responds, from your network — and learn what the numbers mean.

## Prerequisites

You'll need Python 3.9+ and a Telnyx API key. Set the key as an environment variable: `TELNYX_API_KEY`.

In the [Telnyx Portal](https://portal.telnyx.com), search for "API keys" once logged in and create one.

## Quick start

Start with one run of the default sweep to verify your API key, network, and dependencies. This runs every supported engine once.

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export TELNYX_API_KEY="***"
.venv/bin/python run.py --runs 1
```

If that works, use `--runs 3` for a quick comparison or `--runs 10` for more stable medians.

## Run one engine

The full sweep can take a while because it runs every engine back-to-back. To test only one engine, pass `--engine` and `--model`.

```
# Deepgram Flux
.venv/bin/python run.py --engine Deepgram --model flux --runs 3

# Deepgram Nova 3
.venv/bin/python run.py --engine Deepgram --model nova-3 --runs 3

# AssemblyAI
.venv/bin/python run.py --engine AssemblyAI --model assemblyai/universal-streaming --language en-US --runs 3

# xAI Grok
.venv/bin/python run.py --engine xAI --model xai/grok-stt --runs 3

# Soniox
.venv/bin/python run.py --engine Soniox --model soniox/stt-rt-preview --endpointing 500 --trailing-silence-ms 2000 --runs 3

# Speechmatics
.venv/bin/python run.py --engine Speechmatics --model speechmatics/rt --runs 3
```

Use `--runs 1` for smoke tests, `--runs 3` for quick comparisons, and `--runs 10` when you want more stable p50/p95 numbers.

## Full benchmark sweep

Run without `--engine` to benchmark Deepgram **nova-3**, Deepgram **flux**, **AssemblyAI**, **xAI Grok**, **Soniox**, and **Speechmatics** side-by-side.

```
.venv/bin/python run.py --runs 10
```

The sweep runs each iteration across all models back-to-back so short network changes affect each model roughly equally. 10 iterations per model gives you stable medians instead of one noisy sample.

By default the harness sends 1 second of pre-warm silence before the test audio. The timer starts after pre-warm. The multi-engine sweep also applies fixture-specific settings where required so each engine can emit a usable final transcript.

## What you'll see

The script prints the test configuration, explains the metrics, streams iteration 1 live so you can see what the engine is hearing, then prints compact metrics for later iterations.

**Example:**

```
  [3/3] nova-3    ✓    EOU   396ms   first-int    88ms
  [3/3] flux      ✓    EOU   410ms   first-int    95ms
  [3/3] aai/univ  ✓    EOU   553ms   first-int  1506ms

══════════════════════════════════════════════════════════════
  RESULTS
══════════════════════════════════════════════════════════════
  Both columns shown side-by-side keeps us honest. Wall-clock is the
  full number including your network — service-only is what we estimate
  the engine alone is doing. You see both, you do the math.

  flux
  (3/3 iterations)

              service-only (- RTT)                wall-clock
  EOU         mean  322ms  p50  319ms  p95  381ms   mean  394ms  p50  391ms  p95  453ms
  first-int   mean    1ms  p50    0ms  p95   10ms   mean   52ms  p50   51ms  p95   63ms
  total       mean 8125ms  p50 8120ms  p95 8205ms   mean 8197ms  p50 8192ms  p95 8277ms
  RTT                                               mean   71ms  p50   71ms  p95   75ms
```

## What the numbers mean

### The metric that matters

Voice agents feel slow because of ONE number: **EOU latency** — the dead air between when the user stops talking and when the transcript locks. That's the only latency your users actually feel. Cut EOU and your bot replies sooner.

### The marketing number

**TTFT (first-int)** is how fast the first word appears as you talk. It tells you the pipe is alive but doesn't predict conversation feel. We report it but don't optimize for it.

### The two columns

You'll see two side-by-side numbers per metric:

- **wall-clock** — the raw measurement. Stopwatch from when audio starts flowing until the transcript locks. Includes your network round-trip in both directions, so it varies based on where you're running this.
- **service-only** — an estimate of the engine alone. We approximate it by subtracting one measured RTT from the wall-clock number. It's not perfect — a more rigorous test would inject timestamps into the audio sample itself — but it's close enough to compare engines fairly across regions. Very small values may show as `0ms` after RTT subtraction, especially for first interim.

Wall-clock is the honest "what you'll see" number. Service-only isolates engine performance.

## Metric reference

**EOU ("End of Utterance")** — Time from when the user stopped talking until the transcript locked. The dead air your users feel. The number that decides how fast your bot replies.

**first-int ("first interim", a.k.a. TTFT or Time To First Token)** — Time from when audio started flowing until the engine's first interim transcript appeared. Comes back fast. Don't optimize for it.

**total** — End-to-end duration of one run. Sanity check, not a comparison metric.

**RTT ("Round-Trip Time")** — Network latency between your machine and Telnyx, measured by a WebSocket ping at the start of each run. We subtract one RTT from wall-clock to estimate service-only.

**p50 / p95** — The median (p50) and the tail (p95). Half your runs beat p50; 5% are slower than p95. p50 tells you what normal feels like; p95 tells you how bad the bad days get.

## STT vocabulary

Plain-language definitions for the broader terms. Use these directly with customers.

**Interim result** — A live guess. As you talk, the engine streams its best-guess-so-far text. These guesses change. Don't act on them — they're for showing "we're listening."

**Final** — A locked-in chunk of transcript. The engine has decided this part won't change. Safe to feed downstream.

**Speech Final** — A special kind of final that also means "the user just finished talking." This is the signal your bot waits for before responding.

**Endpointing** — How the engine decides "the user is done." It listens for silence — once silence lasts long enough, it fires a Speech Final. You can tune how patient it is.

**VAD (Voice Activity Detection)** — The engine's "is someone talking right now?" detector. Drives endpointing and powers things like barge-in.

**Pre-warm** — Sending a moment of silence before the real audio so the connection and model are already running. Avoids cold-start lag (~1 second penalty without it).

## Supported engines

The default sweep benchmarks six engines side-by-side:

| Engine       | Model                          | Display label   | Best for                                    |
| ------------ | ------------------------------ | --------------- | ------------------------------------------- |
| **Deepgram** | `nova-3`                       | nova-3          | Highest English accuracy, diarization       |
| **Deepgram** | `flux`                         | flux            | Lowest latency, built-in end-of-turn        |
| **AssemblyAI** | `assemblyai/universal-streaming` | aai/univ | Low latency, built-in turn detection        |
| **xAI**      | `xai/grok-stt`                  | grok-stt       | Multilingual auto-detection (25 languages)  |
| **Soniox**   | `soniox/stt-rt-preview`         | soniox         | Low-latency realtime transcription          |
| **Speechmatics** | `speechmatics/rt`             | speechm   | Realtime transcription with broad language coverage |

Two default configs are engine-specific:

- AssemblyAI is run with `language=en-US` for the English sample.
- Soniox is run with `endpointing=500` and 2 seconds of trailing silence; without this, it streams interim text but may not emit a complete final for this fixture.

## Resources

- [Telnyx STT docs](https://developers.telnyx.com/docs/voice/programmable-voice/stt-standalone)
- [Telnyx Portal](https://portal.telnyx.com)

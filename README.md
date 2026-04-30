<h1><a href="https://portal.telnyx.com"><img src="assets/telnyx-mark.svg" width="40" height="42" alt="Telnyx" align="top"></a> Telnyx STT Latency Test</h1>

Measure how fast Telnyx STT actually responds, from your network — and learn what the numbers mean.

## Pre-requisites

You'll need a Telnyx API key. Set it as an environment variable: `TELNYX_API_KEY`.

In the [Telnyx Portal](https://portal.telnyx.com), search for "API keys" once logged in and create one.

## Quick start

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export TELNYX_API_KEY="your-key-here"
.venv/bin/python run.py --runs 10
```

The default run benchmarks Deepgram **nova-3** and **flux** side-by-side. 10 iterations per model so you get stable medians, not one noisy sample.

## What you'll see

After the test config and a quick read of the educational header, the script streams iteration 1 live so you can see what the engine is hearing — then iterations 2+ show tight one-line metrics.

**Example:**

```
  [10/10] nova-3   ok    EOU   408ms   first-int    47ms
  [10/10] flux     ok    EOU   384ms   first-int    51ms

  Transcripts captured:
    nova-3     10/10 agreed: "Hello, my name is Jon and I'm testing speech recognition."
    flux       10/10 agreed: "Hello, my name is Jon and I'm testing speech recognition."

══════════════════════════════════════════════════════════════
  RESULTS
══════════════════════════════════════════════════════════════
  Both columns shown side-by-side keeps us honest. Wall-clock is the
  full number including your network — service-only is what we estimate
  the engine alone is doing. You see both, you do the math.

  nova-3
  (10/10 iterations)

              service-only (- RTT)                wall-clock
  EOU         mean  346ms  p50  343ms  p95  406ms   mean  418ms  p50  415ms  p95  478ms
  first-int   mean    0ms  p50    0ms  p95   10ms   mean   49ms  p50   48ms  p95   62ms
  total       mean 8138ms  p50 8133ms  p95 8218ms   mean 8210ms  p50 8205ms  p95 8290ms
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
- **service-only** — an estimate of the engine alone. We approximate it by subtracting one measured RTT from the wall-clock number. It's not perfect — a more rigorous test would inject timestamps into the audio sample itself — but it's close enough to compare engines fairly across regions.

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

## Resources

- [Telnyx STT docs](https://developers.telnyx.com/docs/voice/programmable-voice/stt-standalone)
- [Telnyx Portal](https://portal.telnyx.com)

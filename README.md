# stt-latency-test

Customer-runnable STT latency test harness for Telnyx standalone STT.

Streams a sample WAV file to the Telnyx STT WebSocket and reports realistic latency metrics for Deepgram nova-3 and flux side-by-side.

## Quick start

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export TELNYX_API_KEY="your-key-here"
.venv/bin/python run.py --realtime --runs 10
```

## Glossary

Plain-language definitions for the terms used in the output and in any STT latency conversation. Built around Deepgram's standard terminology so you can use these directly with customers.

**Interim result** — A live guess. As you talk, the engine streams its best-guess-so-far text. These guesses change. Don't act on them — they're for showing "we're listening."

**Final** — A locked-in chunk of transcript. The engine has decided this part won't change. Safe to feed downstream.

**Speech Final** — A special kind of final that also means "the user just finished talking." This is the signal your bot waits for before responding.

**Endpointing** — How the engine decides "the user is done." It listens for silence — once silence lasts long enough, it fires a Speech Final. You can tune how patient it is.

**Utterance End** — An extra "they stopped talking" event you can opt into. Same idea as endpointing, just delivered as its own message instead of attached to a final.

**TTFT (Time To First Token)** — How fast the first words come back after you start sending audio. Marketing's favorite number. Mostly a "is the pipe alive" check — doesn't tell you much about real conversation feel.

**VAD (Voice Activity Detection)** — The engine's "is someone talking right now?" detector. Drives endpointing and powers things like barge-in.

**EOU latency (Endpointing latency)** — The dead air between "user stopped talking" and "transcript locked." This is the number that matters. Every conversation pause your users feel is at least this long.

**RTT (Round-Trip Time)** — Network latency between your machine and the server. The output reports two columns: **wall-clock** (what your code actually measures, includes network) and **service-only** (wall-clock minus one RTT, approximates Telnyx + Deepgram alone). Wall-clock is the honest "what you'll see" number; service-only is for comparing engines from datacenters with different network distances.

**Pre-warm** — Sending a moment of silence before the real audio so the connection and model are already running. Avoids cold-start lag (~1 second penalty without it).

## The one-line pitch

Most STT benchmarks brag about TTFT — how fast the first word appears. But what your users *feel* is **EOU latency** — the silence after they finish talking before your bot answers. That's the number to optimize.

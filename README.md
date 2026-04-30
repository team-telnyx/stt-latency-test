# stt-latency-test

A canonical, reproducible script for measuring end-to-end latency of [Telnyx standalone STT](https://developers.telnyx.com/docs/voice/programmable-voice/stt-standalone).

When customers ask "how fast is your STT?" — point them here. Same script, same audio, same methodology, every run.

## Quickstart

```bash
pip install -r requirements.txt
export TELNYX_API_KEY=KEY...
python run.py            # blast mode (max-throughput)
python run.py --realtime # 1x pacing (simulates a live mic)
```

Default sweep: Deepgram **nova-3** and **flux** against the bundled sample audio.

## Example output

Realtime mode (recommended for evaluating live-call latency), with default 1000ms prewarm:

```
engine/model           audio     RTT  first-int  first-fin   last-fin     total
-------------------------------------------------------------------------------
Deepgram/nova-3        7.70s    51ms       31ms     2884ms     8105ms    9108ms
  (− RTT)                                    0ms     2833ms     8054ms
Deepgram/flux          7.70s   106ms       90ms     2950ms     8165ms    9166ms
  (− RTT)                                    0ms     2844ms     8059ms
```

The `(− RTT)` row subtracts one network round-trip so you can see service-only latency regardless of where you ran the test from.

For comparison, with `--prewarm-ms 0` (cold-start, no warmup):

```
Deepgram/nova-3        7.70s   109ms     1212ms     3105ms     8105ms    9108ms
  (− RTT)                                 1104ms     2996ms     7996ms
```

That ~1.1s gap is the cost of the upstream connection + Deepgram VAD/model warming up on the first audio bytes. Real voice agents with ambient mic audio hide this naturally; this test makes it explicit and lets you measure either state.

## Metrics

- **RTT** — median of 3 WebSocket ping/pong round trips, measured on the same connection that carries audio. This is the network cost between your machine and Telnyx's STT edge.
- **first-int** (time-to-first-interim) — ms from first audio byte sent until the first partial transcript appears. **This is "streaming latency"**: how fast a word lands while a person is still talking.
- **first-fin** (time-to-first-final) — ms to the first `is_final: true` message. Reflects natural finalization of the first utterance.
- **last-fin** — ms to the last `is_final: true`. Time to lock in all transcribed text.
- **total** — full wall-clock including connection close.

The clock starts after the WebSocket connection and RTT probe complete — so connect overhead doesn't poison the streaming numbers.

## Flags

- `--audio PATH` — WAV file to send (default: `samples/sample.wav`, 16kHz mono)
- `--engine NAME` — `Telnyx`, `Deepgram`, `Google`, `Azure` (default: Deepgram sweep)
- `--model NAME` — `nova-2`, `nova-3`, `flux` (Deepgram only; default: sweep nova-3 + flux)
- `--realtime` — pace audio at 1x speed to simulate a live mic. Without this flag, the file is blasted at network speed (useful for raw throughput, but not representative of voice-agent latency).
- `--prewarm-ms N` — send N ms of silence before real audio to warm the upstream connection + Deepgram VAD/model (default: `1000`). Set to `0` to measure cold-start latency.
- `--strip-wav-header` — skip the 44-byte WAV header (auto-enabled when prewarm is on; switches input format to raw PCM)
- `--json` — also emit machine-readable JSON

## Methodology

- One WebSocket connection per (engine, model) run
- `interim_results=true` is set so we measure real streaming latency, not just finalization
- **Prewarm:** by default, 1000ms of digital silence is sent before the real audio. This warms the upstream connection and Deepgram's VAD/model so the first-interim metric reflects what a customer's voice agent actually feels in steady state, not the cold-start spike. `--prewarm-ms 0` disables it.
- When prewarm or `--strip-wav-header` is used, the script switches to `input_format=linear16` + `sample_rate=16000` (raw PCM). The Telnyx WebSocket rejects non-RIFF leading bytes when `input_format=wav`, so prewarm wouldn't be possible otherwise.
- After the audio, the script sends `{"type": "CloseStream"}` to signal end-of-stream, so the engine finalizes immediately rather than waiting on its own endpointing.
- Timer starts AFTER the WebSocket connect, RTT probe, and prewarm complete — so warmup overhead doesn't poison the streaming numbers.

To compare against other providers or your own harness — feel free. The numbers reported by this script are the canonical Telnyx numbers.

## Important caveats

- **`nova-3` and `flux` here are Telnyx-hosted.** Audio goes to Telnyx infra, Telnyx runs the models. Latency reflects the full Telnyx-hosted stack, not direct Deepgram. A direct-Deepgram comparison mode is on the roadmap (see TODO below).
- Numbers vary with network distance to Telnyx — run from the location you care about.

## Roadmap

- `--include-deepgram-direct` flag for A/B vs. Deepgram cloud (requires `DEEPGRAM_API_KEY`)
- Multi-run averaging (`--runs N`) with p50/p95
- Larger sample audio set (mixed lengths, accents)

## Sample audio

`samples/sample.wav` — ~7.7s, 16kHz mono PCM, three English pangrams. Bring your own with `--audio` for content matching your use case.

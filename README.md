# stt-latency-test

A canonical, reproducible script for measuring end-to-end latency of [Telnyx standalone STT](https://developers.telnyx.com/docs/voice/programmable-voice/stt-standalone).

When customers ask "how fast is your STT?" — point them here. Same script, same audio, same methodology, every run.

## Quickstart

```bash
pip install -r requirements.txt
export TELNYX_API_KEY=KEY...
python run.py
```

That runs the default sweep: Deepgram **nova-3** and **flux** against the bundled sample audio.

## Example output

```
engine/model              audio      TTFP   TTF-final     total
----------------------------------------------------------------
Deepgram/nova-3            7.69s     312ms       890ms     920ms
Deepgram/flux              7.69s     198ms       640ms     670ms
```

## Metrics

- **TTFP** — time-to-first-partial: ms from connection open to first transcript message
- **TTF-final** — ms from connection open to first `is_final: true` message
- **total** — full wall-clock duration including audio streaming and connection close

All times are measured client-side. Run on the network you care about (your own laptop, your prod VPC, etc).

## Flags

| Flag | Default | Notes |
|------|---------|-------|
| `--audio PATH` | `samples/sample.wav` | 16kHz mono WAV |
| `--engine NAME` | `Deepgram` (sweep) | `Telnyx`, `Deepgram`, `Google`, `Azure` |
| `--model NAME` | `nova-3` + `flux` | Deepgram models: `nova-2`, `nova-3`, `flux` |
| `--json` | off | also emit machine-readable JSON |

## Methodology

- One WebSocket connection per (engine, model) run
- Audio is streamed in 2KB binary frames
- Timer starts immediately before the WebSocket connect call
- TTFP is the first message containing a `transcript` field
- TTF-final is the first message with `is_final: true`
- Total includes graceful close

To compare against other providers or your own harness: feel free. Numbers reported by this script are the canonical Telnyx numbers.

## Sample audio

`samples/sample.wav` — ~7.7s, 16kHz mono PCM, English pangrams. Bring your own with `--audio` if you want to test with realistic content for your use case.

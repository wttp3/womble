# Slice 1: BOX-3 → petit hub → list + spoken reply + display card

Flow: "Okay Nabu" (on-device) → ESPHome `voice_assistant` streams 16 kHz PCM over the encrypted native API → `hub.py` (aioesphomeapi client) → energy VAD → mlx-whisper → regex router (`add X to Y list`, `what's on Y list`) → `data/lists.json`. Anything the router does not match is answered by the brain (`grok-4.7`) and spoken back. The frame gets a WebSocket card on `http://<hub-ip>:8080/`. TTS writes a WAV at `/tts/…`, the hub sends that URL in TTS_END, and the BOX-3 plays it.
Timing per utterance is appended to `hub/data/timing.jsonl` (eos/stt/route/display/tts ms since wake + stt_only/tts_only, and `llm_ttft_ms` / `llm_total_ms` on brain turns).

## Brain

List and record commands stay on the regex and work with no network. Other sentences go to `POST https://api.x.ai/v1/responses` (`grok-4.7`, reasoning effort low, `store: false`). The persona is `hub/brain/persona.md`. Tool schemas live only in `hub/brain/tools.py`. The plan is [`docs/plans/GROK-ROUTING-PLAN.md`](../docs/plans/GROK-ROUTING-PLAN.md).

The brain key is its own Keychain item, not the speech-to-text key `womble-xai`. Add it on the hub Mac (the command prompts for the secret):

```
security add-generic-password -a $USER -s womble-brain-xai -U -w
```

The hub reads that item at runtime, the same way `stt_xai.py` reads `womble-xai`. It is never written to the repo or to the log. Cap the key in the xAI console. If xAI is down, the key is missing, or the call times out, the box says "Connection unavailable." and lists still work. A timeout is that turn only: it does not count toward the circuit breaker. After three connection or HTTP failures the brain pauses for 60 seconds. The warning names the stage the request reached (`dns`, `connect`, an HTTP status, `first-byte`, or `first-token`), plus whether a proxy is set and how many IPv4 and IPv6 answers DNS returned. It does not include the key.

An empty transcript after a wake ends the turn without speaking.

| Env | Default | |
|---|---|---|
| `VAD_TRAIL_SIL_S` | `1.2` | Seconds of quiet that end a turn. `0.65` is the old, tighter cutoff. Read at startup. |
| `BRAIN_TIMEOUT_S` | `8` | How long a BOX-3 turn waits on xAI before the offline line. A measured p95 with web search on was about 4.2 s, so 4 s sat inside that tail. |
| `BRAIN_WEB_SEARCH` | `1` | Server-side web search, and only when the utterance looks like a live fact (weather, news, a score, a price, today, who won). Ordinary questions such as boiling an egg do not get it. Set `0` to leave it out entirely. |
| `BRAIN_MODEL` | `grok-4.7` | Responses model. |
| `BRAIN_MAX_OUTPUT_TOKENS` | `200` | Caps the spoken answer. |
| `BRAIN_MEMORY_TURNS` | `6` | Turns kept per device, in process. |
| `BRAIN_MEMORY_TTL_S` | `300` | Drop that memory after this much quiet. |
| `BRAIN_BREAKER_FAILS` | `3` | Failures before the circuit opens. |
| `BRAIN_BREAKER_COOLDOWN_S` | `60` | How long it stays open. |
| `XAI_BRAIN_API_KEY` | unset | Optional override. Leave it unset on the hub so the Keychain item is used. Never commit it. |

The first sentence is synthesised while the rest of the reply is still arriving, then joined into the one WAV the BOX-3 already fetches. `llm_ttft_ms` and `llm_total_ms` are logged on every brain turn.

## Alexa live link

`hub/alexa_link.py` keeps an outbound WebSocket to the womble-alexa worker (`wss://<worker-host>/hub`). It is off unless `ALEXA_LINK=1`. The worker URL is `ALEXA_WORKER_URL`. The bearer token is `HUB_LINK_TOKEN`, or Keychain item `womble-hub-link` (account `$USER`), the same value as the worker secret. Deploy steps and the Alexa console rebuild are in [`alexa/README.md`](../alexa/README.md).

Each job goes through the regex router first, then the brain, with session key `alexa:<sessionId>` (or `alexa:<room>`). The reply is spoken on the Echo and shown on the frame. A brain answer keeps the Echo session open with "Anything else?" so a follow-up does not need a new invocation. A list action still ends the session. The same request id is claimed before the answer, so the queue puller does not show it again. `ALEXA_LINK_TEST_DELAY_S` (unset by default) delays the answer so the worker's "One moment…" and kitchen-frame lines can be checked. The log lines are `alexa job intent=… text=… session=… speech=… end_session=…` (speech is the first 80 characters) and `alexa link hop_ms` / `model_ms`. They do not include the worker URL or the token.

```
python tests/test_alexa_link.py
python tests/test_alexa_pull.py
```

```
python tests/test_brain.py
python brain_latency.py
python brain_latency.py --route
python brain_latency.py --route --hub-timeout
```

Run them from `slice1/hub/`. The latency script calls the live API (about 20 prompts) and prints p50/p90/p95. It needs the key. Plain mode calls `Brain.handle`, which is what the hub calls after the regex misses. `--route` loads the hub and calls `route()` with the same first-sentence TTS hook the BOX-3 uses. `--hub-timeout` waits `BRAIN_TIMEOUT_S` instead of the 30 s measurement budget. A shell run does not inherit the LaunchAgent environment; the failure line's `proxy=` and `dns=` fields are how to compare the two.

## Why the native API method
It is the documented path ESPHome voice firmware uses (the same events HA's esphome integration sends). aioesphomeapi exposes `subscribe_voice_assistant(handle_start, handle_stop, handle_audio)` and `send_voice_assistant_event`. No custom C++ is needed. Returning 0 from handle_start selects API audio, not UDP.
**Verify on first run:** callback signatures in the installed aioesphomeapi (they have changed over time; see `aioesphomeapi/client.py`), and whether firmware 2026.9 needs extra events (e.g. announce/TTS streaming).

## Order of operations
1. Remove the BOX-3 from Home Assistant (Settings → Devices → ESPHome → delete or disable). Otherwise HA keeps subscribing to voice.
2. `./setup.sh` on petit.
3. Store the existing API key in Keychain (`security add-generic-password …`), or generate a new one and put it in `firmware/secrets.yaml`.
4. Flash: `esphome run firmware/box3-hub.yaml --device <box3-ip>` (OTA). Keep the current YAML/firmware as a rollback.
5. `./run.sh`, open `http://<petit-ip>:8080/` on the frame, say "Okay Nabu, add eggs to the shopping list".

## Smoke test results (petit, M2 16 GB, 2026-10-15)
`hub/smoke.py` (say → 16 kHz WAV → mlx-whisper → router → Kokoro); 1.2–2.1 s clips, warm:
| model | lang | STT ms | Kokoro TTS ms |
|---|---|---|---|
| whisper-large-v3-turbo | auto | ~1,850–1,950 | 230–360 |
| whisper-large-v3-turbo | en | ~1,020–1,070 | 260–300 |
| turbo-q4 | auto | ~2,000 | 230–380 |
| turbo-q4 | en | ~1,020–1,050 | 240–290 |
Cold load: whisper 5–14 s, Kokoro first call ~29 s (spaCy model download, once). The hub warms both at startup.
All transcripts were correct; router categories correct. Note: TTS-generated test audio is cleaner than a real kitchen mic.

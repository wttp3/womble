# Slice 1: BOX-3 → petit hub → list + spoken reply + display card

Flow: "Okay Nabu" (on-device) → ESPHome `voice_assistant` streams 16 kHz PCM over the encrypted native API → `hub.py` (aioesphomeapi client) → energy VAD → mlx-whisper → regex router (`add X to Y list`, `what's on Y list`) → `data/lists.json` → WebSocket card on `http://petit:8080/` → TTS WAV served at `/tts/…` and its URL sent in the TTS_END event → BOX-3 media player plays it.
Timing per utterance is appended to `hub/data/timing.jsonl` (eos/stt/route/display/tts ms since wake + stt_only/tts_only).

## Why the native API method
It is the documented path ESPHome voice firmware uses (the same events HA's esphome integration sends). aioesphomeapi exposes `subscribe_voice_assistant(handle_start, handle_stop, handle_audio)` and `send_voice_assistant_event`. No custom C++ is needed. Returning 0 from handle_start selects API audio, not UDP.
**Verify on first run:** callback signatures in the installed aioesphomeapi (they have changed over time; see `aioesphomeapi/client.py`), and whether firmware 2026.9 needs extra events (e.g. announce/TTS streaming).

## Order of operations
1. Remove the BOX-3 from Home Assistant (Settings → Devices → ESPHome → delete or disable). Otherwise HA keeps subscribing to voice.
2. `./setup.sh` on petit.
3. Store the existing API key in Keychain (`security add-generic-password …`), or generate a new one and put it in `firmware/secrets.yaml`.
4. Flash: `esphome run firmware/box3-hub.yaml --device 192.168.0.90` (OTA). Keep the current YAML/firmware as a rollback.
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

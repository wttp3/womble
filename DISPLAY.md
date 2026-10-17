# Womble #1 — Kitchen Display Layout Plan (DRAFT for Andrew's review, nothing built)

_Draft 2026-10-15. Covers the page that `slice1/hub/hub.py` serves at `GET /` and feeds over `WS /ws`._
Mockup: `mockups/kitchen.html?s=<state>`. PNGs: `mockups/01-idle.png` to `10-error.png`, rendered headless at 1280×800 by `mockups/shoot.py`.

## 0. What exists today
- `hub.py` (aiohttp) serves `slice1/display/index.html` on `/`. It keeps a set of WS clients on `/ws` (heartbeat 20 s), and `push_card()` broadcasts `{"ts", **card}`.
- Current card types: `{"type":"list","title","items":[str],"highlight"}` and `{"type":"text","title","body"}`. The page shows one centred card, a small clock and a "live/reconnecting" footer, and reconnects every 2 s.
- This plan keeps that transport and reconnect logic. It adds an `event` envelope (§5) and stays compatible with both slice-1 card types.

## 1. Branding
- The project name **"Womble #1"** is always visible: amber, semibold, 26 px, in the status bar, followed by "·" and the status label.
- To its left, a dog mark and a cream **WOMBLE** wordmark. The mark is a small same-origin WebP cropped from `docs/womble-dog.png` (not the 1.2 MB original). The device name still comes from `snapshot.device_name`, so a #2 display keeps the wordmark and changes the name.
- The page links `/favicon.png` and `/apple-touch-icon.png`, the same dog on the display background.
- The idle screen also has a quiet footer line, "WOMBLE #1 · SAY “WOMBLE”" (24 px, tracked caps, dim grey), at the bottom of the clock column.
- The `<title>` is "Womble #1".

## 2. Device constraints → design rules
The device is a PF1007L: Android 6.0.1, WebView 106 in Fully Kiosk, 1 GB RAM, Allwinner A33.
- One HTML file with inline CSS and vanilla JS. No frameworks, no webfonts, no build step. Use the system font (Roboto). The dog mark, favicon and apple-touch icon are the only extra files, and they are same-origin.
- Fixed px layout for 1280×800 with viewport `width=1280`, and Fully zoom off. [? confirm devicePixelRatio = 1]
- Re-render by swapping `#main` innerHTML. Keep it under ~150 DOM nodes per screen.
- Animation is limited to the opacity pulse on the "listening" ring. No layout transitions, shadows, blur or video.
- Dark theme: #0d0f12 background, #f2efe8 text, amber #ffc94d accent. Every state also carries a text label, so colour is never the only signal.
- The clock updates once per minute (aligned to :00). Today's page updates every second.
- Images are optional, the hub sizes them to ≤400 px, and the page never blocks on loading them.
- Icons: the mockup uses Unicode glyphs. Android 6 emoji support is patchy, so the build should use a few inline SVGs instead.

## 3. Regions (1280×800)
| Region | x, y, w × h | Content |
|---|---|---|
| Status bar | 0, 0, 1280 × 64 | Left: "Womble #1" · dot + status (Ready / Listening / Working… / Problem / Offline). Right: clock (34 px). |
| Main | 0, 64, 1280 × 656. Padding 32/48 gives a 1184 × 592 content area. | State content. |
| Now-playing strip | 0, 720, 1280 × 80 | 56 px art, title/artist (28 px), position. Shown only while playing. Hidden in recipe, step and full now-playing states, where main grows to 736. |
| Toast overlay | 48, 696, 1184 × 80 | One-line confirmations that must not take over the screen (e.g. "Added eggs" during step mode). |

- Idle layout: left column 540 px (clock, date, weather, next item, branding line). Right column 612 px with two glance panels and a 20 px gap.
- Recipe layout: ingredients 440 px, method 712 px.

## 4. Typography (distance reading, ~150 dpi 10" panel, 3–4 m)
| Use | Size |
|---|---|
| Idle clock | 200 px light |
| Step text (step mode) | 76 px medium, max ~4 lines. The hub splits longer steps. |
| Headline | 64 px semibold (72 px for research confirmation) |
| Reply body, list view items | 40–44 px |
| Glance items, recipe method/ingredients | 30–32 px |
| Labels/meta (grey, uppercase tracked) | 24–28 px |
| Minimum | 24 px |

Limits:
- Chat text on screen is capped at ~60 words.
- The list view shows 2 columns × 7 rows. Anything beyond that collapses to "+N more".

## 5. States, timing, return-to-idle
Priority, highest first: blocking error > listening > thinking > step mode > recipe > reply/list/research > full now-playing > idle. An event with lower priority than the current state shows only as a toast.

| # | State | Trigger | Layout | Stays | Then |
|---|---|---|---|---|---|
| 1 | Idle | default / `idle` | Clock, date, weather, "Next" line, branding; Shopping glance (4 + "+N"), Research pile (2) | indefinitely | night dim [? hours] |
| 2 | Listening | `voice.listening` (Session.on_start) | Pulsing amber ring, "Listening…", hint chips | until `voice.heard`, max 10 s | thinking, or previous state on `voice.cancel` |
| 3 | Thinking | `voice.heard` | Dotted blue ring, transcript (48 px), which bot is working | until result, max 45 s | result. At 45 s: "Still working, I'll show it when it's ready", then previous state. |
| 4 | Reply | `reply` | Label, question h1, answer (44 px), source | 30 s + 1 s per 10 words, max 90 s | idle, or previous recipe/step |
| 5 | List updated | `list.updated` | "✓ Added to Shopping", full list in 2 columns, new item highlighted | 12 s | idle |
| 6 | Research added | `research.added` | Big centred item, note, pile count | 8 s | idle |
| 7 | Recipe | `recipe` | Ingredients and numbered method | 10 min or "close" | idle |
| 8 | Step mode | `recipe.step` ("start cooking", "next/back/repeat") | "Step n of N", one step at 76 px, hints, countdown timer | no timeout while cooking. Ends after 60 min without interaction or on "stop cooking". | recipe, then idle |
| 9 | Now playing | `media.state` with `focus:true` | 400 px art, title 64 px, progress bar, 120 px ⏮ ⏸ ⏭ buttons | 20 s | idle + bottom strip |
| 10 | Error | `error` | Red ring, plain headline, what happens next, request text + code | 10 s (stays put if `blocking`) | previous state |
| – | Offline | WS closed for more than 5 s | Status shows "Offline · reconnecting", last screen dimmed | until reconnect | hub sends `snapshot` |

Non-recipe search results use the reply layout with optional bullet `items`.

## 6. WebSocket schema (hub → page)
```json
{"v":1,"event":"<type>","id":"<uuid>","corr":"<correlation_id|null>","ts":1760532180.1,"ttl_s":12,"priority":"normal|high","data":{}}
```
- `ttl_s` overrides the default display time. `0` means sticky.
- If `corr` matches the pending thinking state, the result replaces it.

| event | data |
|---|---|
| `snapshot` | `{device_name:"Womble #1", lists:{name:[{item,added_ts}]}, research:[{title,note?,ts}], media, recipe, step, weather?, next?}`. Sent on every connect. |
| `idle` | `{}` |
| `voice.listening` | `{wake_word:"womble", source:"box3-kitchen"}` |
| `voice.heard` | `{text, category?: list.add/research.add/search.live/media/home/chat, bot?}` |
| `voice.cancel` | `{reason:"no_speech"/"abort"}` |
| `reply` | `{title, body, items?, source?}` |
| `list.updated` | `{list, action:"add"/"remove"/"clear", items:[str], highlight:[str]}` |
| `research.added` | `{title, note?, count}` |
| `recipe` | `{title, minutes?, serves?, image_url?, source_url?, ingredients:[str], steps:[str]}` |
| `recipe.step` | `{index, total, text, timer_s?}`. The page counts the timer down locally. |
| `media.state` | `{state:"playing"/"paused"/"idle", title, artist?, album?, art_url?, position_s, duration_s, player, focus?}`. Sent only on change; the page interpolates position. |
| `home.done` | `{text}`, shown as a toast. Home Assistant is just another node. |
| `toast` | `{text, kind:"ok"/"info"/"warn"}` |
| `error` | `{code:"bot_timeout"/"stt_empty"/"hub_offline"/…, message, detail?, retry_in_s?, blocking?}` |
| `print.status` (later) | `{list, state:"queued"/"printed"/"failed"}`, shown as a toast. For the NETUM 58 mm printer. |

Page → hub messages:
- `{"event":"hello","client":"kitchen-frame","ver":1}` when the socket opens.
- `{"event":"tap","action":"media.toggle|media.next|media.prev|recipe.next|recipe.prev|dismiss"}` for touch input. The hub executes the action, so no tokens are held in the browser.

Backward compatibility: a message with no `event` but with `type:"list"` maps to `list.updated`, and `type:"text"` maps to `reply`.

## 7. Later implementation notes (not now)
- Add `push_event()` to hub.py. Emit `voice.listening` in `Session.on_start`, `voice.heard` after STT in `finish()`, and `snapshot` in `ws_handler`.
- The page is one state machine (current, previous, timer), about 250 lines of JS.
- Screen wake via the Fully Kiosk REST `screenOn` on `voice.listening`. [? needs Fully remote admin on the LAN]
- `shoot.py` can render the real page with fake events for visual regression checks.

## 8. Decisions (approved by Andrew 2026-10-15)
| # | Decision |
|---|---|
| 1 | Keep the "Womble #1" styling. The hub sends the name (`snapshot.device_name`) so a #2 display can be added later. |
| 2 | Idle shows the clock, weather from Open-Meteo (free, no key; hub caches it for 15 min) and the shopping list. No calendar for now. Photo-frame periods may come later. |
| 3 | The lists are **shopping** and **to-do**. Idle shows shopping; other lists appear only when asked about. The Research pile panel moves off idle. |
| 4 | Dim to about 10% from 23:00 to 07:00 (CSS overlay for now). The wake word (`voice.listening`) and any foreground event briefly brighten the screen, then it re-dims after 60 s idle. |
| 5 | Step mode never times out. Timers chime on the BOX-3 (hub side; not built yet). |
| 6 | Replies over ~60 words are shortened on screen (`reply.short`). The BOX-3 speaks the full reply, and "print that" sends the full text to the printer (later). |
| 7 | Large touch buttons (≥120 px) for next step / back, pause or play, and dismiss. Voice stays primary. |
| 8 | The BOX-3 screen shows a simple listening/thinking indicator (firmware change; separate step). |
| 9 | English only. |
| 10 | Show a "say 'print shopping'" hint on list screens once the printer exists, behind a flag (`snapshot.features.printer`). |
| 11 | Dark theme all day. |
| 12 | Errors: the BOX-3 speaks a short line and the screen shows the details. |

## 9. Former open questions (all answered in §8)
1. Is "Womble #1" correct as styled (amber, top-left, plus the idle footer)? Will there be a #2 display or satellite?
2. Idle content: clock + Shopping + Research pile. Add weather or a calendar "Next" line (from which source)? Photo-frame periods?
3. Which lists exist, and which one does idle show? Rotate between them?
4. Night dim/off hours, and should the wake word light the screen at night?
5. Step mode never times out while cooking: OK? Should timers also chime on the BOX-3?
6. Chat replies capped at ~60 words on screen: where should overflow go?
7. Touch controls (media, next step, dismiss), or voice only?
8. Should the BOX-3 LCD mirror listening/thinking?
9. English only, or Spanish dates/labels too?
10. Printing: show a "say 'print shopping'" hint on list views? The 58 mm ticket layout would be a separate plan.
11. Dark theme all day, or light by day?
12. Errors spoken as well as shown?

## 10. Implementation status (2026-10-15, box mirror only, not deployed)
- `slice1/hub/static/display.html`: the real page. Vanilla ES2017 (WebView 106), inline SVG icons, the §6 schema, slice-1 `{type:list|text}` compatibility. Implements priority/toasts, timeouts, step mode with a local countdown, touch buttons (dismiss, step back/next, media prev/toggle/next), the now-playing strip, the offline indicator, and night dim (CSS overlay at 90% black from 23:00 to 07:00, lifted for 60 s by any non-idle event). Debug: `?hour=23&min=30` fakes the clock.
- `slice1/hub/display.py`: the `Display` class. Provides `emit()`, `push_card()` (legacy → events), `snapshot` on connect (name, printer flag, lists, weather, media, recipe/step), Open-Meteo weather cached 15 min with a periodic `weather` push, tap handling (dismiss → `idle`, recipe next/prev → `recipe.step`, media → `media_handler` hook or an "isn't connected yet" toast), `POST /api/event` (localhost-only unless `DISPLAY_TOKEN` is set) and `GET /api/taps`. It also runs standalone for tests.
- `hub.py`: uses `Display`. Emits `voice.listening` on wake and `voice.heard` / `voice.cancel` after STT. `/` now serves the new page and the old page stays at `/old`.
- `tests/fake_driver.py`: fake event driver + headless screenshots (21 checks).
- **Not built yet:** BOX-3 timer chime (#5), BOX-3 LCD listening/thinking indicator (#8, firmware), "print that" / printer (#6, #10; the flag is in place), Music Assistant handler (#7 media taps), spoken short error line (#12; needs the router error path), and the to-do list voice handling (the existing regex already covers "add X to to-do").

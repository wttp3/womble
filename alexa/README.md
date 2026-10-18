# womble-alexa: Echo speaks the hub answer (live link), or the queue when the hub is down

Built 2026-10-15. Live link added 2026-10-18. Locale **en-GB**, invocation **frida**.

"Alexa, ask Frida what's a substitute for buttermilk" is answered by the hub. When the hub's outbound socket is connected and its heartbeat is fresh, the Echo speaks that answer and the kitchen frame shows it. A follow-up in the open session ("what about for scones") continues the same conversation. The worker does not call a model itself.

| Thing | Value |
|---|---|
| Worker (skill endpoint) | `<worker-url>` (the `https://womble-alexa.<subdomain>.workers.dev` URL printed by `cf deploy`; path `/` or `/alexa`) |
| Hub socket | `wss://<worker-host>/hub` (Durable Object `HubLink`, object name `hub`). Any other path on that route is rejected. |
| Queue | `womble-alexa` (id `<queue-id>`), HTTP pull consumer: batch 10, visibility 60 s, max retries 3, retry delay 5 s |
| Worker secrets | `ALEXA_SKILL_ID`, `HUB_LINK_TOKEN` |
| Interaction model | `skill/interaction-model.en-GB.json` |
| Hub modules | `slice1/hub/alexa_pull.py` (queue) and `slice1/hub/alexa_link.py` (live socket, off until `ALEXA_LINK=1`) |
| Device → room map | `slice1/hub/data/alexa_devices.json` (created on first Alexa request; not in git) |

Files: `src/index.js` (entry), `src/app.js` (skill logic), `src/hub_link.js` (Durable Object), `src/verify.js` (+`roots.js`) signature/cert-chain checks,
`cloudflare.config.ts`, `test/test.mjs`. Build/deploy needs Node ≥ 22.18: `export PATH=~/.local/node/bin:$PATH`.
- Test: `node test/test.mjs` (positive path uses a stub verifier passed in by the test only; the deployed entry always verifies)
- Deploy: `cf deploy` (see below). Do not also wrangler-deploy this directory.
- Config outside git: `cloudflare.config.ts` holds no account id, skill id, or hub token. `cf deploy` uses the account you are logged in to (`cf login`).

## What the Echo hears

The signature, timestamp and skill-id checks are unchanged. After they pass:

- **Hub socket connected** (last heartbeat within 30 s). The worker pushes `{id, text, deviceId, sessionId, locale, deadline}` and waits for the matching `{id, speech, end_session, reprompt?}`.
  - At **1.2 s**, if the answer is not back, it sends one progressive response, "One moment…", to `{apiEndpoint}/v1/directives` with the request's `apiAccessToken`. The speech is SSML `<speak>…</speak>`.
  - At a hard cutoff of **6.5 s** it answers "That's taking a while. I'll put it on the kitchen frame." and still enqueues the job, so `AlexaPuller` can show it. The live link claims the request id as soon as it starts, so the queued copy is not shown twice.
  - A brain answer with `end_session: false` (it asked something, or offered more) keeps the session open and sends a short reprompt. If the brain did not send one, the reprompt is "Anything else for the kitchen?" Otherwise the session ends.
- **Hub offline**, or the heartbeat is older than 30 s: today's path. Enqueue the job and say "On it."
- Spoken text is plain, SSML-escaped, and capped well under 8000 characters. Observability stays off. The worker log line is `alexa hub wait_ms=… outcome=…` and does not include the words.

`QuestionIntent` splits the utterance into `ask` (`WombleAsk`) and `query` (`AMAZON.SearchQuery`). The worker joins them back, so "how do I slice a mango" and "why slice a mango" stay different questions, and "is it safe to…" keeps its "is". A query that already starts with that ask phrase is sent unchanged. Session memory on the hub (`alexa:<sessionId>`, or `alexa:<room>` when there is no session id) carries the follow-up. Yes and No send the words `yes` and `no` down the same path.

## Andrew: steps to finish

### 1. Create the skill (Alexa developer console, signed in with the same Amazon account as your Echos)
1. Open https://developer.amazon.com/alexa/console/ask → **Create Skill**.
2. Name: `Frida`. Primary locale: **English (UK)**. Type of experience: **Other** → model **Custom** → hosting **Provision your own** → **Next** → template **Start from Scratch** → **Create skill**.
   (If the console refuses the one-word invocation name `frida`, use `frida helper` and tell me; the model file changes one line.)

### 2. Paste the interaction model
1. Left menu: **Build → Interaction Model → JSON Editor**.
2. Select all, delete, paste the whole of `skill/interaction-model.en-GB.json`.
3. **Save**, then **Build skill** (top). Wait for "Build successful".

### 3. Set the endpoint
1. Left menu: **Endpoint** → choose **HTTPS**.
2. Default Region: `<worker-url>`
3. SSL certificate type: **"My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority"**.
4. **Save**. On the same page, **Copy** the Skill ID (`<skill-id>`) and send it to me — I set it as the Worker secret `ALEXA_SKILL_ID`. Until then the Worker answers 400 and Alexa says there was a problem with the skill.

### 4. Enable it on your Echos
1. **Test** tab → set "Skill testing is enabled in" to **Development**.
2. Dev skills appear automatically on Echos registered to the same Amazon account. Check each Echo's language is **English (United Kingdom)** (Alexa app → Devices → the Echo → Language).
3. Try in the Test tab first.
   - Hub socket down, or `ALEXA_LINK` is not `1`: `ask frida to add milk` still answers "On it." and the frame updates from the queue.
   - Hub socket up: the Echo speaks the real answer (for example "Added milk to shopping.") and the frame shows the same card. "Alexa, ask Frida what's a substitute for buttermilk" speaks the brain's answer. While the session is open, "what about for scones" continues it.
   - To hear the slow path, set `ALEXA_LINK_TEST_DELAY_S=7` on the hub and restart. Expect "One moment…", then "That's taking a while. I'll put it on the kitchen frame.", and the frame still shows the answer when the hub finishes. Unset the delay afterwards.
   - "Alexa, open Frida" → "Hi, what do you need?" → "add milk".
4. If an Echo is on Alexa+, and Frida isn't found, say so: it may hide dev skills.

### Rebuild the model on the existing skill

The skill already exists. Do not create another one, and do not change the endpoint or the skill id.

1. https://developer.amazon.com/alexa/console/ask → open **Frida**.
2. Left menu: **Build → Interaction Model → JSON Editor**.
3. Select all, delete, paste the whole of `skill/interaction-model.en-GB.json`.
4. **Save**, then **Build skill** (top). Wait until the build reports success. The new model is not live until that build finishes.
5. Leave **Endpoint** and the skill id as they are. **Test** stays **Development**.

What the pasted model adds, on top of Add, Find, Save, Remember, Capture, Help, Stop, Cancel, NavigateHome and Fallback:

- **QuestionIntent**, slots `ask` (`WombleAsk`) and `query` (`AMAZON.SearchQuery`). The sample is `{ask} {query}`. `WombleAsk` values are: what, what's, what is, what about, how, how do I, why, when, who, is, can, should, research, tell me about, tell me, and. The worker rebuilds `${ask} ${query}`.
- **AMAZON.YesIntent** and **AMAZON.NoIntent**, built-in, with empty sample lists (Amazon supplies the utterances).

### 5. Create the Cloudflare queue token (you type the secret; it never goes in a file)
1. https://dash.cloudflare.com/profile/api-tokens → **Create Token** → **Create Custom Token** (Get started).
2. Token name: `womble-cf-queue`.
3. Permissions: one row: **Account** · **Queues** · **Edit** (Edit = read+write; write is needed to ack).
4. Account Resources: **Include** · *your account* (your account, `<acct>`). Nothing else.
5. Optional: Client IP Address Filtering → Is in → your home public IP. TTL: leave empty or set a year.
6. **Continue to summary** → **Create Token**. Copy the token (shown once).
7. On petit, logged in as `decoy`, in Terminal:
   ```
   security add-generic-password -s womble-cf-queue -a decoy -w
   ```
   Paste the token at both prompts (nothing is echoed). Then restart the hub:
   ```
   launchctl kickstart -k gui/$(id -u)/com.andrew.box3hub
   ```
   `~/box3-voice/slice1/hub.log` should show `alexa pull running`. Check without revealing anything: `cd ~/box3-voice/slice1/hub && .venv/bin/python alexa_pull.py --status`.
   Kill switch: set `ALEXA_PULL=0` in `~/box3-voice/local.env` (or delete the Keychain item) and restart.

### 6. Deploy the live link (do this yourself; this change was not deployed)

This directory's config is `cloudflare.config.ts`, deployed with `cf`. There is no `wrangler.toml`. Do not also `wrangler deploy` it.

From `alexa/`, after `node test/test.mjs`:

```
cf workers secrets update ALEXA_SKILL_ID --worker womble-alexa --type secret_text --text <skill-id>
cf workers secrets update HUB_LINK_TOKEN --worker womble-alexa --type secret_text --text "$(security find-generic-password -s womble-hub-link -a "$USER" -w)"
cf deploy
```

`ALEXA_SKILL_ID` is the existing secret. `HUB_LINK_TOKEN` is new and must be the same string as the Keychain item below. No model API key goes on this worker.

The Durable Object is declared as an export, which is how `cf` creates the class (exports and a wrangler migrations array cannot both be set):

```ts
exports: { HubLink: exports.durableObject({ storage: "sqlite" }) }
```

The first `cf deploy` after this change creates the SQLite-backed class `HubLink` and the `HUB_LINK` binding (object name `hub`).

Wrangler equivalents, for a tree that used wrangler. Do not run them here:

```
npx wrangler secret put ALEXA_SKILL_ID
npx wrangler secret put HUB_LINK_TOKEN
```

```json
{ "migrations": [{ "tag": "v1", "new_sqlite_classes": ["HubLink"] }] }
```

On the hub Mac, add the matching token (the command prompts; nothing is echoed), then set the two lines in the untracked `local.env` (see `local.env.example`) and restart the hub:

```
security add-generic-password -a "$USER" -s womble-hub-link -U -w
```

```
ALEXA_LINK=1
ALEXA_WORKER_URL=wss://<worker-host>/hub
```

`HUB_LINK_TOKEN` in the environment overrides the Keychain item and is not required when the item exists. Leave `ALEXA_LINK_TEST_DELAY_S` unset except when checking the 6.5 s cutoff. After restart, `hub.log` should show `alexa link connected`. It does not print the worker URL or the token. `alexa link hop_ms` is the socket hop and excludes the model. The link stays off while `ALEXA_LINK` is anything other than `1`.

### 7. Name the rooms (3 Echos)
Say something to each Echo once. Each new one is saved in `hub/data/alexa_devices.json` as "Echo 1/2/3" and the frame toasts "New Echo". Edit `room`/`name` there (e.g. `"room": "Kitchen", "name": "Kitchen Echo"`); no restart needed.

## Poll interval and cost
Defaults in `alexa_pull.py`: 2 s while active (a message in the last 10 min), 5 s idle, 15 s from 23:00 to 07:00.
Override with env `ALEXA_POLL_S`, `ALEXA_IDLE_POLL_S`, `ALEXA_NIGHT_POLL_S`. See the cost note in the build report.

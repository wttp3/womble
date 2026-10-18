# womble-alexa: Echo → Cloudflare Queue → Womble hub (ALEXA.md slices A1–A4)

Built 2026-10-15. Locale **en-GB**, invocation **frida** ("Alexa, ask Frida to add milk").

| Thing | Value |
|---|---|
| Worker (skill endpoint) | `<worker-url>` (the `https://womble-alexa.<subdomain>.workers.dev` URL printed by `cf deploy`; path `/` or `/alexa`) |
| Queue | `womble-alexa` (id `<queue-id>`), HTTP pull consumer: batch 10, visibility 60 s, max retries 3, retry delay 5 s |
| Worker secret | `ALEXA_SKILL_ID` (currently the placeholder `unset` → every request gets 400) |
| Interaction model | `skill/interaction-model.en-GB.json` |
| Hub module | `~/box3-voice/slice1/hub/alexa_pull.py` (wired into hub.py; disabled until the Keychain item exists) |
| Device → room map | `~/box3-voice/slice1/hub/data/alexa_devices.json` (created on first Alexa request) |

Files: `src/index.js` (entry), `src/app.js` (skill logic), `src/verify.js` (+`roots.js`) signature/cert-chain checks,
`cloudflare.config.ts`, `test/test.mjs`. Build/deploy needs Node ≥ 22.18: `export PATH=~/.local/node/bin:$PATH`.
- Test: `node test/test.mjs` (positive path uses a stub verifier passed in by the test only; the deployed entry always verifies)
- Deploy: `cf deploy`
- Config outside git: `cloudflare.config.ts` holds no account or skill id. `cf deploy` uses the account you are logged in to (`cf login`), the skill id lives only in the Worker secret `ALEXA_SKILL_ID`, and the hub reads `CF_ACCOUNT_ID=<acct>` / `CF_QUEUE_ID=<queue-id>` from the untracked `~/box3-voice/local.env` (template `local.env.example`).
- Set skill id: `cf workers secrets update ALEXA_SKILL_ID --worker womble-alexa --type secret_text --text <skill-id>`

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
3. Try in the Test tab first: type `ask frida to add milk` → expect "On it." Then on an Echo: "Alexa, ask Frida to add milk", or "Alexa, open Frida" → "Hi, what do you need?" → "add milk".
4. If an Echo is on Alexa+, and Frida isn't found, say so: it may hide dev skills.

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

### 6. Name the rooms (3 Echos)
Say something to each Echo once. Each new one is saved in `hub/data/alexa_devices.json` as "Echo 1/2/3" and the frame toasts "New Echo". Edit `room`/`name` there (e.g. `"room": "Kitchen", "name": "Kitchen Echo"`); no restart needed.

## Poll interval and cost
Defaults in `alexa_pull.py`: 2 s while active (a message in the last 10 min), 5 s idle, 15 s from 23:00 to 07:00.
Override with env `ALEXA_POLL_S`, `ALEXA_IDLE_POLL_S`, `ALEXA_NIGHT_POLL_S`. See the cost note in the build report.

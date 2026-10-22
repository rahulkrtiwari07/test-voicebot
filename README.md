# Voice Agent Platform

Config-driven outbound voice agents on the same infrastructure as the NDR voicebot:
LiveKit Cloud + the Plivo SIP trunk, Deepgram STT, Gemini LLM, Sarvam TTS and Silero VAD.
Creating a new agent is a form in the portal (or a JSON file in `examples/`), with no code change
and no redeploy.

```
 Browser (portal / dashboard / test call)
        │ REST                      ▲ WebRTC (test call)
        ▼                           │
 FastAPI  vap/api.py ──dispatch──▶ LiveKit Cloud ◀──SIP── Plivo ── phone
   │  Postgres, campaign scheduler    │
   │  /internal (shared secret)       ▼
   └────────◀── config / report ── worker  vap/worker.py
                                     STT → LLM (+ webhook tools) → TTS, streamed
```

## Run it

```bash
uv sync
cp .env.example .env.local        # fill in keys (same values as NDR_voicebot/ndr-api/.env)
docker compose up -d              # Postgres on localhost:5434
uv run uvicorn vap.api:app --port 8080      # portal at http://localhost:8080
uv run python -m vap.worker dev             # agent worker (registers as AGENT_NAME=vap-agent)
uv run pytest                               # 57 tests, SQLite, no network
```

The worker registers as `vap-agent`, not the NDR bot's `my-agent`, so the two never pick up
each other's jobs on the shared LiveKit project. Phone calls and campaigns need `SIP_TRUNK_ID`;
without it the portal offers browser test calls only. There are no migrations: the schema is
created on startup, so after an upgrade start with a fresh database.

## The Cynet Health reference-check agent

It lives in `examples/cynet_reference_check.json`, and in the portal it's **Agents → New from
template → Cynet Health reference check**. It is pure configuration:

- **Start-of-call tool.** `get_candidate_details(candidate_id)` is a webhook tool with
  `run_at_start: true`. The platform calls it while the phone is still ringing (no latency after
  pickup) and gives the result to the LLM before its first turn. The first message names the
  candidate through `{{get_candidate_details.name}}`. If the tool fails, that placeholder can't
  be filled, so the worker lets the LLM open instead. The mock endpoint is
  `/demo/tools/candidate-details` (IDs C-1001 to C-1003).
- **Questions and callback.** The prompt runs the six questions one at a time. A busy reference
  is offered a callback, and the agent confirms the preferred time before ending the call.
- **Extraction schema.** Exactly the spec's fields:
  - `strengths` and `improvements` use the new `string[]` type;
  - `rating` is an integer bounded 1–5, so a 7 or 4.5 is a validation error;
  - `would_rehire` is an enum of yes / no / unsure.

  `dates_match_claim` is judged against the tool result, because post-call analysis sees tool
  calls and their results interleaved in the transcript.
- **Outcome labels.** completed, callback_requested, declined, wrong_number, voicemail, failed.
  Calls decided without an LLM (no answer, busy, voicemail) map onto the agent's own labels: a
  no-answer becomes `failed` for this agent.
- **Call variables.** `reference_name` and `candidate_id`, filled per call or per CSV row.

## How each requirement is met

| Requirement | Where |
|---|---|
| **1. Portal**: agent CRUD with name, prompt, first message, LLM provider/model, TTS voice, language, max duration, end-call conditions, extraction schema, tools | `web/app.js`; `/api/agents`. Validated by `AgentConfig` (`vap/schemas.py`) and, for the Sarvam voice/model pairing, by the plugin's own table (`vap/providers.py`). Soft delete keeps call history. Templates are served from `examples/*.json`. |
| **2. Call runtime**: outbound phone call, browser fallback | `POST /api/calls/phone` dispatches the agent, waits for it to join, then dials over the SIP trunk with `wait_until_answered`. SIP codes map to busy / no_answer / rejected. `POST /api/calls/web` returns a LiveKit token for a WebRTC test call. |
| Streaming STT → LLM → TTS | One generic `AgentSession` built from config (`vap/providers.py`). Providers: google / openai / LiveKit Inference for the LLM; sarvam / deepgram / inference for TTS. |
| Barge-in | `turn_handling.interruption` with false-interruption resume. Interrupted agent turns are stored truncated and flagged `interrupted`. |
| Agent ends the call itself | Built-in `end_call(reason, closing_message)` tool driven by the end-call conditions. A max-duration timer forces a spoken wrap-up. Silence gets one check-in, then a hang-up. |
| **3. Webhook tools** | Each configured tool becomes an LLM function from its JSON schema (`make_webhook_tool`). The response is handed to the LLM. Timeouts, non-2xx responses and connection errors return `{"ok": false, ...}`, and the prompt tells the agent to apologize and never invent the result. Tools can also run at call start (see above). |
| **4. Post-call** | Transcript with speaker labels, timestamps, interrupted flags and per-turn metrics. One LLM call does summary, outcome and extraction (`vap/postcall.py`). Each field is type-checked, coerced and bounds-checked individually, with one repair retry on structural errors; per-field errors are stored, not dropped. |
| Post-call webhook, no duplicates on retry | `vap/webhooks.py`. One row per (call, event), unique constraint, fixed `event_id` and byte-identical body on every attempt. `Idempotency-Key` header plus HMAC `X-VAP-Signature`. Backoff, and a per-row lock against concurrent senders. The worker's report endpoint is idempotent (`finalized_at`). |
| **5. Dashboard** | Call log with status, duration, outcome, agent and avg voice-to-voice latency. Call detail shows summary, extracted data with validation issues, a transcript with tool calls interleaved (start-of-call ones badged), per-turn latency, and webhook deliveries with resend. |

**Voice-to-voice latency** is agent `started_speaking_at` minus the latest user
`stopped_speaking_at` since the previous agent turn, both from LiveKit's speech timestamps. This is
the same definition as LiveKit's `e2e_latency`, which is used when present. LiveKit only computes
that from the *last* user message, though, and it sometimes splits one utterance into two messages
where the second has no timestamp, which left most replies blank in live testing
(`runtime.voice_to_voice_ms`). Check-ins and wrap-ups the platform triggers itself are not replies
and are excluded.

## Stretch goals: campaigns and voicemail handling

**Campaigns** (`vap/campaigns.py`, **Campaigns** tab):
- **Contacts:** upload a CSV with a phone column plus one column per call variable (a sample is in
  `examples/reference_contacts.csv`). Bad numbers, duplicates and rows missing a variable are
  rejected per row and listed.
- **Concurrency:** limited per campaign and by a global `MAX_CONCURRENT_CALLS` (default 4, under
  LiveKit's 5-session Build-plan cap), shared across campaigns.
- **Retries:** no-answer, busy, rejected and voicemail count against the contact's `max_attempts`
  and are retried after the configured delay.
- **Infrastructure failures don't use the retry budget.** A call that ended `failed` without
  ever being answered (agent never joined, trunk or carrier 5xx, dial error, worker died before
  pickup) is our failure, not the contact's. It counts against a separate `max_infra_attempts`
  budget (5, retried after 60s), the same split ndr-api makes with `dial_attempted`.
- **Restarts:** all state is in the database (contact status, attempts, `next_attempt_at`), and
  applying a result is idempotent, so a restart or a repeated hook can't double-count.
- **Scope:** pause and resume are supported. A single API instance is assumed.

**Voicemail handling** (agent setting):
- **Detection:** when enabled, the agent listens briefly after pickup before speaking. A short
  "Hello?" followed by a pause is a person; continuous speech of 2.5s or more is a recorded
  greeting; silence for 5s is treated as a person.
- **Why not LiveKit's built-in detection:** this is the NDR bot's heuristic. LiveKit's built-in
  answering-machine detection read silent audio on SIP tracks there.
- **Action on voicemail:** hang up, or wait for the greeting and beep to finish and leave the
  configured message. The message supports `{{variables}}` and `{{tool.field}}`.
- **Result:** the call gets status `voicemail` and outcome `voicemail` (no LLM call), and
  campaigns retry it.

## Verified live (LiveKit Cloud, India South)

All of these ran through the real worker, Gemini, Sarvam and Deepgram, with a headless WebRTC
caller speaking lines synthesized by Deepgram TTS.

- **Reference check, full:**
  - The start tool ran in about 30ms, and the first message named "Priya Nair".
  - The six questions were asked, and the agent ended the call itself.
  - Extraction was correct: `dates_match_claim: true`, `would_rehire: yes`, `rating: 5`,
    strengths and improvements as lists.
  - The webhook was delivered once.
  - Voice-to-voice averaged about 2.0s (worst 3.0s). Gemini 2.5 Flash time-to-first-token was
    about 1.0s of that; Sarvam first audio about 0.2s.
- **Reference check, busy:** callback offered and confirmed, outcome `callback_requested`,
  `callback_time: "tomorrow, Friday, October 23rd, at 3 PM"`.
- **Voicemail:** a long greeting plus beep was detected as a machine. The agent left the message,
  naming the candidate from the start tool, and the call was labeled `voicemail`.
- **Earlier agents:**
  - tool success with barge-in;
  - a tool timing out after 3s, with a graceful apology;
  - a silent caller (check-in, then hang-up);
  - a 15s max-duration limit.
- **Portal:** every route loaded in headless Chromium (Playwright) with no JavaScript errors:
  template → save, invalid-voice rejection, start-call variables, call detail, campaign form.

Not tested live:
- **A real PSTN call or a live campaign.** Both need real numbers to ring. Campaign scheduling,
  concurrency and both retry budgets are covered by tests with faked telephony.
- **Voicemail detection on a real phone line.** It was tested over WebRTC only; real carrier audio
  and greetings will need threshold tuning.

## Known limitations

- **No portal auth.** Anyone who can reach port 8080 can create agents and place calls. This
  would be the next stretch goal (login plus workspaces).
- **Tool and webhook URLs are called from the worker and API hosts.** If those hosts can reach
  internal services, restrict the URLs (SSRF).
- **The Sarvam plugin's voice list can lag Sarvam's API.** `amelia` passed the plugin check but
  the API rejected it. The worker now ends and reports such calls immediately as `agent_error:…`
  instead of leaving them open.
- **Manual calls don't count toward the campaign concurrency cap.** Only campaign calls are
  limited by it.
- **Demo endpoints (`/demo/*`) are in-memory** sample tools and a webhook receiver.

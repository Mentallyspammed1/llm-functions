# lr.py — v3.7.3 → v4.0.0 upgrade report

`lr.py` is the Pyrmethus auto-reply webhook: the AutoReply Android app POSTs an
incoming text to it, it asks a local Ollama model for one in-character reply,
and it answers with the exact JSON shape the app expects. It runs on a phone
under Termux, so it has to survive flaky networks, duplicate deliveries and the
Android process reaper.

This pass was a full review + rewrite of the internals with **no wire-format
changes**. Everything the app, your `config.lr.yaml`, your database and your
scripts depend on still behaves the same — and in two cases (see below) now
behaves the way it was always documented to.

---

## 1. Compatibility contract (verified unchanged)

| Surface | Status |
|---|---|
| `POST /pyrmethus_autoreply_webhook`, `POST /webhook`, `POST /` | unchanged |
| Reply body `{"data": [{"message": "<text>"}]}` | unchanged, and still used for **every** "no reply" path (200 + `{"data": []}`) |
| Payload parsing: `{"data":[{...}]}` envelope, flat TK-Studio keys, generic aliases, nested `"query"` object | unchanged, plus extra aliases |
| `config.lr.yaml` keys | unchanged; all new keys are optional |
| SQLite table `conversations` | unchanged; new columns added via `ALTER TABLE` |
| CLI `-p/--port`, `--config`, `--no-kill`, `--check` | unchanged |
| Every public function/class name from v3.7.3 | still present (all 48 checked) |
| Terminal card layout | same fields, same colours, now correctly aligned |

---

## 2. Bugs found and fixed

### Security / correctness

| # | Problem in v3.7.3 | Impact | Fix |
|---|---|---|---|
| 1 | `security.require_signature` and `security.secret_file` were parsed but **never used** — a documented feature that did nothing | Anyone could forge a webhook if they reached the port, even with "signature auth" enabled | Real HMAC-SHA256 body verification (`X-Pyrmethus-Signature`, optional `sha256=` prefix), constant-time compare, optional replay window via `X-Signature-Timestamp` |
| 2 | `webhook_token` checked *after* the rate-limit decorator ran | Unauthenticated traffic burned a real sender's per-minute budget (cheap DoS / reply starvation) | Auth is now evaluated first, proven by a regression test |
| 3 | Token compared with `!=` | Timing side-channel | `hmac.compare_digest` |
| 4 | Dedup leader that died never cleared its in-flight key | Every later identical message waited the full follower budget, then got `{"data": []}` forever | In-flight entries expire, followers derive their wait from the leader's start time, and a follower can promote itself to leader |
| 5 | Follower budget = `timeout × (retries+1) + 10` = **85 s** by default | Duplicate deliveries hung past any app timeout | Capped at the request's own hard budget (+2 s), so it can't outlive the request |
| 6 | Reply budget (`max_auto_replies_per_sender_window`) was consumed *before* dedup | A duplicate delivery counted as a second auto-reply, silently reducing real capacity | Budget is consumed only when a reply is actually generated |
| 7 | `DEFAULT_CONFIG` nested dicts were aliased into the live config | Expanding `$HOME` mutated the defaults globally; `--export-config` wrote absolute sandbox paths into what should be a portable file | `deep_merge` deep-copies leaf values |
| 8 | `kill_stale_instance()` signalled whatever PID was in `~/lr.pid.<port>`; busy-port reclaim ran `fuser -k -9` | A recycled PID (or an unrelated service on that port) could be killed | PID + `/proc` cmdline must match this script before any signal; broad killing is opt-in |
| 9 | `ColorFormatter` mutated `record.levelname` in place | ANSI escape codes leaked into the JSON log file | Formatting now works on a cloned record |
| 10 | File logger built at import time with `os.makedirs` unguarded | Read-only `$HOME` crashed the process before it could log why | Falls back to console-only with a warning |
| 11 | `isMessageFromGroup: null` (present but null) bypassed the `isGroup` fallback | DM/group misclassification | Explicit `None`-aware fallback chain |

### Conversation quality

| # | Problem | Fix |
|---|---|---|
| 12 | `build_chat_messages()` accepted `app_id`, `is_group`, `group_name` and then ignored all three | The model was told nothing about who it was replying to, or that it was in a group; group replies read like DMs | Context block appended to the system prompt: sender name, "this is a group chat called X", source app, local time, length cap |
| 13 | History ordered by `timestamp DESC`, which SQLite stores at **1-second** resolution | Messages inside the same second arrived in arbitrary order, scrambling short-term memory | Ordered by `id` (monotonic insertion order) |
| 14 | History trimming popped index 1 repeatedly and could leave a conversation starting on an `assistant` turn | Some models then answered as the wrong party or refused | Trims whole pairs from the oldest end and guarantees `system → user → … → user` |
| 15 | `clean_llm_response()` handled `<think>`/`[think]` only | `Jeremiah: "…"`, `<|im_end|>`, fences and smart quotes reached the recipient verbatim | Full normaliser: reasoning blocks (5 variants), role labels, special tokens, fences, quotes, zero-width chars, whitespace, plus SMS-length clamping on a sentence boundary |
| 16 | `query_ollama()` retried **all** errors, including 400/404 | A wrong model name burned the whole retry budget and the request deadline | Errors classified: transient (timeout/5xx/429/empty) retry with jittered backoff, fatal (4xx/bad model) skip to the next model in `fallback_models` |

### Operability

| # | Problem | Fix |
|---|---|---|
| 17 | `"3.7.3"` hand-copied into health, stats, banner, dashboard, header comment | Version drift | Single `VERSION` constant used everywhere |
| 18 | `runtime.request_timeout_seconds`, `max_workers` and `graceful_shutdown_seconds` were dead config | LLM retries could run far past any sane request budget; shutdown killed in-flight replies | One deadline per request threaded into Ollama retries; graceful drain on SIGTERM/SIGINT |
| 19 | `/health` returned 503 when merely *degraded* (Ollama down) | Orchestrators restart a perfectly healthy process | `/health` keeps its 200-on-healthy / 503-on-degraded contract, and a new `/live` is a pure liveness probe |
| 20 | Dashboard stats SQL was duplicated between `/` and `/stats` | Drift, and two different numbers for the same thing | One `get_stats_snapshot()` behind both |
| 21 | Dashboard timestamps were raw SQLite UTC strings rendered as if local | Every entry looked 5–6 h old in St. Louis | UTC→local conversion (`Nov 10 · 09:44 AM`) plus ISO in a tooltip |
| 22 | Dashboard printed the raw Android package id (`com.whatsapp.w4b`) | Unreadable | `APP_NAMES` mapping, used by API and UI |
| 23 | Retention daemon slept a full 24 h before doing anything; digest fired 24 h after boot | First prune/digest happened at an arbitrary time | Maintenance wakes every 10 min and acts on calendar boundaries; digest has `digest_hour` |
| 24 | `find_available_port()` silently moved to another port | You'd curl the old port and think the service was down | Explicit warning in the log and banner |
| 25 | `--check` always exited 0 | Useless in scripts | Non-zero on DB failure or when signature auth is on but the secret is missing; `--version/--stats/--prune/--vacuum/--export-config` added |
| 26 | `_pct()` used `int(len * p / 100)` | p95 could report the max (`len-1` → n) | Nearest-rank with clamping; p90/p99 added |

---

## 3. New capabilities

**Gates** (all off by default, so behaviour is identical until you opt in):

* `security.quiet_hours` — wrap-around DND window with `drop` or `fallback`.
* `security.min_reply_interval_seconds` — per-sender cooldown against rapid-fire.
* `security.ignore_sender_patterns` / `ignore_message_patterns` — regex lists; the obvious use is not auto-replying to OTP/bank shortcodes.
* `security.allowed_app_ids` — restrict to WhatsApp, or exclude Telegram, etc.
* `security.rate_limit_action` — `429` (explicit, default) or `drop` (silent 200 + empty data, which stops retry storms).

**Ollama**: fallback model chain, per-request deadline, bounded jittered backoff, token accounting, optional background warmup, `num_ctx`/`top_p`/`top_k`/`repeat_penalty`/`seed` passthrough.

**Observability**: `/live`, `/api/dashboard`, `/api/conversations` (filterable), `/api/config` (secrets redacted), `/metrics/prometheus`, request-id echo header, per-app/per-model counters, p50/p90/p95/p99, token totals, `memory_rss_mb`.

**Safety nets**: `X-Content-Type-Options`, `Referrer-Policy`, CSP on HTML, `Cache-Control: no-store` on API responses, payload cap configurable, JSON 404/405/500 handlers.

---

## 4. UI

### Terminal

The old card used `str.ljust()` on strings containing ANSI escapes, so borders
were off by the byte-length of the colour codes, and emoji shifted rows by one
column. Long messages were clipped at 700 characters mid-word.

The new card measures **display** width (ANSI stripped, East-Asian/emoji = 2
cells, combining marks = 0), word-wraps each field with a continuation indent,
and colour-codes latency by threshold (green ≤ 800 ms, amber ≤ 2.5 s, red
above):

```
╭──────────────────────────────────────────────────────────────────────╮
│ 📥 INCOMING via WhatsApp #Family                                     │
├──────────────────────────────────────────────────────────────────────┤
│ Sender   Maya                                                        │
│ Message  hey are you coming saturday? we're doing toasted ravioli    │
│          and then the arch                                           │
├──────────────────────────────────────────────────────────────────────┤
│ 💬 REPLY · nemotron-3-nano:30b-cloud  ⚡342ms                        │
│          yeah prob — what time? i can swing by after the bike ride    │
├──────────────────────────────────────────────────────────────────────┤
│ 🕒 10:42:11 PM                                                       │
╰──────────────────────────────────────────────────────────────────────╯
```

`ui.ascii_only: true` swaps the box-drawing glyphs for `+-` for terminals
without Unicode support.

### Dashboard

A full rebuild — still a single inline template, zero CDN, works offline, and
still renders server-side if JS is disabled:

* **Live** — polls `/api/dashboard` every `dashboard.refresh_seconds`, pauses
  automatically when the tab is hidden, shows an offline state instead of
  freezing, `Pause`/`Refresh` buttons.
* **Six stat cards** — total texts, contacts, last 24 h, past hour, 24 h mean
  latency (p50/p95 in the tooltip), and messages suppressed by the loop/dupe/
  block gates.
* **24-hour activity chart** — pure CSS bars from a SQL `strftime` grouping,
  current hour highlighted, per-bar tooltip.
* **Pipeline panel** — received / replied / model calls / llm errors /
  duplicates / self-loop / rate limited / auth failures / unhandled.
* **Top contacts** — relative bars for the last 24 h.
* **Conversation feed** — instant client-side search (`/`), app dropdown built
  from real traffic, "groups only" toggle, latency tags coloured by threshold,
  app + group badges, local timestamps.
* **Chrome** — health pill (healthy/degraded/unhealthy with a hover breakdown),
  uptime, theme toggle (persisted in `localStorage`, `t`), copy-webhook button,
  keyboard shortcuts (`/`, `r`, `p`, `t`), reduced-motion support, and a
  responsive layout down to phone width.
* Everything user-supplied is inserted with `textContent`/`esc()`, so a contact
  named `<img onerror=…>` cannot execute.

---

## 5. Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/pyrmethus_autoreply_webhook` | main webhook (unchanged) |
| POST | `/webhook`, `/` | aliases (unchanged) |
| GET | `/` | dashboard |
| GET | `/health` | deep-ish health, 200/503 (unchanged semantics) |
| GET | `/live` | liveness, always 200 |
| GET | `/ready` | readiness incl. chat smoke test |
| GET | `/stats` | superset of the old stats JSON |
| GET | `/metrics` | counters/gauges/latency/tokens |
| GET | `/metrics/prometheus` | Prometheus text format |
| GET | `/api/dashboard` | everything the UI renders |
| GET | `/api/conversations` | `limit`, `offset`, `sender`, `q`, `groups_only` |
| GET | `/api/config` | effective config, secrets redacted |
| GET | `/favicon.ico` | inline SVG |

---

## 6. Testing

Everything below runs offline against a stub Ollama (no model required):

| Suite | Checks | What it covers |
|---|---|---|
| `tests/test_lr_upgrades.py` (added) | 53 | wire schema, every gate, auth + HMAC + replay window, dedup single-flight, cleanup helpers, dashboard/API endpoints |
| API integration | 52 | live server: schema, dedup, stale, self-loop, bounds, 413/404/405, dashboard, metrics, filters |
| Feature integration | 32 | bearer/query/header auth, signature, rate limit + `Retry-After`, cooldown, quiet hours, filters, LLM failure/empty/fallback-model, 6-way stampede → 1 model call, graceful shutdown |
| CLI / config | 19 | `--version/--stats/--prune/--vacuum/--export-config/--force`, round-trip of the exported config, malformed YAML/values, missing secret |
| Unit | 81 | normaliser, prompt builder, quiet hours, age parsing, dedup, limiter, guard, metrics, ANSI width maths |
| Dashboard UI (jsdom) | 41 | polling, stat/bar/pipeline/feed rendering, search, filters, pause, theme, shortcuts, offline state, XSS escaping |

Run the in-repo suite with:

```bash
python3 -m unittest tests.test_lr_upgrades -v
```

`pyflakes` is clean.

---

## 7. Upgrading

1. Back up `lr.py` and `~/pyrmethus_conversations.db` (the DB schema only ever
   gains columns — no destructive migration).
2. Drop in the new `lr.py`. Your existing `config.lr.yaml` keeps working
   untouched; new keys are optional.
3. `python3 lr.py --check` — validates config, DB, Ollama and prints any
   security warnings.
4. `python3 lr.py` and open the dashboard.
5. Optional hardening if you bind beyond loopback: set
   `security.webhook_token`, or `security.require_signature: true` with the
   secret in `security.secret_file` and an `X-Pyrmethus-Signature: sha256=<hex>`
   header on the app/webhook sender.

A fully commented starting point is in `config.lr.example.yaml`
(regenerate with `python3 lr.py --export-config config.lr.yaml`).

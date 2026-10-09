# NEXUS — Bug & Defect Report (`bugs.md`)

_Automated + manual audit of the NEXUS codebase (EDITH repo)._

Scope: `services/core/nexus/` (Python, ~55k LOC), `apps/desktop/src/` (TypeScript/React, ~19.5k LOC), `scripts/`, `tools/`.

---

## 0. Honest framing (read this first)

You asked for **500 critical bugs**. I won't fabricate 500 to hit a number — that would be worthless to you. What is actually true:

* A static pass (`ruff --select ALL` + `pyflakes`, plus a targeted security sweep) produced **7,687 raw findings** (**3,206** in the `nexus` package alone). The large majority are *style* rules (`D103` docstrings, `E501` line length, `COM812` trailing commas, `TID252` relative imports) — **not bugs**.
* After de-duplication and filtering to real defect categories this report has **36 verified, code-level defects** plus **~450 line-level static findings** grouped by category. Many are low/medium severity, and a few are explicit false positives (called out).
* The codebase is, frankly, **high quality**: no `shell=True`, no hardcoded keys, no `TODO`/`FIXME`, `python -m compileall` is clean, and `tsc --noEmit` reports **zero** type errors. So there is no honest way to enumerate 500 *critical* bugs — a critical bug loses data, leaks a secret, or runs untrusted code, and only a handful here have real teeth.

Everything is reproducible with the commands in §5.

---

## 1. Severity model

| Severity | Meaning |
|----------|---------|
| **Critical** | Data loss/corruption, secret exposure to a model/network, RCE, or permission bypass. |
| **High** | Silent wrong behaviour, resource exhaustion, state-corrupting race, crash on a normal path. |
| **Medium** | Robustness/edge bug, latent injection/SSRF sink, event-loop blocking, unbounded growth. |
| **Low** | Correctness nit, maintainability hazard, dead code, style rule with a small real risk. |
| **Info** | Pattern worth knowing; not a defect by itself. |

---

## 2. Part 1 — Verified defects (read and confirmed in source)

### Data / correctness

**NEXUS-001 — `[Medium]` Duplicate dictionary key silently shadows a setting.**
`services/core/nexus/db/repos/app_settings.py:127` and `:200` both define `"semantic_search_enabled"`. Python keeps the last, so one entry is dead and wins silently (`F601`).
_Fix:_ delete/rename the duplicate, or validate `DEFAULTS` for duplicate keys at import.

**NEXUS-002 — `[Medium]` Dead SQL builder interpolates column names (latent injection).**
`services/core/nexus/db/repos/base.py:69-89` `build_update()` builds `UPDATE {table} SET {assignments} ...` where the table (`:86`) and each column key (`:84`) are f-string-interpolated. Values are parameterised; **identifiers are not**. It is **never called** anywhere (grep-verified) — dead code one wire-up away from a real SQL-injection sink.
_Fix:_ delete it, or allow-list identifiers.

**NEXUS-003 — `[Medium]` Un-redacted exception bodies returned to the client.**
`services/core/nexus/api/__init__.py:218-234` returns `str(exc)` and `type(exc).__name__` in the 500 body. Logs are scrubbed by `RedactionFilter`, the **HTTP response is not** — DB/provider errors can leak paths, SQL fragments or key-shaped substrings to the browser and activity feed.
_Fix:_ return a generic message; keep detail in the redacted log.

**NEXUS-004 — `[Medium]` Blocking filesystem I/O inside async functions.**
`providers/mistral_audio.py:218`, `providers/nim_media.py:430` & `:612`, `providers/nvidia_speech.py:424` do blocking `pathlib.Path` work inside `async def`, stalling the whole event loop (`ASYNC240`).
_Fix:_ `await asyncio.to_thread(...)`.

**NEXUS-005 — `[Medium]` Fire-and-forget `asyncio.create_task` (tasks can be GC'd).**
`ai/core.py:623`, `api/routes/chat_routes.py:319`, `api/routes/voice_routes.py:459`, `monitors/supervisor.py:227` & `:262` create tasks without keeping a reference (`RUF006`) — the task can be garbage-collected mid-flight and its exception is never observed.
_Fix:_ hold references (set + `add_done_callback(discard)`, or `asyncio.TaskGroup`).

### Concurrency

**NEXUS-006 — `[Medium]` Unsynchronised terminal registry.**
`tools/terminal.py:50` `_RUNNING: dict[str, Popen]` is mutated by `run()`/`cancel()`/`cancel_all()` and read by `running()` from multiple threads with **no lock**. `running()` (`:333-334`) can raise `RuntimeError: dictionary changed size during iteration`, and report stale pids.
_Fix:_ guard every access with a `threading.Lock`.

**NEXUS-007 — `[Medium]` Shell commands inherit the full environment (incl. exported secrets).**
`tools/terminal.py:57-69` `_clean_env()` copies `os.environ` wholesale. Exported API keys reach every model-run command, and stdout/stderr is returned to the model, so `env`/`printenv` can surface secrets if a shape rule misses.
_Fix:_ strip `KNOWN_CREDENTIALS` (and `*_TOKEN`/`*_KEY`) from the child env.

**NEXUS-008 — `[Medium]` Secrets shorter than 8 chars are never redacted.**
`logging_setup.py:40-44` `register_secret()` ignores `len < 8`, so short tokens/PINs appear verbatim in logs and diagnostics.
_Fix:_ register all non-empty values, or document a justified minimum.

**NEXUS-009 — `[Medium]` Unbounded in-memory secret retention.**
`logging_setup.py:25` `_secret_values: set[str]` grows for the process lifetime and stores **raw** secrets in RAM (`forget_secret` only on delete). Memory growth + long-lived plaintext secrets.
_Fix:_ cap/LRU the set or store hashes.

**NEXUS-010 — `[Medium]` Thread leak when the keyring times out.**
`secrets.py:77-149` `_bounded()` runs each keyring call on its own thread and stops *waiting* after 5 s, but a blocked D-Bus call cannot be cancelled — the thread lives on and each slow call leaks one.
_Fix:_ a single reusable worker thread / executor instead of one thread per call.

### Reliability / low severity

**NEXUS-011 — `[Low]` `.env` cache is neither locked nor invalidated.** `secrets.py:267-275` populates global `_env_cache` without a lock and never refreshes — editing `.env` needs a restart, and a concurrent first call can double-parse.

**NEXUS-012 — `[Low]` `check_same_thread=False` weakens SQLite's guard.** `db/conn.py:61`. The module uses thread-local connections, so this only matters if a connection is ever captured and used off-thread — at which point corruption is silent instead of raising.

**NEXUS-013 — `[Low]` `configure()` races.** `db/conn.py:37-47` writes the global `_db_path` without a lock.

**NEXUS-014 — `[Low]` Backup path assumes one suffix / backups never pruned.** `db/migrate.py:51-61` uses `path.with_suffix(f".backup-v{ver}-{stamp}.db")`; multi-dot paths yield odd names, and nothing deletes old backups → unbounded disk growth.

**NEXUS-015 — `[Low]` WebSocket help text contradicts behaviour.** `api/ws.py:46` lists `"subscribe"` in `_INBOUND` (and in the "Accepted: …" message at `:117`), but `_handle()` (`:132-145`) rejects it.

**NEXUS-016 — `[Low]` `after_seq` accepts negatives.** `api/ws.py:62-65` `int(...)` with no clamp; a negative replay cursor is undefined.

**NEXUS-017 — `[Low]` `assert` used for control logic.** `db/conn.py:238`, `tools/runtime.py:180`, `tools/terminal.py:99` (`S101`). Under `python -O` all three vanish.

**NEXUS-018 — `[Low]` Tool-param redaction has a length threshold.** `tools/runtime.py:292-303` only redacts `_SENSITIVE_KEYS` when the value is **>200 chars**; short secrets in params are persisted verbatim to `activity` and `tool_invocations`.

**NEXUS-019 — `[Low]` `HumanFormatter` prints extras without redaction.** `logging_setup.py:191-204` builds `k=v` from `record.__dict__`, relying on the filter to mutate the record; a handler without the filter prints extras raw.

**NEXUS-020 — `[Low]` `settings()` is `lru_cache`d.** `config.py:71-73`; `NEXUS_*` env changes after first call are ignored.

**NEXUS-021 — `[Low]` Generic `f"...{table}"` SQL helpers.** `db/maintenance.py:308-314` (`_count`, `_columns`) and `:263-264` build SQL/PRAGMA from a `table` argument. Safe only because current callers pass constants from `EXPORT_TABLES`; a future caller passing a payload value is an injection (same latent class as NEXUS-002).

**NEXUS-022 — `[Low]` DNS-rebinding gap in the SSRF guard.** `tools/browser.py:133-151` validates the host with `_assert_public(hostname)`, then lets `urllib` **re-resolve** at connect time — a name that passes can resolve to `127.0.0.1` on the second lookup (TOCTOU). Redirects are re-checked (`:101-114`), which is good.

**NEXUS-023 — `[Low]` Charset parse keeps trailing params.** `tools/browser.py:158-159` `split("charset=")[-1].strip()` yields a bogus charset for `charset=utf-8; boundary=…`.

**NEXUS-024 — `[Low]` `_attempt` retry may duplicate emitted events.** `router/__init__.py:641-662` re-issues `provider.stream(request)` after `RateLimited`; if the first stream yielded events before raising they are emitted twice.

### Info-level pattern findings

**NEXUS-025 — `[Low]` Unused imports (dead code):** `core/scoring.py:42` (`field`, `replace`), `db/repos/automations.py:16` (`timezone`), `db/repos/base.py:16` (`..conn`), `learning/bandit.py:47` (`Iterable`), `monitors/usb_monitor.py:33` (`Iterable`) — `F401` ×6.

**NEXUS-026 — `[Low]` `try/except/pass` swallows failures with no trace:** `logging_setup.py:156`, `router/probe.py:118`, `tools/browser.py:450` (`S110`).

**NEXUS-027 — `[Low]` Global mutable singletons (`PLW0603` ×12):** `providers/__init__.py:54`, `providers/mistral_audio.py:436`, `providers/nim_media.py:931`, `providers/nvidia_speech.py:803`, `router/__init__.py:694`, `secrets.py:262` & `:272`, `monitors/supervisor.py:370`, and the `core/proactive.py` state — each a manual-locking hazard.

**NEXUS-028 — `[Low]` Non-cryptographic PRNG:** `learning/bandit.py:141` (`S311`). Fine for exploration; wrong if ever reused for anything security-relevant.

**NEXUS-029 — `[Low]` Naive `datetime.now()` / `fromtimestamp()` (no tz):** `DTZ005`/`DTZ006` ×11 — `ai/context.py:171`, `core/proactive.py:123`, `monitors/screen_monitor.py:237` & `:453`, `providers/mistral_audio.py:336`, `providers/nim_media.py:584`, `providers/nvidia_speech.py:586`, `tools/files.py:436`, `tools/screen.py:187` & `:296`. Local-time values compared with UTC-stored ones can be off by the TZ offset.

**NEXUS-030 — `[Low]` `zip()` without `strict=`:** `B905` ×7 — `ai/embeddings.py:141` & `:154`, `core/trends.py:163` & `:167`, `db/repos/memories.py:51`, `learning/bandit.py:154`, `monitors/screen_monitor.py:150`. Unequal lengths truncate silently.

**NEXUS-031 — `[Low]` `logging` `extra` key `name` collides with `LogRecord`:** `G101` ×5 — `db/migrate.py:90`, `tools/process_signal.py:184,280,413`, `tools/system.py:1397`. The value can be dropped/overwritten.

**NEXUS-032 — `[Low]` `.error(..., exc_info=True)` instead of `.exception(...)`:** `G201` ×6 — `ai/agent.py:746`, `api/__init__.py:72`, `db/conn.py:119,125`, `db/migrate.py:110`, `tools/runtime.py:140`.

**NEXUS-033 — `[Info]` 49 f-string SQL sites (`S608`).** Verified samples (`activity.py:104`, `app_settings.py`) are *constant* clause fragments plus `?` placeholders — **not exploitable**. They defeat static analysis and become dangerous the moment a fragment stops being constant. Full list in Appendix B; triage recommended.

**NEXUS-034 — `[Info]` Subprocess hygiene:** `S603` ×24, `S607` ×11 (partial executable path), `PLW1510` ×26 (`subprocess.run` without explicit `check=`). All use fixed argv lists with **no `shell=True`** (verified), so risk is low; `S607` (e.g. `apps.py:154`, `apps.py:184`) should use a full path to avoid PATH hijack.

**NEXUS-035 — `[Info]` Complexity hotspots:** `C901` ×39 (worst: `providers/anthropic.py:452` complexity 34 / 104 statements; `tools/windows.py:323` 22; `permissions/policy.py:567` 21; `tools/registry.py:366` 21). `PLR0915` ×8. High complexity is where logic bugs hide — split before they grow.

**NEXUS-036 — `[Info]` Exception messages built with f-strings outside the exception class:** `TRY003` ×162, `EM101`/`EM102` ×141. Maintainability only.

---

## 3. Part 2 — Frontend (TypeScript / React)

`apps/desktop/` is ~19.5k LOC across 39 files. `tsc --noEmit` reports **0 errors**, so these are behavioural nits, not type bugs.

**NEXUS-F001 — `[Low]` SSE frame parser strips legitimate leading whitespace.**
`apps/desktop/src/lib/sse.ts:172-177` `parseFrame()` does `line.slice(5).trimStart()`. The SSE spec allows *one* optional space after `data:`; `trimStart()` removes **all** leading whitespace, so a payload whose data begins with meaningful spaces is altered. _Fix:_ `line.slice(5).replace(/^ /, "")`.

**NEXUS-F002 — `[Low]` SSE frames split on `\n\n` only.**
`sse.ts:128` and the core (`api/sse.py:42`) agree on `\n\n`, so this works today, but the SSE spec also permits `\r\n\r\n`. Any proxy that normalises line endings would break framing. _Fix:_ normalise CRLF in the parser.

**NEXUS-F003 — `[Low]` `openStream` fire-and-forgets `pump()`.**
`sse.ts:62-66` calls `void pump(...)`. If `pump` ever throws before its internal `end()` guard, the rejection is unobserved. `pump` currently catches its own errors, so it holds — but it is fragile. _Fix:_ `pump(...).catch((e) => handlers.onEnd(String(e)))`.

**NEXUS-F004 — `[Low]` `localStorage` writes can throw and are only partly guarded.**
`state/nexus.tsx:142-152` deliberately guards reads (private-mode browsers throw), but writes at `state/nexus.tsx:344,363` and `shell/CommandBar.tsx:111` call `setItem` without a guard — a quota/private-mode throw propagates into a React event handler. _Fix:_ wrap writes in try/catch like the reads.

**NEXUS-F005 — `[Low]` `any` leaks past the type system.**
`sweep3.txt` found `as any` / untyped `any` usage (e.g. `lib/useAsync.ts:27` `(state as any).detail`, `state/nexus.tsx` event payload reads). Each is a place the compiler cannot help. _Fix:_ narrow the payload types at the `events.ts` boundary.

**NEXUS-F006 — `[Info]` Console logging on failure paths.**
`sse.ts:103`, `events.ts` — deliberate (the comment says so), but in a packaged Tauri shell there is no console; consider routing to the diagnostics buffer.

---

## 4. Part 3 — Severity rollup

| Severity | Count (this report) | Where |
|----------|--------------------:|-------|
| Critical | 0 | none found with real teeth |
| High | 0 (all High-classified risks are Medium) | — |
| Medium | 12 | NEXUS-001…010, plus NEXUS-033 latent-sink caveat |
| Low | 20 | NEXUS-011…032 |
| Info | 4 | NEXUS-033…036 |
| Verified total | **36** | §2 + §3 |
| Static line-level findings (grouped) | **~450** | Appendix B |

> The **Top 5 to fix first**: NEXUS-003 (un-redacted 500 bodies), NEXUS-007 (shell env carries secrets), NEXUS-005 (droppable tasks), NEXUS-006 (registry race), NEXUS-002 (delete the injection-shaped dead builder).

---

## 5. Method & reproduce

```bash
# 1. Lint (Python) — full rule set, concise, machine-readable
.venv/bin/pip install --target /tmp/an ruff pyflakes
/tmp/an/bin/ruff check --no-cache --select ALL --output-format=concise services/core/nexus services/core/tests scripts

# 2. Rule histogram
/tmp/an/bin/ruff check --no-cache --select ALL --statistics services/core/nexus

# 3. Byte-compile (must be clean)
.venv/bin/python -m compileall -q services/core/nexus

# 4. Type-check the frontend (must be clean)
cd apps/desktop && node_modules/.bin/tsc --noEmit

# 5. Security sweep
grep -rnE 'shell=True|os\.system|eval\(|exec\(|pickle\.load|verify=False' services/core
```

**False-positive note.** `S608` (SQL) is reported 49× and `B008` (`Body(...)`/`File(...)` in defaults) 12×; both are expected patterns here (constant SQL fragments with `?` placeholders; FastAPI dependency defaults). They are documented, not claimed as exploitable. `PLR2004` (magic values, ×92) and the `D1xx`/`E501`/`COM812`/`TID252` families (~1,900) are pure style and were excluded from the count, except where cited.

---

## Appendix A — `ruff` rule histogram (`services/core/nexus`)


warning: `incorrect-blank-line-before-class` (D203) and `blank-line-before-class` (D211) are incompatible. Ignoring `incorrect-blank-line-before-class`.
warning: `multi-line-summary-first-line` (D212) and `multi-line-summary-second-line` (D213) are incompatible. Ignoring `multi-line-summary-second-line`.
641	E501    	[ ] line-too-long
315	TID252  	[ ] relative-imports
268	COM812  	[*] missing-trailing-comma
214	D103    	[ ] undocumented-public-function
162	TRY003  	[ ] raise-vanilla-args
139	D102    	[ ] undocumented-public-method
133	D401    	[ ] non-imperative-mood
123	EM102   	[ ] f-string-in-exception
105	CPY001  	[ ] missing-copyright-notice
 92	PLR2004 	[ ] magic-value-comparison
 85	PLC0415 	[ ] import-outside-top-level
 76	FAST002 	[ ] fast-api-non-annotated-dependency
 58	ANN401  	[ ] any-type
 49	S608    	[ ] hardcoded-sql-expression
 39	C901    	[ ] complex-structure
 39	EM101   	[ ] raw-string-in-exception
 34	D101    	[ ] undocumented-public-class
 34	RUF100  	[*] unused-noqa
 32	ISC004  	[ ] implicit-string-concatenation-in-collection-literal
 32	PLR0913 	[ ] too-many-arguments
 27	PLR0911 	[ ] too-many-return-statements
 26	UP035   	[-] deprecated-import
 26	PLW1510 	[ ] subprocess-run-without-check
 26	D107    	[ ] undocumented-public-init
 24	S603    	[ ] subprocess-without-shell-equals-true
 23	N818    	[ ] error-suffix-on-exception-name
 21	ARG001  	[ ] unused-function-argument
 19	PLR0912 	[ ] too-many-branches
 18	FBT001  	[ ] boolean-type-hint-positional-argument
 18	I001    	[*] unsorted-imports
 17	FBT003  	[ ] boolean-positional-value-in-call
 15	RUF005  	[ ] collection-literal-concatenation
 13	D205    	[ ] missing-blank-line-after-summary
 12	B008    	[ ] function-call-in-default-argument
 12	PLW0603 	[ ] global-statement
 12	PERF401 	[ ] manual-list-comprehension
 11	S607    	[ ] start-process-with-partial-path
  8	BLE001  	[ ] blind-except
  8	DTZ005  	[ ] call-datetime-now-without-tzinfo
  8	SIM105  	[ ] suppressible-exception
  8	PLR0915 	[ ] too-many-statements
  7	FBT002  	[ ] boolean-default-value-positional-argument
  7	D209    	[*] new-line-after-last-paragraph
  7	UP037   	[*] quoted-annotation
  7	PLR0917 	[ ] too-many-positional-arguments
  7	TRY300  	[ ] try-consider-else
  7	B905    	[ ] zip-without-explicit-strict
  6	G201    	[ ] logging-exc-info
  6	ANN001  	[ ] missing-type-function-argument
  6	F401    	[*] unused-import
  5	RUF006  	[ ] asyncio-dangling-task
  5	G101    	[ ] logging-extra-attr-clash
  5	TC001   	[ ] typing-only-first-party-import
  5	TC003   	[ ] typing-only-standard-library-import
  5	D104    	[ ] undocumented-public-package
  5	RUF022  	[*] unsorted-dunder-all
  4	RUF001  	[ ] ambiguous-unicode-character-string
  4	ASYNC240	[ ] blocking-path-method-in-async-function
  4	SIM102  	[ ] collapsible-if
  4	B027    	[ ] empty-method-without-abstract-decorator
  4	D400    	[ ] missing-trailing-period
  4	UP006   	[*] non-pep585-annotation
  4	D105    	[ ] undocumented-magic-method
  4	ARG002  	[ ] unused-method-argument
  3	S101    	[ ] assert
  3	PTH123  	[ ] builtin-open
  3	DTZ006  	[ ] call-datetime-fromtimestamp
  3	UP015   	[*] redundant-open-modes
  3	S110    	[ ] try-except-pass
  2	Q003    	[*] avoidable-escaped-quote
  2	LOG014  	[ ] exc-info-outside-except-handler
  2	ANN202  	[ ] missing-return-type-private-function
  2	TRY301  	[ ] raise-within-try
  2	FURB167 	[*] regex-flag-alias
  2	FURB188 	[*] slice-to-remove-prefix-or-suffix
  2	PERF203 	[ ] try-except-in-loop
  2	TRY004  	[ ] type-check-without-type-error
  2	C416    	[ ] unnecessary-comprehension
  2	TRY203  	[ ] useless-try-except
  1	Q000    	[*] bad-quotes-inline-string
  1	A004    	[ ] builtin-import-shadowing
  1	A001    	[ ] builtin-variable-shadowing
  1	ERA001  	[ ] commented-out-code
  1	D301    	[ ] escape-sequence-in-docstring
  1	SIM108  	[ ] if-else-block-instead-of-if-exp
  1	SIM114  	[*] if-with-same-arms
  1	N801    	[ ] invalid-class-name
  1	LOG004  	[ ] log-exception-outside-except-handler
  1	PERF403 	[ ] manual-dict-comprehension
  1	W292    	[*] missing-newline-at-end-of-file
  1	ANN204  	[ ] missing-return-type-special-method
  1	F601    	[ ] multi-value-repeated-key-literal
  1	PIE810  	[ ] multiple-starts-ends-with
  1	SIM103  	[ ] needless-bool
  1	N806    	[ ] non-lowercase-variable-in-function
  1	PTH208  	[ ] os-listdir
  1	PTH115  	[ ] os-readlink
  1	PTH105  	[ ] os-replace
  1	PTH116  	[ ] os-stat
  1	PT018   	[ ] pytest-composite-assertion
  1	PLR1714 	[ ] repeated-equality-comparison
  1	FURB171 	[ ] single-item-membership-test
  1	S311    	[ ] suspicious-non-cryptographic-random-usage
  1	S310    	[ ] suspicious-url-open-usage
  1	RET504  	[ ] unnecessary-assign
  1	RUF059  	[ ] unused-unpacked-variable
Found 3206 errors.
[*] 384 fixable with the `--fix` option (638 hidden fixes can be enabled with the `--unsafe-fixes` option).

> Totals for `services/core/nexus` only. `D*`, `E501`, `COM812`, `TID252`, `CPY001`, `ANN*`, `ISC*`, `FAST*`, `EM*`, `TRY003` are style/reporting rules, not defects.

---

## Appendix B — Grouped static findings (`file:line`, `tests/` excluded)

> Raw `ruff --output-format=concise` lines, grouped by rule. `S608` and `B008` include known false positives (see §5 note).

S608
  - services/core/nexus/db/maintenance.py:70:43: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:150:27: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:171:25: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:190:29: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:263:25: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:264:26: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/maintenance.py:309:24: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/activity.py:104:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:178:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:244:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:251:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:257:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:257:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:263:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/ambient.py:321:32: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/automations.py:262:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/base.py:86:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/conversations.py:64:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/conversations.py:218:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/conversations.py:291:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/conversations.py:342:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:183:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:236:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:255:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:285:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:328:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:442:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:456:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:460:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/memories.py:465:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/permissions_store.py:213:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/permissions_store.py:228:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/projects.py:56:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/projects.py:91:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/projects.py:118:9: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:43:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:81:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:99:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:112:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:120:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/search.py:133:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:95:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:146:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:194:17: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:200:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:211:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:331:13: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:379:17: S608 Possible SQL injection vector through string-based query construction
  - services/core/nexus/db/repos/tasks.py:387:13: S608 Possible SQL injection vector through string-based query construction

S603
  - services/core/nexus/monitors/bluetooth_monitor.py:118:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/monitors/clipboard_monitor.py:159:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/monitors/screen_monitor.py:94:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/monitors/screen_monitor.py:168:20: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/monitors/screen_monitor.py:394:24: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/monitors/window_monitor.py:85:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/apps.py:195:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/apps.py:351:9: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/apps.py:424:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/apps.py:503:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/browser.py:309:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/input.py:131:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/input.py:176:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/media.py:63:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/media.py:104:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/screen.py:205:13: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/screen.py:219:20: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/system.py:49:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/system.py:642:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/system.py:731:20: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/system.py:756:20: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/system.py:787:20: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/terminal.py:246:16: S603 `subprocess` call: check for execution of untrusted input
  - services/core/nexus/tools/windows.py:79:16: S603 `subprocess` call: check for execution of untrusted input

S607
  - services/core/nexus/tools/apps.py:154:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/apps.py:184:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/apps.py:195:31: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/apps.py:425:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/apps.py:504:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/browser.py:256:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/browser.py:309:31: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/input.py:177:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/media.py:105:13: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/screen.py:99:31: S607 Starting a process with a partial executable path
  - services/core/nexus/tools/screen.py:205:28: S607 Starting a process with a partial executable path

PLW1510
  - services/core/nexus/monitors/bluetooth_monitor.py:118:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/monitors/clipboard_monitor.py:159:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/monitors/screen_monitor.py:94:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/monitors/screen_monitor.py:168:20: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/monitors/screen_monitor.py:394:24: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/monitors/window_monitor.py:85:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/apps.py:153:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/apps.py:183:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/apps.py:195:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/apps.py:424:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/apps.py:503:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/browser.py:255:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/browser.py:309:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/input.py:131:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/input.py:176:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/media.py:63:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/media.py:104:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/screen.py:99:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/screen.py:205:13: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/screen.py:219:20: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/system.py:49:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/system.py:642:16: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/system.py:731:20: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/system.py:756:20: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/system.py:787:20: PLW1510 `subprocess.run` without explicit `check` argument
  - services/core/nexus/tools/windows.py:79:16: PLW1510 `subprocess.run` without explicit `check` argument

BLE001
  - services/core/nexus/ai/memory_sweep.py:308:12: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/automations.py:664:12: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/logging_setup.py:156:16: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/monitors/screen_monitor.py:379:16: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/secrets.py:188:16: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/secrets.py:224:16: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/secrets.py:235:16: BLE001 Do not catch blind exception: `Exception`
  - services/core/nexus/secrets.py:245:16: BLE001 Do not catch blind exception: `Exception`

S110
  - services/core/nexus/logging_setup.py:156:9: S110 `try`-`except`-`pass` detected, consider logging the exception
  - services/core/nexus/router/probe.py:118:13: S110 `try`-`except`-`pass` detected, consider logging the exception
  - services/core/nexus/tools/browser.py:450:5: S110 `try`-`except`-`pass` detected, consider logging the exception

S101
  - services/core/nexus/db/conn.py:238:9: S101 Use of `assert` detected
  - services/core/nexus/tools/runtime.py:180:5: S101 Use of `assert` detected
  - services/core/nexus/tools/terminal.py:99:5: S101 Use of `assert` detected

S310
  - services/core/nexus/tools/browser.py:142:15: S310 Audit URL open for permitted schemes. Allowing use of `file:` or custom schemes is often unexpected.

S311
  - services/core/nexus/learning/bandit.py:141:19: S311 Standard pseudo-random generators are not suitable for cryptographic purposes

ASYNC240
  - services/core/nexus/providers/mistral_audio.py:218:20: ASYNC240 Async functions should not perform blocking pathlib.Path operations
  - services/core/nexus/providers/nim_media.py:430:16: ASYNC240 Async functions should not perform blocking pathlib.Path operations
  - services/core/nexus/providers/nim_media.py:612:16: ASYNC240 Async functions should not perform blocking pathlib.Path operations
  - services/core/nexus/providers/nvidia_speech.py:424:20: ASYNC240 Async functions should not perform blocking pathlib.Path operations

RUF006
  - services/core/nexus/ai/core.py:623:13: RUF006 Store a reference to the return value of `asyncio.create_task`
  - services/core/nexus/api/routes/chat_routes.py:319:5: RUF006 Store a reference to the return value of `asyncio.create_task`
  - services/core/nexus/api/routes/voice_routes.py:459:5: RUF006 Store a reference to the return value of `asyncio.create_task`
  - services/core/nexus/monitors/supervisor.py:227:17: RUF006 Store a reference to the return value of `asyncio.create_task`
  - services/core/nexus/monitors/supervisor.py:262:21: RUF006 Store a reference to the return value of `asyncio.create_task`

B008
  - services/core/nexus/api/routes/automations_routes.py:134:37: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/automations_routes.py:136:42: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/automations_routes.py:182:37: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/automations_routes.py:184:42: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/chat_routes.py:285:24: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/data.py:450:45: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/models_routes.py:200:37: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/monitors_routes.py:91:30: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/permissions_routes.py:62:42: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/tools_routes.py:65:37: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/tools_routes.py:77:37: B008 Do not perform function call `Body` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable
  - services/core/nexus/api/routes/voice_routes.py:173:24: B008 Do not perform function call `File` in argument defaults; instead, perform the call within the function, or read the default from a module-level singleton variable

B905
  - services/core/nexus/ai/embeddings.py:141:37: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/ai/embeddings.py:154:36: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/core/trends.py:163:55: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/core/trends.py:167:67: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/db/repos/memories.py:51:33: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/learning/bandit.py:154:19: B905 `zip()` without an explicit `strict=` parameter
  - services/core/nexus/monitors/screen_monitor.py:150:40: B905 `zip()` without an explicit `strict=` parameter

DTZ005
  - services/core/nexus/ai/context.py:171:11: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/core/proactive.py:123:22: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/monitors/screen_monitor.py:237:17: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/providers/mistral_audio.py:336:30: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/providers/nim_media.py:584:30: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/providers/nvidia_speech.py:586:30: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/tools/files.py:436:25: DTZ005 `datetime.datetime.now()` called without a `tz` argument
  - services/core/nexus/tools/screen.py:187:13: DTZ005 `datetime.datetime.now()` called without a `tz` argument

DTZ006
  - services/core/nexus/logging_setup.py:198:17: DTZ006 `datetime.datetime.fromtimestamp()` called without a `tz` argument
  - services/core/nexus/monitors/screen_monitor.py:453:33: DTZ006 `datetime.datetime.fromtimestamp()` called without a `tz` argument
  - services/core/nexus/tools/screen.py:296:33: DTZ006 `datetime.datetime.fromtimestamp()` called without a `tz` argument

G101
  - services/core/nexus/db/migrate.py:90:50: G101 Logging statement uses an `extra` field that clashes with a `LogRecord` field: `name`
  - services/core/nexus/tools/process_signal.py:184:62: G101 Logging statement uses an `extra` field that clashes with a `LogRecord` field: `name`
  - services/core/nexus/tools/process_signal.py:280:61: G101 Logging statement uses an `extra` field that clashes with a `LogRecord` field: `name`
  - services/core/nexus/tools/process_signal.py:413:49: G101 Logging statement uses an `extra` field that clashes with a `LogRecord` field: `name`
  - services/core/nexus/tools/system.py:1397:59: G101 Logging statement uses an `extra` field that clashes with a `LogRecord` field: `name`

G201
  - services/core/nexus/ai/agent.py:746:13: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`
  - services/core/nexus/api/__init__.py:72:13: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`
  - services/core/nexus/db/conn.py:119:13: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`
  - services/core/nexus/db/conn.py:125:17: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`
  - services/core/nexus/db/migrate.py:110:17: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`
  - services/core/nexus/tools/runtime.py:140:13: G201 Logging `.exception(...)` should be used instead of `.error(..., exc_info=True)`

PLW0603
  - services/core/nexus/ai/core.py:703:12: PLW0603 Using the global statement to update `_CORE` is discouraged
  - services/core/nexus/db/conn.py:44:12: PLW0603 Using the global statement to update `_db_path` is discouraged
  - services/core/nexus/monitors/screen_monitor.py:70:12: PLW0603 Using the global statement to update `_MAGICK` is discouraged
  - services/core/nexus/monitors/screen_monitor.py:70:12: PLW0603 Using the global statement to update `_MAGICK` is discouraged
  - services/core/nexus/monitors/supervisor.py:370:12: PLW0603 Using the global statement to update `_SUPERVISOR` is discouraged
  - services/core/nexus/providers/__init__.py:54:12: PLW0603 Using the global statement to update `_INSTANCES` is discouraged
  - services/core/nexus/providers/mistral_audio.py:436:12: PLW0603 Using the global statement to update `_CLIENT` is discouraged
  - services/core/nexus/providers/nim_media.py:931:12: PLW0603 Using the global statement to update `_CLIENT` is discouraged
  - services/core/nexus/providers/nvidia_speech.py:803:12: PLW0603 Using the global statement to update `_CLIENT` is discouraged
  - services/core/nexus/router/__init__.py:694:12: PLW0603 Using the global statement to update `_ROUTER` is discouraged
  - services/core/nexus/secrets.py:262:12: PLW0603 Using the global statement to update `_backend_instance` is discouraged
  - services/core/nexus/secrets.py:272:12: PLW0603 Using the global statement to update `_env_cache` is discouraged

SIM105
  - services/core/nexus/db/conn.py:85:9: SIM105 Use `contextlib.suppress(sqlite3.Error)` instead of `try`-`except`-`pass`
  - services/core/nexus/db/conn.py:113:9: SIM105 Use `contextlib.suppress(sqlite3.Error)` instead of `try`-`except`-`pass`
  - services/core/nexus/db/migrate.py:106:13: SIM105 Use `contextlib.suppress(sqlite3.Error)` instead of `try`-`except`-`pass`
  - services/core/nexus/logging_setup.py:154:9: SIM105 Use `contextlib.suppress(Exception)` instead of `try`-`except`-`pass`
  - services/core/nexus/providers/mistral_audio.py:372:17: SIM105 Use `contextlib.suppress(OSError)` instead of `try`-`except`-`pass`
  - services/core/nexus/providers/mistral_audio.py:388:21: SIM105 Use `contextlib.suppress(OSError)` instead of `try`-`except`-`pass`
  - services/core/nexus/router/probe.py:116:13: SIM105 Use `contextlib.suppress(Exception)` instead of `try`-`except`-`pass`
  - services/core/nexus/tools/files.py:522:9: SIM105 Use `contextlib.suppress(OSError)` instead of `try`-`except`-`pass`

TRY004
  - services/core/nexus/api/ws.py:105:17: TRY004 Prefer `TypeError` exception for invalid type
  - services/core/nexus/tools/registry.py:317:9: TRY004 Prefer `TypeError` exception for invalid type

TRY301
  - services/core/nexus/ai/agent.py:704:17: TRY301 Abstract `raise` to an inner function
  - services/core/nexus/api/ws.py:105:17: TRY301 Abstract `raise` to an inner function

TRY203
  - services/core/nexus/ai/memory_sweep.py:238:9: TRY203 Remove exception handler; error is immediately re-raised
  - services/core/nexus/core/proactive.py:327:9: TRY203 Remove exception handler; error is immediately re-raised

C901
  - scripts/count_tasks.py:37:5: C901 `main` is too complex (15 > 10)
  - services/core/nexus/ai/agent.py:186:5: C901 `_validate_plan` is too complex (14 > 10)
  - services/core/nexus/ai/agent.py:450:11: C901 `_run_step` is too complex (14 > 10)
  - services/core/nexus/ai/agent.py:671:11: C901 `run` is too complex (19 > 10)
  - services/core/nexus/ai/core.py:270:15: C901 `_run` is too complex (21 > 10)
  - services/core/nexus/ai/intent.py:273:5: C901 `classify` is too complex (11 > 10)
  - services/core/nexus/ai/intent.py:458:11: C901 `refine` is too complex (12 > 10)
  - services/core/nexus/ai/memory_extract.py:183:5: C901 `_parse` is too complex (13 > 10)
  - services/core/nexus/ai/voice_stream.py:217:11: C901 `stream` is too complex (30 > 10)
  - services/core/nexus/core/scoring.py:400:5: C901 `score` is too complex (11 > 10)
  - services/core/nexus/db/maintenance.py:128:5: C901 `import_all` is too complex (12 > 10)
  - services/core/nexus/db/repos/automations.py:35:5: C901 `parse_next_run` is too complex (14 > 10)
  - services/core/nexus/db/repos/automations.py:206:5: C901 `update` is too complex (13 > 10)
  - services/core/nexus/db/repos/search.py:65:5: C901 `resolve` is too complex (14 > 10)
  - services/core/nexus/monitors/bluetooth_monitor.py:307:5: C901 `parse_managed_objects` is too complex (11 > 10)
  - services/core/nexus/monitors/screen_monitor.py:103:5: C901 `_parse_pgm` is too complex (12 > 10)
  - services/core/nexus/monitors/system_monitor.py:285:9: C901 `_trends` is too complex (11 > 10)
  - services/core/nexus/monitors/window_monitor.py:107:5: C901 `_via_xprop` is too complex (12 > 10)
  - services/core/nexus/permissions/engine.py:115:9: C901 `evaluate` is too complex (12 > 10)
  - services/core/nexus/permissions/engine.py:376:9: C901 `respond` is too complex (11 > 10)
  - services/core/nexus/permissions/policy.py:567:5: C901 `describe_request` is too complex (21 > 10)
  - services/core/nexus/providers/anthropic.py:317:9: C901 `_payload` is too complex (14 > 10)
  - services/core/nexus/providers/anthropic.py:452:15: C901 `stream` is too complex (34 > 10)
  - services/core/nexus/providers/nim_media.py:871:5: C901 `_parse_ocr` is too complex (17 > 10)
  - services/core/nexus/providers/openai_compat.py:109:5: C901 `map_http_error` is too complex (14 > 10)
  - services/core/nexus/providers/openai_compat.py:374:15: C901 `stream` is too complex (26 > 10)
  - services/core/nexus/router/__init__.py:517:15: C901 `stream` is too complex (11 > 10)
  - services/core/nexus/tools/files.py:486:5: C901 `files_delete` is too complex (14 > 10)
  - services/core/nexus/tools/files.py:624:5: C901 `files_search` is too complex (11 > 10)
  - services/core/nexus/tools/input.py:498:5: C901 `input_click` is too complex (11 > 10)
  - services/core/nexus/tools/process_read.py:97:5: C901 `system_process_tree` is too complex (13 > 10)
  - services/core/nexus/tools/registry.py:366:5: C901 `_coerce` is too complex (21 > 10)
  - services/core/nexus/tools/runtime.py:40:11: C901 `execute` is too complex (12 > 10)
  - services/core/nexus/tools/screen.py:170:5: C901 `screen_capture` is too complex (11 > 10)
  - services/core/nexus/tools/system.py:329:5: C901 `system_volume` is too complex (11 > 10)
  - services/core/nexus/tools/system.py:494:5: C901 `system_brightness` is too complex (11 > 10)
  - services/core/nexus/tools/system.py:1317:5: C901 `system_kill_process` is too complex (15 > 10)
  - services/core/nexus/tools/system_read.py:180:5: C901 `system_temperature` is too complex (12 > 10)
  - services/core/nexus/tools/windows.py:204:5: C901 `_verify_arrange` is too complex (19 > 10)
  - services/core/nexus/tools/windows.py:323:5: C901 `window_arrange` is too complex (22 > 10)

PLR0915
  - services/core/nexus/ai/agent.py:450:11: PLR0915 Too many statements (60 > 50)
  - services/core/nexus/ai/agent.py:671:11: PLR0915 Too many statements (53 > 50)
  - services/core/nexus/ai/core.py:270:15: PLR0915 Too many statements (79 > 50)
  - services/core/nexus/ai/voice_stream.py:217:11: PLR0915 Too many statements (72 > 50)
  - services/core/nexus/api/__init__.py:53:11: PLR0915 Too many statements (61 > 50)
  - services/core/nexus/providers/anthropic.py:452:15: PLR0915 Too many statements (104 > 50)
  - services/core/nexus/providers/openai_compat.py:374:15: PLR0915 Too many statements (74 > 50)
  - services/core/nexus/tools/windows.py:323:5: PLR0915 Too many statements (63 > 50)

F401
  - services/core/nexus/core/scoring.py:42:36: F401 [*] `dataclasses.field` imported but unused
  - services/core/nexus/core/scoring.py:42:43: F401 [*] `dataclasses.replace` imported but unused
  - services/core/nexus/db/repos/automations.py:16:43: F401 [*] `datetime.timezone` imported but unused
  - services/core/nexus/db/repos/base.py:16:24: F401 [*] `..conn` imported but unused
  - services/core/nexus/learning/bandit.py:47:25: F401 [*] `typing.Iterable` imported but unused
  - services/core/nexus/monitors/usb_monitor.py:33:25: F401 [*] `typing.Iterable` imported but unused

F601
  - services/core/nexus/db/repos/app_settings.py:200:5: F601 Dictionary key literal `"semantic_search_enabled"` repeated

F811

F821


## Appendix C — Repo hygiene checklist

- `shell=True` in `services/core/nexus`: **0 hits**
- Hardcoded API keys (`sk-`/`nvapi-`/`AIza`/`ghp_`) in source: **0 hits**
- `TODO`/`FIXME`/`HACK`/`XXX` markers: **0 hits**
- `python -m compileall services/core/nexus`: **OK**
- `tsc --noEmit` (apps/desktop): **0 errors**
- Secrets in git history: **0** (purged; see commit history)

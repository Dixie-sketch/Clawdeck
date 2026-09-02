# crabd correctness + concurrency audit (QA lane 1)

Scope: `companion/crabd.py` in full, measured against `docs/STATE-CONTRACT.md`.
Read-only pass. Reproductions were run by importing the module directly (classes driven
in-process, no server bound, production port 2722 never touched). Test suite
(`test_crabd.py` 6429 ln, `test_crabd_livefire.py`, `test_crabd_datalane.py`) read as a
coverage map — mutation-proven areas were not re-audited and are listed under SOUND.

Ranked most-severe first. Severity is about correctness/availability of a localhost
companion (not remote reach — the trust posture is lane 5's).

---

*Status 2026-08-27, v0.16.0/v0.17.0 waves: ALL fixed — F1 (FORECAST_MAX_KEYS LRU) + F2 (locks)
in 0.16.0; F3 (release() authoritative under lock), F4 (OTLP series LRU 512), F5 (peek→send→drain),
F6 (null exhaustAt on unparseable resetsAt), F7 (create=True) in 0.17.0. Each mutation-proven.*

## F1 — Forecaster history keyspace is never pruned by key → unbounded growth (MEDIUM, VERIFIED)

**Path:** `DepletionForecaster._observe` (crabd.py:2931-2945), fed every build via
`StateBuilder._limits_block` → `_forecaster.annotate` (:3154, :3162) → `_annotated`
(:2906) → `_forecast` → `_observe`. Keys are `"fiveHour"`, `"weekly"`, and
`"extra:" + str(w.get("label"))` for every window in `limits.extra`
(`annotate`, :2898-2904).

**Defect:** `_observe` prunes SAMPLES within a key (by `FORECAST_WINDOW_SEC` and
`FORECAST_MAX_SAMPLES`) but nothing ever removes a KEY from `self._history`. Every
distinct extra label ever seen leaves a permanent entry. Worse, a key that stops
reappearing is never re-observed, so even its stale sample list is never pruned — it
sits at its last value forever.

**Trigger / wrong result:** the extra labels are attacker-influenced through the
UNAUTHENTICATED `POST /v1/statusline`. `StatusLineReader._map_limits` (:2078-2088) turns
every `seven_day_*` key in a posted `rate_limits` object into an extra whose label is
`key[len("seven_day_"):] + " weekly"`. One crafted document (bounded only by the 256 KB
`STATUSLINE_MAX_BODY`) yields thousands of distinct labels; the builder's next
`annotate` mints one permanent `DepletionForecaster._history` key per label. Sustained
POSTs with fresh random `seven_day_<n>` keys grow the dict without bound → memory
exhaustion of a daemon meant to run for weeks. A non-malicious slow drip also exists via
`LimitsReader.map_payload`'s model-scoped-weekly labels and the degraded-shape fallback
(:1981-1985) that labels every top-level key.

**Reproduced (in-process):**
- `annotate` with 5000 distinct extra labels → `len(_history) == 5002` (fiveHour +
  weekly + 5000), none evicted.
- One crafted `StatusLineReader.ingest({'rate_limits': {five_hour, seven_day_0..1999}})`
  → `limits()` serves 2000 extras → a single `annotate` creates 2001 forecaster keys.
- A key annotated once and never again retains its sample list indefinitely (no GC).

**Note it is memory only:** it grows the in-memory forecaster, not served state, and the
served `exhaustAt` for each junk window is still null/harmless. The failure is
availability (OOM over time), not a wrong number.

---

## F2 — Cold-start concurrent `build()` races unlocked shared dicts (MEDIUM→LOW, VERIFIED by inspection)

**Path:** `Handler.do_GET` answers `/v1/state` by calling `self.builder.build()` directly
whenever `self.builder.state is None` (crabd.py:3473-3476). That is the startup window:
`_refresh_loop` (:4086) is also running `build()` and has not yet stored the first
`_state`. The cold transcript scan is ~1.1 s (the code's own measured figure, :162), and
the widget polls `/v1/state` every 3 s from the instant crabd binds — so a poll landing in
that first cold build is ordinary, not exotic.

**Defect:** two `build()` calls then run concurrently over state that has NO lock:
- `TranscriptStore` (:1418) — `scan()` inserts `self.files[key] = facts` and deletes keys
  (:1443-1447) while `build()`'s `for facts in self.store.files.values()` (:3054)
  iterates the same dict. Concurrent insert during another thread's `.values()` iteration
  is a `RuntimeError: dictionary changed size during iteration`.
- `GitLookup` (:1060) — `get()` mutates `self._cache[cwd] = (...)` (:1075) with no lock.

Confirmed by inspection: `TranscriptStore` and `GitLookup` have no `_lock`; `do_GET`
holds none; the builder's own `_lock` guards only the `_state` swap, not `build()`'s body.
(Steady state does not race — after warm-up `scan()` stops mutating `files`, and once
`_refresh_loop` sets `_state` the request path never calls `build()` again — which is why
this is a cold-start / file-set-change window, not a permanent hazard. A stress harness did
not force the `RuntimeError` here; CPython's GIL makes the mutate-vs-iterate interleaving
tight, so the crash is probabilistic while the unsynchronized access is certain.)

**Wrong result / bound:** at worst one `/v1/state` poll returns 500 (the request thread's
`build()` raises; `_refresh_loop` catches its own and retries in 2 s). The widget renders
that as a failed poll → stale/worried crab, which is the contract's correct degradation.
No persistent corruption: the request build's result is discarded, and `store.files`
self-heals on the next scan. Still a real unsynchronized-shared-state defect (brief item 3)
and trivially removed by serving a "warming up" state until `_state` is set, or by locking
`build()`.

---

## F3 — `decide()` in the timeout/release gap records a phantom approval (LOW, INFERRED)

**Path:** `PermissionBroker.wait` returns `entry["decision"]` after `event.wait(timeout)`
(:2789-2790); on timeout `_await_permission` (:3818-3821) reads `decision is None` and
calls `broker.release`. `decide()` (:2792-2808) can land in the window AFTER `wait`'s
55 s times out (decision still None, captured by the handler) but BEFORE `release` runs.

**Interleaving → wrong result:**
1. `wait` times out, returns `None`; handler is about to call `release`.
2. `decide(allow)` acquires the lock, finds the entry (decision still None), sets
   `decision="allow"`, deletes it from `_pending`, fires the event, returns the tool.
   `_do_decide` then writes the contract history line `"approved from panel: Bash"` and
   answers the widget **204**.
3. `release` runs: `self._pending.get(session_id) is entry` is now False (already
   deleted) → no-op.
4. The permission hook handler already captured `decision=None` → answers
   `HOOK_PASS_THROUGH`, so the **terminal dialog** actually owns the decision.

Net: history says "approved from panel", the widget got a 204 (tap accepted), but the tool
was NOT auto-approved — the operator re-answers in the terminal. **Safe** (it never
produces `behavior:allow` on the hook without a tap; the invariant holds), but the panel's
record and the reality disagree. Very narrow (tap within the sub-millisecond gap at the
55 s mark). Not covered by the timeout tests, which decide well before expiry.

---

## F4 — OTLP cumulative series keyspace unbounded within a day (LOW, VERIFIED by inspection)

**Path:** `OtlpReceiver._take_point` for `aggregationTemporality == CUMULATIVE` keys
`self._cumulative` by `(day, _series_key(point))` (:2282-2286); `_series_key` (:2313-2316)
is the point's sorted attribute set. `prune()` (:2429-2436) only drops days outside
today/yesterday.

**Trigger:** unauthenticated `POST /v1/metrics` with `claude_code.cost.usage` points each
carrying a distinct attribute set → distinct series keys → `_cumulative` grows without
bound WITHIN the current day (never pruned until the day rolls). Same class as F1 but
day-bounded, so lower. Cost served stays correct (it sums the day's series); the cost is
memory.

---

## F5 — Stop-hook drain is destructive before the answer is confirmed (LOW, INFERRED)

**Path:** `_drain_stop` (:3738-3755) calls `queue.drain(session_id)` which **pops** the
entry, THEN `_do_hook_stop` (:3729-3736) sends the 200 carrying the prompt. If the send
fails — CLI's ≤2 s HTTP-hook client already timed out and closed, or the socket reset
mid-write — the prompt has been removed from the queue but never delivered, and it is not
re-queued. The queued continue is silently lost (not double-delivered). Loss, not
corruption; client disconnect on loopback is rare. A peek-then-drain-on-confirmed-send
would close it, at the cost of a redelivery risk the current order deliberately avoids.

---

## F6 — Far-future `exhaustAt` when a window has no parseable `resetsAt` (LOW/INFO, INFERRED)

**Path:** `DepletionForecaster._forecast` (:2926-2929) caps the projection with
`if resets is not None and projected >= resets: return None`. When `resetsAt` is
null/unparseable (`_window` sets it None on absent/garbage reset time), the cap is skipped.
A tiny positive slope (a genuine 1e-4 utilization step over ~900 s) then projects days-to-
year-3000 out; `_utc_iso` clamps to the year-3000 ceiling and serves it. The contract says
`exhaustAt` is "never extrapolated past the window's own resetsAt" — silently not enforced
when there is no resetsAt. Mitigated by the widget's "~" hedge and "sooner than reset"
render gate (which itself needs a reset to compare). A real window always carries a reset,
so this is an edge/robustness note.

---

## F7 — Permission timeout history line uses `create=False` (INFO, INFERRED)

`_await_permission`'s timeout branch calls `self.builder.hooks.note_external(session_id,
"permission passed through: …")` with the DEFAULT `create=False` (:3820-3821), whereas
`_do_decide` uses `create=True` (:3916). A session that aged out of the served set during
the 55 s hold loses its "passed through" timeline entry, so the operator cannot always
distinguish "I did not tap in time" from "the panel never saw it" — the exact distinction
`PERMISSION_EVENT_TIMEOUT` exists to preserve (:418-420). Cosmetic (history only).

---

## Audited and found SOUND (coverage, not just defects)

- **Permission broker never auto-allows.** No path from timeout, saturation, disabled
  config, malformed body, or exception yields `behavior:allow`; only `decide(...,"allow")`
  does, and its sole caller is `/v1/action` answering a tap. Two decides for one request
  cannot both apply (second finds `decision is not None` or the entry gone, :2803).
  Replace-releases-old as pass-through (:2778-2780); `release` is identity-checked so it
  cannot un-register a successor (:2810-2816). Bounded twice (55 s, 8 pending) and holds no
  lock while waiting. `serving()` gate blocks ghost sessions from parking threads. F3 is the
  only crack and it stays safe.
- **Continue queue.** Newest-wins (overwrite, :2691-2693); drain is an atomic pop under
  lock so two Stop hooks for one session deliver at most once (:2702-2710); TTL re-derived
  from stored `at` in `peek`/`drain`/`entry` rather than trusting the sweep (:2695-2724);
  per-session isolation. No path to double-delivery or stale survival. F5 is a loss edge,
  not a double-send.
- **`_read_body` / `MAX_BODY_BYTES` / chunked de-framing / `_drain` bound.** Cap applied
  while reading; lying/chunked/oversized/non-numeric Content-Length all answer and never
  block or over-allocate (livefire-proven).
- **`_parse_ts` / `_utc_iso` totality.** 1e30, NaN, ms-vs-s, bool, ISO-out-of-range,
  epoch bounds — all return None/clamp, never raise. Every untrusted timestamp
  (statusline `resets_at`, OAuth resets, transcript ts, history ts, OTLP `timeUnixNano`
  via `_point_time`'s own bound) routes through it.
- **OTLP delta vs cumulative arithmetic.** Delta summed, cumulative max-per-series then
  summed across series; negative/NaN/±inf costs rejected (:2277); per-export event cap
  (:2326) bounds one batch's push into a session ring.
- **HookTracker locking.** `record`/`snapshot`/`ack`/`note_external`/`replay`/`prune`/
  `note_titles`/`done_*` all mutate under `self._lock`; `snapshot` deep-copies `stops`/
  `events` so a served copy cannot mutate the tracker. `note_external` deliberately cannot
  move the state machine (telemetry can't resurrect a finished session).
- **LimitsReader.** Last-good survives restart via disk cache; `LIMITS_CACHE_MIN_EPOCH`
  rejects the 1970-poisoned file; 429 exponential backoff; token read/used/dropped, never
  logged or served; qualifying an aged serve does not mutate stored last-good.
- **Fire-and-forget.** `/v1/hook`, `/v1/statusline`, `/v1/metrics`, `/v1/logs` all send
  204 then parse under a bare `except` (:3633, :3677, :3707); `/v1/hook/stop` and
  `/v1/hook/permission` compute under `try` and fall to `HOOK_PASS_THROUGH` on any error,
  which equals a crabd-is-down answer. No exception after the send reaches the operator or
  hangs a hook.
- **/v1/config whitelist.** Exact key set (:3919), whole-body reject on any unknown key or
  any failed member validation before the single write; `set_keys` read-modify-writes
  preserving non-whitelisted keys (e.g. `allowReply`, `recapRepos` unreachable over HTTP);
  bool-before-int checks throughout.
- **Resource lifetime elsewhere.** `HookTracker.sessions/dones/_titles`,
  `TranscriptStore.files`, `StatusLineReader._sessions`, `OtlpReceiver._delta_by_day`,
  `ContinueQueue._queued`, `PermissionBroker._pending` are all pruned or bounded. The two
  unpruned growers are F1 (worst — no key GC at all) and F4 (day-bounded).

---

### Bottom line for the register
No concurrency defect can hang a hook or produce an unrequested `allow`, and none can
persistently corrupt served state — the build() race (F2) at worst fails a single poll and
self-heals. The material items are two unauthenticated-input memory growers (F1 medium, F4
low) and the cold-start data race (F2). Everything else is low/edge.

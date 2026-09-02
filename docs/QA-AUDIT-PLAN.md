# SideCrab full QA audit — plan (Joe-requested, 2026-08-27)

Launches only when the tree is quiescent (no feature lanes writing) AND the crabd suite is
proven deterministic. Fan-out of Opus 5 lanes by component ownership + cross-cutting concerns.
House rules: **auditors owe EVIDENCE, not prescriptions** (charter §3.4.1); every finding is
**adversarially verified** (an independent lane tries to REFUTE it) before it reaches the register;
findings are ranked most-severe first; the wave is CAPPED — fix the confirmed findings, ONE
recheck of that diff, ship. No recursive readiness reviews.

## Why now (the case, from tonight)
Seven feature lanes tonight each surfaced a real defect the others could not see: a use-after-free
misdiagnosed for the whole session, a false-passing test worse than a flake, a shipped card-clip, two
daemons silently running stale code, a snooze-vs-security judgment, a host networking issue that can
stall live sessions. A stack that dense with just-found bugs has not had an adversarial pass. It has
been sprinting. This is the pass.

## Lanes (find → structured findings; then a separate verify wave refutes each)

1. **crabd correctness + concurrency** — the permission broker (slot exhaustion, the 55s holds,
   decide/timeout races), continue queue, all readers' locking, the fire-and-forget answer-before-
   parse contract, resource lifetime (threads, sockets, file handles), every endpoint's failure
   behavior under malformed/oversized/absent input. Owns: companion/crabd.py.
2. **Widget correctness** — the state machine, gesture discrimination edge cases, persistence races
   (prefs object), presence-gating on EVERY v-field, and the one that matters most:
   **untrusted-string rendering.** Session titles and question text originate in hooks and are
   rendered — audit textContent vs innerHTML on every path; a title with markup must not execute.
3. **Notifier + protocol handlers** — toast XML built from titles/questions (injection via the
   base64/EncodedCommand path?), the ack/snooze handlers' URI validation, ledger integrity across
   concurrent writers, the WAL-mode verification trap.
4. **Setup / PowerShell** — settings.json merge edge cases, ShouldProcess coverage, the restore
   foreign-edit guard's completeness, injection surfaces in the chained statusline command.
5. **SECURITY, whole-trust-chain (the priority lane)** — the localhost API is UNAUTHENTICATED by
   design. That was fine for a read-only feed. It now: approves permission requests, injects prompts
   into live sessions (queue-continue), and toggles config. "Any local process on this box can
   decide/inject" is a materially different trust posture than wave 1 made the call under. Model the
   threat (a malicious local process, a browser SSRF'ing 127.0.0.1:2722, a CSRF from a page the
   widget's QtWebEngine loads), and evaluate: does /v1/action need an origin check / a token the
   widget carries / a same-machine attestation? Evidence + a risk rating, not a prescription.
6. **Contract conformance** — every producer/consumer measured against STATE-CONTRACT.md as written,
   BOTH directions; drift is a finding.

## Register + close-out
Findings → `docs/findings/QA-Audit-2026-08-27.md`, severity-ranked, each with a concrete failure
scenario (inputs → wrong output). Fix confirmed criticals/highs; log the rest to BACKLOG. ONE
adversarial recheck of the fix diff. `Repair-SideCrab.ps1` green + one clean crabd suite run =
done. Do not let the audit manufacture the next audit's input.

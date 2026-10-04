# A9 — BEHAVIOR & RESPONSE MASTER PROMPT vs the code

Doc: `docs/NEXUS — JARVIS - FRIDAY - EDITH BEHAVIOR & RESPONSE MASTER PROMPT.md`
(1474 lines, headers 0–48: identity, personality, truthfulness/no-fake, tool-use
discipline, permission behaviour, safe autonomy, communication style, model-routing
behaviour, multi-modal behaviour, device intelligence).

## What this doc is

A **behavioural** spec, not a build spec. Its sibling is the
BEHAVIOR/RESPONSE/SYSTEM-CONTROL doc (audited as A6); this one covers the
*personality and conduct* half — *how NEXUS should behave* while operating. It
adds almost no new buildable features; nearly every requirement here is either (a)
persona text, or (b) a discipline that the code already enforces structurally. So
the verdicts below are mostly "IMPLEMENTED as persona + verifier discipline", with
the same product gaps A6/A10 already surfaced.

## Section-by-section (grouped)

| Area | Doc claim | Verdict | Evidence |
|---|---|---|---|
| Identity (§0, §1) | "not a chatbot", JARVIS/FRIDAY/EDITH blend — calm, precise, restrained | IMPLE
MENTED | `ai/persona.py` builds the system prompt from the **live tool registry**, embedding the no-
fake contract and the tone directives. The "Do not imitate copyrighted dialogue" line is respected —
 persona sets tone, never quotes characters. |
| Truthfulness (§2–§6, "never fake") | never claim an unverified success; prefer "I can't verify X"
over simulating X | IMPLEMENTED (the codebase's core rule) | `ToolResult.verified` is tri-state; ver
ifiers return `verified=False` + reason rather than a lie (apps launch polls `wmctrl`; clipboard rea
ds back; screenshots validate the PNG header). The fakes sweep (B8) found zero canned success. |
| Tool-use discipline (§12) | "a tool returning without error ≠ goal achieved"; inspect→verify→repor
t | IMPLEMENTED | every non-idempotent tool carries a `verifier` callable; the agent loop checks `re
sult.verified` before marking a step done (`ai/agent.py`). |
| Permission behaviour (§13) | explain what/why/impact/choice; don't re-ask when a grant is already
held | IMPLEMENTED | `permissions/engine.py` + `shell/PermissionDialog.tsx` render the what/why/impa
ct/choice; `capabilities.py` `offered_grant_kinds` and the constrained-grant path stop repeat prompt
ing. |
| Safe autonomy (§14) | multi-step work within authorized bounds | IMPLEMENTED | `ai/agent.py` plan→
execute→verify→replan; permission re-asked per dangerous step; hard step ceiling. |
| Model-routing behaviour (§32) | pick cheapest adequate model; never lie about which model ran | IM
PLEMENTED | `ai/router/jobs.py` maps request kinds→jobs and **forbids silent substitution** (`jobs.p
y:140`); the UI shows the actual model chip (ModelsScreen). |
| Multi-modal behaviour (§33) | combine text/voice/image/screen; "what is wrong with this" uses curr
ent vision | PARTIAL | vision routing exists (`needs_vision` in `ai/intent.py`, screenshot→VLM verif
ication); but there is no automatic "attach current screenshot to a vague 'fix this'" path — the mod
el must call `screen.capture` explicitly. |
| Device intelligence (§34) | treat devices as controlled extensions | MISSING | no `device.*` tools
; `db/repos/devices.py` is uncalled dead code (A10 Finding 1). Same gap as A6. |
| Developer/power-user (§31) | expose logs, stack traces, routing, tool details | IMPLEMENTED | term
inal output stream, `activity` log, ToolsScreen (per-tool audit trail), ModelsScreen probes, `/api/d
iagnostics`. |
| Communication style (§s "Sir", concise, no AI-isms) | calm, operational, never corporate-cheerful;
 plan-then-act fragments | IMPLEMENTED (partial on tone) | persona enforces concision and the verifi
cation-first voice; "Sir" address is dropped by design (flagged in A1 doc-master-prompt §4). |
| Proactivity (§s) | speak unprompted only when worth it | IMPLEMENTED | `core/scoring.py` dispositi
ons gate when a monitor may speak (act/investigate/notify vs log/ignore); monitors only voice when d
isposition warrants. (Caveat: `score()` itself has zero production callers — see Finding 1 below.) |

## Findings carried over (not unique to this doc, already recorded)

1. **The proactive-scoring layer is not wired**: `core/scoring.py score()` has zero
   production callers — the disposition discipline above is written but not
   invoked end-to-end. This is the single most important cross-doc gap.
2. `{monitor.id}.stopped` event kind never matches the `monitor.stopped` scoring rule.
3. `devices.py` dead code; no device tools; no Devices screen.
4. No per-process inspection, so "why is my computer slow" (§25's worked example)
   cannot name the culprit process.
5. `terminal.cancel()` zero callers — a running agent command can't be user-stopped.

## Verdict

This doc is genuinely satisfied where it makes *behavioural* demands: the persona
is built from the live registry, the no-fake rule is a structural property (not
trust-me prose), the permission dialogue actually explains what/why/impact/choice,
and the model-routing "never lie about the model" rule is enforced by code. Its
novel claims that are *not* delivered — device intelligence, the automatic
vision-attach on vague requests — are the same gaps A6/A10 already found. Nothing
here is a new fake; nothing here is a new buildable feature. The one claim that
looks implemented but is not *invoked* (proactive scoring) is the finding that
matters.


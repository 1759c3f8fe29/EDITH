# A8 — ULTRA-INTELLIGENT OPERATING INTELLIGENCE PROMPT vs the code

Doc: `docs/NEXUS — ULTRA-INTELLIGENT JARVIS - FRIDAY - EDITH OPERATING INTELLIGENCE PROMPT.md`
(1645 lines, ~51 sections). It is an **operating-doctrine** doc: identity,
mission, operating principles, tool intelligence/selection, anticipation, proactive
intelligence, communication discipline. Most of it is conduct/persona (like A9).
Its distinctive *buildable* claims — anticipation, proactive intelligence, tool
selection — are the same three the A1/A7 audits already pinpointed.

## Section-by-section (grouped)

| Area | Doc claim | Verdict | Evidence |
|---|---|---|---|
| Identity / Mission (§0–§2) | a real-time operator, not a chatbot; perceive→understand→reason→plan→authorize→act→observe→verify→adapt→c
omplete | IMPLEMENTED | `ai/agent.py` is this loop; `persona.py` builds the non-chatbot identity from the live registry. |
| Operating principles (§3–§8) | infer intent, don't force the user to pick tools | IMPLEMENTED | `ai/intent.py` maps request kinds→task
s/jobs (8 request kinds); the user never sees a tool picker. |
| Memory & context (§9–§14) | remember user-approved info, 6+ memory types, recall by relevance | PARTIAL | `db/repos/memories.py` (cate
gories general/preference/project/person/task/file/device/goal — **8, not the doc's 6**); semantic recall via `ai/embeddings.py` (Mistra
l embed) + FTS; short-term = conversation store. **Procedural memory MISSING**; episodic/semantic are approximated by ambient + FTS + em
beddings, not first-class. |
| Model routing (§15–§17, "use the right model, never lie about it") | fast for simple, reasoning for planning, vision for images, codin
g for code | IMPLEMENTED | `ai/router/jobs.py` routes by job; `ai/intent.py` decides model need; **silent substitution forbidden** (`job
s.py:140`); UI shows the actual model chip. |
| **Anticipation** (§18) | propose next likely step when context strongly supports it ("deps missing — I can prepare them next") | **MIS
SING** | no prediction/trend/next-step inference anywhere; the agent reacts to the current step only. Same gap as A1 §37, A7 §21. |
| **Proactive intelligence** (§19) | surface important conditions unprompted (server stopped, app crashed, disk full, net down, permissi
on revoked, automation failed) | PARTIAL | `core/scoring.py` dispositions are built for exactly this (act/investigate/notify) and monito
rs do surface disk/net/battery; **but `score()` has zero production callers**, so the *unprompted* surfacing is not wired end-to-end. |
| Tool intelligence / selection (§20–§21) | choose tool in order: correctness→safety→reliability→speed; don't call every tool | IMPLEMEN
TED | registry + `requires` gating (unavailable tools withheld with reason, `registry.py:244-250`); per-job routing; no shotgun tool cal
ling in the agent loop. |
| Verification doctrine (§22–§28) | never claim success on "command sent"; verify meaningful outcome | IMPLEMENTED (strongest theme) | `
ToolResult.verified` tri-state; per-tool `verifier` callables (apps launch polls `wmctrl`/`pgrep`, clipboard read-back, screenshot PNG-h
eader check, lock via `LockedHint`). |
| Communication intelligence (§46–§49) | no ego, minimal/factual/evidence-based, "probably" forbidden where a check exists | IMPLEMENTED
 | persona; response discipline; `_NO_PLAYER`/"no players found" and "may have exited immediately" show the factual, non-defensive regis
ter. |
| Smart proactivity (§50) | recommend an obvious low-risk next step without pestering "would you like me to…?" | PARTIAL | the *capabili
ty* to recommend exists (agent could); there is no deliberate "offer next step" mechanism, and it is gated on the same unwired proactivi
ty layer. |
| Intent preservation (§51) | never lose the user's original goal across steps | IMPLEMENTED | `ai/agent.py` keeps the plan as data and
replans from the failure point (§22); task records retain goal/result/errors. |

## Genuine gaps (unique to, or sharpened by, this doc)

1. **Anticipation** (§18) — absent. This doc puts it first-class (JARVIS = "anticipation").
2. **Proactive surfacing not wired** (§19) — `score()`/disposition layer is built and tested but not invoked in the live loop (the share
d highest-priority finding).
3. **Procedural memory** (§9) — 8 category names exist, "learned workflow" does not.
4. **Learning from outcomes** — tasks store result/errors; nothing feeds back into plan quality (A1 §38, A7 §23–29).
5. **Smart-proactivity mechanism** (§50) — no explicit "offer the obvious step" path.

## Verdict

This doc is the most *behaviorally* disciplined of the family and is satisfied
wherever it demands conduct: no ego, evidence-based confidence, honest verification,
tool selection by safety-then-correctness, and a no-chatbot identity. Its two
named differentiators — **anticipation** and **proactive intelligence** — are the
parts that are *written but not running* (proactivity) or *not written at all*
(anticipation). No fakes: the doctrines that would tempt a fake (claim success
before verifying) are enforced, not merely asserted.

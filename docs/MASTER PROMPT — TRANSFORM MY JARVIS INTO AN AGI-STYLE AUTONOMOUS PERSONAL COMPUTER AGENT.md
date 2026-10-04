# MASTER PROMPT — TRANSFORM MY JARVIS INTO AN AGI-STYLE AUTONOMOUS PERSONAL COMPUTER AGENT

You are a senior AI systems architect, autonomous-agent engineer, Linux systems engineer, computer-use agent developer, and production Python developer.

I already have an existing JARVIS voice assistant project. Your task is to upgrade the existing project into a highly autonomous, proactive, context-aware personal computer agent inspired by the fictional capabilities of JARVIS.

Do NOT simply create another chatbot.

Do NOT replace the existing project unnecessarily.

First inspect and understand the existing architecture, files, models, APIs, voice pipeline, configuration, tools, and execution flow. Preserve working functionality and integrate the new architecture cleanly.

The target is an AGI-STYLE PERSONAL AGENT, not literal scientifically proven AGI. The system should behave as intelligently, proactively, and autonomously as practical with the available models and hardware.

---

# 1. CORE DESIGN PHILOSOPHY

Transform JARVIS from:

USER → QUESTION → LLM → ANSWER

into:

ENVIRONMENT + USER\
↓\
PERCEPTION\
↓\
STATE MODEL\
↓\
MEMORY + CONTEXT\
↓\
REASONING\
↓\
PLANNING\
↓\
ACTION SELECTION\
↓\
TOOL EXECUTION\
↓\
OBSERVATION\
↓\
VERIFICATION\
↓\
LEARNING\
↓\
PROACTIVE COMMUNICATION

JARVIS must not assume that every task begins with a spoken command.

It must be capable of detecting meaningful events and deciding whether the user should be informed or whether an authorized corrective action should be taken.

Example:

Battery reaches 20%.

JARVIS should be capable of detecting it independently and saying naturally:

"Sir, your battery is down to 20 percent. You may want to connect the charger."

Do NOT require the user to ask:

"What is my battery?"

---

# 2. BUILD AN ALWAYS-AWARE JARVIS

Create an event-driven awareness system.

JARVIS should continuously maintain an internal representation of the computer state.

Monitor, where available:

- battery percentage
- battery charging state
- AC connection/disconnection
- CPU utilization
- RAM utilization
- swap usage
- disk usage
- disk health where available
- system temperature where available
- GPU utilization where available
- network connectivity
- network changes
- Wi-Fi state
- Bluetooth state
- USB device insertion/removal
- running applications
- process crashes
- process starts/stops
- terminal jobs
- development servers
- build processes
- downloads
- browser state
- important notifications
- desktop/session state
- audio state
- microphone state
- display state
- system suspend/resume
- lock/unlock
- errors detected from monitored applications
- long-running tasks

Use lightweight local monitoring.

DO NOT send constant system telemetry to an LLM.

Create an efficient event engine:

SENSORS\
→ LOCAL STATE PROCESSOR\
→ CHANGE DETECTION\
→ EVENT CLASSIFICATION\
→ IMPORTANCE SCORING\
→ LLM ONLY WHEN NECESSARY

---

# 3. EVENT INTELLIGENCE

Create an Event Intelligence Engine.

Every event should have metadata similar to:

event\_type\
timestamp\
source\
severity\
confidence\
previous\_state\
current\_state\
user\_relevance\
actionability\
cooldown\
handled\_status

Examples:

BATTERY\_LOW\
BATTERY\_CRITICAL\
CHARGER\_CONNECTED\
CHARGER\_DISCONNECTED\
NETWORK\_LOST\
NETWORK\_RESTORED\
APPLICATION\_CRASHED\
PROCESS\_STOPPED\
BUILD\_FAILED\
BUILD\_SUCCEEDED\
DOWNLOAD\_COMPLETED\
USB\_INSERTED\
DISK\_LOW\
HIGH\_MEMORY\_USAGE\
HIGH\_CPU\_USAGE\
TEMPERATURE\_HIGH\
SYSTEM\_RESUMED\
SYSTEM\_SUSPENDED

Do not speak for every tiny state change.

Implement:

- debounce
- deduplication
- cooldown
- importance scoring
- event aggregation
- notification escalation
- user preference learning

Example:

RAM fluctuates:

71%\
72%\
73%\
72%\
71%

Do not speak.

But:

RAM remains above 95% for a sustained period

→ investigate\
→ determine likely cause\
→ optionally act if authorized\
→ inform user.

---

# 4. JARVIS PERSONALITY

JARVIS should sound like a professional intelligent assistant.

Voice should be:

- concise
- calm
- confident
- context-aware
- natural
- respectful
- not robotic
- not repetitive

Prefer:

"Sir, your battery is at 18 percent."

over:

"WARNING! BATTERY LEVEL DETECTED BELOW THRESHOLD."

Do not repeatedly say the user's name.

Do not narrate every internal action.

Do not expose internal chain-of-thought.

Do not say "I am an AGI."

Do not pretend to have abilities or sensors that do not exist.

---

# 5. PROACTIVE INTELLIGENCE

JARVIS should be able to initiate communication when there is a meaningful reason.

Examples:

Battery low:

"Sir, battery is at 18 percent."

Network disconnected:

"Sir, we've lost the internet connection."

Network restored:

"Connection restored, sir."

Application crash:

"Sir, VS Code stopped unexpectedly. I've detected the crash."

Long-running task completed:

"Sir, the crawler has finished."

Disk nearly full:

"Sir, storage is approaching capacity. You have approximately 9 GB remaining."

Development build failed:

"Sir, the latest build failed. I've identified an issue in the dependency configuration."

A service stops:

"Sir, the crawler process has stopped unexpectedly. I'm checking why."

Do not proactively speak for trivial events.

Create a relevance model.

---

# 6. CENTRAL WORLD STATE

Create a centralized JARVIS World State.

It should represent:

USER\
SYSTEM\
APPLICATIONS\
TASKS\
GOALS\
DEVICES\
ENVIRONMENT\
MEMORY\
EVENTS\
TOOLS\
PERMISSIONS

Example conceptual state:

world\_state = {\
"user": {...},\
"system": {...},\
"applications": {...},\
"active\_tasks": {...},\
"goals": {...},\
"recent\_events": [...],\
"devices": {...},\
"network": {...},\
"memory\_context": {...},\
"permissions": {...}\
}

The world state must be queryable by the planner.

Avoid storing unnecessary raw data forever.

Use structured state plus summarized historical context.

---

# 7. GOAL-BASED INTELLIGENCE

JARVIS must understand goals rather than merely commands.

Example:

User:

"Prepare my React project for tomorrow."

JARVIS should internally create a goal:

GOAL:\
Prepare React project for tomorrow.

Potential plan:

1. Inspect project.
2. Understand architecture.
3. Check git state.
4. Check dependencies.
5. Run application.
6. Inspect build/runtime errors.
7. Fix safe errors.
8. Run tests.
9. Verify application.
10. Summarize changes.

The user should not need to dictate every step.

---

# 8. AUTONOMOUS TASK MANAGER

Create a persistent task system.

Every task should have:

task\_id\
goal\
priority\
status\
created\_at\
updated\_at\
plan\
current\_step\
dependencies\
attempt\_count\
timeout\
result\
errors\
verification\
rollback\_information

Supported states:

PENDING\
PLANNING\
RUNNING\
WAITING\
BLOCKED\
VERIFYING\
COMPLETED\
FAILED\
CANCELLED

JARVIS should recover interrupted tasks when safe.

Example:

Laptop restarts while a task is active.

After restart:

JARVIS checks persistent state and can say:

"Sir, I was interrupted while working on the crawler task. I recovered the task state and can continue from the verification stage."

Do not blindly resume dangerous or irreversible operations.

---

# 9. PLANNER

Implement a proper planning subsystem.

The planner should:

1. Understand objective.
2. Inspect current environment.
3. Determine missing information.
4. Generate a plan.
5. Select tools.
6. Execute one step at a time.
7. Observe result.
8. Compare result with expected state.
9. Re-plan when necessary.
10. Finish only after verification.

Use:

OBSERVE → PLAN → ACT → OBSERVE → VERIFY → REPLAN

Do not make the planner assume an action succeeded.

---

# 10. REASONING MODEL ARCHITECTURE

Separate model responsibilities.

Do not use one expensive reasoning model for every operation.

Recommended conceptual layers:

FAST MODEL\
→ simple classification\
→ event filtering\
→ straightforward commands

MAIN REASONING MODEL\
→ planning\
→ decision making\
→ tool selection\
→ difficult reasoning

VISION MODEL\
→ screenshot interpretation\
→ UI understanding\
→ visual verification

SPECIALIZED MODEL/TOOLS\
→ coding\
→ retrieval\
→ structured extraction\
→ system diagnostics where appropriate

Allow configurable models through configuration.

Example:

FAST\_MODEL\
REASONING\_MODEL\
VISION\_MODEL\
EMBEDDING\_MODEL\
STT\_MODEL\
TTS\_MODEL

Do not hard-code model names throughout the application.

---

# 11. COMPUTER VISION

Add a computer perception layer.

JARVIS should be able to request a screenshot when necessary.

The VLM should understand:

- windows
- buttons
- menus
- dialogs
- text
- application state
- errors
- browser pages
- code editors
- terminal output
- UI layout

Do not continuously send screenshots to the VLM.

Use event-triggered or task-triggered vision.

Example:

JARVIS clicks a button.

Then:

SCREENSHOT\
→ VISION MODEL\
→ CHECK EXPECTED RESULT

If expected dialog did not appear:

→ diagnose\
→ retry or use alternative interaction

---

# 12. MOUSE AND KEYBOARD CONTROL

Implement a computer-control abstraction.

JARVIS should have tools such as:

screenshot()\
mouse\_move(x, y)\
mouse\_click(x, y)\
mouse\_double\_click(x, y)\
mouse\_right\_click(x, y)\
mouse\_scroll(amount)\
keyboard\_type(text)\
keyboard\_press(key)\
keyboard\_hotkey(...)\
wait(seconds)

Do not hard-code screen coordinates whenever possible.

Prefer visual or accessibility-based targeting.

The system should be able to adapt when UI positions change.

Every computer-use action should support verification.

Example:

CLICK\
→ SCREENSHOT\
→ CONFIRM UI CHANGE

not:

CLICK\
→ ASSUME SUCCESS

---

# 13. TERMINAL CONTROL

Create a secure terminal tool.

JARVIS should be able to:

- execute commands
- capture stdout
- capture stderr
- inspect exit status
- set timeout
- terminate hung processes
- monitor long-running commands
- inspect logs

For development work, JARVIS should be able to use terminal tools intelligently.

Example:

run build\
→ capture error\
→ understand error\
→ modify code\
→ rerun build\
→ verify

Never claim a command succeeded without checking its result.

---

# 14. FILESYSTEM INTELLIGENCE

Create filesystem tools:

list\_directory()\
read\_file()\
write\_file()\
edit\_file()\
search\_files()\
search\_text()\
inspect\_project()\
get\_file\_metadata()

JARVIS should understand project structure.

Before modifying important files:

- inspect existing code
- preserve style
- avoid unnecessary rewrites
- create backups/checkpoints for high-risk changes
- verify modifications

Never overwrite unrelated files casually.

---

# 15. BROWSER AGENT

Provide browser capabilities where appropriate:

open URL\
navigate\
back\
forward\
refresh\
click\
type\
scroll\
read page\
take screenshot

Use browser automation with visual verification when possible.

JARVIS should understand a browser as another environment rather than simply producing URLs.

---

# 16. TOOL REGISTRY

Create a dynamic tool registry.

Every tool should expose metadata:

name\
description\
arguments\
permission\_level\
risk\_level\
timeout\
reversible\
requires\_confirmation\
execution\_handler

Example:

{\
"name": "restart\_service",\
"risk\_level": "medium",\
"reversible": true,\
"requires\_confirmation": false\
}

The planner should select tools from the registry.

Do not expose arbitrary Python functions directly to the LLM.

---

# 17. PERMISSION / SAFETY SUPERVISOR

Implement a Supervisor between the reasoning system and execution layer.

Architecture:

LLM\
↓\
ACTION PROPOSAL\
↓\
SUPERVISOR\
↓\
ALLOW / DENY / CONFIRM\
↓\
EXECUTOR

Create risk levels:

LEVEL 0\
Read-only

LEVEL 1\
Low-risk reversible actions

LEVEL 2\
Moderate system changes

LEVEL 3\
High-risk or irreversible actions

LEVEL 4\
Restricted actions

Examples of actions requiring stronger approval:

- deleting important files
- destructive filesystem operations
- changing security settings
- changing authentication
- exposing secrets
- installing untrusted software
- destructive disk operations
- irreversible account changes

JARVIS should be highly autonomous for safe actions while requiring confirmation for dangerous actions.

---

# 18. MEMORY SYSTEM

Implement multiple memory types.

SHORT-TERM MEMORY\
Recent conversation and immediate context.

WORKING MEMORY\
Current task state and active reasoning context.

EPISODIC MEMORY\
Important previous events and tasks.

SEMANTIC MEMORY\
Stable facts and useful knowledge.

PREFERENCE MEMORY\
User preferences.

PROCEDURAL MEMORY\
Successful workflows and procedures.

Do not blindly store every conversation.

Create relevance-based memory storage.

Example:

User repeatedly prefers a particular coding workflow.

JARVIS can learn that preference and use it later.

---

# 19. MEMORY RETRIEVAL

Before planning complex tasks:

retrieve relevant memories.

Use:

current goal\
current application\
current project\
recent history\
user preferences\
previous failures\
successful procedures

as retrieval signals.

Do not dump the entire database into the model.

Retrieve only relevant context.

---

# 20. SELF-MONITORING

Create a JARVIS health monitor.

Monitor:

- agent process
- worker processes
- event engine
- tool executor
- model API connectivity
- STT
- TTS
- memory service
- browser controller
- computer-control layer

If a noncritical internal component fails, JARVIS should attempt recovery where safe.

Example:

TTS worker dies.

Supervisor restarts it.

JARVIS can report:

"Sir, the voice service briefly stopped responding. I've restarted it."

---

# 21. ERROR RECOVERY

Do not simply return errors.

Implement:

DETECT\
→ CLASSIFY\
→ DIAGNOSE\
→ RECOVER\
→ VERIFY

Example:

Crawler stopped.

JARVIS:

1. Detects process exit.
2. Checks exit code.
3. Reads recent logs.
4. Determines whether the failure is recoverable.
5. Restarts it if authorized.
6. Monitors startup.
7. Verifies process health.
8. Reports result.

If recovery fails:

"Sir, the crawler could not be recovered automatically. I've preserved the error details."

---

# 22. SELF-CORRECTION

JARVIS should compare:

EXPECTED STATE\
vs\
ACTUAL STATE

Example:

Expected:\
development server running on port 5173

Actual:\
process exited

JARVIS must recognize that the objective is incomplete.

It should investigate rather than declaring success.

This principle must apply everywhere.

---

# 23. ADAPTIVE PLANNING

Plans are not fixed.

If a step fails:

DO NOT immediately terminate.

Instead:

observe failure\
→ classify failure\
→ consider alternatives\
→ modify plan\
→ retry within limits\
→ verify

Example:

Method A:\
click UI button

fails.

Possible alternative:

keyboard shortcut\
terminal command\
accessibility API\
different UI target

Use the safest suitable alternative.

Prevent infinite retry loops.

Implement retry budgets.

---

# 24. PROACTIVE TASKS

JARVIS should support background objectives.

Examples:

"Monitor my crawler."

"Watch my development server."

"Keep an eye on storage."

"Tell me if something important happens."

"Monitor this build."

The system should create background watchers rather than repeatedly asking the user.

---

# 25. NATURAL CONVERSATION

The voice interface should work naturally.

User:

"JARVIS, what's going on?"

JARVIS should summarize relevant current state.

Example:

"Everything is running normally, sir. Your crawler is active, network connectivity is stable, and memory usage is at 62 percent."

User:

"Keep an eye on it."

JARVIS should understand "it" from context.

Maintain conversational references and active-task context.

---

# 26. CONTEXTUAL UNDERSTANDING

JARVIS must resolve references such as:

"this"\
"that"\
"it"\
"the project"\
"the server"\
"my crawler"\
"the browser"\
"the previous error"

using recent context and world state.

Do not require the user to repeat exact names.

---

# 27. USER INTENT INTERPRETATION

Convert natural-language requests into structured goals.

Example:

"Make my laptop faster."

should become something like:

goal = SYSTEM\_OPTIMIZATION

Then JARVIS should inspect the machine before making changes.

It should not blindly execute cleanup commands.

---

# 28. SYSTEM AWARENESS

JARVIS should know:

current OS\
hostname\
hardware\
CPU\
RAM\
storage\
network\
battery\
running applications\
available resources\
active tasks

This information should be available through tools rather than fabricated by the model.

---

# 29. ACTION OBSERVABILITY

Create complete logs for the agent.

Each action should record:

timestamp\
task\_id\
tool\
arguments\_summary\
result\
duration\
success\
error\
verification\
risk\_level

Do NOT log secrets.

Provide a developer mode where detailed diagnostics are available.

Normal voice interaction should remain clean.

---

# 30. MODEL COST OPTIMIZATION

Do not call the expensive model unnecessarily.

Use local deterministic code for:

- battery checks
- CPU checks
- RAM checks
- disk checks
- process checks
- network checks
- threshold detection
- event deduplication
- timers

Use models when interpretation or reasoning is actually required.

This is critical for latency and API cost.

---

# 31. MULTI-AGENT INTERNAL ARCHITECTURE

Where useful, separate responsibilities conceptually:

EXECUTIVE\
Overall objective and prioritization.

PLANNER\
Creates task plans.

PERCEPTION\
Interprets screen/system state.

RESEARCHER\
Finds required information.

CODER\
Handles software engineering tasks.

EXECUTOR\
Performs authorized actions.

CRITIC\
Checks whether the result is correct.

MEMORY MANAGER\
Stores and retrieves useful information.

SUPERVISOR\
Controls permissions and risk.

Do not create agents merely for complexity.

Use them when specialization improves reliability.

---

# 32. PRIORITY ENGINE

JARVIS should prioritize events and tasks.

Example priorities:

CRITICAL\
Security/system failure

HIGH\
Task failure affecting user objective

MEDIUM\
Important notification or resource issue

LOW\
Informational event

Ignore trivial noise.

---

# 33. ATTENTION MANAGEMENT

JARVIS should know when NOT to interrupt the user.

Consider:

event importance\
current user activity\
frequency\
recent announcements\
time\
current task\
user preferences

For example:

Do not interrupt every time CPU briefly reaches 80%.

But sustained critical overheating or system failure may warrant immediate communication.

---

# 34. TIME AWARENESS

JARVIS should understand:

current time\
duration\
deadlines\
scheduled tasks\
time-sensitive goals

Example:

"Your build has been running for 38 minutes, which is considerably longer than its previous runs."

---

# 35. SCHEDULER

Implement a scheduler for:

one-time tasks\
recurring tasks\
background monitoring\
deadline reminders\
delayed actions

Persistent scheduled tasks should survive restarts where appropriate.

---

# 36. LONG-RUNNING AGENTS

Allow a task to run for extended periods.

Example:

"Monitor my crawler overnight."

The agent should:

- monitor process
- monitor errors
- restart on authorized recoverable failures
- record events
- avoid unnecessary interaction
- report important events

Provide limits:

max runtime\
max retries\
max resource usage\
allowed tools\
allowed actions

---

# 37. ANTICIPATION

Add a limited prediction layer.

Based on current context, JARVIS may identify likely useful actions.

Example:

If a laptop is unplugged and battery is falling rapidly:

"Sir, you're currently on battery power and the battery is dropping quickly."

Do not perform speculative actions without sufficient evidence.

Prediction must be confidence-based.

---

# 38. LEARNING FROM OUTCOMES

After a task completes:

record:

goal\
plan\
actions\
failures\
successful recovery\
final result\
duration

Use this information to improve future planning.

Example:

If a particular workflow repeatedly succeeds, JARVIS can reuse it rather than rediscovering it every time.

---

# 39. VERIFICATION-FIRST POLICY

NEVER say:

"Done."

unless the intended result has been verified.

Instead:

action\
→ inspect result\
→ verify expected state\
→ complete

If verification is impossible:

say clearly:

"I've completed the requested action, but I couldn't independently verify the final state."

---

# 40. CONFIDENCE MODEL

Every important decision should have confidence.

Possible conceptual values:

0.0–0.39\
uncertain

0.40–0.69\
moderate

0.70–0.89\
high

0.90–1.00\
very high

Low-confidence actions should trigger more observation or confirmation.

---

# 41. INTERRUPT SYSTEM

The user must always be able to interrupt JARVIS.

Implement:

STOP\
CANCEL\
PAUSE\
ABORT CURRENT TASK

Voice interruption should stop active noncritical execution where technically possible.

Example:

User:\
"JARVIS, stop."

JARVIS immediately stops the current autonomous workflow.

---

# 42. TRANSPARENCY

JARVIS does not need to narrate every internal reasoning step.

Instead provide concise status:

"Checking the project."

"I found the build error."

"I'm testing the fix."

"Verification passed."

Never expose hidden chain-of-thought.

---

# 43. CONFIGURATION

Create centralized configuration.

Example:

config/\
models\
permissions\
monitoring\
thresholds\
memory\
voice\
browser\
computer\_control\
scheduler\
logging

Allow settings such as:

battery\_warning\_percent\
battery\_critical\_percent\
disk\_warning\_percent\
cpu\_warning\_percent\
ram\_warning\_percent\
event\_cooldown\_seconds\
max\_tool\_retries\
max\_task\_runtime\
require\_confirmation\_for\_high\_risk

---

# 44. PLUGIN-STYLE TOOLS

Design the tool system so new capabilities can be added without changing the core planner.

For example:

tools/\
system.py\
filesystem.py\
terminal.py\
browser.py\
computer.py\
vision.py\
network.py\
process.py\
scheduler.py\
memory.py

The tool registry should discover and register them.

---

# 45. ASYNC ARCHITECTURE

Use an asynchronous/event-driven architecture where appropriate.

Do not block the voice interface while long-running tasks execute.

Conceptually:

VOICE LOOP\
EVENT LOOP\
AGENT LOOP\
TOOL WORKERS\
MONITORING WORKERS\
MEMORY WORKER\
TTS WORKER

These should communicate through queues/events rather than tightly coupled blocking calls.

---

# 46. VOICE PIPELINE

Preserve the existing STT/TTS system.

Voice path:

MIC\
→ STT\
→ INTENT / CONVERSATION\
→ AGENT\
→ RESPONSE\
→ TTS

But proactive path must also exist:

SYSTEM EVENT\
→ EVENT ENGINE\
→ JARVIS BRAIN\
→ TTS

Therefore JARVIS can speak without STT being triggered.

Prevent overlapping speech.

Use a speech queue.

---

# 47. STATE MACHINE

Create explicit agent states.

IDLE\
LISTENING\
THINKING\
PLANNING\
EXECUTING\
OBSERVING\
VERIFYING\
WAITING\
MONITORING\
PAUSED\
ERROR\
RECOVERING

This will make the system easier to debug.

---

# 48. NO FAKE CAPABILITIES

Never fabricate:

- sensor data
- files
- command results
- application state
- successful actions
- network state
- memory contents

If a capability does not exist, implement the required tool or state that it is unavailable.

---

# 49. DEVELOPMENT STRATEGY

Do not rewrite everything in one uncontrolled change.

Work incrementally.

PHASE 1\
Inspect current architecture.

PHASE 2\
Create core event engine.

PHASE 3\
Create world state.

PHASE 4\
Create tool registry.

PHASE 5\
Create supervisor.

PHASE 6\
Create agent planner.

PHASE 7\
Add computer control.

PHASE 8\
Add vision.

PHASE 9\
Add memory.

PHASE 10\
Add autonomous background tasks.

PHASE 11\
Add recovery.

PHASE 12\
Optimize latency and reliability.

After each phase:

- run tests
- verify existing functionality
- document changes
- fix regressions

---

# 50. TESTING

Create automated tests for:

battery events\
disk events\
network events\
process crashes\
tool failures\
planner failures\
task recovery\
memory retrieval\
permissions\
interrupts\
timeouts\
retry limits\
verification\
voice queue\
event deduplication

Also create integration tests such as:

1. Simulate low battery.
2. Verify event detected.
3. Verify importance scored.
4. Verify LLM called only when required.
5. Verify TTS response.

Another:

1. Start test process.
2. Kill process.
3. Verify JARVIS detects failure.
4. Verify recovery.
5. Verify process healthy.
6. Verify spoken notification.

---

# 51. DEVELOPER DASHBOARD

Create a developer/debug interface showing:

JARVIS state\
active goal\
current task\
events\
system state\
tool calls\
tool results\
worker health\
memory retrieval\
model latency\
API usage\
errors\
retry count

Do not make the dashboard necessary for normal operation.

---

# 52. REAL-WORLD EXAMPLES JARVIS MUST HANDLE

EXAMPLE A:

Battery reaches 20%.

Expected:

JARVIS detects event independently.

"Sir, your battery is at 20 percent."

---

EXAMPLE B:

Wi-Fi disconnects.

Expected:

JARVIS detects connectivity loss.

"Sir, the network connection has been lost."

When restored:

"Network connection restored, sir."

---

EXAMPLE C:

Crawler crashes.

Expected sequence:

detect\
→ inspect process\
→ inspect logs\
→ diagnose\
→ recover if authorized\
→ verify\
→ notify

---

EXAMPLE D:

User says:

"Open my project and fix whatever is wrong."

JARVIS:

inspect project\
→ determine project type\
→ inspect files\
→ run project\
→ observe error\
→ diagnose\
→ modify appropriate files\
→ test\
→ verify\
→ report

Do NOT ask unnecessary questions when the environment already contains enough information.

---

EXAMPLE E:

User says:

"Monitor this overnight."

JARVIS creates a persistent monitoring task.

It continues watching within configured limits.

---

EXAMPLE F:

User says:

"Clean up my laptop."

JARVIS should first inspect the system.

It should classify possible cleanup actions by risk.

It may safely perform approved cleanup.

It should not blindly delete user data.

---

# 53. IMPORTANT: USE THE EXISTING CODEBASE

Before writing major code:

1. Inspect repository.
2. Identify entrypoint.
3. Identify voice system.
4. Identify model client.
5. Identify current tools.
6. Identify configuration.
7. Identify memory.
8. Identify background workers.
9. Identify platform dependencies.
10. Identify duplicated functionality.

Then design the integration around the existing project.

Do not destroy functioning modules simply because a cleaner rewrite is possible.

---

# 54. CODE QUALITY

Use:

- clear module boundaries
- type hints
- structured logging
- error handling
- async where appropriate
- dependency injection where beneficial
- testable components
- configuration-driven behavior
- clean interfaces
- minimal global state

Do not create a giant single Python file.

---

# 55. EXPECTED PROJECT ARCHITECTURE

Adapt this to the actual existing project:

jarvis/\
│\
├── core/\
│   ├── agent.py\
│   ├── planner.py\
│   ├── world\_state.py\
│   ├── task\_manager.py\
│   ├── event\_engine.py\
│   ├── supervisor.py\
│   ├── attention.py\
│   └── confidence.py\
│\
├── perception/\
│   ├── system\_monitor.py\
│   ├── screen.py\
│   ├── vision.py\
│   └── notifications.py\
│\
├── tools/\
│   ├── registry.py\
│   ├── terminal.py\
│   ├── filesystem.py\
│   ├── computer.py\
│   ├── browser.py\
│   ├── process.py\
│   └── system.py\
│\
├── memory/\
│   ├── short\_term.py\
│   ├── episodic.py\
│   ├── semantic.py\
│   ├── preferences.py\
│   └── retrieval.py\
│\
├── voice/\
│   ├── stt.py\
│   ├── tts.py\
│   └── speech\_queue.py\
│\
├── scheduler/\
│   └── scheduler.py\
│\
├── models/\
│   ├── fast.py\
│   ├── reasoning.py\
│   └── vision.py\
│\
├── security/\
│   └── permissions.py\
│\
├── config/\
│   └── settings.py\
│\
├── tests/\
│\
└── main.py

Adapt this architecture rather than blindly copying it.

---

# 56. IMPORTANT AGENT LOOP

Implement the core loop approximately as:

while agent\_running:

```
observe_environment()

update_world_state()

events = process_events()

update_active_tasks()

if important_event(events):
    create_agent_context()

if active_goal_requires_action():
    observe_relevant_context()

    plan_or_replan()

    action = select_next_action()

    decision = supervisor.evaluate(action)

    if decision.allowed:
        result = execute(action)

        observe_result()

        verify_result()

        update_memory()

    elif decision.requires_confirmation:
        request_confirmation()

    else:
        reject_action()

sleep_or_wait_for_event()
```

Optimize this architecture appropriately.

Do not busy-loop unnecessarily.

Prefer event-driven wakeups where possible.

---

# 57. SUCCESS CRITERIA

The finished JARVIS should feel like an intelligent computer companion rather than a command-response chatbot.

It should be able to:

✓ hear the user\
✓ understand context\
✓ remember useful information\
✓ see the computer\
✓ understand the current environment\
✓ monitor system state\
✓ detect important events\
✓ speak proactively\
✓ plan multi-step tasks\
✓ use tools\
✓ control mouse\
✓ control keyboard\
✓ use terminal\
✓ interact with files\
✓ interact with browser\
✓ execute tasks\
✓ verify results\
✓ recover from failures\
✓ maintain persistent tasks\
✓ manage background monitoring\
✓ respect permissions\
✓ learn useful preferences\
✓ interrupt safely\
✓ avoid unnecessary model calls\
✓ never claim success without verification

---

# 58. FINAL IMPLEMENTATION REQUIREMENT

Do not merely explain how this could be built.

Inspect the existing project and IMPLEMENT the architecture.

Before making changes:

- map the existing codebase
- identify integration points
- identify dependencies
- identify missing components

Then implement the highest-value components first.

After implementation:

1. Run the application.
2. Run tests.
3. Verify voice interaction.
4. Verify proactive events.
5. Verify keyboard/mouse.
6. Verify terminal.
7. Verify screenshot/VLM workflow.
8. Verify task planning.
9. Verify recovery.
10. Fix errors.
11. Provide a concise architecture summary.
12. List files changed.
13. List new dependencies.
14. Provide exact commands to install/run.
15. Explain what is fully functional and what is still limited by the available models/APIs.

Do not claim "real AGI" has been created.

The goal is to make this JARVIS as autonomous, persistent, proactive, multimodal, tool-using, self-monitoring, and computer-controlling as practically possible.

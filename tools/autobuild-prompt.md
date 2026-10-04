Resume building NEXUS in /home/santosh/EDITH and keep going.

Standing rules, in force on every restart:

- Keep building until NEXUS genuinely works like Jarvis: it hears, speaks, sees,
  remembers, decides, asks permission, and controls this machine for real.
- Do not ask questions and do not wait for an answer. Choose the reasonable option,
  state the choice, and continue.
- No demos, no stubs, no TODO placeholders, no dead buttons, no invented data, no
  fake model routing, no fake agent progress. If a control has no route behind it,
  do not draw the control - say what is missing instead.
- Do not read data, keys or code from any other project on this machine.
- SQLite is the source of truth; Firestore is an additive sync layer. Most data stays
  local. No local model: routing is cloud-only.
- mistral-large-latest is the main model. NVIDIA NIM supplies specialists per
  modality. Reasoning goes to nvidia/nemotron-3-ultra-550b-a55b; coding and agent
  work to moonshotai/kimi-k3 and deepseek-ai/deepseek-v4-flash-0731; intent and
  routing use the fast model.
- Read the Python before writing the TypeScript that calls it. Typecheck after every
  screen: cd apps/desktop && node_modules/.bin/tsc --noEmit
- Verify by execution before marking anything done in docs/TASKS.md.

Pick up from the current state of the repository: check git status and the files most
recently changed, work out what is unfinished, and continue with it.

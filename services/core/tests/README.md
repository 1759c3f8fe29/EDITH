# Core tests

```bash
.venv/bin/python -m pip install -r services/core/requirements-dev.txt   # once
cd services/core && ../../.venv/bin/python -m pytest                    # 18 tests, ~5s
```

## What is real and what is not

Two things are stubbed, and only two: the model call (`agent._one_shot`) and tool
execution (`runtime.execute`). One is a network call, the other touches this
computer — neither can be part of a repeatable test.

Everything else is the real thing: the real SQLite schema with its `CHECK`
constraints on `tasks.status` and `task_steps.status`, the real repos, the real
permission engine, the real event bus, and — in `test_task_recovery.py` — the real
FastAPI lifespan, driven in-process. So a test here can fail because the schema and
the code disagree, which is most of what they are for.

The database is a fresh temp file per session. `conftest.py` sets `NEXUS_DB_PATH`
**before importing `nexus`**, because `config.settings()` is cached on first use and
anything reading it later — including `create_app`, which calls `db.configure()` with
no arguments — would otherwise open the user's real `~/.local/share/nexus/nexus.db`.
Do not move that assignment below the imports.

## Conventions

- **No asyncio plugin.** Async tests are wrapped by a local `sync` decorator that
  calls `asyncio.run`. One less thing that can break under a plugin release.
- **Settings are global**, so an autouse fixture snapshots and restores the three
  `agent_*` keys around every test.
- **Assertion messages state the consequence**, not the expected value — pytest
  already prints the value. `"cancel that finishes the work first is not cancel"`
  says why the line exists; `"expected 3, got 4"` does not.

## The tests are mutation-checked

Every assertion here was verified to fail when the behaviour it describes is broken —
five deliberate mutations, each caught by exactly the test that claims to cover it:

| Mutation | Caught by |
| --- | --- |
| `reconcile_interrupted` sweeps `pending` too | `test_reconciliation_leaves_a_pending_task_alone` |
| interrupted steps go to `failed`, not `pending` | `test_startup_reconciles_a_stranded_task_…` |
| `set_step_status` stops clearing `result`/`error` on `running` | `test_a_step_that_starts_again_drops…` |
| `set_status` stops clearing `finished_at` on resume | `test_a_resumed_task_does_not_keep_a_stale_finish_time` |
| the run loop's step budget is not enforced | `test_agent_max_steps_stops_the_run_and_names_itself` |
| `STARTABLE` admits finished tasks | `test_cancelling_a_running_task_leaves_nothing…` |
| both double-start guards removed | `test_starting_twice_does_not_run_the_task_twice` |
| the loop's mid-run cancellation check removed | `test_a_run_notices_a_cancel_it_did_not_hear_about` |

Add a test the same way: break the thing on purpose first, watch it go red, then put
it back. A green test that cannot fail is worse than no test, because it is read as
proof.

Two of those need saying out loud, because they were found this way rather than by
reading the code:

- **Double-start has two guards** (`start`'s `is_running` check and `run`'s own status
  check) and removing either one alone still passes. It only goes red when both are
  gone. `run`'s is the one holding the line, because `create_task` defers, so a second
  `start` sees a task that is still `pending`.
- **Cancelling through `agent.cancel` proves nothing about the loop**, because it also
  cancels the asyncio task. The loop's own check is only reachable when there is no
  local handle — another process, or a bare `await run()` — which is why
  `test_a_run_notices_a_cancel_it_did_not_hear_about` flips the row directly instead.

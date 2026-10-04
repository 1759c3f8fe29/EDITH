"""Schema definition and migration list (tasks B1, B2, B7, B8).

Conventions every syncable table follows so Phase K can compute a delta without
a second migration pass:

* ``id``          TEXT primary key (uuid4 hex)
* ``created_at`` / ``updated_at``  ISO-8601 UTC strings
* ``rev``         monotonic integer from ``sync_seq``

Deletions insert a row into ``tombstones`` so a hard delete (§70) can still be
propagated to a remote replica later without keeping the deleted data itself.
"""

from __future__ import annotations

from typing import NamedTuple


class Migration(NamedTuple):
    version: int
    name: str
    statements: tuple[str, ...]


# --------------------------------------------------------------------------
# v1 -- initial schema
# --------------------------------------------------------------------------

_V1_CORE = (
    """
    CREATE TABLE sync_seq (
        id    INTEGER PRIMARY KEY CHECK (id = 1),
        value INTEGER NOT NULL DEFAULT 0
    )
    """,
    "INSERT INTO sync_seq (id, value) VALUES (1, 0)",
    """
    CREATE TABLE tombstones (
        entity     TEXT NOT NULL,
        entity_id  TEXT NOT NULL,
        deleted_at TEXT NOT NULL,
        rev        INTEGER NOT NULL,
        PRIMARY KEY (entity, entity_id)
    )
    """,
    """
    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rev        INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE VIRTUAL TABLE search_index USING fts5(
        entity     UNINDEXED,
        entity_id  UNINDEXED,
        title,
        body,
        tokenize = 'porter unicode61'
    )
    """,
)

_V1_PROJECTS = (
    """
    CREATE TABLE projects (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        rev         INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_projects_status ON projects(status, updated_at DESC)",
)

_V1_CONVERSATIONS = (
    """
    CREATE TABLE conversations (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL DEFAULT 'New conversation',
        project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
        archived   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rev        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_conversations_recent ON conversations(archived, updated_at DESC)",
    "CREATE INDEX idx_conversations_project ON conversations(project_id, updated_at DESC)",
    """
    CREATE TABLE messages (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL
                        CHECK (role IN ('user', 'assistant', 'system', 'tool')),
        content         TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'complete'
                        CHECK (status IN ('streaming', 'complete', 'failed', 'interrupted')),
        attachments     TEXT NOT NULL DEFAULT '[]',
        capability      TEXT,
        provider        TEXT,
        model           TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        latency_ms      INTEGER,
        error           TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        rev             INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at)",
)

_V1_MEMORY = (
    """
    CREATE TABLE memories (
        id                     TEXT PRIMARY KEY,
        title                  TEXT NOT NULL,
        body                   TEXT NOT NULL,
        category               TEXT NOT NULL DEFAULT 'general',
        project_id             TEXT REFERENCES projects(id) ON DELETE SET NULL,
        source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        source_message_id      TEXT,
        approved               INTEGER NOT NULL DEFAULT 0,
        pinned                 INTEGER NOT NULL DEFAULT 0,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL,
        rev                    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_memories_approved ON memories(approved, updated_at DESC)",
    "CREATE INDEX idx_memories_category ON memories(category, updated_at DESC)",
    "CREATE INDEX idx_memories_project ON memories(project_id)",
    """
    CREATE TABLE memory_vectors (
        memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
        dim       INTEGER NOT NULL,
        model     TEXT NOT NULL,
        vec       BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

_V1_TASKS = (
    """
    CREATE TABLE tasks (
        id              TEXT PRIMARY KEY,
        objective       TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'waiting_permission',
                                          'completed', 'failed', 'cancelled', 'interrupted')),
        project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
        conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
        result          TEXT,
        error           TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        rev             INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_tasks_status ON tasks(status, created_at DESC)",
    """
    CREATE TABLE task_steps (
        id         TEXT PRIMARY KEY,
        task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        intent     TEXT NOT NULL,
        capability TEXT,
        tool       TEXT,
        params     TEXT NOT NULL DEFAULT '{}',
        depends_on TEXT NOT NULL DEFAULT '[]',
        status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'waiting_permission',
                                     'completed', 'failed', 'skipped', 'cancelled')),
        result     TEXT,
        error      TEXT,
        -- Nullable on purpose: NULL means "no verifier exists for this step",
        -- which is a different fact from 0 ("checked and not confirmed").
        verified   INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX idx_task_steps_order ON task_steps(task_id, idx)",
    """
    CREATE TABLE tool_invocations (
        id          TEXT PRIMARY KEY,
        tool        TEXT NOT NULL,
        params      TEXT NOT NULL DEFAULT '{}',
        task_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
        step_id     TEXT,
        ok          INTEGER NOT NULL DEFAULT 0,
        -- Tri-state, see task_steps.verified.
        verified    INTEGER,
        result      TEXT,
        error       TEXT,
        duration_ms INTEGER,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_tool_invocations_recent ON tool_invocations(created_at DESC)",
)

_V1_ACTIVITY = (
    """
    CREATE TABLE activity (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        summary         TEXT NOT NULL,
        detail          TEXT NOT NULL DEFAULT '{}',
        level           TEXT NOT NULL DEFAULT 'info'
                        CHECK (level IN ('info', 'success', 'warning', 'error')),
        task_id         TEXT,
        conversation_id TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_activity_recent ON activity(created_at DESC)",
    "CREATE INDEX idx_activity_kind ON activity(kind, created_at DESC)",
)

_V1_PERMISSIONS = (
    """
    CREATE TABLE permissions (
        id          TEXT PRIMARY KEY,
        capability  TEXT NOT NULL,
        scope       TEXT NOT NULL DEFAULT '*',
        grant_kind  TEXT NOT NULL
                    CHECK (grant_kind IN ('deny', 'once', 'session', 'always', 'constrained')),
        constraints TEXT NOT NULL DEFAULT '{}',
        granted_at  TEXT NOT NULL,
        expires_at  TEXT,
        updated_at  TEXT NOT NULL,
        rev         INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX idx_permissions_capability_scope ON permissions(capability, scope)",
)

_V1_DEVICES = (
    """
    CREATE TABLE devices (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        type       TEXT NOT NULL DEFAULT 'unknown',
        state      TEXT NOT NULL DEFAULT 'offline'
                   CHECK (state IN ('online', 'offline', 'unavailable')),
        last_seen  TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rev        INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE device_capabilities (
        device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        capability TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (device_id, capability)
    )
    """,
)

_V1_AUTOMATIONS = (
    """
    CREATE TABLE automations (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        trigger_kind TEXT NOT NULL DEFAULT 'schedule'
                     CHECK (trigger_kind IN ('schedule', 'interval', 'manual')),
        schedule     TEXT NOT NULL DEFAULT '',
        action       TEXT NOT NULL DEFAULT '{}',
        enabled      INTEGER NOT NULL DEFAULT 1,
        last_run_at  TEXT,
        next_run_at  TEXT,
        last_result  TEXT,
        unavailable_reason TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        rev          INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_automations_due ON automations(enabled, next_run_at)",
    """
    CREATE TABLE automation_runs (
        id            TEXT PRIMARY KEY,
        automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
        detail        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_automation_runs ON automation_runs(automation_id, started_at DESC)",
)

# FTS5 upkeep. A unified index because §83 wants search across all of these and
# one index avoids four near-identical query paths.
_V1_SEARCH_TRIGGERS = (
    """
    CREATE TRIGGER trg_conversations_ai AFTER INSERT ON conversations BEGIN
        INSERT INTO search_index (entity, entity_id, title, body)
        VALUES ('conversation', new.id, new.title, '');
    END
    """,
    """
    CREATE TRIGGER trg_conversations_au AFTER UPDATE OF title ON conversations BEGIN
        UPDATE search_index SET title = new.title
        WHERE entity = 'conversation' AND entity_id = new.id;
    END
    """,
    """
    CREATE TRIGGER trg_conversations_ad AFTER DELETE ON conversations BEGIN
        DELETE FROM search_index WHERE entity = 'conversation' AND entity_id = old.id;
    END
    """,
    """
    CREATE TRIGGER trg_messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO search_index (entity, entity_id, title, body)
        VALUES ('message', new.id, new.conversation_id, new.content);
    END
    """,
    """
    CREATE TRIGGER trg_messages_au AFTER UPDATE OF content ON messages BEGIN
        UPDATE search_index SET body = new.content
        WHERE entity = 'message' AND entity_id = new.id;
    END
    """,
    """
    CREATE TRIGGER trg_messages_ad AFTER DELETE ON messages BEGIN
        DELETE FROM search_index WHERE entity = 'message' AND entity_id = old.id;
    END
    """,
    """
    CREATE TRIGGER trg_memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO search_index (entity, entity_id, title, body)
        VALUES ('memory', new.id, new.title, new.body);
    END
    """,
    """
    CREATE TRIGGER trg_memories_au AFTER UPDATE OF title, body ON memories BEGIN
        UPDATE search_index SET title = new.title, body = new.body
        WHERE entity = 'memory' AND entity_id = new.id;
    END
    """,
    """
    CREATE TRIGGER trg_memories_ad AFTER DELETE ON memories BEGIN
        DELETE FROM search_index WHERE entity = 'memory' AND entity_id = old.id;
    END
    """,
    """
    CREATE TRIGGER trg_tasks_ai AFTER INSERT ON tasks BEGIN
        INSERT INTO search_index (entity, entity_id, title, body)
        VALUES ('task', new.id, new.objective, '');
    END
    """,
    """
    CREATE TRIGGER trg_tasks_au AFTER UPDATE OF objective ON tasks BEGIN
        UPDATE search_index SET title = new.objective
        WHERE entity = 'task' AND entity_id = new.id;
    END
    """,
    """
    CREATE TRIGGER trg_tasks_ad AFTER DELETE ON tasks BEGIN
        DELETE FROM search_index WHERE entity = 'task' AND entity_id = old.id;
    END
    """,
    """
    CREATE TRIGGER trg_activity_ai AFTER INSERT ON activity BEGIN
        INSERT INTO search_index (entity, entity_id, title, body)
        VALUES ('activity', new.id, new.summary, new.kind);
    END
    """,
    """
    CREATE TRIGGER trg_activity_ad AFTER DELETE ON activity BEGIN
        DELETE FROM search_index WHERE entity = 'activity' AND entity_id = old.id;
    END
    """,
)


# --------------------------------------------------------------------------
# v2 -- ambient events (task L9)
# --------------------------------------------------------------------------
#
# What the monitors observe, kept separately from ``activity``. The distinction
# matters: ``activity`` is the record of what NEXUS *did* -- every row there is a
# consequence of a decision, and the user needs it permanently to audit the
# system. Ambient events are what NEXUS *noticed* while nobody asked it to: which
# window had focus, that the screen changed, that the battery crossed 20%. That is
# a fundamentally different kind of data. It arrives without being requested, it
# is voluminous, and it is about the user rather than about the assistant.
#
# So it is stored under different rules:
#
# * **It expires.** Every row carries ``expires_at`` and is purged, whether or not
#   anything references it. Observation that accumulates forever is surveillance
#   (spec 70, 81); observation with a horizon is a memory of the last few days.
# * **It is not full-text indexed.** Window titles and clipboard shapes are the
#   most revealing text in the system. Copying them into ``search_index`` would
#   make a second copy that the retention purge would have to chase, and any miss
#   there leaks content the user believed was deleted. The timeline asks "what
#   happened between these times, of this kind", which indexes answer directly.
# * **It does not sync.** No ``rev``, no ``updated_at``, no tombstones. An ambient
#   event is true of one device at one moment; "the screen changed" is meaningless
#   on the phone. Phase K has nothing to carry here.
_V2_AMBIENT = (
    """
    CREATE TABLE ambient_events (
        id          TEXT PRIMARY KEY,
        -- Which monitor saw it ('system', 'window', 'screen', 'clipboard').
        monitor     TEXT NOT NULL,
        -- The specific observation, e.g. 'window.focus', 'battery.low'. Open set:
        -- monitors are added over time and a CHECK here would mean a migration
        -- for every new observation.
        kind        TEXT NOT NULL,
        summary     TEXT NOT NULL,
        data        TEXT NOT NULL DEFAULT '{}',
        severity    TEXT NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('info', 'notice', 'alert')),
        -- Identity of the *condition*, not the moment -- 'battery.low' rather
        -- than 'battery.low at 14:03'. Lets the timeline collapse a condition
        -- that persisted into one entry instead of one per sample.
        dedupe_key  TEXT NOT NULL DEFAULT '',
        -- True when this event reports a condition ending ("battery back above
        -- 20%"), so an alert can be shown as resolved rather than repeated.
        clears      INTEGER NOT NULL DEFAULT 0
                    CHECK (clears IN (0, 1)),
        -- When the monitor saw it, which is not when the row was written: a
        -- focus session is recorded when it ends, and is about when it started.
        observed_at TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        -- NULL means "keep until the user deletes it" and is used only for events
        -- the user explicitly pinned. Everything else has a date.
        expires_at  TEXT
    )
    """,
    "CREATE INDEX idx_ambient_recent ON ambient_events(observed_at DESC)",
    "CREATE INDEX idx_ambient_kind ON ambient_events(kind, observed_at DESC)",
    "CREATE INDEX idx_ambient_monitor ON ambient_events(monitor, observed_at DESC)",
    "CREATE INDEX idx_ambient_expiry ON ambient_events(expires_at)",
    "CREATE INDEX idx_ambient_dedupe ON ambient_events(dedupe_key, observed_at DESC)",
)


# --------------------------------------------------------------------------
# v3 -- model probe results
# --------------------------------------------------------------------------
#
# What a provider *lists* and what your key can actually *call* are different
# sets. Enumerating NVIDIA's hosted listing returns ids that then answer 404
# "Function not found for account" -- the model is real, deployed, and simply not
# enabled for this account. There is no way to know which without asking.
#
# So the answer is remembered. Without this table the router re-learns the same
# 404 on every single request, and the user waits through a round-trip to a model
# that was never going to work.
#
# Deliberately NOT syncable -- no `id`/`rev`, and Phase K skips it. The result is
# a fact about *this machine's API key*, not about the user. Copying it to another
# device with a different key would assert something that was never measured
# there, which is the kind of confident-and-wrong claim this whole design exists
# to avoid.
_V3_MODEL_PROBES = (
    """
    CREATE TABLE model_probes (
        key        TEXT PRIMARY KEY,
        provider   TEXT NOT NULL,
        model      TEXT NOT NULL,
        transport  TEXT NOT NULL DEFAULT 'chat',
        ok         INTEGER NOT NULL,
        -- Load-bearing: `durable` splits "this will never work until you change
        -- something" (404 not_found, auth) from "this failed once" (timeout,
        -- rate limit, overload). Only durable failures are allowed to remove a
        -- model from routing; skipping a model forever because it was slow once
        -- would quietly shrink the bench with no way for the user to see why.
        error_kind TEXT,
        durable    INTEGER NOT NULL DEFAULT 0,
        detail     TEXT NOT NULL DEFAULT '',
        latency_ms INTEGER,
        checked_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_model_probes_ok ON model_probes(ok, durable)",
)


# v4: what an automation was allowed to do when the user armed it (§34, §36, §5).
#
# ``capability_scope`` is the consent captured at arm time, as a JSON array of
# capability names. The default of ``'[]'`` is why this migration is safe on a
# populated database: the only action kind that exists is ``notify``, which reaches
# nothing outside NEXUS's own tables, so every existing row is already correctly
# scoped by an empty list. Nothing is disarmed by upgrading.
#
# The column exists because trusted mode is a standing answer to questions that
# *would have been asked*, not a blank cheque for a rule that changes shape later.
# An action edited to need more power than the stored scope carries is disarmed and
# labelled, rather than quietly inheriting the trust the narrower version earned.
#
# ``denied_capability`` is a column rather than a key in the run's ``detail`` JSON
# because "which rules are being refused, and for what" is a question worth being
# able to ask in SQL.
_V4_AUTOMATION_CAPABILITIES = (
    "ALTER TABLE automations ADD COLUMN capability_scope TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE automation_runs ADD COLUMN denied_capability TEXT",
)


# v5: what each model actually did, so routing can learn instead of guessing
# (§15 routing is explainable, §30 cost, §38 learning from outcomes, §78).
#
# `model_probes` (v3) answers "can this key call this model at all" -- one row per
# model, overwritten, a capability fact. This table answers a different question:
# "when NEXUS used this model for this job, how did it go." That needs one row per
# *call*, kept over time, because the useful quantity is a rate rather than a last
# known state.
#
# Why a separate table rather than more columns on model_probes: a probe is a
# yes/no about reachability and is replaced each time it is taken, while an outcome
# is an observation that must accumulate. Overwriting outcomes would destroy the
# only thing they are for.
#
# `reward` is stored rather than recomputed on read, because the formula that
# produced it is a property of the moment: it folds in the latency budget and the
# verification result that were true for *that* call. Recomputing later with a
# changed formula would silently rewrite history, and the router would be learning
# from numbers nobody measured.
#
# `verified` is deliberately three-valued (NULL / 0 / 1) to match the tool layer's
# tri-state: "could not be checked" is not the same as "checked and wrong", and
# collapsing them would teach the learner that unverifiable work failed.
_V5_MODEL_OUTCOMES = (
    """
    CREATE TABLE model_outcomes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job        TEXT NOT NULL,
        key        TEXT NOT NULL,
        provider   TEXT NOT NULL,
        model      TEXT NOT NULL,
        ok         INTEGER NOT NULL,
        -- NULL where the call never reported one, not 0: a zero would be read as
        -- "instant" by anything averaging this column.
        latency_ms INTEGER,
        first_token_ms INTEGER,
        -- Tri-state, matching ToolResult.verified: NULL = not checkable.
        verified   INTEGER,
        error_kind TEXT,
        -- 0.0..1.0, computed at the moment of the call. See reward() in
        -- nexus/learning/outcomes.py for the formula and why it is stored.
        reward     REAL NOT NULL,
        at         TEXT NOT NULL
    )
    """,
    # The learner's only hot query is "rows for this job and model, newest first".
    "CREATE INDEX idx_model_outcomes_job_key ON model_outcomes(job, key, at DESC)",
    "CREATE INDEX idx_model_outcomes_at ON model_outcomes(at)",
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            _V1_CORE
            + _V1_PROJECTS
            + _V1_CONVERSATIONS
            + _V1_MEMORY
            + _V1_TASKS
            + _V1_ACTIVITY
            + _V1_PERMISSIONS
            + _V1_DEVICES
            + _V1_AUTOMATIONS
            + _V1_SEARCH_TRIGGERS
        ),
    ),
    Migration(
        version=2,
        name="ambient_events",
        statements=_V2_AMBIENT,
    ),
    Migration(
        version=3,
        name="model_probes",
        statements=_V3_MODEL_PROBES,
    ),
    Migration(
        version=4,
        name="automation_capability_scope",
        statements=_V4_AUTOMATION_CAPABILITIES,
    ),
    Migration(
        version=5,
        name="model_outcomes",
        statements=_V5_MODEL_OUTCOMES,
    ),
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)

/**
 * PROJECTS — grouping what belongs together (§23).
 *
 * A project is a label with three real relationships: conversations, tasks and memories
 * carry a nullable `project_id`. That is the whole feature, and the screen is shaped by
 * what it means:
 *
 * - **Counts come from the list query**, which computes them with subqueries per row.
 *   They are real, so they are shown as numbers; the screen does not fetch the contents.
 * - **Deleting a project does not delete its contents.** The route reports what was
 *   *detached*, and the confirmation says so before the fact rather than after — a user
 *   who thinks "delete project" means "delete 40 conversations" would never press it,
 *   and a user who thinks the opposite would press it by mistake.
 * - **There is no "open the project's conversations" view.** Nothing filters
 *   conversations by project over HTTP, so the counts are stated and not linked. A link
 *   that led to an unfiltered list would be worse than no link.
 */

import { useState } from "react";

import {
  ActionResult,
  Button,
  ConfirmDialog,
  DataRow,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  ListRow,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  TextArea,
  TextInput,
  clockTime,
  relativeTime,
} from "../components/primitives";
import { api, type Project } from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";

/** The only two statuses `projects.update` accepts. Anything else is ignored by it. */
const STATUSES = [
  { value: "active", label: "Active" },
  { value: "archived", label: "Archived" },
] as const;

type StatusValue = (typeof STATUSES)[number]["value"];

export function ProjectsScreen({ focus }: { focus: string | null }) {
  const { go, link } = useNexus();
  const [status, setStatus] = useState<StatusValue>("active");
  const [composing, setComposing] = useState(false);

  const list = useAsync(() => api.projects.list(status), [status]);
  const projects = list.data?.projects ?? [];
  const selected = projects.find((project) => project.id === focus) ?? null;

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 lg:grid-cols-[minmax(240px,340px)_1fr]">
      <Panel
        title="PROJECTS"
        meta={list.data ? `${projects.length}` : undefined}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <IconButton
            name="plus"
            label="Create a project"
            onClick={() => setComposing(true)}
            disabled={!link.connected}
            reason="The core is not reachable, so it cannot store one."
          />
        }
      >
        <div className="border-b border-line px-3 py-2">
          <Select<StatusValue>
            value={status}
            onChange={setStatus}
            options={STATUSES.map((entry) => ({ value: entry.value, label: entry.label }))}
          />
        </div>

        {composing && (
          <Composer
            onClose={() => setComposing(false)}
            onCreated={(project) => {
              list.reload();
              go("projects", project.id);
            }}
          />
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {list.state === "loading" ? (
            <LoadingState what="projects" />
          ) : !list.data ? (
            <div className="p-2">
              <ErrorState
                what={list.error ?? "The project list did not load."}
                did="Nothing is listed. An empty list would claim you have no projects."
                next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
                offline={list.offline}
                onRetry={list.reload}
                compact
              />
            </div>
          ) : projects.length === 0 ? (
            <EmptyState
              icon="projects"
              title={status === "archived" ? "Nothing archived" : "No projects yet"}
              detail={
                status === "archived"
                  ? "Archiving keeps a project and everything attached to it. None have been archived."
                  : "A project groups conversations, tasks and memories. Nothing requires one — it is a label you add when the grouping starts to matter."
              }
            />
          ) : (
            <ul>
              {projects.map((project) => (
                <li key={project.id}>
                  <ListRow
                    selected={project.id === focus}
                    onClick={() => go("projects", project.id)}
                  >
                    <p className="truncate text-xs text-ink">{project.title}</p>
                    <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                      {countLine(project)} · {relativeTime(project.updated_at)}
                    </p>
                  </ListRow>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Panel>

      {selected ? (
        <Detail project={selected} onChanged={list.reload} />
      ) : (
        <Panel title="PROJECT" className="min-h-0">
          <EmptyState
            icon="projects"
            title={focus ? "That project is not in this list" : "No project selected"}
            detail={
              focus
                ? "It may be archived. Switch the filter above to find it."
                : "Pick one on the left to see what is attached to it and to rename or archive it."
            }
          />
        </Panel>
      )}
    </div>
  );
}

/**
 * The attachment counts as one line.
 *
 * Each count is optional on `Project` because a payload could omit it. An absent count
 * is skipped rather than printed as zero — "0 conversations" and "the server did not
 * say" are different, and only one of them is a fact about the project.
 */
function countLine(project: Project): string {
  const parts: string[] = [];
  if (project.conversation_count !== undefined) parts.push(`${project.conversation_count} conv`);
  if (project.task_count !== undefined) parts.push(`${project.task_count} tasks`);
  if (project.memory_count !== undefined) parts.push(`${project.memory_count} memories`);
  return parts.length > 0 ? parts.join(" · ") : "no counts reported";
}

/** One project: what is attached, and the three things that can be changed. */
function Detail({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const { go } = useNexus();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(project.title);
  const [description, setDescription] = useState(project.description);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const save = useAction(async () => {
    const nextTitle = title.trim();
    if (!nextTitle) throw new Error("A project needs a name.");
    await api.projects.update(project.id, { title: nextTitle, description: description.trim() });
    onChanged();
    setEditing(false);
    return "Saved.";
  });

  const toggleStatus = useAction(async () => {
    const next = project.status === "archived" ? "active" : "archived";
    await api.projects.update(project.id, { status: next });
    onChanged();
    return next === "archived"
      ? "Archived. Everything attached to it is untouched."
      : "Restored to active.";
  });

  const remove = useAction(async () => {
    const result = await api.projects.remove(project.id);
    setConfirmDelete(false);
    if (!result.deleted) {
      throw new Error("The core reported that nothing was deleted — the project may already be gone.");
    }
    onChanged();
    go("projects", null);
    const { conversations, memories, tasks } = result.detached;
    return `Project deleted. Detached and kept: ${conversations} conversations, ${tasks} tasks, ${memories} memories.`;
  });

  const attached =
    (project.conversation_count ?? 0) + (project.task_count ?? 0) + (project.memory_count ?? 0);

  return (
    <Panel
      title={project.title.toUpperCase()}
      meta={`${project.status} · updated ${clockTime(project.updated_at)}`}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      actions={
        <>
          <IconButton
            name="edit"
            label="Rename or re-describe this project"
            onClick={() => {
              setTitle(project.title);
              setDescription(project.description);
              setEditing(true);
            }}
          />
          <IconButton
            name={project.status === "archived" ? "refresh" : "pin"}
            label={project.status === "archived" ? "Make active again" : "Archive this project"}
            onClick={toggleStatus.run}
            busy={toggleStatus.busy}
          />
          <IconButton
            name="trash"
            label="Delete this project"
            tone="danger"
            onClick={() => setConfirmDelete(true)}
          />
        </>
      }
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {editing ? (
          <div className="chamfer-sm space-y-2 border border-line-bright bg-raised px-2.5 py-2">
            <Field label="Name" htmlFor="project-title">
              <TextInput id="project-title" value={title} onChange={setTitle} autoFocus />
            </Field>
            <Field
              label="What it is for"
              htmlFor="project-description"
              hint="Shown here only. It is not sent to the model as context."
            >
              <TextArea
                id="project-description"
                value={description}
                onChange={setDescription}
                rows={3}
              />
            </Field>
            <div className="flex items-center gap-2">
              <Button size="sm" tone="accent" onClick={save.run} busy={save.busy}>
                Save
              </Button>
              <Button size="sm" tone="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
            <ActionResult done={save.done} failed={save.failed} onClear={save.clear} />
          </div>
        ) : (
          <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
            <span className="label">What it is for</span>
            <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-soft">
              {project.description || "No description was written."}
            </p>
          </div>
        )}

        <ActionResult
          done={toggleStatus.done}
          failed={toggleStatus.failed}
          onClear={toggleStatus.clear}
        />
        <ActionResult done={remove.done} failed={remove.failed} onClear={remove.clear} />

        <Section title="Attached to this project" meta={`${attached} in total`}>
          <div className="flex flex-col">
            <DataRow
              label="Conversations"
              value={
                project.conversation_count === undefined
                  ? "not reported"
                  : String(project.conversation_count)
              }
            />
            <DataRow
              label="Tasks"
              value={project.task_count === undefined ? "not reported" : String(project.task_count)}
            />
            <DataRow
              label="Memories"
              value={
                project.memory_count === undefined ? "not reported" : String(project.memory_count)
              }
            />
          </div>
          <p className="px-3 py-1.5 text-[11px] leading-relaxed text-ink-dim">
            These are counted by the core. There is no route that lists a project's
            conversations, so they cannot be opened from here — CONVERSATIONS and TASKS
            show everything, unfiltered.
          </p>
        </Section>

        <Section title="Identity">
          <div className="flex flex-col">
            <DataRow
              label="Status"
              value={project.status}
              state={project.status === "active" ? "ok" : "idle"}
            />
            <DataRow label="Created" value={clockTime(project.created_at)} />
            <DataRow label="Id" value={project.id} title="Used as `project_id` on attached rows." />
          </div>
        </Section>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this project"
        consequence={`"${project.title}" is removed. Its ${project.conversation_count ?? 0} conversations, ${project.task_count ?? 0} tasks and ${project.memory_count ?? 0} memories are NOT deleted — they are detached and stay in the database without a project. Deleting the project cannot be undone.`}
        confirmLabel="Delete the project"
        onConfirm={remove.run}
        onCancel={() => setConfirmDelete(false)}
        busy={remove.busy}
        failed={remove.failed}
      />
    </Panel>
  );
}

/** Creating one. Title is required by the route, so it is required here. */
function Composer({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (project: Project) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  const create = useAction(async () => {
    const cleanTitle = title.trim();
    if (!cleanTitle) throw new Error("A project needs a name.");
    const result = await api.projects.create(cleanTitle, description.trim());
    onCreated(result.project);
    setTitle("");
    setDescription("");
    onClose();
    return `Created "${result.project.title}".`;
  });

  return (
    <div className="flex flex-col gap-2 border-b border-line bg-raised/40 px-3 py-2.5">
      <div className="flex items-center justify-between">
        <span className="label">New project</span>
        <IconButton name="close" label="Close without creating" onClick={onClose} />
      </div>
      <Field label="Name" htmlFor="new-project-title">
        <TextInput
          id="new-project-title"
          value={title}
          onChange={setTitle}
          autoFocus
          placeholder="NEXUS desktop"
          onEnter={create.run}
        />
      </Field>
      <Field label="What it is for" htmlFor="new-project-description">
        <TextArea
          id="new-project-description"
          value={description}
          onChange={setDescription}
          rows={2}
          placeholder="Optional."
        />
      </Field>
      <div className="flex items-center gap-2">
        <Button size="sm" tone="accent" icon="check" onClick={create.run} busy={create.busy}>
          Create
        </Button>
        <StatusPill state="idle" size="sm">
          STARTS ACTIVE
        </StatusPill>
      </div>
      <ActionResult done={create.done} failed={create.failed} onClear={create.clear} />
    </div>
  );
}

/**
 * CONVERSATIONS — every exchange, and what answered it (§14, §15, §16).
 *
 * A list of threads on the left, one thread on the right. The part that makes this
 * more than a chat log is per-message attribution: `Message` carries `provider`,
 * `model` and `capability` from the database, so each answer says which model produced
 * it rather than all of them being "NEXUS" (§26).
 *
 * Three decisions:
 *
 * 1. **The live turn is appended, not merged.** While a turn is streaming on this
 *    conversation, the stored messages are shown as they are and the in-flight turn is
 *    rendered underneath by `TurnView`. Splicing a fake assistant row into the history
 *    would mean the screen shows a message that is not in the database yet.
 * 2. **A finished turn triggers a reload.** The core writes the assistant message when
 *    the turn completes, so the thread is re-read then — that is the only way what is
 *    on screen is what was stored.
 * 3. **Deleting says what will be lost, then verifies.** `remove` returns `{ok,
 *    message}` and the message is shown; the list is reloaded rather than optimistically
 *    spliced, so a failed delete cannot leave the row hidden.
 */

import { useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { TurnView } from "../components/TurnView";
import {
  ActionResult,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  IconButton,
  ListRow,
  LoadingState,
  Panel,
  StatusPill,
  TextInput,
  clockTime,
  relativeTime,
  stateOf,
} from "../components/primitives";
import { api, type Message } from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useNexus } from "../state/nexus";
import { useTurn } from "../state/turn";

export function ConversationsScreen({ focus }: { focus: string | null }) {
  const { go, link } = useNexus();
  const { turn, running } = useTurn();
  const [archived, setArchived] = useState(false);

  const list = useAsync(() => api.conversations.list(archived), [archived]);

  const create = useAction(async () => {
    const result = await api.conversations.create();
    list.reload();
    go("conversations", result.conversation.id);
    return `Created "${result.conversation.title}".`;
  });

  // The core writes the assistant message when the turn ends, so the thread is
  // re-read at that moment rather than on a timer.
  const finishedId = turn?.finished ? turn.finished.message_id : "";
  useEffect(() => {
    if (finishedId) list.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedId]);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 lg:grid-cols-[minmax(220px,320px)_1fr]">
      <Panel
        title="THREADS"
        meta={list.data ? `${list.data.conversations.length}` : undefined}
        className="min-h-0"
        bodyClassName="flex min-h-0 flex-1 flex-col"
        actions={
          <>
            <IconButton
              name={archived ? "conversations" : "trash"}
              label={archived ? "Show active conversations" : "Show archived conversations"}
              onClick={() => setArchived((current) => !current)}
            />
            <IconButton
              name="plus"
              label="Start a new conversation"
              onClick={create.run}
              busy={create.busy}
              disabled={!link.connected}
              reason="The core is not reachable, so it cannot create one."
            />
          </>
        }
      >
        <div className="min-h-0 flex-1 overflow-y-auto">
          {list.state === "loading" ? (
            <LoadingState what="conversations" />
          ) : !list.data ? (
            <div className="p-2">
              <ErrorState
                what={list.error ?? "The conversation list did not load."}
                did="Nothing is listed. An empty list would claim you have no conversations."
                next={list.offline ? "Start the core, then retry." : list.detail || "Retry."}
                offline={list.offline}
                onRetry={list.reload}
                compact
              />
            </div>
          ) : list.data.conversations.length === 0 ? (
            <EmptyState
              icon="conversations"
              title={archived ? "Nothing archived" : "No conversations yet"}
              detail={
                archived
                  ? "Archived threads are kept, not deleted. None have been archived."
                  : "Type an instruction below and NEXUS will start one."
              }
            />
          ) : (
            <ul>
              {list.data.conversations.map((item) => (
                <li key={item.id}>
                  <ListRow selected={item.id === focus} onClick={() => go("conversations", item.id)}>
                    <p className="truncate text-xs text-ink">{item.title}</p>
                    <p className="mt-0.5 font-mono text-[10px] text-ink-faint">
                      {item.message_count !== undefined && `${item.message_count} messages · `}
                      {relativeTime(item.updated_at)}
                      {item.archived ? " · archived" : ""}
                    </p>
                  </ListRow>
                </li>
              ))}
            </ul>
          )}
        </div>
        <ActionResult done={create.done} failed={create.failed} onClear={create.clear} />
      </Panel>

      {focus ? (
        <Thread id={focus} onChanged={list.reload} live={running ? turn : null} />
      ) : (
        <Panel title="THREAD" className="min-h-0">
          <EmptyState
            icon="conversations"
            title="No thread selected"
            detail="Pick one on the left. Each message shows the model that produced it and the tools it ran."
          />
        </Panel>
      )}
    </div>
  );
}

/** One thread: its stored messages, then the turn in flight if it belongs here. */
function Thread({
  id,
  onChanged,
  live,
}: {
  id: string;
  onChanged: () => void;
  live: ReturnType<typeof useTurn>["turn"];
}) {
  const { go } = useNexus();
  const thread = useAsync(() => api.conversations.get(id), [id]);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const rename = useAction(
    async () => {
      const next = title.trim();
      if (!next) throw new Error("A title cannot be empty.");
      await api.conversations.rename(id, next);
      thread.reload();
      onChanged();
      setRenaming(false);
      return "Renamed.";
    },
    { onDone: () => setTitle("") },
  );

  const archive = useAction(async () => {
    const current = thread.data?.conversation.archived ? true : false;
    const result = await api.conversations.archive(id, !current);
    thread.reload();
    onChanged();
    return result.conversation.archived ? "Archived." : "Restored.";
  });

  const remove = useAction(async () => {
    const result = await api.conversations.remove(id);
    setConfirmDelete(false);
    onChanged();
    go("conversations", null);
    return result.message || "Deleted.";
  });

  if (thread.state === "loading") {
    return (
      <Panel title="THREAD" className="min-h-0">
        <LoadingState what="this conversation" />
      </Panel>
    );
  }

  if (!thread.data) {
    return (
      <Panel title="THREAD" className="min-h-0">
        <div className="p-2">
          <ErrorState
            what={thread.error ?? "This conversation did not load."}
            did="Nothing is shown. A blank thread would look like a conversation with no messages."
            next={thread.offline ? "Start the core, then retry." : thread.detail || "Retry."}
            offline={thread.offline}
            onRetry={thread.reload}
            compact
          />
        </div>
      </Panel>
    );
  }

  const { conversation, messages } = thread.data;
  const showLive = live !== null && live.conversationId === id;

  return (
    <Panel
      title={conversation.title.toUpperCase()}
      meta={`${messages.length} messages · updated ${clockTime(conversation.updated_at)}`}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      active={showLive}
      actions={
        <>
          <IconButton
            name="edit"
            label="Rename this conversation"
            onClick={() => {
              setTitle(conversation.title);
              setRenaming(true);
            }}
          />
          <IconButton
            name={conversation.archived ? "refresh" : "pin"}
            label={conversation.archived ? "Restore from the archive" : "Archive this conversation"}
            onClick={archive.run}
            busy={archive.busy}
          />
          <IconButton
            name="trash"
            label="Delete this conversation"
            tone="danger"
            onClick={() => setConfirmDelete(true)}
          />
        </>
      }
    >
      {renaming && (
        <div className="flex items-center gap-2 border-b border-line px-3 py-2">
          <TextInput
            value={title}
            onChange={setTitle}
            autoFocus
            placeholder="Conversation title"
            onEnter={rename.run}
          />
          <Button size="sm" tone="accent" onClick={rename.run} busy={rename.busy}>
            Save
          </Button>
          <Button size="sm" tone="ghost" onClick={() => setRenaming(false)}>
            Cancel
          </Button>
        </div>
      )}
      <ActionResult done={rename.failed ? "" : rename.done} failed={rename.failed} onClear={rename.clear} />
      <ActionResult done={archive.done} failed={archive.failed} onClear={archive.clear} />

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {messages.length === 0 && !showLive && (
          <EmptyState
            icon="conversations"
            title="No messages"
            detail="This conversation exists but nothing has been said in it. Type below to start."
          />
        )}
        {messages.map((message) => (
          <MessageBlock key={message.id} message={message} />
        ))}
        {showLive && live && (
          <div className="chamfer-sm border border-accent/30 bg-accent/4 p-2">
            <span className="label label-accent">In flight — not stored yet</span>
            <div className="mt-2">
              <TurnView turn={live} />
            </div>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete conversation"
        consequence={`"${conversation.title}" and its ${messages.length} messages will be removed from the local database. Memories extracted from it are kept — they live in their own table. This cannot be undone.`}
        confirmLabel="Delete"
        onConfirm={remove.run}
        onCancel={() => setConfirmDelete(false)}
        busy={remove.busy}
        failed={remove.failed}
      />
    </Panel>
  );
}

/**
 * One stored message.
 *
 * `provider`, `model` and `capability` are columns on the row, so an assistant message
 * can name what produced it. They are nullable — a message written before routing was
 * recorded has none — and the absence is rendered as absence rather than as "unknown
 * model", which would imply the core lost the information.
 */
function MessageBlock({ message }: { message: Message }) {
  const mine = message.role === "user";
  const attribution = [message.provider, message.model].filter(Boolean).join(" · ");
  return (
    <div
      className={[
        "chamfer-sm border px-2.5 py-2",
        mine ? "border-line-bright bg-raised" : "border-accent/25 bg-accent/4",
      ].join(" ")}
    >
      <div className="flex items-center gap-2">
        <span className={`label ${mine ? "" : "label-accent"}`}>{mine ? "You" : "NEXUS"}</span>
        {message.status && message.status !== "complete" && (
          <StatusPill state={stateOf(message.status)} size="sm">
            {message.status.replace(/_/g, " ")}
          </StatusPill>
        )}
        <span className="ml-auto shrink-0 font-mono text-[10px] text-ink-faint">
          {clockTime(message.created_at)}
        </span>
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">
        {message.content || (
          <span className="text-ink-faint">
            This message has no text. It was stored empty — usually a turn that failed
            before the model answered.
          </span>
        )}
      </p>
      {!mine && (attribution || message.capability) && (
        <p className="mt-1 flex items-center gap-1.5 font-mono text-[10px] text-ink-faint">
          <Icon name="network" size={10} />
          {attribution || "model not recorded"}
          {message.capability && ` · ${message.capability}`}
        </p>
      )}
    </div>
  );
}



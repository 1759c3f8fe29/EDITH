/**
 * The command bar (§64: "Type instruction or command for NEXUS...").
 *
 * One input for everything, because that is the product: there is no separate "chat"
 * and "command" mode, the router decides from the words. So this file's job is to get
 * a message, a response mode and any attachments to `/api/chat` and then get out of
 * the way.
 *
 * The four decisions here:
 *
 * 1. **Recording is real or the button is absent.** `MediaRecorder` and
 *    `getUserMedia` are feature-detected, and a browser without them gets no mic
 *    button and a sentence saying why — not a button that fails on click (§56). The
 *    blob goes to `/api/voice/transcribe`, which is a real route.
 *
 *    What happens to the transcript depends on `voice_enabled`, and the difference is
 *    the whole of §9. With voice off, speaking is *dictation*: the words land in the
 *    box as editable text, because a wrong transcription that auto-sends is a command
 *    the user never gave. With voice on, speaking is *commanding*: the words are sent,
 *    because a user who is across the room cannot proofread a textarea, and a
 *    voice-first assistant that requires a keypress to act on speech is not one. The
 *    transcript stays on screen either way, so a mishearing is attributable.
 *
 * 2. **Stop and Cancel are different buttons with different words.** While a turn
 *    runs, "Stop" cancels it in the core (`/api/chat/cancel`, cooperative, at the next
 *    checkpoint — anything already done is not undone, and the button says so).
 *    Closing the stream is a separate, quieter control.
 *
 * 3. **Attachments are bytes the user picked, never a path.** The route accepts a
 *    filesystem path only inside the screenshot directory, precisely so the UI cannot
 *    turn a text box into an unpermissioned file read. This composer therefore only
 *    ever sends base64 from a file picker.
 *
 * 4. **Enter sends, Shift+Enter breaks the line.** A textarea rather than an input,
 *    because a multi-line instruction is normal and a single-line box that eats
 *    newlines silently truncates intent.
 *
 * 5. **Hands-free is the same path with a different trigger.** When `wake_word_enabled`
 *    is on, `useWake` holds the microphone, measures the room locally, and only sends a
 *    segment that crossed the room's own noise floor to be transcribed. If the wake
 *    word is at the head of it, what follows goes through this same `submit` — one code
 *    path, so a spoken command and a typed one cannot diverge. It disarms itself while
 *    a manual recording is running and while NEXUS is speaking, because a microphone
 *    that can hear the answer can be commanded by it.
 *
 * 6. **With speech on, the turn takes the streaming route.** `submit` sets `speak`,
 *    which sends the turn through `/api/voice/converse`: the answer is spoken sentence
 *    by sentence as the model writes it, rather than synthesised in one request after
 *    the last token. Same composer, same modes, same attachments — the only difference
 *    is where the audio comes from, and how soon.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import { Button, IconButton, Select, StatusPill } from "../components/primitives";
import { api, type ModelSpec, ApiError } from "../lib/api";
import { useWake, type WakeState } from "../lib/useWake";
import { useNexus } from "../state/nexus";
import { useTurn, type SendInput } from "../state/turn";
import { useVoice } from "../state/voice";

/** The core's four response modes — `persona.MODES`, nothing invented. */
const MODES = [
  { value: "quick", label: "QUICK" },
  { value: "detailed", label: "DETAILED" },
  { value: "silent", label: "SILENT" },
  { value: "expert", label: "EXPERT" },
] as const;

type Mode = (typeof MODES)[number]["value"];

const MODE_NOTE: Record<Mode, string> = {
  quick: "Short answers.",
  detailed: "Full explanations.",
  silent: "Acts and reports.",
  expert: "Strongest model, slower.",
};

const MODE_KEY = "nexus.mode";

/** The route's own ceilings, so the refusal happens here with a sentence. */
const MAX_ATTACHMENTS = 4;
const MAX_ATTACHMENT_BYTES = 8_000_000;

type Attachment = { data: string; media_type: string; name: string; bytes: number };

const canRecord = (): boolean =>
  typeof window !== "undefined" &&
  typeof window.MediaRecorder !== "undefined" &&
  Boolean(navigator.mediaDevices?.getUserMedia);

export function CommandBar({ conversationId }: { conversationId: string | null }) {
  const { link } = useNexus();
  const { send, cancel, running, turn } = useTurn();
  const voice = useVoice();

  const [text, setText] = useState("");
  const [mode, setMode] = useState<Mode>(() => storedMode());
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [problem, setProblem] = useState("");

  const box = useRef<HTMLTextAreaElement | null>(null);
  const picker = useRef<HTMLInputElement | null>(null);

  const recorder = useRecorder();
  const [handsFreePaused, setHandsFreePaused] = useState(false);

  // Model selection state
  const [selectedModel, setSelectedModel] = useState<string>(""); // empty = auto (router)
  const [availableModels, setAvailableModels] = useState<ModelSpec[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);

  useEffect(() => {
    try {
      window.localStorage.setItem(MODE_KEY, mode);
    } catch {
      /* the mode still applies to this session */
    }
  }, [mode]);

  // Load available models for the model selector - only working models with streaming
  useEffect(() => {
    let mounted = true;
    const loadModels = async () => {
      setModelsLoading(true);
      try {
        const result = await api.models.overview();
        const models: ModelSpec[] = [];
        const blockedModels = new Set(Object.keys(result.probes.blocked));
        for (const provider of result.providers) {
          if (provider.configured) {
            for (const model of provider.models) {
              // Only include models that support streaming and are not blocked
              if (model.supports_streaming && !blockedModels.has(model.key)) {
                models.push({ ...model, provider: provider.name });
              }
            }
          }
        }
        // Add Groq models (only those with tools and not blocked)
        for (const provider of result.providers) {
          if (provider.id === "groq" && provider.configured) {
            for (const model of provider.models) {
              if (
                model.supports_streaming &&
                model.supports_tools &&
                !blockedModels.has(model.key)
              ) {
                models.push({ ...model, provider: "Groq" });
              }
            }
          }
        }
        if (mounted) {
          setAvailableModels(models);
        }
      } catch (e) {
        console.warn("Failed to load models:", e);
      } finally {
        if (mounted) {
          setModelsLoading(false);
        }
      }
    };
    loadModels();
    return () => { mounted = false; };
  }, []);

  const submit = (spoken?: string) => {
    const message = (spoken ?? text).trim();
    if (!message || running) return;
    const input: SendInput = {
      message,
      conversationId: conversationId ?? turn?.conversationId ?? null,
      mode,
      model: selectedModel || null, // null = use router
      attachments: attachments.map(({ data, media_type }) => ({ data, media_type })),
      // With speech on, the turn goes through `/api/voice/converse`, which speaks each
      // sentence as it is written instead of synthesising the finished answer. The
      // setting is read here rather than inside the provider because it is a property
      // of *this* send: an answer that is going to be read has no reason to pay for a
      // TTS model to be routed.
      speak: voice.ttsEnabled,
      voice: voice.ttsVoice || null,
      language: voice.inputLanguage || null,
    };
    send(input);
    setText("");
    setAttachments([]);
    setProblem("");
  };

  /**
   * Hands-free listening.
   *
   * Armed only when nothing else holds the microphone, NEXUS is not talking, and the
   * core is reachable — a transcription needs all three, and an indicator that claims
   * to be listening while any of them is false is the §60 failure.
   *
   * A command heard while a turn is already running is put in the box instead of being
   * dropped: the core allows one turn per conversation, and words spoken into a busy
   * assistant are still words the user said.
   */
  const wake = useWake({
    enabled:
      voice.wakeWordEnabled &&
      !handsFreePaused &&
      !recorder.recording &&
      !recorder.transcribing &&
      !voice.speaking &&
      !voice.synthesizing &&
      link.connected,
    wakeWord: voice.wakeWord,
    language: voice.inputLanguage || undefined,
    onCommand: (heard) => {
      if (running) {
        setText((current) => (current ? `${current} ${heard}` : heard));
        setProblem(
          `Heard "${heard}" while NEXUS was still working. It is in the box — press Send when the current turn ends.`,
        );
        return;
      }
      setText(heard);
      submit(heard);
    },
  });

  // The sphere on NEXUS CORE draws LISTENING from this. Reported rather than inferred:
  // true exactly while audio is being captured — a push-to-talk recording, or a phrase
  // hands-free is in the middle of. Not while hands-free is merely armed: the mic is
  // open then, but nothing is being kept, and a LISTENING state that never goes out
  // would hide THINKING behind it for the rest of the session.
  useEffect(() => {
    voice.setListening(recorder.recording || wake.state === "hearing");
  }, [recorder.recording, wake.state, voice.setListening]);

  useEffect(() => () => voice.setListening(false), [voice.setListening]);

  const attach = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setProblem("");
    const room = MAX_ATTACHMENTS - attachments.length;
    if (room <= 0) {
      setProblem(`The core accepts ${MAX_ATTACHMENTS} attachments per message.`);
      return;
    }
    const accepted: Attachment[] = [];
    for (const file of Array.from(files).slice(0, room)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setProblem(
          `${file.name} is ${(file.size / 1_000_000).toFixed(1)} MB. The core's limit is 8 MB per attachment.`,
        );
        continue;
      }
      try {
        accepted.push({
          data: await base64Of(file),
          media_type: file.type || "application/octet-stream",
          name: file.name,
          bytes: file.size,
        });
      } catch {
        setProblem(`${file.name} could not be read.`);
      }
    }
    if (accepted.length > 0) setAttachments((current) => [...current, ...accepted]);
  };

  const transcribe = async () => {
    setProblem("");
    if (recorder.recording) {
      const blob = await recorder.stop();
      if (!blob) {
        setProblem("Nothing was recorded.");
        return;
      }
      try {
        const result = await api.voice.transcribe(
          blob,
          "recording.webm",
          voice.inputLanguage || undefined,
        );
        if (result.empty || !result.text.trim()) {
          setProblem(result.message || "No speech was found in that recording.");
          return;
        }
        const heard = result.text.trim();
        if (voice.voiceEnabled && !running) {
          // Voice-first: spoken words are a command, so they go. The text is put in
          // the box first so what was heard is on screen even as it is sent — and if
          // a turn is already running the send is refused, which leaves the words
          // there to be edited rather than silently dropped.
          setText(heard);
          submit(heard);
          return;
        }
        // Dictation: inserted, not sent. A transcription the user has not read is not
        // a command they gave — and the model is named so a bad transcript is
        // attributable.
        setText((current) => (current ? `${current} ${heard}` : heard));
        box.current?.focus();
      } catch (cause) {
        setProblem(
          cause instanceof ApiError
            ? cause.detail
              ? `${cause.message} ${cause.detail}`
              : cause.message
            : "The transcription failed.",
        );
      }
      return;
    }
    const started = await recorder.start();
    if (!started) setProblem(recorder.problem || "The microphone could not be opened.");
  };

  const offline = !link.connected;
  const handsFree = handsFreeSentence({
    state: wake.state,
    word: voice.wakeWord,
    paused: handsFreePaused,
    busy: recorder.recording || recorder.transcribing,
    talking: voice.speaking || voice.synthesizing,
    offline,
    heard: wake.lastHeard,
    ignored: wake.ignored,
    problem: wake.problem,
  });

  return (
    <div className="shrink-0 border-t border-line bg-panel">
      {attachments.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-line/60 px-3 py-1.5">
          {attachments.map((item, index) => (
            <span
              key={`${item.name}-${index}`}
              className="chamfer-sm flex items-center gap-1.5 border border-line-bright bg-raised px-1.5 py-0.5"
            >
              <Icon name="attach" size={11} className="text-ink-dim" />
              <span className="max-w-[180px] truncate font-mono text-[10px] text-ink-soft">
                {item.name}
              </span>
              <span className="font-mono text-[10px] text-ink-faint">
                {(item.bytes / 1000).toFixed(0)}k
              </span>
              <IconButton
                name="close"
                label={`Remove ${item.name}`}
                size={11}
                onClick={() => setAttachments((current) => current.filter((_, at) => at !== index))}
              />
            </span>
          ))}
        </div>
      )}

      {problem && (
        <p className="border-b border-danger/30 bg-danger/8 px-3 py-1.5 text-[11px] text-danger">
          {problem}
        </p>
      )}

      {recorder.recording && (
        <p className="flex items-center gap-2 border-b border-accent/30 bg-accent/8 px-3 py-1.5 text-[11px] text-accent">
          <Icon name="mic" size={12} />
          Recording. Press the microphone again to stop
          {voice.voiceEnabled ? " — what you said is sent." : " and transcribe."}
        </p>
      )}

      {voice.wakeWordEnabled && (
        <div
          className={`flex items-center gap-2 border-b px-3 py-1.5 text-[11px] ${
            handsFree.tone === "warn"
              ? "border-warn/30 bg-warn/8 text-warn"
              : handsFree.tone === "accent"
                ? "border-accent/30 bg-accent/8 text-accent"
                : "border-line/60 text-ink-dim"
          }`}
        >
          <Icon name="wave" size={12} className="shrink-0" />
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider">
            {handsFree.label}
          </span>
          <span className="min-w-0 flex-1 truncate">{handsFree.text}</span>

          {wake.remaining <= 5 && wake.state !== "off" && wake.state !== "stopped" && (
            <span className="shrink-0 font-mono text-[10px] text-ink-faint">
              {wake.remaining} left this minute
            </span>
          )}

          <Button
            size="sm"
            icon={handsFreePaused || wake.state === "stopped" ? "play" : "pause"}
            onClick={() => {
              if (handsFreePaused) {
                setHandsFreePaused(false);
                return;
              }
              if (wake.state === "stopped") {
                wake.resume();
                return;
              }
              setHandsFreePaused(true);
            }}
          >
            {handsFreePaused || wake.state === "stopped" ? "Resume" : "Pause"}
          </Button>
        </div>
      )}

      <div className="flex items-end gap-2 px-3 py-2">
        <textarea
          ref={box}
          value={text}
          rows={1}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          onFocus={() => voice.clearProblem()}
          placeholder="Type instruction or command for NEXUS..."
          aria-label="Instruction for NEXUS"
          className="max-h-32 min-h-8 flex-1 resize-y bg-transparent py-1 font-mono text-xs leading-relaxed text-ink placeholder:text-ink-faint focus:outline-none"
        />

        <div className="flex shrink-0 items-center gap-1.5 pb-0.5">
          <div className="w-[104px]">
            <Select<Mode>
              value={mode}
              onChange={setMode}
              options={MODES.map((entry) => ({ value: entry.value, label: entry.label }))}
            />
          </div>

          <div className="w-[320px]">
            <Select<string>
              value={selectedModel}
              onChange={setSelectedModel}
              options={[
                { value: "", label: "AUTO (router)" },
                ...availableModels.map((m) => ({ value: `${m.provider}:${m.id}`, label: `${m.provider} · ${m.label}` })),
              ]}
              disabled={modelsLoading}
              renderOption={(opt) => {
                if (!opt.value) return opt.label;
                const model = availableModels.find((m) => `${m.provider}:${m.id}` === opt.value);
                if (!model) return opt.label;
                const badges: Array<{label: string; state: "ok" | "active" | "idle"}> = [];
                if (model.supports_tools) badges.push({ label: "tools", state: "ok" });
                if (model.supports_vision) badges.push({ label: "vision", state: "active" });
                if (model.supports_streaming) badges.push({ label: "stream", state: "ok" });
                if (model.free) badges.push({ label: "free", state: "idle" });
                return (
                  <span className="flex items-center gap-1.5">
                    <span>{opt.label}</span>
                    {badges.map((b) => (
                      <StatusPill key={b.label} state={b.state} size="sm">{b.label.toUpperCase()}</StatusPill>
                    ))}
                  </span>
                );
              }}
            />
          </div>

          <input
            ref={picker}
            type="file"
            multiple
            accept="image/*"
            className="sr-only"
            onChange={(event) => {
              void attach(event.target.files);
              event.target.value = "";
            }}
          />
          <IconButton
            name="attach"
            label="Attach an image to this message"
            onClick={() => picker.current?.click()}
            disabled={attachments.length >= MAX_ATTACHMENTS}
            reason={`The core accepts ${MAX_ATTACHMENTS} attachments per message.`}
          />

          {canRecord() ? (
            <IconButton
              name={recorder.recording ? "stop" : "mic"}
              label={recorder.recording ? "Stop recording and transcribe" : "Record and transcribe"}
              onClick={() => void transcribe()}
              busy={recorder.transcribing}
              tone={recorder.recording ? "danger" : "default"}
            />
          ) : (
            <span
              className="font-mono text-[10px] text-ink-faint"
              title="This shell has no MediaRecorder, so nothing can capture audio here. Typing works."
            >
              NO MIC
            </span>
          )}

          {running ? (
            <Button
              tone="danger"
              size="sm"
              icon="stop"
              onClick={() => void cancel()}
              title="Ask the core to stop at its next safe point. Anything already done is not undone."
            >
              Stop
            </Button>
          ) : (
            <Button
              tone="accent"
              size="sm"
              icon="send"
              onClick={() => submit()}
              disabled={text.trim().length === 0 || offline}
              reason={
                offline
                  ? "The core is not reachable, so there is nothing to send to."
                  : "Type something to send."
              }
            >
              Send
            </Button>
          )}
        </div>
      </div>

      <p className="px-3 pb-1.5 font-mono text-[10px] text-ink-faint">
        {MODE_NOTE[mode]}
        {voice.voiceEnabled && " Voice sends."}
        {voice.wakeWordEnabled && ` Say "${voice.wakeWord}".`}
        {voice.ttsEnabled && " Spoken answers."}
      </p>
    </div>
  );
}

/**
 * One line describing hands-free listening, and how loudly to say it.
 *
 * `state` alone is not enough: "off" has four different causes here and three of them
 * are temporary and expected. Saying which one it is turns a dead indicator into an
 * explanation — the difference §60 is about.
 */
function handsFreeSentence(now: {
  state: WakeState;
  word: string;
  paused: boolean;
  busy: boolean;
  talking: boolean;
  offline: boolean;
  heard: string;
  ignored: boolean;
  problem: string;
}): { label: string; text: string; tone: "dim" | "accent" | "warn" } {
  if (now.state === "stopped") {
    return { label: "HANDS-FREE", text: now.problem, tone: "warn" };
  }
  if (now.paused) {
    return {
      label: "PAUSED",
      text: "Hands-free is paused for this session. The setting is still on.",
      tone: "dim",
    };
  }
  if (now.offline) {
    return {
      label: "HANDS-FREE",
      text: "Waiting for the core. A phrase cannot be transcribed without it.",
      tone: "dim",
    };
  }
  if (now.busy) {
    return { label: "HANDS-FREE", text: "Standing by while the microphone is in use.", tone: "dim" };
  }
  if (now.talking) {
    return {
      label: "HANDS-FREE",
      text: "Standing by while NEXUS speaks, so it does not hear its own answer.",
      tone: "dim",
    };
  }
  switch (now.state) {
    case "off":
      return { label: "HANDS-FREE", text: "Not armed.", tone: "dim" };
    case "opening":
      return { label: "HANDS-FREE", text: "Opening the microphone.", tone: "dim" };
    case "calibrating":
      return { label: "CALIBRATING", text: "Measuring how quiet the room is.", tone: "dim" };
    case "hearing":
      return { label: "HEARING", text: "Capturing a phrase.", tone: "accent" };
    case "checking":
      return { label: "CHECKING", text: "Transcribing what was said.", tone: "accent" };
    case "waiting": {
      const said = now.heard
        ? now.ignored
          ? ` Last heard "${now.heard}" — not addressed to NEXUS.`
          : ` Last heard "${now.heard}".`
        : "";
      const problem = now.problem ? ` ${now.problem}` : "";
      return {
        label: "LISTENING FOR",
        text: `"${now.word}" — say it and then the instruction.${said}${problem}`,
        tone: "dim",
      };
    }
  }
}

function storedMode(): Mode {
  try {
    const saved = window.localStorage.getItem(MODE_KEY);
    if (saved && MODES.some((entry) => entry.value === saved)) return saved as Mode;
  } catch {
    /* fall through */
  }
  return "quick";
}

/** `FileReader` gives `data:<type>;base64,<data>`; the route wants only the data. */
function base64Of(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      resolve(comma === -1 ? result : result.slice(comma + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

/**
 * A microphone, as a hook.
 *
 * Every track is stopped on unmount and on stop, deliberately: a `MediaStream` left
 * open keeps the operating system's recording indicator lit, and an assistant that
 * appears to be listening when it is not is the §60 failure in its most alarming form.
 */
function useRecorder() {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [problem, setProblem] = useState("");

  const engine = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const stream = useRef<MediaStream | null>(null);

  const release = useCallback(() => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    engine.current = null;
  }, []);

  useEffect(() => release, [release]);

  const start = useCallback(async (): Promise<boolean> => {
    setProblem("");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.current = media;
      const recorder = new MediaRecorder(media);
      chunks.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.current.push(event.data);
      };
      recorder.start();
      engine.current = recorder;
      setRecording(true);
      return true;
    } catch (cause) {
      release();
      setProblem(
        cause instanceof DOMException && cause.name === "NotAllowedError"
          ? "Microphone access was refused by the browser."
          : "No microphone could be opened.",
      );
      return false;
    }
  }, [release]);

  const stop = useCallback(async (): Promise<Blob | null> => {
    const recorder = engine.current;
    if (!recorder) return null;
    setRecording(false);
    setTranscribing(true);
    const blob = await new Promise<Blob | null>((resolve) => {
      recorder.onstop = () => {
        const parts = chunks.current;
        resolve(parts.length > 0 ? new Blob(parts, { type: recorder.mimeType || "audio/webm" }) : null);
      };
      recorder.stop();
    });
    release();
    setTranscribing(false);
    return blob;
  }, [release]);

  return { recording, transcribing, problem, start, stop };
}

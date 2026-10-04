/**
 * VOICE - hearing, and speaking (§9, §12, §37, §61, §94).
 *
 * NEXUS is voice-first, so this screen is the front door rather than an accessory.
 * Three facts about the core shape everything on it, and each one would be a lie if
 * the screen were drawn the obvious way:
 *
 * 1. **`/api/voice/speak` returns two locations, not audio.** The route writes a WAV
 *    into the core's audio directory and reports both where it is on disk and the
 *    `/api/voice/audio/{name}` URL that serves it, so the same synthesis can be
 *    replayed without being paid for twice. The URL is what this window plays; the
 *    path is for a shell with filesystem access. Both are offered, because they are
 *    not the same act: playing here comes out of whatever this window's audio device
 *    is, and `aplay` comes out of this machine's speakers - which are different things
 *    when the window is not on this machine. The `aplay` route runs through the tool
 *    runtime, so it is a permissioned command like any other.
 * 2. **`empty: true` from `/transcribe` is a success, not a failure.** Silence or a
 *    muted microphone produces exactly that. It is reported as "the call worked and
 *    heard nothing", because rendering it as an error would send the user looking for
 *    a broken key, and rendering it as an empty transcript would look like a bug.
 * 3. **The two voice-list routes are split on purpose.** `/voices` reads a default and
 *    a boolean with no network call, so this screen opens with the core offline;
 *    `/voices/live` asks the provider and can answer 409 (no key) or 502 (provider
 *    down). The live list is only fetched when the user asks for it, and its failure
 *    never blanks the rest of the screen.
 *
 * The microphone is the browser's own `MediaRecorder`. Permission for it belongs to
 * the browser, not to NEXUS's permission engine, so a refusal here is reported in the
 * browser's terms - the fix is in site settings, and saying "NEXUS needs permission"
 * would send the user to the wrong place.
 */

import { useEffect, useRef, useState } from "react";

import {
  ActionResult,
  Button,
  DataRow,
  ErrorState,
  Field,
  LoadingState,
  Panel,
  Section,
  Select,
  StatusPill,
  TextArea,
  UnavailableState,
  clockTime,
  duration,
} from "../components/primitives";
import { api, type Speech, type Transcript, type Voice } from "../lib/api";
import { useAction, useAsync } from "../lib/useAsync";
import { useVoice } from "../state/voice";

/** The provider's own ceiling, mirrored so the counter can warn before the 413. */
const MAX_TTS_CHARS = 4_000;

/** `MAX_UPLOAD_BYTES` in `voice_routes.py`. The route checks it too; this is earlier. */
const MAX_UPLOAD_BYTES = 25_000_000;

export function VoiceScreen() {
  const config = useAsync(() => api.voice.voices(), []);
  const [voice, setVoice] = useState("");
  const [live, setLive] = useState<Voice[] | null>(null);

  const configured = config.data?.configured ?? false;
  const fallbackVoice = config.data?.default ?? "";

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-2 p-2 xl:grid-cols-2">
      <Hearing configured={configured} loading={config.state === "loading"} />
      <Speaking
        configured={configured}
        loading={config.state === "loading"}
        defaultVoice={fallbackVoice}
        voice={voice}
        onVoice={setVoice}
        live={live}
        onLive={setLive}
        configError={config.data ? null : config.error}
        offline={config.offline}
        onRetry={config.reload}
      />
    </div>
  );
}

/**
 * Hearing: record, or hand over a file, and get a transcript back.
 *
 * The microphone belongs to the browser. `getUserMedia` is what asks for it, the
 * refusal is the browser's, and the fix is in site settings - so a denial is worded
 * that way instead of pointing at PERMISSIONS, which governs what NEXUS does to the
 * machine and has nothing to say about this.
 *
 * A recording is sent when the user stops it. That is one deliberate network call per
 * press, never a stream: `/transcribe` is a POST that spends quota, and a screen that
 * transcribed continuously while open would bill for silence.
 */
function Hearing({ configured, loading }: { configured: boolean; loading: boolean }) {
  const { inputLanguage: language } = useVoice();
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [micError, setMicError] = useState("");
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [source, setSource] = useState("");

  const staged = useRef<Blob | null>(null);
  const name = useRef("recording.webm");
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const stream = useRef<MediaStream | null>(null);

  const send = useAction(async () => {
    const audio = staged.current;
    if (!audio) throw new Error("There is no recording to send.");
    if (audio.size === 0) {
      throw new Error("The recording came back empty, so there is nothing to transcribe.");
    }
    if (audio.size > MAX_UPLOAD_BYTES) {
      throw new Error(
        `That is ${(audio.size / 1_000_000).toFixed(1)} MB, over the 25 MB the core accepts. Record a shorter take.`,
      );
    }
    // The language comes from the `input_language` setting, the same one every other
    // recording in the app is sent with — so a transcript tested here matches one taken
    // from the instruction bar instead of quietly using a different configuration.
    const result = await api.voice.transcribe(
      audio,
      name.current,
      language || undefined,
    );
    setTranscript(result);
    // `empty` is a successful call. It is reported as one, in the core's own words.
    return result.empty
      ? (result.message ?? "That call worked and heard no speech.")
      : `${result.words} word${result.words === 1 ? "" : "s"}, heard by ${result.routed}.`;
  });

  // The tick that drives the elapsed counter, and the teardown that releases the
  // microphone if this screen is left while still recording - a live mic behind a
  // closed screen is exactly the kind of thing §37 forbids.
  useEffect(() => {
    if (!recording) return;
    const timer = window.setInterval(() => setSeconds((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [recording]);

  useEffect(() => {
    return () => {
      if (recorder.current?.state === "recording") recorder.current.stop();
      stream.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  async function start() {
    setMicError("");
    setTranscript(null);
    send.clear();
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setMicError(
        "This browser exposes no microphone API, so nothing can be recorded here. Uploading a file still works.",
      );
      return;
    }
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (cause) {
      const why = cause instanceof Error ? cause.name : "";
      setMicError(
        why === "NotAllowedError"
          ? "The browser refused microphone access. That permission is the browser's, not NEXUS's - allow it in this site's settings and press record again."
          : why === "NotFoundError"
            ? "No microphone was found on this machine."
            : `The microphone could not be opened${why ? ` (${why})` : ""}.`,
      );
      return;
    }
    stream.current = media;
    chunks.current = [];
    const rec = new MediaRecorder(media);
    recorder.current = rec;
    rec.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.current.push(event.data);
    };
    rec.onstop = () => {
      const type = rec.mimeType || "audio/webm";
      const blob = new Blob(chunks.current, { type });
      staged.current = blob;
      name.current = type.includes("ogg") ? "recording.ogg" : "recording.webm";
      setSource(`${(blob.size / 1000).toFixed(0)} kB recorded here`);
      media.getTracks().forEach((track) => track.stop());
      stream.current = null;
      setRecording(false);
      // Stopping is the press that means "send it". `run` is stable, so calling it
      // from this callback reaches the current action.
      send.run();
    };
    setSeconds(0);
    setRecording(true);
    rec.start();
  }

  function stop() {
    recorder.current?.stop();
  }

  return (
    <Panel
      title="HEARING"
      meta={recording ? `recording ${seconds}s` : undefined}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
      active={recording}
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {loading ? (
          <LoadingState what="the voice configuration" />
        ) : !configured ? (
          <UnavailableState
            what="Speech cannot run"
            why="No Mistral API key is configured. Transcription runs on Voxtral, and NVIDIA NIM serves no speech over HTTP at all - so with no Mistral key there is no second path to fall back to."
            fix="Add a Mistral key in SETTINGS, then reload this screen."
          />
        ) : (
          <>
            <div className="chamfer-sm border border-line-bright bg-raised px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-2">
                {recording ? (
                  <Button size="sm" tone="danger" icon="stop" onClick={stop}>
                    Stop and transcribe
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    tone="accent"
                    icon="mic"
                    onClick={start}
                    disabled={send.busy}
                    reason="A recording is being transcribed."
                  >
                    Record
                  </Button>
                )}
                <label className="chamfer-sm cursor-pointer border border-line px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-ink-soft hover:border-line-bright hover:text-ink">
                  Upload a file
                  <input
                    type="file"
                    accept="audio/*"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (!file) return;
                      staged.current = file;
                      name.current = file.name;
                      setTranscript(null);
                      setSource(`${file.name}, ${(file.size / 1_000_000).toFixed(2)} MB`);
                      send.run();
                      event.target.value = "";
                    }}
                  />
                </label>
                {recording && (
                  <StatusPill state="active" size="sm" pulse>
                    LIVE {seconds}s
                  </StatusPill>
                )}
                {send.busy && (
                  <StatusPill state="pending" size="sm" pulse>
                    TRANSCRIBING
                  </StatusPill>
                )}
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-ink-dim">
                {recording
                  ? "The microphone is open. Nothing is sent until you stop - this records locally, then makes one request."
                  : "Recording stops and sends in one press. Uploads accept anything the provider can read, up to 25 MB."}
              </p>
              {source && !recording && (
                <p className="mt-1 font-mono text-[10px] text-ink-faint">{source}</p>
              )}
            </div>

            {micError && (
              <div className="chamfer-sm border border-warn/40 bg-warn/8 px-2.5 py-2">
                <span className="label text-warn">The microphone</span>
                <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">{micError}</p>
              </div>
            )}

            <ActionResult done={send.done} failed={send.failed} onClear={send.clear} />

            {transcript && <TranscriptView transcript={transcript} />}
          </>
        )}
      </div>
    </Panel>
  );
}

/**
 * What came back.
 *
 * `empty: true` gets its own block rather than an empty quotation, because the two
 * mean different things and only one of them is worth re-recording for. Everything
 * else - which model heard it, how long it took, what language it decided on - is
 * printed because §26 wants the routing visible: a wrong transcript is only
 * actionable if the user knows which model produced it.
 */
function TranscriptView({ transcript }: { transcript: Transcript }) {
  return (
    <Section title="What it heard" meta={transcript.routed}>
      {transcript.empty ? (
        <p className="px-3 py-2 text-[11px] leading-relaxed text-ink-soft">
          {transcript.message ?? "No speech was found in that recording."} The call
          reached the model and completed - this is silence, a muted microphone or a
          device recording from the wrong input, not a failure.
        </p>
      ) : (
        <p className="whitespace-pre-wrap break-words px-3 py-2 text-xs leading-relaxed text-ink">
          {transcript.text}
        </p>
      )}
      <div className="flex flex-col border-t border-line/60">
        <DataRow label="Heard by" value={transcript.model} />
        <DataRow
          label="Language"
          value={transcript.language ?? "not reported"}
          state={transcript.language ? "idle" : "pending"}
        />
        <DataRow
          label="Audio length"
          value={
            transcript.duration_s === null
              ? "not reported"
              : duration(transcript.duration_s * 1000)
          }
        />
        <DataRow label="Words" value={String(transcript.words)} />
        <DataRow label="Took" value={duration(transcript.latency_ms)} />
      </div>
    </Section>
  );
}

/**
 * Speaking: synthesise, then play it - here, or on this machine's speakers.
 *
 * The synthesis is shown as what it is (a file, its size, its length) and offered two
 * ways to hear. In-window playback is an `<audio>` element pointed at
 * `/api/voice/audio/{name}`, a real route that serves only files this core wrote.
 * `aplay` through `terminal.run` is the other, and it is kept rather than replaced:
 * it plays on *this machine*, which is a different speaker from this window whenever
 * the window is somewhere else. It is a normal tool invocation, so it goes through the
 * permission engine - running a command is running a command, even when the command
 * is a speaker.
 *
 * A browser will refuse `play()` until the page has been interacted with. Since the
 * user pressed Synthesise to get here that gesture exists, but the rejection is still
 * reported rather than swallowed: a player that shows as playing over silence is the
 * §61 failure.
 */
function Speaking({
  configured,
  loading,
  defaultVoice,
  voice,
  onVoice,
  live,
  onLive,
  configError,
  offline,
  onRetry,
}: {
  configured: boolean;
  loading: boolean;
  defaultVoice: string;
  voice: string;
  onVoice: (next: string) => void;
  live: Voice[] | null;
  onLive: (voices: Voice[]) => void;
  configError: string | null;
  offline: boolean;
  onRetry: () => void;
}) {
  const [text, setText] = useState("");
  const [speech, setSpeech] = useState<Speech | null>(null);
  const [played, setPlayed] = useState("");
  const here = useRef<HTMLAudioElement | null>(null);

  const loadVoices = useAction(async () => {
    const result = await api.voice.voicesLive();
    onLive(result.voices);
    return result.count === 0
      ? "The account has no voices beyond the default."
      : `${result.count} voice${result.count === 1 ? "" : "s"} on this account.`;
  });

  const speak = useAction(async () => {
    const body = text.trim();
    if (!body) throw new Error("There is nothing to speak.");
    if (body.length > MAX_TTS_CHARS) {
      throw new Error(
        `That is ${body.length.toLocaleString()} characters; one request carries ${MAX_TTS_CHARS.toLocaleString()}. Speak it in parts.`,
      );
    }
    setPlayed("");
    const result = await api.voice.speak(body, voice || undefined);
    setSpeech(result);
    return `${(result.bytes / 1000).toFixed(0)} kB written by ${result.routed}. Press Play to hear it.`;
  });

  const hear = useAction(async () => {
    const target = speech;
    if (!target) throw new Error("Nothing has been synthesised yet.");
    if (!target.url) throw new Error("The core did not say where the audio is.");
    if (typeof Audio === "undefined") throw new Error("This shell has no audio element.");
    const audio = here.current ?? new Audio();
    here.current = audio;
    audio.pause();
    audio.src = target.url;
    try {
      await audio.play();
    } catch {
      // Autoplay policy, a missing output device, or a codec the engine will not
      // take. Named as the browser's refusal rather than as a synthesis failure,
      // because the file is real and downloadable at the same URL.
      throw new Error("This window would not start playback. The file itself is fine.");
    }
    return `Playing in this window${target.duration_s === null ? "" : ` (${duration(target.duration_s * 1000)})`}.`;
  });

  const play = useAction(async () => {
    const target = speech;
    if (!target) throw new Error("Nothing has been synthesised yet.");
    // Single-quoted for the shell, with any embedded quote closed and re-opened. The
    // path is the core's own, but a command built by string concatenation gets quoted
    // whether or not today's input needs it.
    const quoted = `'${target.path.replace(/'/g, "'\\''")}'`;
    const result = await api.tools.execute("terminal.run", {
      command: `aplay -q ${quoted}`,
      timeout_seconds: 120,
    });
    if (!result.ok) {
      throw new Error(result.error ?? "The player did not run.");
    }
    setPlayed(`Played at ${clockTime(new Date().toISOString())} with aplay.`);
    return result.summary || "It played on this machine's speakers.";
  });

  const over = text.length > MAX_TTS_CHARS;

  const copy = useAction(async () => {
    if (!speech) throw new Error("There is no path to copy yet.");
    if (!navigator.clipboard) throw new Error("This browser exposes no clipboard API.");
    await navigator.clipboard.writeText(speech.path);
    return "Path copied.";
  });

  const options = [
    { value: "", label: defaultVoice ? `Default - ${defaultVoice}` : "Default voice" },
    ...(live ?? []).map((entry) => ({
      value: entry.slug,
      label: entry.name ? `${entry.name} (${entry.slug})` : entry.slug,
    })),
  ];

  return (
    <Panel
      title="SPEAKING"
      meta={speech ? `${(speech.bytes / 1000).toFixed(0)} kB written` : undefined}
      className="min-h-0"
      bodyClassName="flex min-h-0 flex-1 flex-col"
    >
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {loading ? (
          <LoadingState what="the voice configuration" />
        ) : configError ? (
          <ErrorState
            what={configError}
            did="Nothing about voice is shown. Reporting it as unconfigured would be a guess."
            next={offline ? "Start the core, then retry." : "Retry."}
            offline={offline}
            onRetry={onRetry}
            compact
          />
        ) : !configured ? (
          <UnavailableState
            what="Nothing can be spoken"
            why="No Mistral API key is configured, and speech has no second provider - NVIDIA NIM serves no TTS over HTTP."
            fix="Add a Mistral key in SETTINGS, then reload."
          />
        ) : (
          <>
            <Field label="What to say" htmlFor="speak-text">
              <TextArea
                id="speak-text"
                value={text}
                onChange={setText}
                rows={5}
                placeholder="Type a sentence to hear it in the chosen voice."
              />
            </Field>
            <p
              className={`font-mono text-[10px] ${over ? "text-danger" : "text-ink-faint"}`}
            >
              {text.length.toLocaleString()} / {MAX_TTS_CHARS.toLocaleString()} characters
              {over ? " - the core will refuse this with a 413. Split it." : ""}
            </p>

            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-[200px] flex-1">
                <Field
                  label="Voice"
                  htmlFor="speak-voice"
                  hint={
                    live
                      ? "Read live from the account, so cloned voices appear here too."
                      : "Only the default is known until the account's list is read."
                  }
                >
                  <Select<string>
                    id="speak-voice"
                    value={voice}
                    onChange={onVoice}
                    options={options}
                  />
                </Field>
              </div>
              <Button
                size="sm"
                tone="ghost"
                icon="refresh"
                onClick={loadVoices.run}
                busy={loadVoices.busy}
                title="Asks the provider for this account's voices. One network call."
              >
                Read voices
              </Button>
            </div>
            <ActionResult done={loadVoices.done} failed={loadVoices.failed} onClear={loadVoices.clear} />

            <Button
              tone="accent"
              icon="wave"
              onClick={speak.run}
              busy={speak.busy}
              disabled={!text.trim() || over}
              reason={over ? "It is over the character limit." : "There is nothing to speak."}
              block
            >
              Synthesise
            </Button>
            <ActionResult done={speak.done} failed={speak.failed} onClear={speak.clear} />

            {speech ? <SpeechResult speech={speech} /> : null}

            <div className="chamfer-sm border border-line bg-base px-2.5 py-2">
              <span className="label">Two speakers, not one</span>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-soft">
                The core writes a WAV and returns where it is, so the same file can be
                replayed without paying for synthesis twice.{" "}
                <span className="font-mono">Play here</span> streams it from{" "}
                <span className="font-mono">/api/voice/audio/</span> into this window.{" "}
                <span className="font-mono">Play on this machine</span> runs{" "}
                <span className="font-mono">aplay</span> through the tool runtime, which
                asks for permission - running a command is running a command - and comes
                out of this computer's speakers even if this window is elsewhere. If
                terminal access is switched off in SETTINGS the engine refuses that one
                before asking, and the button says so rather than appearing to work.
              </p>
            </div>

            {speech && (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    tone="accent"
                    icon="volume"
                    onClick={hear.run}
                    busy={hear.busy}
                    disabled={!speech.url}
                    reason="The core did not say where the audio is."
                  >
                    Play here
                  </Button>
                  <Button size="sm" icon="play" onClick={play.run} busy={play.busy}>
                    Play on this machine
                  </Button>
                  <Button size="sm" tone="ghost" icon="attach" onClick={copy.run} busy={copy.busy}>
                    Copy the path
                  </Button>
                </div>
                {played && (
                  <p className="font-mono text-[10px] text-ink-faint">{played}</p>
                )}
                <ActionResult done={hear.done} failed={hear.failed} onClear={hear.clear} />
                <ActionResult done={play.done} failed={play.failed} onClear={play.clear} />
                <ActionResult done={copy.done} failed={copy.failed} onClear={copy.clear} />
              </>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

/** The file, stated as a file. Every field is one the route actually returned. */
function SpeechResult({ speech }: { speech: Speech }) {
  return (
    <Section title="What was written" meta={speech.routed}>
      <div className="flex flex-col">
        <DataRow
          label="File"
          value={speech.path}
          title="On this machine's disk, in the core's audio directory."
        />
        <DataRow
          label="Served at"
          value={speech.url ?? "not reported"}
          state={speech.url ? "idle" : "pending"}
          title="The route that streams those bytes to a browser."
        />
        <DataRow label="Voice" value={speech.voice} />
        <DataRow label="Model" value={speech.model} />
        <DataRow label="Format" value={speech.media_type} />
        <DataRow label="Size" value={`${(speech.bytes / 1000).toFixed(0)} kB`} />
        <DataRow
          label="Length"
          value={
            speech.duration_s === null ? "not reported" : duration(speech.duration_s * 1000)
          }
          state={speech.duration_s === null ? "pending" : "idle"}
        />
        <DataRow label="Synthesis took" value={duration(speech.latency_ms)} />
      </div>
    </Section>
  );
}

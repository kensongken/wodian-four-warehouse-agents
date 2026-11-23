import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import type { IncomingMessage } from "node:http";
import { basename, extname, join } from "node:path";

import { StudioMusicRightsSchema, type StudioContentType, type StudioMusicRights } from "@wearable/contracts";
import { type CustomerMaterialLibrary, type StudioEditorialMusicTrack, type StudioScope } from "@wearable/mobile-agent";
import Busboy from "busboy";

const MAX_MUSIC_BYTES = 64 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set([".mp3", ".m4a", ".wav", ".aac"]);

export type StudioMusicTrackRecord = Readonly<{
  assetId: string;
  digest: string;
  fileName: string;
  durationSeconds: number;
  mixPreference: StudioMusicMixPreference;
  audioProfile: StudioMusicAudioProfile;
  rights: StudioMusicRights;
  createdAt: string;
}>;

export type StudioMusicAudioProfile = Readonly<{
  analyzer: "local-audio-energy-v1";
  energy: "soft" | "balanced" | "strong";
  tempoBpm: number | null;
  highlightStartMs: number;
  waveform: readonly number[];
  reason: string;
}>;

export type StudioMusicMixPreference = Readonly<{
  trackStartMs: number;
  originalGainDb: number;
  musicGainDb: number;
}>;

export type StudioMusicPreferenceSummary = Readonly<{
  trackId: string;
  contentType: StudioContentType;
  selectedCount: number;
  clearedCount: number;
  mixAdjustedCount: number;
  preferenceScore: number;
}>;

type StudioMusicPreferenceEvent = Readonly<{
  id: string;
  trackId: string;
  briefId: string;
  contentType: StudioContentType;
  action: "selected" | "cleared" | "mix-adjusted";
  createdAt: string;
}>;

type StudioMusicFeedbackContext = Readonly<{
  briefId: string;
  contentType: StudioContentType;
}>;

const DEFAULT_MIX_PREFERENCE: StudioMusicMixPreference = Object.freeze({
  trackStartMs: 0, originalGainDb: 0, musicGainDb: -16,
});

type BriefMusicSelection = {
  selectedTrackId: string | null;
  mixPreferences: Record<string, StudioMusicMixPreference>;
};

type State = {
  schemaVersion: 1;
  selectedTrackId: string | null;
  tracks: StudioMusicTrackRecord[];
  briefSelections: Record<string, BriefMusicSelection>;
  preferenceEvents: StudioMusicPreferenceEvent[];
};

export class StudioMusicLibrary {
  private readonly statePath: string;
  private state: State = {
    schemaVersion: 1, selectedTrackId: null, tracks: [], briefSelections: {}, preferenceEvents: [],
  };

  private constructor(
    root: string,
    private readonly library: CustomerMaterialLibrary,
    private readonly scope: StudioScope,
    private readonly ffprobePath: string,
    private readonly ffmpegPath: string,
    private readonly now: () => string,
  ) {
    this.statePath = join(root, "music-library.json");
  }

  static async open(input: Readonly<{
    root: string; legacyRoot?: string; library: CustomerMaterialLibrary; scope: StudioScope; ffprobePath: string; ffmpegPath: string; now: () => string;
  }>): Promise<StudioMusicLibrary> {
    const result = new StudioMusicLibrary(input.root, input.library, input.scope, input.ffprobePath, input.ffmpegPath, input.now);
    await mkdir(input.root, { recursive: true, mode: 0o700 });
    try {
      const value = JSON.parse(await readFile(result.statePath, "utf8")) as State;
      if (value.schemaVersion !== 1 || !Array.isArray(value.tracks)) throw new Error("studio-music-state-invalid");
      result.state = {
        ...value,
        briefSelections: normalizeBriefSelections(value.briefSelections),
        preferenceEvents: normalizePreferenceEvents(value.preferenceEvents),
        tracks: value.tracks.map((track) => ({
          ...track,
          mixPreference: normalizeMixPreference(track.mixPreference, track.durationSeconds),
          audioProfile: normalizeAudioProfile(track.audioProfile),
        })),
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      if (input.legacyRoot) {
        try {
          const legacy = JSON.parse(await readFile(join(input.legacyRoot, "music-library.json"), "utf8")) as State;
          if (legacy.schemaVersion !== 1 || !Array.isArray(legacy.tracks)) throw new Error("studio-music-state-invalid");
          result.state = {
            schemaVersion: 1,
            selectedTrackId: null,
            briefSelections: {},
            preferenceEvents: [],
            tracks: legacy.tracks.map((track) => ({
              ...track,
              mixPreference: normalizeMixPreference(track.mixPreference, track.durationSeconds),
              audioProfile: normalizeAudioProfile(track.audioProfile),
            })),
          };
          await result.persist();
        } catch (legacyError) {
          if ((legacyError as NodeJS.ErrnoException).code !== "ENOENT") throw legacyError;
        }
      }
    }
    return result;
  }

  preferenceSummaries(contentType: StudioContentType): readonly StudioMusicPreferenceSummary[] {
    return this.state.tracks.map((track) => {
      const events = this.state.preferenceEvents.filter((event) =>
        event.trackId === track.assetId && event.contentType === contentType);
      const selectedCount = events.filter((event) => event.action === "selected").length;
      const clearedCount = events.filter((event) => event.action === "cleared").length;
      const mixAdjustedCount = events.filter((event) => event.action === "mix-adjusted").length;
      return {
        trackId: track.assetId,
        contentType,
        selectedCount,
        clearedCount,
        mixAdjustedCount,
        preferenceScore: Math.max(-8, Math.min(12, selectedCount * 3 + mixAdjustedCount - clearedCount * 2)),
      };
    });
  }

  list(briefId: string | null = null): Readonly<{ tracks: readonly StudioMusicTrackRecord[]; selectedTrackId: string | null }> {
    if (briefId === null) {
      return structuredClone({ tracks: this.state.tracks, selectedTrackId: this.state.selectedTrackId });
    }
    const selection = this.state.briefSelections[briefId];
    return structuredClone({
      selectedTrackId: selection?.selectedTrackId ?? null,
      tracks: this.state.tracks.map((track) => ({
        ...track,
        mixPreference: selection?.mixPreferences[track.assetId] ?? defaultMixPreference(track),
      })),
    });
  }

  async importMultipart(
    request: IncomingMessage,
    selectionNamespace: string | null = null,
  ): Promise<Readonly<{ track: StudioMusicTrackRecord; briefId: string | null }>> {
    const upload = await readMultipart(request, this.scope.actorId, this.now());
    const durationSeconds = await probeAudio(this.ffprobePath, upload.bytes, upload.fileName);
    const audioProfile = await analyzeStudioMusicAudio(this.ffmpegPath, upload.bytes, upload.fileName, durationSeconds);
    const asset = await this.library.importOriginal(this.scope.actorId, this.scope.customerId, {
      requestId: `studio-music:${this.scope.sessionId}:${upload.operationKey}`,
      bytes: upload.bytes,
      mediaKind: "audio",
      fileName: upload.fileName,
      capturedAt: null,
      sessionId: this.scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    const record: StudioMusicTrackRecord = {
      assetId: asset.id, digest: asset.digest, fileName: asset.fileName,
      durationSeconds,
      mixPreference: { ...DEFAULT_MIX_PREFERENCE, trackStartMs: audioProfile.highlightStartMs },
      audioProfile,
      rights: upload.rights, createdAt: this.now(),
    };
    const existing = this.state.tracks.find((item) => item.assetId === record.assetId);
    if (existing && JSON.stringify(existing.rights) !== JSON.stringify(record.rights)) {
      throw new Error("studio-music-rights-conflict");
    }
    if (!existing) this.state.tracks.push(record);
    const selectionKey = selectionNamespace === null
      ? upload.briefId
      : `${selectionNamespace}:${upload.briefId ?? "session-default"}`;
    if (selectionKey) {
      const selection = this.state.briefSelections[selectionKey] ?? { selectedTrackId: null, mixPreferences: {} };
      selection.selectedTrackId = record.assetId;
      selection.mixPreferences[record.assetId] = record.mixPreference;
      this.state.briefSelections[selectionKey] = selection;
    } else {
      this.state.selectedTrackId = record.assetId;
    }
    await this.persist();
    return { track: structuredClone(existing ?? record), briefId: upload.briefId };
  }

  async select(
    trackId: string | null,
    briefId: string | null = null,
    feedback: StudioMusicFeedbackContext | null = null,
  ): Promise<void> {
    if (trackId !== null && !this.state.tracks.some((item) => item.assetId === trackId)) {
      throw new Error("studio-music-track-not-found");
    }
    const previousTrackId = briefId === null
      ? this.state.selectedTrackId
      : this.state.briefSelections[briefId]?.selectedTrackId ?? null;
    if (briefId === null) this.state.selectedTrackId = trackId;
    else {
      const selection = this.state.briefSelections[briefId] ?? { selectedTrackId: null, mixPreferences: {} };
      selection.selectedTrackId = trackId;
      this.state.briefSelections[briefId] = selection;
    }
    if (feedback && previousTrackId !== trackId) {
      if (trackId) this.recordPreference(trackId, "selected", feedback);
      else if (previousTrackId) this.recordPreference(previousTrackId, "cleared", feedback);
    }
    await this.persist();
  }

  async updateMixPreference(
    trackId: string,
    value: unknown,
    briefId: string | null = null,
    feedback: StudioMusicFeedbackContext | null = null,
  ): Promise<void> {
    const index = this.state.tracks.findIndex((item) => item.assetId === trackId);
    if (index < 0) throw new Error("studio-music-track-not-found");
    const track = this.state.tracks[index]!;
    const mixPreference = normalizeMixPreference(value, track.durationSeconds, false);
    const previousMixPreference = briefId === null
      ? track.mixPreference
      : this.state.briefSelections[briefId]?.mixPreferences[trackId] ?? defaultMixPreference(track);
    if (briefId === null) {
      this.state.tracks[index] = { ...track, mixPreference };
      this.state.selectedTrackId = trackId;
    } else {
      const selection = this.state.briefSelections[briefId] ?? { selectedTrackId: null, mixPreferences: {} };
      selection.selectedTrackId = trackId;
      selection.mixPreferences[trackId] = mixPreference;
      this.state.briefSelections[briefId] = selection;
    }
    if (feedback && JSON.stringify(previousMixPreference) !== JSON.stringify(mixPreference)) {
      this.recordPreference(trackId, "mix-adjusted", feedback);
    }
    await this.persist();
  }

  async readTrack(trackId: string): Promise<Readonly<{ bytes: Buffer; fileName: string }>> {
    const track = this.state.tracks.find((item) => item.assetId === trackId);
    if (!track) throw new Error("studio-music-track-not-found");
    return {
      bytes: await this.library.readOriginal(this.scope.actorId, this.scope.customerId, track.assetId),
      fileName: track.fileName,
    };
  }

  async currentTrack(briefId: string | null = null): Promise<StudioEditorialMusicTrack | null> {
    const selectedTrackId = briefId === null
      ? this.state.selectedTrackId
      : this.state.briefSelections[briefId]?.selectedTrackId ?? null;
    const record = this.state.tracks.find((item) => item.assetId === selectedTrackId);
    if (!record) return null;
    const bytes = await this.library.readOriginal(this.scope.actorId, this.scope.customerId, record.assetId);
    const mixPreference = briefId === null
      ? record.mixPreference
      : this.state.briefSelections[briefId]?.mixPreferences[record.assetId] ?? defaultMixPreference(record);
    return { ...structuredClone(record), mixPreference: structuredClone(mixPreference), bytes };
  }

  private recordPreference(
    trackId: string,
    action: StudioMusicPreferenceEvent["action"],
    feedback: StudioMusicFeedbackContext,
  ): void {
    this.state.preferenceEvents.push({
      id: `music-feedback-${randomUUID()}`,
      trackId,
      briefId: feedback.briefId,
      contentType: feedback.contentType,
      action,
      createdAt: this.now(),
    });
    if (this.state.preferenceEvents.length > 1_000) {
      this.state.preferenceEvents.splice(0, this.state.preferenceEvents.length - 1_000);
    }
  }

  private async persist(): Promise<void> {
    const temporary = `${this.statePath}.${randomUUID()}.tmp`;
    try {
      await writeFile(temporary, `${JSON.stringify(this.state)}\n`, { flag: "wx", mode: 0o600 });
      await rename(temporary, this.statePath);
    } finally {
      await rm(temporary, { force: true });
    }
  }
}

function normalizeAudioProfile(value: unknown): StudioMusicAudioProfile {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { analyzer: "local-audio-energy-v1", energy: "balanced", tempoBpm: null,
      highlightStartMs: 0, waveform: [], reason: "旧配乐尚未重新运行本地音频分析" };
  }
  const profile = value as Partial<StudioMusicAudioProfile>;
  return {
    analyzer: "local-audio-energy-v1",
    energy: profile.energy === "soft" || profile.energy === "strong" ? profile.energy : "balanced",
    tempoBpm: typeof profile.tempoBpm === "number" && Number.isFinite(profile.tempoBpm) ? profile.tempoBpm : null,
    highlightStartMs: Number.isInteger(profile.highlightStartMs) && profile.highlightStartMs! >= 0 ? profile.highlightStartMs! : 0,
    waveform: Array.isArray(profile.waveform)
      ? profile.waveform.slice(0, 120).map((item) => Math.max(0, Math.min(1, Number(item) || 0))) : [],
    reason: typeof profile.reason === "string" && profile.reason.trim() ? profile.reason.trim() : "本地音频能量分析",
  };
}

function normalizeMixPreference(
  value: unknown,
  durationSeconds: number,
  allowDefault = true,
): StudioMusicMixPreference {
  if (value === undefined && allowDefault) return DEFAULT_MIX_PREFERENCE;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("studio-music-mix-preference-invalid");
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join(",") !== "musicGainDb,originalGainDb,trackStartMs") {
    throw new Error("studio-music-mix-preference-invalid");
  }
  const trackStartMs = Number(record.trackStartMs);
  const originalGainDb = Number(record.originalGainDb);
  const musicGainDb = Number(record.musicGainDb);
  if (!Number.isInteger(trackStartMs) || trackStartMs < 0 || trackStartMs >= durationSeconds * 1_000 ||
      !Number.isFinite(originalGainDb) || originalGainDb < -24 || originalGainDb > 6 ||
      !Number.isFinite(musicGainDb) || musicGainDb < -36 || musicGainDb > 0) {
    throw new Error("studio-music-mix-preference-invalid");
  }
  return { trackStartMs, originalGainDb, musicGainDb };
}

async function readMultipart(request: IncomingMessage, actorId: string, confirmedAt: string) {
  const header = request.headers["content-type"] ?? "";
  if (!/^multipart\/form-data\s*;/i.test(header)) throw new Error("studio-music-multipart-required");
  const parser = Busboy({ headers: request.headers, defParamCharset: "utf8", limits: { files: 1, fields: 5, fileSize: MAX_MUSIC_BYTES + 1 } });
  const fields = new Map<string, string>();
  const chunks: Buffer[] = [];
  let fileName = "";
  let failure: Error | null = null;
  parser.on("field", (name, value) => fields.set(name, value));
  parser.on("file", (name, stream, info) => {
    const normalized = basename(info.filename.normalize("NFC")).trim();
    if (name !== "file" || !ALLOWED_EXTENSIONS.has(extname(normalized).toLowerCase())) failure = new Error("studio-music-file-invalid");
    else fileName = normalized;
    stream.on("limit", () => { failure = new Error("studio-music-too-large"); });
    stream.on("data", (chunk: Buffer) => chunks.push(chunk));
  });
  await new Promise<void>((resolve, reject) => {
    parser.once("error", () => reject(new Error("studio-music-multipart-invalid")));
    parser.once("close", resolve);
    request.pipe(parser);
  });
  if (failure) throw failure;
  if (!fileName || chunks.length === 0) throw new Error("studio-music-file-required");
  const operationKey = String(fields.get("operationKey") ?? "").trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/.test(operationKey)) throw new Error("studio-music-operation-key-invalid");
  const rights = StudioMusicRightsSchema.parse({
    sourceKind: fields.get("sourceKind"), licenseNote: fields.get("licenseNote"),
    previewUseAllowed: true, publishUseAllowed: fields.get("publishUseAllowed") === "true",
    confirmedBy: actorId, confirmedAt,
  });
  const briefIdValue = String(fields.get("briefId") ?? "").trim();
  if (briefIdValue && !/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$/.test(briefIdValue)) {
    throw new Error("studio-music-brief-id-invalid");
  }
  return { bytes: Buffer.concat(chunks), fileName, operationKey, rights, briefId: briefIdValue || null };
}

function defaultMixPreference(track: StudioMusicTrackRecord): StudioMusicMixPreference {
  return {
    trackStartMs: track.audioProfile.highlightStartMs,
    originalGainDb: DEFAULT_MIX_PREFERENCE.originalGainDb,
    musicGainDb: DEFAULT_MIX_PREFERENCE.musicGainDb,
  };
}

function normalizeBriefSelections(value: unknown): Record<string, BriefMusicSelection> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).flatMap(([briefId, candidate]) => {
    if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) return [];
    const row = candidate as Partial<BriefMusicSelection>;
    const selectedTrackId = typeof row.selectedTrackId === "string" ? row.selectedTrackId : null;
    const mixPreferences = typeof row.mixPreferences === "object" && row.mixPreferences !== null && !Array.isArray(row.mixPreferences)
      ? row.mixPreferences as Record<string, StudioMusicMixPreference>
      : {};
    return [[briefId, { selectedTrackId, mixPreferences }]];
  }));
}

function normalizePreferenceEvents(value: unknown): StudioMusicPreferenceEvent[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-1_000).flatMap((candidate) => {
    if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) return [];
    const event = candidate as Partial<StudioMusicPreferenceEvent>;
    if (typeof event.id !== "string" || typeof event.trackId !== "string" ||
        typeof event.briefId !== "string" || typeof event.createdAt !== "string" ||
        !["single-product", "jewelry-making-process", "designer-viewpoint"].includes(String(event.contentType)) ||
        !["selected", "cleared", "mix-adjusted"].includes(String(event.action))) return [];
    return [event as StudioMusicPreferenceEvent];
  });
}

async function probeAudio(ffprobePath: string, bytes: Buffer, fileName: string): Promise<number> {
  const root = join(process.cwd(), ".studio-local", "music-probe");
  await mkdir(root, { recursive: true, mode: 0o700 });
  const path = join(root, `${randomUUID()}${extname(fileName)}`);
  try {
    await writeFile(path, bytes, { flag: "wx", mode: 0o600 });
    const output = await new Promise<string>((resolve, reject) => {
      const child = spawn(ffprobePath, ["-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration", "-of", "json", path], { stdio: ["ignore", "pipe", "pipe"] });
      let stdout = ""; child.stdout.on("data", (chunk) => { stdout += String(chunk); });
      child.once("error", () => reject(new Error("studio-music-probe-failed")));
      child.once("close", (code) => code === 0 ? resolve(stdout) : reject(new Error("studio-music-probe-failed")));
    });
    const parsed = JSON.parse(output) as { streams?: Array<{ codec_type?: string }>; format?: { duration?: string } };
    const duration = Number(parsed.format?.duration);
    if (!parsed.streams?.some((item) => item.codec_type === "audio") || !Number.isFinite(duration) || duration <= 0 || duration > 3600) {
      throw new Error("studio-music-audio-invalid");
    }
    return duration;
  } finally {
    await rm(path, { force: true });
  }
}

export async function analyzeStudioMusicAudio(
  ffmpegPath: string,
  bytes: Buffer,
  fileName: string,
  durationSeconds: number,
): Promise<StudioMusicAudioProfile> {
  const root = join(process.cwd(), ".studio-local", "music-analysis");
  await mkdir(root, { recursive: true, mode: 0o700 });
  const path = join(root, `${randomUUID()}${extname(fileName)}`);
  try {
    await writeFile(path, bytes, { flag: "wx", mode: 0o600 });
    const pcm = await new Promise<Buffer>((resolve, reject) => {
      const child = spawn(ffmpegPath, [
        "-hide_banner", "-loglevel", "error", "-i", path,
        "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1",
      ], { stdio: ["ignore", "pipe", "pipe"] });
      const chunks: Buffer[] = [];
      let byteLength = 0;
      child.stdout.on("data", (chunk: Buffer) => {
        byteLength += chunk.byteLength;
        if (byteLength <= 128 * 1024 * 1024) chunks.push(chunk);
        else child.kill("SIGKILL");
      });
      child.once("error", () => reject(new Error("studio-music-analysis-failed")));
      child.once("close", (code) => code === 0
        ? resolve(Buffer.concat(chunks))
        : reject(new Error("studio-music-analysis-failed")));
    });
    const samples = new Float32Array(pcm.buffer, pcm.byteOffset, Math.floor(pcm.byteLength / 4));
    const windowSamples = 800;
    const energies: number[] = [];
    for (let offset = 0; offset < samples.length; offset += windowSamples) {
      let squareSum = 0;
      const end = Math.min(samples.length, offset + windowSamples);
      for (let index = offset; index < end; index += 1) squareSum += samples[index]! * samples[index]!;
      energies.push(Math.sqrt(squareSum / Math.max(1, end - offset)));
    }
    const peak = Math.max(...energies, 0.000001);
    const mean = energies.reduce((total, value) => total + value, 0) / Math.max(1, energies.length);
    const waveform = downsampleWaveform(energies, 96, peak);
    const highlightWindow = Math.max(1, Math.min(60, energies.length));
    let bestStart = 0;
    let bestScore = -1;
    for (let start = 0; start <= energies.length - highlightWindow; start += 1) {
      const slice = energies.slice(start, start + highlightWindow);
      const average = slice.reduce((total, value) => total + value, 0) / slice.length;
      const score = average + Math.max(...slice) * 0.2;
      if (score > bestScore) { bestScore = score; bestStart = start; }
    }
    const highlightStartMs = Math.min(
      Math.max(0, Math.round(bestStart * 100)),
      Math.max(0, Math.floor(durationSeconds * 1_000) - 100),
    );
    const beatTimes = energies.flatMap((value, index) => {
      if (index < 1 || index >= energies.length - 1) return [];
      const threshold = Math.max(mean * 1.45, 0.015);
      return value >= threshold && value > energies[index - 1]! && value >= energies[index + 1]!
        ? [index * 100] : [];
    }).filter((time, index, values) => index === 0 || time - values[index - 1]! >= 320);
    const intervals = beatTimes.slice(1).map((time, index) => time - beatTimes[index]!)
      .filter((value) => value >= 333 && value <= 1_000).sort((a, b) => a - b);
    const medianInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)]! : null;
    const tempoBpm = medianInterval === null ? null : Math.round(60_000 / medianInterval);
    const energy = mean >= 0.22 ? "strong" : mean >= 0.07 ? "balanced" : "soft";
    const reason = `本地分析建议从 ${formatSeconds(highlightStartMs)} 秒附近进入；整体能量${energy === "strong" ? "强" : energy === "soft" ? "柔和" : "均衡"}${tempoBpm ? `，约 ${tempoBpm} BPM` : "，未取得稳定节拍"}`;
    return { analyzer: "local-audio-energy-v1", energy, tempoBpm, highlightStartMs, waveform, reason };
  } finally {
    await rm(path, { force: true });
  }
}

function downsampleWaveform(values: readonly number[], count: number, peak: number): number[] {
  if (!values.length) return [];
  const outputLength = Math.min(count, values.length);
  return Array.from({ length: outputLength }, (_unused, index) => {
    const start = Math.floor(index * values.length / outputLength);
    const end = Math.max(start + 1, Math.floor((index + 1) * values.length / outputLength));
    const average = values.slice(start, end).reduce((total, value) => total + value, 0) / Math.max(1, end - start);
    return Number(Math.min(1, average / peak).toFixed(3));
  });
}

function formatSeconds(milliseconds: number): string {
  return (milliseconds / 1_000).toFixed(1).replace(/\.0$/, "");
}

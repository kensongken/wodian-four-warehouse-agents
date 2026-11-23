import { execFile as execFileCallback } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

export type VoiceStyle = "professional" | "warm" | "direct";

export type VoicePreview = {
  audio: Uint8Array;
  contentType: "audio/mp4" | "audio/aiff";
  fileName: string;
  downloadable: boolean;
};

export class VoicePreviewError extends Error {
  constructor(readonly code: "CHINESE_FEMALE_VOICE_UNAVAILABLE" | "VOICE_GENERATION_FAILED") {
    super(code);
    this.name = "VoicePreviewError";
  }
}

type VoiceDependencies = {
  execute: (command: string, arguments_: string[]) => Promise<{ stdout: string }>;
  makeTemporaryDirectory: (prefix: string) => Promise<string>;
  readFile: (path: string) => Promise<Uint8Array>;
  removeDirectory: (path: string) => Promise<void>;
};

const execFile = promisify(execFileCallback);
const CHINESE_FEMALE_VOICES = ["Tingting", "Meijia"] as const;

const defaultDependencies: VoiceDependencies = {
  async execute(command, arguments_) {
    const { stdout } = await execFile(command, arguments_);
    return { stdout: String(stdout) };
  },
  makeTemporaryDirectory: (prefix) => mkdtemp(prefix),
  async readFile(path) {
    return new Uint8Array(await readFile(path));
  },
  removeDirectory: (path) => rm(path, { recursive: true, force: true }),
};

let dependencies: VoiceDependencies = { ...defaultDependencies };

export function __setVoiceDependenciesForTest(overrides: Partial<VoiceDependencies>) {
  dependencies = { ...defaultDependencies, ...overrides };
}

export function __resetVoiceDependenciesForTest() {
  dependencies = { ...defaultDependencies };
}

const rateFor = (style: VoiceStyle) => (style === "direct" ? "210" : "185");

function selectAllowedVoice(voices: string) {
  return voices
    .split(/\r?\n/)
    .map((line) => line.trim().split(/\s+/, 1)[0])
    .find((voice): voice is (typeof CHINESE_FEMALE_VOICES)[number] =>
      CHINESE_FEMALE_VOICES.includes(voice as (typeof CHINESE_FEMALE_VOICES)[number]),
    );
}

export async function synthesizeLocalPreview(text: string, style: VoiceStyle): Promise<VoicePreview> {
  let temporaryDirectory: string | undefined;

  try {
    const { stdout } = await dependencies.execute("say", ["-v", "?"]);
    const voice = selectAllowedVoice(stdout);
    if (voice === undefined) {
      throw new VoicePreviewError("CHINESE_FEMALE_VOICE_UNAVAILABLE");
    }

    temporaryDirectory = await dependencies.makeTemporaryDirectory(join(tmpdir(), "sales-voice-"));
    const aiffPath = join(temporaryDirectory, "preview.aiff");
    const m4aPath = join(temporaryDirectory, "preview.m4a");

    await dependencies.execute("say", ["-v", voice, "-r", rateFor(style), "-o", aiffPath, text]);

    try {
      await dependencies.execute("afconvert", ["-f", "m4af", "-d", "aac", aiffPath, m4aPath]);
      return {
        audio: await dependencies.readFile(m4aPath),
        contentType: "audio/mp4",
        fileName: "销售回复.m4a",
        downloadable: true,
      };
    } catch {
      return {
        audio: await dependencies.readFile(aiffPath),
        contentType: "audio/aiff",
        fileName: "销售回复.aiff",
        downloadable: false,
      };
    }
  } catch (error) {
    if (error instanceof VoicePreviewError) {
      throw error;
    }
    throw new VoicePreviewError("VOICE_GENERATION_FAILED");
  } finally {
    if (temporaryDirectory !== undefined) {
      try {
        await dependencies.removeDirectory(temporaryDirectory);
      } catch {
        // Cleanup is best-effort and must not expose or retain the spoken text.
      }
    }
  }
}

import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { basename, join, resolve } from "node:path";

import {
  HerWorldClipHandoffV1Schema,
  assertHerWorldClipHandoffFresh,
  type HerWorldClipHandoffV1,
} from "@wearable/contracts";

type Fetcher = (url: string, init: RequestInit) => Promise<Response>;
type IntakeOptions = Readonly<{
  /** Server-owned private directory. Never accept this value from a handoff. */
  root: string;
  allowedMediaOrigins: readonly string[];
  fetcher?: Fetcher;
  now?: () => Date;
  requestTimeoutMs?: number;
  maxMediaBytes?: number;
}>;

export type HerWorldStagedMedia = Readonly<{
  id: string;
  kind: "image" | "video" | "audio";
  mimeType: string;
  sha256: string;
  byteLength: number;
  relativePath: string;
  absolutePath: string;
}>;

export type HerWorldStagedClipJob = Readonly<{
  jobId: string;
  storyId: string;
  storyRevision: number;
  title: string;
  place: string;
  theme?: string | null;
  themeMaterials?: NonNullable<HerWorldClipHandoffV1["story"]["themeMaterials"]>;
  acts: HerWorldClipHandoffV1["story"]["acts"];
  output: HerWorldClipHandoffV1["output"];
  narration: HerWorldClipHandoffV1["narration"];
  media: readonly HerWorldStagedMedia[];
  publishClaimed: false;
}>;

const extensionByMime = Object.freeze<Record<string, string>>({
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "video/mp4": ".mp4",
  "video/quicktime": ".mov",
  "audio/mpeg": ".mp3",
  "audio/mp4": ".m4a",
  "audio/wav": ".wav",
});

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

async function ensurePrivateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("her-world-intake-directory-invalid");
  await chmod(path, 0o700);
}

export class HerWorldClipIntake {
  private readonly root: string;
  private readonly allowedMediaOrigins: ReadonlySet<string>;
  private readonly fetcher: Fetcher;
  private readonly now: () => Date;
  private readonly requestTimeoutMs: number;
  private readonly maxMediaBytes: number;

  constructor(options: IntakeOptions) {
    this.root = resolve(options.root);
    const origins = options.allowedMediaOrigins.map((value) => new URL(value).origin);
    if (origins.length === 0) throw new Error("her-world-media-origin-required");
    this.allowedMediaOrigins = new Set(origins);
    this.fetcher = options.fetcher ?? ((url, init) => fetch(url, init));
    this.now = options.now ?? (() => new Date());
    this.requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
    this.maxMediaBytes = options.maxMediaBytes ?? 250_000_000;
    if (!Number.isSafeInteger(this.requestTimeoutMs) || this.requestTimeoutMs < 1) throw new Error("her-world-request-timeout-invalid");
    if (!Number.isSafeInteger(this.maxMediaBytes) || this.maxMediaBytes < 1) throw new Error("her-world-max-media-bytes-invalid");
  }

  async stage(handoffValue: HerWorldClipHandoffV1, bearerToken: string): Promise<HerWorldStagedClipJob> {
    const handoff = HerWorldClipHandoffV1Schema.parse(handoffValue);
    assertHerWorldClipHandoffFresh(handoff, this.now());
    if (!bearerToken.trim()) throw new Error("her-world-job-token-required");
    const jobDirectory = join(this.root, handoff.jobId);
    await ensurePrivateDirectory(this.root);
    await ensurePrivateDirectory(jobDirectory);

    const staged: HerWorldStagedMedia[] = [];
    try {
      for (const media of [...handoff.media].sort((left, right) => left.order - right.order)) {
        staged.push(await this.stageOne(jobDirectory, media, bearerToken));
      }
    } catch (error) {
      await rm(jobDirectory, { recursive: true, force: true });
      throw error;
    }
    return {
      jobId: handoff.jobId,
      storyId: handoff.story.id,
      storyRevision: handoff.story.revision,
      title: handoff.story.title,
      place: handoff.story.place,
      theme: handoff.story.theme ?? null,
      themeMaterials: handoff.story.themeMaterials ?? [],
      acts: handoff.story.acts,
      output: handoff.output,
      narration: handoff.narration,
      media: staged,
      publishClaimed: false,
    };
  }

  private async stageOne(
    jobDirectory: string,
    media: HerWorldClipHandoffV1["media"][number],
    bearerToken: string,
  ): Promise<HerWorldStagedMedia> {
    const sourceUrl = new URL(media.fetch.url);
    if (sourceUrl.username || sourceUrl.password) throw new Error(`her-world-media-url-credentials-blocked:${media.id}`);
    if (!this.allowedMediaOrigins.has(sourceUrl.origin)) throw new Error(`her-world-media-origin-blocked:${media.id}`);
    if (media.byteLength > this.maxMediaBytes) throw new Error(`her-world-media-too-large:${media.id}`);
    const extension = extensionByMime[media.mimeType];
    if (!extension) throw new Error(`her-world-media-type-unsupported:${media.id}`);
    const fileName = `${media.order.toString().padStart(3, "0")}-${media.sha256.slice(0, 24)}${extension}`;
    if (fileName !== basename(fileName)) throw new Error("her-world-media-file-name-invalid");
    const finalPath = join(jobDirectory, fileName);
    const relativePath = `${basename(jobDirectory)}/${fileName}`;

    const existing = await this.readVerifiedExisting(finalPath, media.sha256, media.byteLength);
    if (existing) return { ...media, relativePath, absolutePath: finalPath };

    const temporaryPath = join(jobDirectory, `.download-${media.id}-${media.sha256.slice(0, 12)}.part`);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    let handle: Awaited<ReturnType<typeof open>> | null = null;
    try {
      const response = await this.fetcher(media.fetch.url, {
        method: "GET",
        headers: { authorization: `Bearer ${bearerToken}` },
        redirect: "error",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`her-world-media-fetch-failed:${media.id}:${response.status}`);
      const responseType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
      if (responseType !== media.mimeType) throw new Error(`her-world-media-type-mismatch:${media.id}`);
      const lengthHeader = response.headers.get("content-length");
      if (lengthHeader !== null) {
        const declaredLength = Number(lengthHeader);
        if (!Number.isSafeInteger(declaredLength) || declaredLength !== media.byteLength) {
          throw new Error(`her-world-media-length-mismatch:${media.id}`);
        }
      }
      if (!response.body) throw new Error(`her-world-media-body-missing:${media.id}`);

      handle = await open(temporaryPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o600);
      const reader = response.body.getReader();
      const hash = createHash("sha256");
      let received = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        received += value.byteLength;
        if (received > media.byteLength || received > this.maxMediaBytes) {
          await reader.cancel("media-too-large");
          throw new Error(`her-world-media-length-mismatch:${media.id}`);
        }
        hash.update(value);
        await handle.write(value);
      }
      if (received !== media.byteLength) throw new Error(`her-world-media-length-mismatch:${media.id}`);
      if (hash.digest("hex") !== media.sha256) throw new Error(`her-world-media-digest-mismatch:${media.id}`);
      await handle.sync();
      await handle.chmod(0o600);
      await handle.close();
      handle = null;
      await rename(temporaryPath, finalPath);
      await chmod(finalPath, 0o600);
    } catch (error) {
      if (handle) await handle.close().catch(() => undefined);
      await rm(temporaryPath, { force: true });
      throw error;
    } finally {
      clearTimeout(timer);
    }
    return { ...media, relativePath, absolutePath: finalPath };
  }

  private async readVerifiedExisting(path: string, expectedDigest: string, expectedLength: number): Promise<boolean> {
    try {
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size !== expectedLength) {
        throw new Error("her-world-existing-media-invalid");
      }
      const bytes = await readFile(path);
      if (sha256(bytes) !== expectedDigest) throw new Error("her-world-existing-media-digest-mismatch");
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
      throw error;
    }
  }
}

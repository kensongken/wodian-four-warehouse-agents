import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { chmod, link, lstat, mkdir, open, readdir, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { promisify } from "node:util";
import { execFile } from "node:child_process";
import { basename, extname, join, resolve, sep } from "node:path";

import {
  ExternalEditHandoffSchema,
  ExternalEditReturnCandidateSchema,
  type ExternalEditHandoff,
  type ExternalEditReturnCandidate,
} from "@wearable/contracts";
import { z } from "zod";
import type { FileHandle } from "node:fs/promises";

const execFileAsync = promisify(execFile);
const require = createRequire(import.meta.url);
const ffprobeModule = require("ffprobe-static") as { path?: unknown };
const defaultFfprobePath = typeof ffprobeModule.path === "string" ? ffprobeModule.path : null;

type VideoFacts = Readonly<{ durationSeconds: number; width: number; height: number }>;
type CandidateUpload = Readonly<{ bytes: Buffer; fileName: string }>;
type SeenFile = Readonly<{ size: number; mtimeMs: number; firstSeenAtMs: number }>;
type InboxOptions = Readonly<{
  /** Server-owned inbox root. This never comes from a browser request. */
  root: string;
  stableForMs?: number;
  maxFileBytes?: number;
  now?: () => string;
  probeVideo?: (path: string) => Promise<VideoFacts | null>;
  ffprobePath?: string;
}>;

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function opaque(value: string): string {
  return sha256(value).slice(0, 32);
}

function sameStat(left: { dev: number | bigint; ino: number | bigint; size: number | bigint; mtimeMs: number | bigint }, right: { dev: number | bigint; ino: number | bigint; size: number | bigint; mtimeMs: number | bigint }): boolean {
  return String(left.dev) === String(right.dev)
    && String(left.ino) === String(right.ino)
    && String(left.size) === String(right.size)
    && String(left.mtimeMs) === String(right.mtimeMs);
}

async function defaultProbe(path: string, ffprobePath: string | null): Promise<VideoFacts | null> {
  if (!ffprobePath) return null;
  try {
    const { stdout } = await execFileAsync(ffprobePath, [
      "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
      "-of", "json", path,
    ], { timeout: 10_000, maxBuffer: 1024 * 1024 });
    const parsed = z.object({
      streams: z.array(z.object({ codec_type: z.string(), width: z.number().optional(), height: z.number().optional() }).passthrough()),
      format: z.object({ duration: z.string().optional() }).passthrough().optional(),
    }).passthrough().parse(JSON.parse(stdout));
    const video = parsed.streams.find((stream) => stream.codec_type === "video");
    const durationSeconds = Number(parsed.format?.duration);
    if (!video?.width || !video.height || !Number.isFinite(durationSeconds)) return null;
    return { durationSeconds, width: video.width, height: video.height };
  } catch {
    return null;
  }
}

async function ensurePrivateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("external-edit-inbox-directory-invalid");
  await chmod(path, 0o700);
}

export class ExternalEditInbox {
  private readonly root: string;
  private readonly stableForMs: number;
  private readonly maxFileBytes: number;
  private readonly now: () => string;
  private readonly probeVideo: (path: string) => Promise<VideoFacts | null>;
  private readonly seen = new Map<string, SeenFile>();

  constructor(options: InboxOptions) {
    this.root = resolve(options.root);
    this.stableForMs = options.stableForMs ?? 2_000;
    this.maxFileBytes = options.maxFileBytes ?? 1_500_000_000;
    if (!Number.isInteger(this.stableForMs) || this.stableForMs < 0) throw new Error("external-edit-stability-window-invalid");
    if (!Number.isSafeInteger(this.maxFileBytes) || this.maxFileBytes <= 0) throw new Error("external-edit-file-size-limit-invalid");
    this.now = options.now ?? (() => new Date().toISOString());
    const ffprobePath = options.ffprobePath ?? defaultFfprobePath;
    this.probeVideo = options.probeVideo ?? ((path) => defaultProbe(path, ffprobePath));
  }

  async createReturnDirectory(handoffValue: ExternalEditHandoff): Promise<string> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const directory = this.directoryFor(handoff);
    await ensurePrivateDirectory(this.root);
    await ensurePrivateDirectory(join(this.root, opaque(handoff.customerId)));
    await ensurePrivateDirectory(directory);
    return directory;
  }

  /**
   * Places one completely received browser upload into the same private inbox
   * used by Finder exports. This is a manual fallback, not proof that Jianying
   * produced the file; the accepted draft keeps `return-folder-only`
   * provenance and still requires preview plus explicit human confirmation.
   */
  async importCandidate(
    handoffValue: ExternalEditHandoff,
    upload: CandidateUpload,
  ): Promise<ExternalEditReturnCandidate> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const extension = extname(basename(upload.fileName.normalize("NFC")).trim()).toLowerCase();
    if (![".mp4", ".mov", ".m4v"].includes(extension)) throw new Error("external-edit-upload-file-name-invalid");
    if (upload.bytes.byteLength < 1) throw new Error("external-edit-upload-file-required");
    if (upload.bytes.byteLength > this.maxFileBytes) throw new Error("external-edit-upload-too-large");
    const directory = await this.createReturnDirectory(handoff);
    const nowText = this.now();
    const nowMs = Date.parse(nowText);
    if (!Number.isFinite(nowMs)) throw new Error("external-edit-clock-invalid");
    const digest = sha256(upload.bytes);
    const fileName = `manual-${digest.slice(0, 24)}${extension}`;
    const path = join(directory, fileName);
    const temporaryPath = join(directory, `.upload-${randomUUID()}.part`);
    let temporaryExists = false;
    try {
      const handle = await open(
        temporaryPath,
        constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0),
        0o600,
      );
      temporaryExists = true;
      try {
        await handle.writeFile(upload.bytes);
        await handle.sync();
        await handle.chmod(0o600);
      } finally {
        await handle.close();
      }
      const facts = await this.probeVideo(temporaryPath);
      if (!facts) throw new Error("external-edit-upload-media-invalid");
      if (facts.durationSeconds < 30 || facts.durationSeconds > 60) {
        throw new Error("external-edit-upload-duration-invalid");
      }
      try {
        await link(temporaryPath, path);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        const existing = await this.digestNoFollow(path);
        if (!existing || existing.digest !== digest || existing.sizeBytes !== upload.bytes.byteLength) {
          throw new Error("external-edit-upload-output-conflict");
        }
      }
      await rm(temporaryPath, { force: true });
      temporaryExists = false;
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size !== upload.bytes.byteLength) {
        throw new Error("external-edit-upload-output-invalid");
      }
      this.seen.set(`${handoff.id}\u0000${fileName}`, {
        size: info.size,
        mtimeMs: info.mtimeMs,
        firstSeenAtMs: nowMs - this.stableForMs,
      });
      const candidate = (await this.scan(handoff)).find((item) => item.digest === digest);
      if (!candidate) throw new Error("external-edit-upload-candidate-invalid");
      return candidate;
    } finally {
      if (temporaryExists) await rm(temporaryPath, { force: true });
    }
  }

  async scan(handoffValue: ExternalEditHandoff): Promise<ExternalEditReturnCandidate[]> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const directory = this.directoryFor(handoff);
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
    const candidates: ExternalEditReturnCandidate[] = [];
    const nowText = this.now();
    const nowMs = Date.parse(nowText);
    if (!Number.isFinite(nowMs)) throw new Error("external-edit-clock-invalid");
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (entry.name !== basename(entry.name) || ![".mp4", ".mov", ".m4v"].includes(extname(entry.name).toLowerCase())) continue;
      const path = join(directory, entry.name);
      let before;
      try {
        before = await lstat(path);
      } catch {
        continue;
      }
      if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size <= 0 || before.size > this.maxFileBytes) continue;
      const seenKey = `${handoff.id}\u0000${entry.name}`;
      let previous = this.seen.get(seenKey);
      if (!previous || previous.size !== before.size || previous.mtimeMs !== before.mtimeMs) {
        previous = { size: before.size, mtimeMs: before.mtimeMs, firstSeenAtMs: nowMs };
        this.seen.set(seenKey, previous);
        if (this.stableForMs > 0) continue;
      }
      if (nowMs - previous.firstSeenAtMs < this.stableForMs) continue;
      const facts = await this.probeVideo(path);
      if (!facts || facts.durationSeconds < 30 || facts.durationSeconds > 60) continue;
      let after;
      try { after = await lstat(path); } catch { continue; }
      if (!after.isFile() || after.isSymbolicLink() || after.nlink !== 1 || !sameStat(before, after)) continue;
      const fileDigest = await this.digestNoFollow(path, before);
      if (!fileDigest || fileDigest.sizeBytes !== before.size) continue;
      const relativePath = `${opaque(handoff.customerId)}/${opaque(handoff.id)}/${entry.name}`;
      const candidate = ExternalEditReturnCandidateSchema.parse({
        id: `candidate_${sha256(`${handoff.id}\u0000${relativePath}\u0000${fileDigest.digest}`).slice(0, 32)}`,
        handoffId: handoff.id,
        parentDraftId: handoff.parentDraftId,
        relativePath,
        digest: fileDigest.digest,
        sizeBytes: fileDigest.sizeBytes,
        durationSeconds: facts.durationSeconds,
        mediaFacts: { width: facts.width, height: facts.height },
        stableSince: new Date(previous.firstSeenAtMs).toISOString(),
        detectedAt: nowText,
        status: "pending-review",
      });
      candidates.push(candidate);
    }
    return candidates;
  }

  async copyCandidateTo(
    handoffValue: ExternalEditHandoff,
    candidateValue: ExternalEditReturnCandidate,
    destinationPath: string,
  ): Promise<{ digest: string; sizeBytes: number }> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const candidate = ExternalEditReturnCandidateSchema.parse(candidateValue);
    if (candidate.handoffId !== handoff.id || candidate.parentDraftId !== handoff.parentDraftId) {
      throw new Error("external-edit-candidate-binding-mismatch");
    }
    const expectedPrefix = `${opaque(handoff.customerId)}/${opaque(handoff.id)}/`;
    if (!candidate.relativePath.startsWith(expectedPrefix) || candidate.relativePath.slice(expectedPrefix.length).includes("/")) {
      throw new Error("external-edit-candidate-path-invalid");
    }
    const sourcePath = join(this.root, candidate.relativePath);
    const before = await lstat(sourcePath);
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) throw new Error("external-edit-candidate-invalid");
    if (before.size !== candidate.sizeBytes) throw new Error("external-edit-candidate-changed");
    const input = await open(sourcePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const output = await open(destinationPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600).catch(async (error) => {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const existing = await this.digestNoFollow(destinationPath);
      if (existing?.digest === candidate.digest && existing.sizeBytes === candidate.sizeBytes) return null;
      throw new Error("external-edit-return-output-conflict");
    });
    try {
      if (output === null) return { digest: candidate.digest, sizeBytes: candidate.sizeBytes };
      const opened = await input.stat();
      if (!opened.isFile() || opened.nlink !== 1 || !sameStat(before, opened)) throw new Error("external-edit-candidate-invalid");
      const hash = createHash("sha256");
      const chunk = Buffer.allocUnsafe(1024 * 1024);
      let position = 0;
      while (true) {
        const { bytesRead } = await input.read(chunk, 0, chunk.byteLength, position);
        if (!bytesRead) break;
        const data = chunk.subarray(0, bytesRead);
        hash.update(data);
        let written = 0;
        while (written < data.length) {
          const result = await output.write(data, written, data.length - written, position + written);
          if (!result.bytesWritten) throw new Error("external-edit-return-copy-failed");
          written += result.bytesWritten;
        }
        position += bytesRead;
      }
      const after = await input.stat();
      const copiedDigest = hash.digest("hex");
      if (!sameStat(opened, after) || copiedDigest !== candidate.digest || position !== candidate.sizeBytes) {
        throw new Error("external-edit-candidate-changed");
      }
      await output.sync();
      await output.chmod(0o600);
      return { digest: copiedDigest, sizeBytes: position };
    } catch (error) {
      if (output) {
        await output.close();
        await rm(destinationPath, { force: true });
      }
      throw error;
    } finally {
      await input.close();
      if (output) await output.close().catch(() => undefined);
    }
  }

  async openCandidateForRead(
    handoffValue: ExternalEditHandoff,
    candidateValue: ExternalEditReturnCandidate,
  ): Promise<FileHandle> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const candidate = ExternalEditReturnCandidateSchema.parse(candidateValue);
    if (candidate.handoffId !== handoff.id || candidate.parentDraftId !== handoff.parentDraftId) {
      throw new Error("external-edit-candidate-binding-mismatch");
    }
    const expectedPrefix = `${opaque(handoff.customerId)}/${opaque(handoff.id)}/`;
    if (!candidate.relativePath.startsWith(expectedPrefix) || candidate.relativePath.slice(expectedPrefix.length).includes("/")) {
      throw new Error("external-edit-candidate-path-invalid");
    }
    const sourcePath = join(this.root, candidate.relativePath);
    const before = await lstat(sourcePath);
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1
      || before.size !== candidate.sizeBytes) throw new Error("external-edit-candidate-invalid");
    const verified = await this.digestNoFollow(sourcePath, before);
    if (!verified || verified.digest !== candidate.digest || verified.sizeBytes !== candidate.sizeBytes) {
      throw new Error("external-edit-candidate-changed");
    }
    const handle = await open(sourcePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const opened = await handle.stat();
    if (!opened.isFile() || opened.nlink !== 1 || !sameStat(before, opened)) {
      await handle.close();
      throw new Error("external-edit-candidate-invalid");
    }
    return handle;
  }

  private directoryFor(handoff: ExternalEditHandoff): string {
    return join(this.root, opaque(handoff.customerId), opaque(handoff.id));
  }

  private async digestNoFollow(path: string, expected?: Awaited<ReturnType<typeof lstat>>): Promise<{ digest: string; sizeBytes: number } | null> {
    let before = expected;
    try {
      before ??= await lstat(path);
      if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) return null;
      const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const opened = await handle.stat();
        if (!opened.isFile() || opened.nlink !== 1 || !sameStat(before, opened)) return null;
        const hash = createHash("sha256");
        for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk as Buffer);
        const after = await handle.stat();
        if (!sameStat(opened, after)) return null;
        return { digest: hash.digest("hex"), sizeBytes: opened.size };
      } finally {
        await handle.close();
      }
    } catch {
      return null;
    }
  }
}

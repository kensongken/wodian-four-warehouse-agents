import { spawn } from "node:child_process";
import { link, mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";

import ffmpegPath from "ffmpeg-static";

import { importGlass3LocalOriginal, sha256, type ImportedMedia } from "./glass3-import-adapter";
import type { CreativeCaptureUpload, CreativeOriginalStore } from "./creative-handoff";

/** Local A-0 original storage. A final path is created with a hard-link, which
 * deliberately fails rather than overwriting an original already on disk. */
export class LocalCreativeOriginalStore implements CreativeOriginalStore {
  constructor(private readonly directory: string) {}

  async prepare(input: Readonly<{
    identity: string;
    operationKey: string;
    uploads: readonly CreativeCaptureUpload[];
  }>): Promise<Readonly<{
    importedMedia: readonly ImportedMedia[];
    commit(): Promise<void>;
    rollback(): Promise<void>;
  }>> {
    await mkdir(this.directory, { recursive: true });
    const staged: Array<{
      upload: CreativeCaptureUpload; digest: string; mediaId: string; target: string; pending: string;
      created: boolean;
    }> = [];
    try {
      for (const [index, upload] of input.uploads.entries()) {
        assertUploadMetadata(upload);
        const digest = sha256(upload.bytes);
        const mediaId = `glass3_${digest}`;
        const target = join(this.directory, `${input.identity}--${mediaId}${upload.extension}`);
        const pending = `${target}.pending-${encodeURIComponent(input.operationKey)}-${index}`;
        await writeFile(pending, upload.bytes, { flag: "wx" });
        const item = { upload, digest, mediaId, target, pending, created: false };
        staged.push(item);
        const verified = await readFile(pending);
        if (sha256(verified) !== digest) throw new Error("creative-original-digest-mismatch");
        await assertDecodableFile(pending, upload.kind);
      }
    } catch (error) {
      await Promise.all(staged.map((item) => unlink(item.pending).catch(() => undefined)));
      throw error;
    }
    const importedMedia = staged.map(({ upload, digest, target }) => importGlass3LocalOriginal({
      localPath: target,
      originalBytes: upload.bytes,
      manifest: {
        origin: "phone-local-original",
        deviceModel: upload.deviceModel,
        // The receipt key, not wall-clock time, defines a retry's asset identity.
        importedAt: "1970-01-01T00:00:00.000Z",
        digest,
      },
    }));
    return {
      importedMedia,
      commit: async () => {
        for (const item of staged) {
          try {
            await link(item.pending, item.target);
            item.created = true;
          } catch (error) {
            const current = await readFile(item.target).catch(() => null);
            if (!current || sha256(current) !== item.digest) throw error;
          } finally {
            await unlink(item.pending).catch(() => undefined);
          }
        }
      },
      rollback: async () => {
        await Promise.all(staged.map(async (item) => {
          await unlink(item.pending).catch(() => undefined);
          if (item.created) await unlink(item.target).catch(() => undefined);
        }));
      },
    };
  }
}

function assertUploadMetadata(upload: CreativeCaptureUpload): void {
  if (!upload.deviceModel.trim()) throw new Error("creative-capture-device-model-required");
  const extensionKind = upload.extension === ".mp4" ? "video" : upload.extension === ".m4a"
    ? "audio" : "image";
  if (extensionKind !== upload.kind) throw new Error("creative-capture-extension-kind-mismatch");
}

async function assertDecodableFile(
  path: string,
  kind: CreativeCaptureUpload["kind"],
): Promise<void> {
  const binary = ffmpegPath;
  if (!binary) throw new Error("ffmpeg-binary-unavailable");
  const decoded = await new Promise<boolean>((resolve) => {
    const inputArgs = kind === "image" ? ["-f", "image2", "-i", path, "-frames:v", "1"] : ["-i", path];
    const child = spawn(binary, ["-v", "error", ...inputArgs, "-f", "null", "-"], { stdio: "ignore" });
    child.once("error", () => resolve(false));
    child.once("close", (code: number | null) => resolve(code === 0));
  });
  if (!decoded) throw new Error("creative-capture-media-not-decodable");
}

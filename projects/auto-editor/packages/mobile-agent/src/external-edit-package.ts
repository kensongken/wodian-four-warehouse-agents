import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { dirname, extname, join, resolve, sep } from "node:path";

import {
  DraftPackageSchema,
  ExternalEditHandoffSchema,
  ExternalEditPackageSchema,
  type DraftPackage,
  type ExternalEditHandoff,
  type ExternalEditPackage,
} from "@wearable/contracts";
import { z } from "zod";

const SourceSchema = z.object({
  role: z.enum(["parent-video", "source-video", "subtitle", "timeline"]),
  sourcePath: z.string().min(1),
  relativePath: z.string().min(1),
  digest: z.string().regex(/^[a-f0-9]{64}$/),
  sourceAssetId: z.string().min(1).optional(),
}).strict();

export type ExternalEditPackageSource = Readonly<z.infer<typeof SourceSchema>>;

type ExternalEditPackageBuilderOptions = Readonly<{
  /** Private, server-owned root. Never accept this value from an HTTP request. */
  root: string;
  /** Resolves approved asset IDs to trusted internal source references. */
  resolveSources: (handoff: ExternalEditHandoff, parentDraft: DraftPackage) => Promise<readonly ExternalEditPackageSource[]>;
  now?: () => string;
}>;

type BuiltPackage = Readonly<{ package: ExternalEditPackage; packageRoot: string }>;

const ManifestSchema = z.object({
  package: ExternalEditPackageSchema,
  revisionInstruction: z.string().min(1),
  mustKeep: z.array(z.string()),
  includeSources: z.boolean(),
  publishClaimed: z.literal(false),
}).strict();

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function safePackagePath(root: string, relativePath: string): string {
  const segments = relativePath.split("/");
  if (relativePath.startsWith("/") || relativePath.includes("\\") || segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new Error("external-edit-package-path-invalid");
  }
  const target = resolve(root, ...segments);
  const boundary = `${resolve(root)}${sep}`;
  if (!target.startsWith(boundary)) throw new Error("external-edit-package-path-invalid");
  return target;
}

function sourceTarget(source: ExternalEditPackageSource): string {
  if (source.role === "parent-video") return "video/parent.mp4";
  if (source.role === "source-video") {
    if (!source.sourceAssetId) throw new Error("external-edit-source-asset-id-required");
    const extension = extname(source.relativePath).toLowerCase();
    if (![".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png"].includes(extension)) {
      throw new Error("external-edit-source-extension-unsupported");
    }
    const id = sha256(source.sourceAssetId).slice(0, 16);
    return `sources/source-${id}${extension}`;
  }
  const extension = source.role === "subtitle" ? ".srt" : ".json";
  return `support/${source.role}${extension}`;
}

async function ensurePrivateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("external-edit-package-directory-invalid");
  await chmod(path, 0o700);
}

async function hashFile(path: string): Promise<{ digest: string; sizeBytes: number }> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error("external-edit-package-file-invalid");
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const opened = await handle.stat();
    if (!opened.isFile() || opened.nlink !== 1 || opened.dev !== info.dev || opened.ino !== info.ino) {
      throw new Error("external-edit-package-file-invalid");
    }
    const hash = createHash("sha256");
    for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk as Buffer);
    return { digest: hash.digest("hex"), sizeBytes: opened.size };
  } finally {
    await handle.close();
  }
}

async function copyVerified(source: ExternalEditPackageSource, destination: string): Promise<number> {
  const before = await lstat(source.sourcePath);
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) {
    throw new Error("external-edit-source-not-regular-file");
  }
  const input = await open(source.sourcePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  const output = await open(destination, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    const opened = await input.stat();
    if (!opened.isFile() || opened.nlink !== 1 || opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error("external-edit-source-not-regular-file");
    }
    const hash = createHash("sha256");
    const chunk = Buffer.allocUnsafe(1024 * 1024);
    let position = 0;
    while (true) {
      const { bytesRead } = await input.read(chunk, 0, chunk.byteLength, position);
      if (bytesRead === 0) break;
      const slice = chunk.subarray(0, bytesRead);
      hash.update(slice);
      let written = 0;
      while (written < bytesRead) {
        const result = await output.write(slice, written, bytesRead - written, position + written);
        if (result.bytesWritten <= 0) throw new Error("external-edit-package-write-failed");
        written += result.bytesWritten;
      }
      position += bytesRead;
    }
    const after = await input.stat();
    if (after.dev !== opened.dev || after.ino !== opened.ino || after.size !== opened.size || after.mtimeMs !== opened.mtimeMs) {
      throw new Error("external-edit-source-changed-during-copy");
    }
    if (hash.digest("hex") !== source.digest) throw new Error("external-edit-source-digest-mismatch");
    await output.sync();
    await output.chmod(0o600);
    return position;
  } finally {
    await input.close();
    await output.close();
  }
}

function calculatedPackageDigest(value: Pick<ExternalEditPackage, "handoffId" | "customerId" | "parentDraftId" | "files">): string {
  return sha256(JSON.stringify({
    handoffId: value.handoffId,
    customerId: value.customerId,
    parentDraftId: value.parentDraftId,
    files: value.files,
  }));
}

export class ExternalEditPackageBuilder {
  private readonly root: string;
  private readonly resolveSources: ExternalEditPackageBuilderOptions["resolveSources"];
  private readonly now: () => string;

  constructor(options: ExternalEditPackageBuilderOptions) {
    this.root = resolve(options.root);
    this.resolveSources = options.resolveSources;
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async build(handoffValue: ExternalEditHandoff, parentDraftValue: DraftPackage): Promise<BuiltPackage> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const parentDraft = DraftPackageSchema.parse(parentDraftValue);
    if (handoff.customerId !== parentDraft.customerId
      || handoff.sessionId !== parentDraft.sessionId
      || handoff.parentDraftId !== parentDraft.id
      || handoff.parentDraftVersion !== parentDraft.version
      || handoff.parentDraftDigest !== parentDraft.outputDigest) {
      throw new Error("external-edit-parent-scope-mismatch");
    }
    const resolvedSources = (await this.resolveSources(handoff, parentDraft)).map((source) => SourceSchema.parse(source));
    const parentSources = resolvedSources.filter((source) => source.role === "parent-video");
    const sourceAssets = resolvedSources.filter((source) => source.role === "source-video");
    if (parentSources.length !== 1) throw new Error("external-edit-parent-video-required");
    if (parentSources[0]!.digest !== parentDraft.outputDigest) throw new Error("external-edit-source-digest-mismatch");
    if (!handoff.includeSources && sourceAssets.length > 0) throw new Error("external-edit-source-consent-required");
    const bindings = new Map(handoff.sourceAssetBindings.map((item) => [item.sourceAssetId, item.digest]));
    if (handoff.includeSources) {
      if (bindings.size !== sourceAssets.length || sourceAssets.some((source) =>
        !source.sourceAssetId || bindings.get(source.sourceAssetId) !== source.digest)) {
        throw new Error("external-edit-source-consent-binding-mismatch");
      }
    }
    if (new Set(sourceAssets.map((source) => source.sourceAssetId)).size !== sourceAssets.length) {
      throw new Error("external-edit-source-asset-duplicate");
    }

    const handoffToken = sha256(handoff.id).slice(0, 32);
    const customerToken = sha256(handoff.customerId).slice(0, 32);
    const packageRoot = join(this.root, customerToken, handoffToken);
    await ensurePrivateDirectory(this.root);
    await ensurePrivateDirectory(join(this.root, customerToken));

    const existing = await this.readExisting(packageRoot, handoff);
    if (existing) return existing;

    const stageRoot = join(join(this.root, customerToken), `.tmp-${randomUUID()}`);
    await ensurePrivateDirectory(stageRoot);
    try {
      const copySources = [...resolvedSources].sort((left, right) => {
        const order = { "parent-video": 0, "source-video": 1, subtitle: 2, timeline: 3 } as const;
        return order[left.role] - order[right.role] || left.digest.localeCompare(right.digest);
      });
      const fileEntries: ExternalEditPackage["files"][number][] = [];
      for (const source of copySources) {
        const relativePath = sourceTarget(source);
        const destination = safePackagePath(stageRoot, relativePath);
        await ensurePrivateDirectory(dirname(destination));
        const sizeBytes = await copyVerified(source, destination);
        fileEntries.push({ relativePath, role: source.role, digest: source.digest, sizeBytes });
      }

      const instructions = [
        "剪映精修操作说明",
        "",
        "这是一份剪映交接包，不代表已经导入剪映。请在剪映中手动新建项目并导入 video/parent.mp4。",
        "如果包里有 sources 文件夹，可以按需要把其中已经授权的素材一起导入。",
        `修改要求：${handoff.revisionInstruction}`,
        `必须保留：${handoff.mustKeep.length ? handoff.mustKeep.join("、") : "无额外指定"}`,
        "导出后，请把成片保存为“剪映精修版.mp4”并放入内容总编为本次交接准备的回传文件夹。",
        "回传文件只会成为待预览候选；客户确认后才会生成新版本，不会自动发布。",
        "",
      ].join("\n");
      const instructionsDigest = sha256(instructions);
      const instructionsPath = safePackagePath(stageRoot, "README.txt");
      const instructionsHandle = await open(instructionsPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
      try {
        await instructionsHandle.writeFile(instructions, "utf8");
        await instructionsHandle.sync();
        await instructionsHandle.chmod(0o600);
      } finally {
        await instructionsHandle.close();
      }
      fileEntries.push({ relativePath: "README.txt", role: "instructions", digest: instructionsDigest, sizeBytes: Buffer.byteLength(instructions) });

      const packageBase = {
        id: `package_${sha256(handoff.id).slice(0, 24)}`,
        handoffId: handoff.id,
        customerId: handoff.customerId,
        parentDraftId: handoff.parentDraftId,
        files: fileEntries,
      };
      const packageValue = ExternalEditPackageSchema.parse({
        ...packageBase,
        packageDigest: calculatedPackageDigest(packageBase),
        createdAt: handoff.createdAt,
      });
      const manifest = {
        package: packageValue,
        revisionInstruction: handoff.revisionInstruction,
        mustKeep: handoff.mustKeep,
        includeSources: handoff.includeSources,
        publishClaimed: false,
      };
      const manifestHandle = await open(join(stageRoot, "manifest.json"), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
      try {
        await manifestHandle.writeFile(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
        await manifestHandle.sync();
        await manifestHandle.chmod(0o600);
      } finally {
        await manifestHandle.close();
      }

      await ensurePrivateDirectory(join(this.root, customerToken));
      await rename(stageRoot, packageRoot);
      return { package: packageValue, packageRoot };
    } catch (error) {
      await rm(stageRoot, { recursive: true, force: true });
      if ((error as NodeJS.ErrnoException).code === "EEXIST" || (error as NodeJS.ErrnoException).code === "ENOTEMPTY") {
        const existingAfterRace = await this.readExisting(packageRoot, handoff);
        if (existingAfterRace) return existingAfterRace;
      }
      throw error;
    }
  }

  private async readExisting(packageRoot: string, handoff: ExternalEditHandoff): Promise<BuiltPackage | null> {
    try {
      const rootInfo = await lstat(packageRoot);
      if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) throw new Error("external-edit-package-directory-invalid");
      const manifest = ManifestSchema.parse(JSON.parse(await readFile(join(packageRoot, "manifest.json"), "utf8")));
      if (manifest.package.handoffId !== handoff.id
        || manifest.package.customerId !== handoff.customerId
        || manifest.package.parentDraftId !== handoff.parentDraftId
        || manifest.revisionInstruction !== handoff.revisionInstruction
        || manifest.includeSources !== handoff.includeSources
        || JSON.stringify(manifest.mustKeep) !== JSON.stringify(handoff.mustKeep)
        || manifest.package.packageDigest !== calculatedPackageDigest(manifest.package)) {
        throw new Error("external-edit-existing-package-mismatch");
      }
      for (const file of manifest.package.files) {
        const target = safePackagePath(packageRoot, file.relativePath);
        const facts = await hashFile(target);
        if (facts.digest !== file.digest || facts.sizeBytes !== file.sizeBytes) {
          throw new Error("external-edit-existing-package-mismatch");
        }
      }
      return { package: manifest.package, packageRoot };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }
}

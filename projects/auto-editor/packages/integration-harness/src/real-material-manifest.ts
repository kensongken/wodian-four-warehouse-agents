import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, relative, sep } from "node:path";

import {
  RealMaterialRunManifestSchema,
  type RealMaterialRunManifest,
} from "@wearable/contracts";

export type LockedRealMaterialInput = Readonly<{
  declared: RealMaterialRunManifest["inputs"][number];
  canonicalPath: string;
  byteLength: number;
  device: number;
  inode: number;
  lockedDigest: string;
}>;

export async function loadLockedRealMaterialManifest(input: Readonly<{
  manifestPath: string;
  approvedRoots: readonly string[];
}>): Promise<Readonly<{
  manifest: RealMaterialRunManifest;
  inputs: readonly LockedRealMaterialInput[];
}>> {
  if (input.approvedRoots.length === 0) throw new Error("real-material-approved-root-required");

  const manifestMetadata = await stat(input.manifestPath);
  if (!manifestMetadata.isFile()) throw new Error("real-material-manifest-regular-file-required");
  if ((manifestMetadata.mode & 0o777) !== 0o600) {
    throw new Error("real-material-manifest-mode-must-be-0600");
  }

  const manifest = RealMaterialRunManifestSchema.parse(
    JSON.parse(await readFile(input.manifestPath, "utf8")),
  );
  const canonicalRoots = await Promise.all(input.approvedRoots.map(async (root) => {
    const canonicalRoot = await realpath(root);
    if (!(await stat(canonicalRoot)).isDirectory()) {
      throw new Error("real-material-approved-root-directory-required");
    }
    return canonicalRoot;
  }));

  const inputs: LockedRealMaterialInput[] = [];
  const canonicalPaths = new Set<string>();
  const digestRolePairs = new Set<string>();
  for (const declared of manifest.inputs) {
    const canonicalPath = await realpath(declared.sourcePath);
    if (!canonicalRoots.some((root) => isStrictDescendant(root, canonicalPath))) {
      throw new Error("real-material-source-outside-approved-roots");
    }
    if (canonicalPaths.has(canonicalPath)) throw new Error("duplicate-real-material-canonical-path");

    const before = await stat(canonicalPath);
    if (!before.isFile()) throw new Error("real-material-source-regular-file-required");
    const lockedDigest = await sha256File(canonicalPath);
    const after = await stat(canonicalPath);
    if (!after.isFile() || before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
      throw new Error("real-material-source-changed-during-lock");
    }
    if (lockedDigest !== declared.sourceDigest) {
      throw new Error("real-material-source-digest-mismatch");
    }

    const digestRolePair = `${lockedDigest}\0${declared.role}`;
    if (digestRolePairs.has(digestRolePair)) throw new Error("duplicate-real-material-digest-role");
    canonicalPaths.add(canonicalPath);
    digestRolePairs.add(digestRolePair);
    inputs.push(Object.freeze({
      declared,
      canonicalPath,
      byteLength: before.size,
      device: before.dev,
      inode: before.ino,
      lockedDigest,
    }));
  }

  return Object.freeze({ manifest, inputs: Object.freeze(inputs) });
}

export async function readLockedMaterialBytes(input: LockedRealMaterialInput): Promise<Buffer> {
  try {
    if (await realpath(input.declared.sourcePath) !== input.canonicalPath) {
      throw new Error("real-material-source-changed");
    }
    const before = await stat(input.canonicalPath);
    if (
      !before.isFile()
      || before.size !== input.byteLength
      || before.dev !== input.device
      || before.ino !== input.inode
    ) {
      throw new Error("real-material-source-changed");
    }
    const bytes = await readFile(input.canonicalPath);
    const after = await stat(input.canonicalPath);
    if (
      !after.isFile()
      || after.size !== input.byteLength
      || after.dev !== input.device
      || after.ino !== input.inode
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
      || sha256Bytes(bytes) !== input.lockedDigest
      || await realpath(input.declared.sourcePath) !== input.canonicalPath
    ) {
      throw new Error("real-material-source-changed");
    }
    return bytes;
  } catch (error) {
    if (error instanceof Error && error.message === "real-material-source-changed") throw error;
    throw new Error("real-material-source-changed", { cause: error });
  }
}

export async function assertLockedMaterialUnchanged(
  inputs: readonly LockedRealMaterialInput[],
): Promise<void> {
  for (const input of inputs) {
    try {
      if (await realpath(input.declared.sourcePath) !== input.canonicalPath) {
        throw new Error("real-material-source-changed");
      }
      const before = await stat(input.canonicalPath);
      if (!before.isFile() || before.size !== input.byteLength) {
        throw new Error("real-material-source-changed");
      }
      const currentDigest = await sha256File(input.canonicalPath);
      const after = await stat(input.canonicalPath);
      if (
        !after.isFile()
        || after.size !== input.byteLength
        || before.size !== after.size
        || before.mtimeMs !== after.mtimeMs
        || currentDigest !== input.lockedDigest
      ) {
        throw new Error("real-material-source-changed");
      }
    } catch (error) {
      if (error instanceof Error && error.message === "real-material-source-changed") throw error;
      throw new Error("real-material-source-changed", { cause: error });
    }
  }
}

export function publicInputReceipts(
  inputs: readonly LockedRealMaterialInput[],
): readonly Readonly<{
  inputId: string;
  role: string;
  mediaKind: string;
  sourceDigest: string;
  byteLength: number;
}>[] {
  return inputs.map((input) => Object.freeze({
    inputId: input.declared.inputId,
    role: input.declared.role,
    mediaKind: input.declared.mediaKind,
    sourceDigest: input.lockedDigest,
    byteLength: input.byteLength,
  }));
}

function isStrictDescendant(canonicalRoot: string, canonicalPath: string): boolean {
  const child = relative(canonicalRoot, canonicalPath);
  return child !== "" && child !== ".." && !child.startsWith(`..${sep}`) && !isAbsolute(child);
}

function sha256Bytes(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

async function sha256File(path: string): Promise<string> {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

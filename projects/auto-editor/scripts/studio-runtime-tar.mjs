import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { chmod, lstat, mkdir, open, readdir } from "node:fs/promises";
import { dirname, join, relative, sep } from "node:path";

import {
  compareRuntimePathsByUtf8Bytes,
  decodeRuntimeUtf8,
  isPortableRuntimeRelativePath,
  runtimePathCollisionKey,
} from "./studio-runtime-canonical.mjs";

const BLOCK_BYTES = 512;
const ZERO_BLOCK = Buffer.alloc(BLOCK_BYTES);

function fail(code, detail = "") {
  throw new Error(detail ? `${code}:${detail}` : code);
}

function portableRelative(root, path) {
  const value = relative(root, path).split(sep).join("/");
  if (!isPortableRuntimeRelativePath(value)) fail("studio-runtime-tar-path-invalid", value);
  return value;
}

async function walk(root, current = root) {
  const names = await readdir(current);
  for (const name of names) {
    if (!isPortableRuntimeRelativePath(name)) fail("studio-runtime-tar-path-invalid", name);
  }
  names.sort(compareRuntimePathsByUtf8Bytes);
  const entries = [];
  for (const name of names) {
    if (name === ".DS_Store" || name.startsWith("._")) {
      fail("studio-runtime-tar-resource-fork-rejected", name);
    }
    const path = join(current, name);
    const details = await lstat(path);
    const relativePath = portableRelative(root, path);
    if (details.isSymbolicLink() || (!details.isDirectory() && !details.isFile())) {
      fail("studio-runtime-tree-link-rejected", relativePath);
    }
    if (details.isFile() && details.nlink !== 1) {
      fail("studio-runtime-tree-link-rejected", relativePath);
    }
    if (details.isDirectory()) {
      entries.push({ path, relativePath: `${relativePath}/`, type: "directory", details });
      entries.push(...await walk(root, path));
    } else {
      entries.push({ path, relativePath, type: "file", details });
    }
  }
  return entries;
}

function writeString(header, value, offset, length) {
  const bytes = Buffer.from(value, "utf8");
  if (bytes.length > length) fail("studio-runtime-tar-path-too-long", value);
  bytes.copy(header, offset);
}

function writeOctal(header, value, offset, length) {
  const encoded = value.toString(8).padStart(length - 1, "0");
  if (encoded.length !== length - 1) fail("studio-runtime-tar-number-too-large");
  writeString(header, `${encoded}\0`, offset, length);
}

function splitUstarPath(relativePath) {
  const path = relativePath.endsWith("/") ? relativePath.slice(0, -1) : relativePath;
  if (Buffer.byteLength(path) <= 100) return { name: path, prefix: "" };
  const components = path.split("/");
  for (let index = components.length - 1; index > 0; index -= 1) {
    const prefix = components.slice(0, index).join("/");
    const name = components.slice(index).join("/");
    if (Buffer.byteLength(prefix) <= 155 && Buffer.byteLength(name) <= 100) {
      return { name, prefix };
    }
  }
  fail("studio-runtime-tar-path-too-long", relativePath);
}

function tarHeader(entry) {
  const header = Buffer.alloc(BLOCK_BYTES);
  const { name, prefix } = splitUstarPath(entry.relativePath);
  writeString(header, name, 0, 100);
  writeOctal(header, entry.type === "directory" ? 0o755 : entry.mode, 100, 8);
  writeOctal(header, 0, 108, 8);
  writeOctal(header, 0, 116, 8);
  writeOctal(header, entry.type === "directory" ? 0 : entry.details.size, 124, 12);
  writeOctal(header, 0, 136, 12);
  header.fill(0x20, 148, 156);
  header[156] = entry.type === "directory" ? 0x35 : 0x30;
  writeString(header, "ustar\0", 257, 6);
  writeString(header, "00", 263, 2);
  writeString(header, prefix, 345, 155);
  const checksum = header.reduce((total, byte) => total + byte, 0);
  writeString(header, `${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  return header;
}

export async function createDeterministicRuntimeTar(runtimeRoot, archivePath) {
  const entries = (await walk(runtimeRoot)).sort((left, right) => (
    compareRuntimePathsByUtf8Bytes(left.relativePath, right.relativePath)
  ));
  const collisionPaths = new Set();
  for (const entry of entries) {
    const path = entry.type === "directory" ? entry.relativePath.slice(0, -1) : entry.relativePath;
    const collisionPath = runtimePathCollisionKey(path);
    if (collisionPaths.has(collisionPath)) fail("studio-runtime-tar-path-invalid", path);
    collisionPaths.add(collisionPath);
  }
  await mkdir(dirname(archivePath), { recursive: true });
  const handle = await open(archivePath, "wx", 0o600);
  try {
    for (const entry of entries) {
      entry.mode = entry.type === "file" && (entry.details.mode & 0o111) !== 0 ? 0o755 : 0o644;
      await handle.write(tarHeader(entry));
      if (entry.type === "file") {
        for await (const chunk of createReadStream(entry.path)) await handle.write(chunk);
        const padding = (BLOCK_BYTES - (entry.details.size % BLOCK_BYTES)) % BLOCK_BYTES;
        if (padding > 0) await handle.write(Buffer.alloc(padding));
      }
    }
    await handle.write(ZERO_BLOCK);
    await handle.write(ZERO_BLOCK);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await chmod(archivePath, 0o600);
  return entries;
}

function readString(block, offset, length) {
  const bytes = block.subarray(offset, offset + length);
  const end = bytes.indexOf(0);
  try {
    return decodeRuntimeUtf8(bytes.subarray(0, end === -1 ? bytes.length : end));
  } catch {
    fail("studio-runtime-tar-metadata-invalid");
  }
}

function readOctal(block, offset, length) {
  const value = readString(block, offset, length).trim();
  if (!/^[0-7]*$/u.test(value)) fail("studio-runtime-tar-metadata-invalid");
  return value === "" ? 0 : Number.parseInt(value, 8);
}

export async function assertDeterministicRuntimeTar(archivePath) {
  const bytes = await import("node:fs/promises").then(({ readFile }) => readFile(archivePath));
  if (bytes.length < BLOCK_BYTES * 2 || bytes.length % BLOCK_BYTES !== 0) {
    fail("studio-runtime-tar-metadata-invalid");
  }
  let offset = 0;
  let previousPath = "";
  const collisionPaths = new Set();
  let zeroBlocks = 0;
  while (offset < bytes.length) {
    const block = bytes.subarray(offset, offset + BLOCK_BYTES);
    offset += BLOCK_BYTES;
    if (block.equals(ZERO_BLOCK)) {
      zeroBlocks += 1;
      continue;
    }
    if (zeroBlocks !== 0) fail("studio-runtime-tar-metadata-invalid");
    const storedChecksum = readOctal(block, 148, 8);
    const checksumBlock = Buffer.from(block);
    checksumBlock.fill(0x20, 148, 156);
    const computedChecksum = checksumBlock.reduce((total, byte) => total + byte, 0);
    const type = String.fromCharCode(block[156]);
    const name = readString(block, 0, 100);
    const prefix = readString(block, 345, 155);
    const pathWithoutTypeSuffix = prefix ? `${prefix}/${name}` : name;
    const path = type === "5" ? `${pathWithoutTypeSuffix}/` : pathWithoutTypeSuffix;
    const mode = readOctal(block, 100, 8);
    const size = readOctal(block, 124, 12);
    const collisionPath = runtimePathCollisionKey(pathWithoutTypeSuffix);
    if (
      storedChecksum !== computedChecksum
      || readOctal(block, 108, 8) !== 0
      || readOctal(block, 116, 8) !== 0
      || readOctal(block, 136, 12) !== 0
      || readString(block, 257, 6) !== "ustar"
      || readString(block, 263, 2) !== "00"
      || readString(block, 265, 32) !== ""
      || readString(block, 297, 32) !== ""
      || !["0", "5"].includes(type)
      || !isPortableRuntimeRelativePath(pathWithoutTypeSuffix)
      || collisionPaths.has(collisionPath)
      || (type === "5" && (mode !== 0o755 || size !== 0))
      || (type === "0" && ![0o644, 0o755].includes(mode))
      || (previousPath !== "" && compareRuntimePathsByUtf8Bytes(path, previousPath) <= 0)
    ) fail("studio-runtime-tar-metadata-invalid", path);
    collisionPaths.add(collisionPath);
    previousPath = path;
    offset += Math.ceil(size / BLOCK_BYTES) * BLOCK_BYTES;
    if (offset > bytes.length) fail("studio-runtime-tar-metadata-invalid", path);
  }
  if (zeroBlocks !== 2) fail("studio-runtime-tar-metadata-invalid");
  return { status: "deterministic", sha256: createHash("sha256").update(bytes).digest("hex") };
}

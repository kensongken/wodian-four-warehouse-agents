import { isAbsolute } from "node:path";

const PRINTABLE_ASCII = /^[\x20-\x7e]+$/u;
const ASCII_WHITESPACE = /^[\x09-\x0d\x20]$/u;
const SHA256 = /^[a-f0-9]{64}$/u;
const PACKAGE_INVENTORY_FIELDS = new Set([
  "schemaVersion", "python", "packages", "recordOwnership", "licenseReview",
]);
const PYTHON_FIELDS = new Set([
  "name", "version", "build", "sourceMaterialId", "sourceSha256",
  "licenseExpression", "licenseEvidence", "moduleRoots",
]);
const PACKAGE_FIELDS = new Set([
  "name", "version", "sourceMaterialId", "sourceSha256", "licenseExpression",
  "licenseEvidence", "moduleRoots",
]);

function exactFields(value, fields) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === fields.size
    && Object.keys(value).every((key) => fields.has(key));
}

function validMetadataString(value) {
  return typeof value === "string"
    && PRINTABLE_ASCII.test(value)
    && !ASCII_WHITESPACE.test(value[0])
    && !ASCII_WHITESPACE.test(value.at(-1));
}

function validSchemaVersion(value) {
  return typeof value === "number"
    && Number.isFinite(value)
    && Number.isInteger(value)
    && value === 1;
}

export function compareRuntimePathsByUtf8Bytes(left, right) {
  if (
    typeof left !== "string"
    || typeof right !== "string"
    || !left.isWellFormed()
    || !right.isWellFormed()
  ) {
    throw new TypeError("studio-runtime-path-comparator-input-invalid");
  }
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

export function decodeRuntimeUtf8(bytes) {
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

export function parseRuntimeJsonBytes(bytes) {
  return JSON.parse(decodeRuntimeUtf8(bytes));
}

export function isPortableRuntimeRelativePath(value, { maximumBytes } = {}) {
  if (typeof value !== "string" || !value.isWellFormed() || !PRINTABLE_ASCII.test(value)) return false;
  if (maximumBytes !== undefined && value.length > maximumBytes) return false;
  if (isAbsolute(value) || value.includes("\\")) return false;
  return value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

export function sortPortableRuntimePaths(values, options = {}) {
  const paths = [...values];
  for (const relativePath of paths) {
    if (!isPortableRuntimeRelativePath(relativePath, options)) {
      const error = new TypeError(`studio-runtime-path-invalid:${String(relativePath)}`);
      error.relativePath = relativePath;
      throw error;
    }
  }
  return paths.sort(compareRuntimePathsByUtf8Bytes);
}

function validPortablePathList(value, { singleSegment = false } = {}) {
  if (!Array.isArray(value) || value.length === 0) return false;
  const collisionKeys = new Set();
  for (const relativePath of value) {
    if (
      !isPortableRuntimeRelativePath(relativePath)
      || (singleSegment && relativePath.includes("/"))
    ) return false;
    const collisionKey = runtimePathCollisionKey(relativePath);
    if (collisionKeys.has(collisionKey)) return false;
    collisionKeys.add(collisionKey);
  }
  return true;
}

function validRuntimePackageEntry(value, fields, { pythonVersion, python = false } = {}) {
  return exactFields(value, fields)
    && validMetadataString(value.name)
    && validMetadataString(value.version)
    && (!python || value.version === pythonVersion)
    && (!python || value.build === null || validMetadataString(value.build))
    && validMetadataString(value.sourceMaterialId)
    && typeof value.sourceSha256 === "string"
    && SHA256.test(value.sourceSha256)
    && validMetadataString(value.licenseExpression)
    && validPortablePathList(value.licenseEvidence)
    && validPortablePathList(value.moduleRoots, { singleSegment: true });
}

export function isValidRuntimePackageInventory(value, { pythonVersion } = {}) {
  if (
    !exactFields(value, PACKAGE_INVENTORY_FIELDS)
    || !validSchemaVersion(value.schemaVersion)
    || !validRuntimePackageEntry(value.python, PYTHON_FIELDS, { pythonVersion, python: true })
    || !Array.isArray(value.packages)
    || value.packages.length === 0
    || !exactFields(value.recordOwnership, new Set(["unownedFiles"]))
    || !Array.isArray(value.recordOwnership.unownedFiles)
    || value.recordOwnership.unownedFiles.length !== 0
    || value.licenseReview !== "pending"
  ) return false;
  const packageNames = new Set();
  for (const packageEntry of value.packages) {
    if (!validRuntimePackageEntry(packageEntry, PACKAGE_FIELDS)) return false;
    const packageName = runtimePathCollisionKey(packageEntry.name).replace(/[-_.]+/gu, "-");
    if (packageNames.has(packageName)) return false;
    packageNames.add(packageName);
  }
  return true;
}

export function runtimePathCollisionKey(value) {
  return value.replace(/[A-Z]/gu, (character) => (
    String.fromCharCode(character.charCodeAt(0) + 0x20)
  ));
}

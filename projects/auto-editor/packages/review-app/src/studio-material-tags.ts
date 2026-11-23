import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

export type StudioMaterialTag = Readonly<{
  id: string;
  label: string;
  createdAt: string;
}>;

export type StudioMaterialTagAssignment = Readonly<{
  materialCardId: string;
  tagIds: readonly string[];
  updatedAt: string;
}>;

export type StudioMaterialTagState = Readonly<{
  version: number;
  tags: readonly StudioMaterialTag[];
  assignments: readonly StudioMaterialTagAssignment[];
}>;

type StoredOperation = Readonly<{ key: string; fingerprint: string }>;
type StoredState = Readonly<{
  schemaVersion: 1;
  customerId: string;
  version: number;
  tags: readonly StudioMaterialTag[];
  assignments: readonly StudioMaterialTagAssignment[];
  operations: readonly StoredOperation[];
}>;

const MAX_TAGS = 30;
const MAX_TAGS_PER_MATERIAL = 8;
const MAX_OPERATIONS = 256;
const OPERATION_KEY = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;

export class StudioMaterialTagStore {
  private static readonly queues = new Map<string, Promise<void>>();
  private readonly root: string;
  private readonly statePath: string;

  constructor(
    root: string,
    private readonly customerId: string,
    private readonly now: () => string = () => new Date().toISOString(),
  ) {
    if (!root.trim()) throw new Error("material-tag-root-required");
    if (!customerId.trim()) throw new Error("material-tag-customer-required");
    this.root = resolve(root);
    this.statePath = join(this.root, "material-tags.json");
  }

  async list(): Promise<StudioMaterialTagState> {
    return this.withLock(async () => publicState(await this.readState()));
  }

  async create(labelValue: unknown, operationKeyValue: unknown): Promise<StudioMaterialTagState> {
    const label = normalizeLabel(labelValue);
    const operationKey = requireOperationKey(operationKeyValue);
    const fingerprint = digest(`create\0${label}`);
    return this.withLock(async () => {
      const state = await this.readState();
      const repeated = findOperation(state, operationKey, fingerprint);
      if (repeated) return publicState(state);
      if (state.tags.some((tag) => tag.label.toLocaleLowerCase("zh-CN") === label.toLocaleLowerCase("zh-CN"))) {
        throw new Error("material-tag-label-exists");
      }
      if (state.tags.length >= MAX_TAGS) throw new Error("material-tag-limit-reached");
      const createdAt = this.now();
      const tag: StudioMaterialTag = {
        id: `tag_${digest(`${this.customerId}\0${operationKey}`)}`,
        label,
        createdAt,
      };
      const next: StoredState = {
        ...state,
        version: state.version + 1,
        tags: [...state.tags, tag],
        operations: appendOperation(state.operations, { key: operationKey, fingerprint }),
      };
      await this.writeState(next);
      return publicState(next);
    });
  }

  async assign(
    materialCardIdValue: unknown,
    tagIdsValue: unknown,
    operationKeyValue: unknown,
  ): Promise<StudioMaterialTagState> {
    const materialCardId = requireId(materialCardIdValue, "material-tag-card-id-required");
    const tagIds = requireTagIds(tagIdsValue);
    const operationKey = requireOperationKey(operationKeyValue);
    const fingerprint = digest(`assign\0${materialCardId}\0${tagIds.join("\0")}`);
    return this.withLock(async () => {
      const state = await this.readState();
      const repeated = findOperation(state, operationKey, fingerprint);
      if (repeated) return publicState(state);
      const knownTagIds = new Set(state.tags.map((tag) => tag.id));
      if (tagIds.some((tagId) => !knownTagIds.has(tagId))) throw new Error("material-tag-not-found");
      const assignment: StudioMaterialTagAssignment = {
        materialCardId,
        tagIds,
        updatedAt: this.now(),
      };
      const assignments = state.assignments.filter((item) => item.materialCardId !== materialCardId);
      if (tagIds.length) assignments.push(assignment);
      const next: StoredState = {
        ...state,
        version: state.version + 1,
        assignments,
        operations: appendOperation(state.operations, { key: operationKey, fingerprint }),
      };
      await this.writeState(next);
      return publicState(next);
    });
  }

  private async readState(): Promise<StoredState> {
    const raw = await readFile(this.statePath, "utf8").catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    if (raw === null) return emptyState(this.customerId);
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      throw new Error("material-tag-state-invalid");
    }
    return parseState(value, this.customerId);
  }

  private async writeState(state: StoredState): Promise<void> {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const temporary = join(this.root, `.material-tags-${randomUUID()}.tmp`);
    try {
      await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { flag: "wx", mode: 0o600 });
      await rename(temporary, this.statePath);
    } finally {
      await rm(temporary, { force: true });
    }
  }

  private async withLock<T>(work: () => Promise<T>): Promise<T> {
    const previous = StudioMaterialTagStore.queues.get(this.statePath) ?? Promise.resolve();
    let release!: () => void;
    const current = new Promise<void>((resolvePromise) => { release = resolvePromise; });
    const tail = previous.then(() => current);
    StudioMaterialTagStore.queues.set(this.statePath, tail);
    await previous;
    try {
      return await work();
    } finally {
      release();
      if (StudioMaterialTagStore.queues.get(this.statePath) === tail) {
        StudioMaterialTagStore.queues.delete(this.statePath);
      }
    }
  }
}

function emptyState(customerId: string): StoredState {
  return { schemaVersion: 1, customerId, version: 0, tags: [], assignments: [], operations: [] };
}

function publicState(state: StoredState): StudioMaterialTagState {
  return { version: state.version, tags: state.tags, assignments: state.assignments };
}

function normalizeLabel(value: unknown): string {
  if (typeof value !== "string") throw new Error("material-tag-label-required");
  const label = value.normalize("NFC").trim().replace(/\s+/gu, " ");
  if (!label || [...label].length > 20 || /[\u0000-\u001f\u007f]/u.test(label)) {
    throw new Error("material-tag-label-invalid");
  }
  return label;
}

function requireOperationKey(value: unknown): string {
  if (typeof value !== "string" || !OPERATION_KEY.test(value)) {
    throw new Error("material-tag-operation-key-invalid");
  }
  return value;
}

function requireId(value: unknown, code: string): string {
  if (typeof value !== "string" || !value.trim() || value.length > 256) throw new Error(code);
  return value;
}

function requireTagIds(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > MAX_TAGS_PER_MATERIAL) {
    throw new Error("material-tag-selection-invalid");
  }
  const tagIds = value.map((item) => requireId(item, "material-tag-selection-invalid"));
  if (new Set(tagIds).size !== tagIds.length) throw new Error("material-tag-selection-invalid");
  return [...tagIds].sort();
}

function findOperation(state: StoredState, key: string, fingerprint: string): boolean {
  const existing = state.operations.find((operation) => operation.key === key);
  if (!existing) return false;
  if (existing.fingerprint !== fingerprint) throw new Error("material-tag-operation-conflict");
  return true;
}

function appendOperation(operations: readonly StoredOperation[], operation: StoredOperation): StoredOperation[] {
  return [...operations, operation].slice(-MAX_OPERATIONS);
}

function parseState(value: unknown, customerId: string): StoredState {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("material-tag-state-invalid");
  const input = value as Partial<StoredState>;
  if (input.schemaVersion !== 1 || input.customerId !== customerId ||
      !Number.isSafeInteger(input.version) || Number(input.version) < 0 ||
      !Array.isArray(input.tags) || !Array.isArray(input.assignments) || !Array.isArray(input.operations)) {
    throw new Error("material-tag-state-invalid");
  }
  const tags = input.tags.map((tag) => {
    if (!tag || typeof tag !== "object") throw new Error("material-tag-state-invalid");
    return {
      id: requireId((tag as StudioMaterialTag).id, "material-tag-state-invalid"),
      label: normalizeLabel((tag as StudioMaterialTag).label),
      createdAt: requireId((tag as StudioMaterialTag).createdAt, "material-tag-state-invalid"),
    };
  });
  const tagIds = new Set(tags.map((tag) => tag.id));
  if (tagIds.size !== tags.length || tags.length > MAX_TAGS) throw new Error("material-tag-state-invalid");
  const assignments = input.assignments.map((assignment) => {
    if (!assignment || typeof assignment !== "object") throw new Error("material-tag-state-invalid");
    const parsed = assignment as StudioMaterialTagAssignment;
    const assignedTagIds = requireTagIds(parsed.tagIds);
    if (assignedTagIds.some((tagId) => !tagIds.has(tagId))) throw new Error("material-tag-state-invalid");
    return {
      materialCardId: requireId(parsed.materialCardId, "material-tag-state-invalid"),
      tagIds: assignedTagIds,
      updatedAt: requireId(parsed.updatedAt, "material-tag-state-invalid"),
    };
  });
  if (new Set(assignments.map((assignment) => assignment.materialCardId)).size !== assignments.length) {
    throw new Error("material-tag-state-invalid");
  }
  const operations = input.operations.map((operation) => {
    if (!operation || typeof operation !== "object") throw new Error("material-tag-state-invalid");
    const parsed = operation as StoredOperation;
    return {
      key: requireOperationKey(parsed.key),
      fingerprint: requireId(parsed.fingerprint, "material-tag-state-invalid"),
    };
  });
  return {
    schemaVersion: 1,
    customerId,
    version: Number(input.version),
    tags,
    assignments,
    operations,
  };
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === code);
}

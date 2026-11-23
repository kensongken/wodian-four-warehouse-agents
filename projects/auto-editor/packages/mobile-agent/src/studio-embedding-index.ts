import { createHash, randomUUID } from "node:crypto";
import { open as openFile, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import { z } from "zod";

import type { StudioScope } from "./studio-capabilities";

const MAX_ENTRIES = 100;
const MIN_VECTOR_DIMENSION = 2;
const MAX_VECTOR_DIMENSION = 2_048;
const MIN_SCENE_STRUCTURE_SCORE = 0.8;
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const IdSchema = z.string().trim().min(1).max(200);
const DateTimeSchema = z.string().datetime();
const EmbeddingSpaceSchema = z.object({
  artifactId: IdSchema,
  modelVersion: IdSchema,
  checkpointDigest: DigestSchema,
}).strict();
const SceneSchema = z.object({
  startMs: z.number().int().nonnegative(),
  endMs: z.number().int().positive(),
}).strict().refine((scene) => scene.endMs > scene.startMs, "studio-embedding-scene-invalid");
const ScenesSchema = z.array(SceneSchema).min(1).max(512).superRefine((scenes, context) => {
  for (let index = 1; index < scenes.length; index += 1) {
    if (scenes[index]!.startMs < scenes[index - 1]!.endMs) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [index],
        message: "studio-embedding-scenes-not-ordered",
      });
    }
  }
});
const StoredVectorSchema = z.array(z.number().finite())
  .min(MIN_VECTOR_DIMENSION)
  .max(MAX_VECTOR_DIMENSION)
  .superRefine((vector, context) => {
    if (Math.abs(vectorNorm(vector) - 1) > 1e-9) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "studio-embedding-vector-not-normalized" });
    }
  });
const StoredEntrySchema = z.object({
  assetId: IdSchema,
  sourceDigest: DigestSchema,
  embeddingSpace: EmbeddingSpaceSchema,
  vector: StoredVectorSchema,
  scenes: ScenesSchema,
  addedAt: DateTimeSchema,
}).strict();
const KeepDecisionSchema = z.object({
  operationKey: IdSchema,
  groupAssetIds: z.array(IdSchema).min(2).max(MAX_ENTRIES),
  keepAssetIds: z.array(IdSchema).min(1).max(MAX_ENTRIES),
  createdAt: DateTimeSchema,
}).strict().superRefine((decision, context) => {
  if (new Set(decision.groupAssetIds).size !== decision.groupAssetIds.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["groupAssetIds"], message: "duplicate-asset-id" });
  }
  if (new Set(decision.keepAssetIds).size !== decision.keepAssetIds.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["keepAssetIds"], message: "duplicate-asset-id" });
  }
  const group = new Set(decision.groupAssetIds);
  if (decision.keepAssetIds.some((assetId) => !group.has(assetId))) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["keepAssetIds"], message: "keep-asset-outside-group" });
  }
});
const StateSchema = z.object({
  schemaVersion: z.literal(1),
  actorId: IdSchema,
  customerId: IdSchema,
  sessionId: IdSchema,
  dimension: z.number().int().min(MIN_VECTOR_DIMENSION).max(MAX_VECTOR_DIMENSION).nullable(),
  embeddingSpace: EmbeddingSpaceSchema.nullable(),
  entries: z.array(StoredEntrySchema).max(MAX_ENTRIES),
  decisions: z.array(KeepDecisionSchema),
}).strict().superRefine((state, context) => {
  if (new Set(state.entries.map((entry) => entry.assetId)).size !== state.entries.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["entries"], message: "duplicate-asset-id" });
  }
  if (new Set(state.decisions.map((decision) => decision.operationKey)).size !== state.decisions.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["decisions"], message: "duplicate-operation-key" });
  }
  if ((state.entries.length === 0) !== (state.dimension === null)) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["dimension"], message: "embedding-dimension-state-invalid" });
  }
  if ((state.entries.length === 0) !== (state.embeddingSpace === null)) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["embeddingSpace"], message: "embedding-space-state-invalid" });
  }
  if (state.dimension !== null && state.entries.some((entry) => entry.vector.length !== state.dimension)) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["entries"], message: "embedding-dimension-state-invalid" });
  }
  if (
    state.embeddingSpace !== null
    && state.entries.some((entry) => !sameEmbeddingSpace(entry.embeddingSpace, state.embeddingSpace!))
  ) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["entries"], message: "embedding-space-state-invalid" });
  }
  const assetIds = new Set(state.entries.map((entry) => entry.assetId));
  for (const [index, decision] of state.decisions.entries()) {
    if (decision.groupAssetIds.some((assetId) => !assetIds.has(assetId))) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["decisions", index, "groupAssetIds"],
        message: "embedding-decision-asset-missing",
      });
    }
  }
});

export type StudioEmbeddingInput = Readonly<{
  assetId: string;
  sourceDigest: string;
  embeddingSpace: StudioEmbeddingSpaceIdentity;
  vector: readonly number[];
  scenes: readonly Readonly<{ startMs: number; endMs: number }>[];
}>;

export type StudioEmbeddingSpaceIdentity = Readonly<z.infer<typeof EmbeddingSpaceSchema>>;

export type StudioEmbeddingSearchResult = Readonly<{
  assetId: string;
  similarity: number;
}>;

export type StudioDuplicateGroup = Readonly<{
  kind: "exact" | "near";
  assetIds: readonly string[];
}>;

export type StudioKeepDecisionInput = Readonly<{
  operationKey: string;
  groupAssetIds: readonly string[];
  keepAssetIds: readonly string[];
}>;

export type StudioKeepDecision = z.infer<typeof KeepDecisionSchema>;
type StoredEntry = z.infer<typeof StoredEntrySchema>;
type State = z.infer<typeof StateSchema>;

export type StudioEmbeddingIndexOptions = Readonly<{
  now?: () => string;
  atomicRename?: (from: string, to: string) => Promise<void>;
}>;

/** Small local index whose scope is part of both its path and persisted state. */
export class StudioEmbeddingIndex {
  private static readonly operationQueues = new Map<string, Promise<void>>();
  private readonly root: string;
  private readonly now: () => string;
  private readonly atomicRename: (from: string, to: string) => Promise<void>;

  constructor(root: string, options: StudioEmbeddingIndexOptions = {}) {
    if (!root.trim()) throw new Error("studio-embedding-index-root-required");
    this.root = resolve(root);
    this.now = options.now ?? (() => new Date().toISOString());
    this.atomicRename = options.atomicRename ?? rename;
  }

  async add(scopeInput: StudioScope, input: StudioEmbeddingInput): Promise<void> {
    const scope = parseScope(scopeInput);
    const path = this.statePath(scope);
    await enqueueLock(StudioEmbeddingIndex.operationQueues, path, async () => {
      const state = await this.load(scope);
      const entry = parseEntry(input, this.timestamp());
      if (state.embeddingSpace !== null && !sameEmbeddingSpace(entry.embeddingSpace, state.embeddingSpace)) {
        throw new Error("studio-embedding-space-mismatch");
      }
      if (state.dimension !== null && entry.vector.length !== state.dimension) {
        throw new Error("studio-embedding-vector-dimension-mismatch");
      }
      const existing = state.entries.find((candidate) => candidate.assetId === entry.assetId);
      if (existing) {
        if (!sameEntryBinding(existing, entry)) throw new Error("studio-embedding-asset-conflict");
        return;
      }
      if (state.entries.length >= MAX_ENTRIES) {
        throw new Error("studio-embedding-index-capacity-exceeded");
      }
      const next = parseState({
        ...state,
        dimension: state.dimension ?? entry.vector.length,
        embeddingSpace: state.embeddingSpace ?? entry.embeddingSpace,
        entries: [...state.entries, entry],
      }, scope);
      await this.persist(scope, next);
    });
  }

  async search(
    scopeInput: StudioScope,
    textVector: readonly number[],
    limitInput: number,
    embeddingSpaceInput: StudioEmbeddingSpaceIdentity,
  ): Promise<StudioEmbeddingSearchResult[]> {
    const scope = parseScope(scopeInput);
    const limit = parseLimit(limitInput);
    const embeddingSpace = parseEmbeddingSpace(embeddingSpaceInput);
    const query = normalizeVector(textVector);
    return enqueueLock(StudioEmbeddingIndex.operationQueues, this.statePath(scope), async () => {
      const state = await this.load(scope);
      if (state.entries.length === 0) return [];
      if (!sameEmbeddingSpace(state.embeddingSpace!, embeddingSpace)) {
        throw new Error("studio-embedding-space-mismatch");
      }
      if (query.length !== state.dimension) throw new Error("studio-embedding-vector-dimension-mismatch");
      return state.entries
        .map((entry) => ({ assetId: entry.assetId, similarity: roundSimilarity(cosine(entry.vector, query)) }))
        .sort((left, right) => right.similarity - left.similarity || left.assetId.localeCompare(right.assetId))
        .slice(0, limit);
    });
  }

  async groups(scopeInput: StudioScope, thresholdInput: number): Promise<StudioDuplicateGroup[]> {
    const scope = parseScope(scopeInput);
    const threshold = parseThreshold(thresholdInput);
    return enqueueLock(StudioEmbeddingIndex.operationQueues, this.statePath(scope), async () =>
      duplicateGroups((await this.load(scope)).entries, threshold));
  }

  async groupNearDuplicates(
    scope: StudioScope,
    threshold: number,
  ): Promise<StudioDuplicateGroup[]> {
    return (await this.groups(scope, threshold)).filter((group) => group.kind === "near");
  }

  async recordKeepDecision(
    scopeInput: StudioScope,
    input: StudioKeepDecisionInput,
  ): Promise<StudioKeepDecision> {
    const scope = parseScope(scopeInput);
    const path = this.statePath(scope);
    return enqueueLock(StudioEmbeddingIndex.operationQueues, path, async () => {
      const state = await this.load(scope);
      const normalized = parseKeepDecision(input, this.timestamp());
      const existing = state.decisions.find((decision) => decision.operationKey === normalized.operationKey);
      if (existing) {
        if (
          canonical(existing.groupAssetIds) !== canonical(normalized.groupAssetIds)
          || canonical(existing.keepAssetIds) !== canonical(normalized.keepAssetIds)
        ) {
          throw new Error("studio-embedding-decision-conflict");
        }
        return clone(existing);
      }
      const available = new Set(state.entries.map((entry) => entry.assetId));
      if (normalized.groupAssetIds.some((assetId) => !available.has(assetId))) {
        throw new Error("studio-embedding-decision-asset-missing");
      }
      const next = parseState({ ...state, decisions: [...state.decisions, normalized] }, scope);
      await this.persist(scope, next);
      return clone(normalized);
    });
  }

  async decisions(scopeInput: StudioScope): Promise<StudioKeepDecision[]> {
    const scope = parseScope(scopeInput);
    return enqueueLock(StudioEmbeddingIndex.operationQueues, this.statePath(scope), async () =>
      clone((await this.load(scope)).decisions));
  }

  private async load(scope: StudioScope): Promise<State> {
    const path = this.statePath(scope);
    const raw = await readFile(path, "utf8").catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    if (raw === null) return emptyState(scope);
    try {
      return parseState(JSON.parse(raw), scope);
    } catch {
      throw new Error("studio-embedding-index-state-invalid");
    }
  }

  private async persist(scope: StudioScope, state: State): Promise<void> {
    const path = this.statePath(scope);
    await mkdir(dirname(path), { recursive: true });
    const pending = `${path}.next-${randomUUID()}`;
    try {
      await writeFile(pending, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
      const handle = await openFile(pending, "r+");
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
      await this.atomicRename(pending, path);
    } finally {
      await unlink(pending).catch(() => undefined);
    }
  }

  private statePath(scope: StudioScope): string {
    const key = digest(`${scope.customerId}\0${scope.sessionId}\0${scope.actorId}`);
    return join(this.root, "embedding-scopes", key, "embedding-index.json");
  }

  private timestamp(): string {
    const value = this.now();
    try {
      return DateTimeSchema.parse(value);
    } catch {
      throw new Error("studio-embedding-timestamp-invalid");
    }
  }
}

function parseScope(scope: StudioScope): StudioScope {
  try {
    return z.object({ actorId: IdSchema, customerId: IdSchema, sessionId: IdSchema }).strict().parse(scope);
  } catch {
    throw new Error("studio-embedding-scope-invalid");
  }
}

function parseEntry(input: StudioEmbeddingInput, addedAt: string): StoredEntry {
  if (!input || typeof input !== "object") throw new Error("studio-embedding-input-invalid");
  const vector = normalizeVector(input.vector);
  try {
    return StoredEntrySchema.parse({
      assetId: input.assetId,
      sourceDigest: input.sourceDigest,
      embeddingSpace: parseEmbeddingSpace(input.embeddingSpace),
      vector,
      scenes: input.scenes,
      addedAt,
    });
  } catch {
    throw new Error("studio-embedding-input-invalid");
  }
}

function parseEmbeddingSpace(input: StudioEmbeddingSpaceIdentity): StudioEmbeddingSpaceIdentity {
  try {
    return EmbeddingSpaceSchema.parse(input);
  } catch {
    throw new Error("studio-embedding-space-invalid");
  }
}

function parseKeepDecision(input: StudioKeepDecisionInput, createdAt: string): StudioKeepDecision {
  try {
    return KeepDecisionSchema.parse({
      operationKey: input.operationKey,
      groupAssetIds: [...input.groupAssetIds].sort(),
      keepAssetIds: [...input.keepAssetIds].sort(),
      createdAt,
    });
  } catch {
    throw new Error("studio-embedding-decision-invalid");
  }
}

function parseState(input: unknown, scope: StudioScope): State {
  const state = StateSchema.parse(input);
  if (
    state.actorId !== scope.actorId
    || state.customerId !== scope.customerId
    || state.sessionId !== scope.sessionId
  ) {
    throw new Error("studio-embedding-index-state-invalid");
  }
  return state;
}

function emptyState(scope: StudioScope): State {
  return {
    schemaVersion: 1,
    actorId: scope.actorId,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    dimension: null,
    embeddingSpace: null,
    entries: [],
    decisions: [],
  };
}

function normalizeVector(input: readonly number[]): number[] {
  if (!Array.isArray(input) || input.length < MIN_VECTOR_DIMENSION || input.length > MAX_VECTOR_DIMENSION) {
    throw new Error("studio-embedding-vector-dimension-invalid");
  }
  if (input.some((value) => typeof value !== "number" || !Number.isFinite(value))) {
    throw new Error("studio-embedding-vector-invalid");
  }
  const norm = vectorNorm(input);
  if (!Number.isFinite(norm) || norm <= Number.EPSILON) {
    throw new Error("studio-embedding-vector-zero");
  }
  return input.map((value) => value / norm);
}

function vectorNorm(vector: readonly number[]): number {
  return Math.hypot(...vector);
}

function cosine(left: readonly number[], right: readonly number[]): number {
  let result = 0;
  for (let index = 0; index < left.length; index += 1) result += left[index]! * right[index]!;
  return Math.max(-1, Math.min(1, result));
}

function duplicateGroups(entries: readonly StoredEntry[], threshold: number): StudioDuplicateGroup[] {
  const byDigest = new Map<string, StoredEntry[]>();
  for (const entry of entries) {
    const group = byDigest.get(entry.sourceDigest) ?? [];
    group.push(entry);
    byDigest.set(entry.sourceDigest, group);
  }
  const exact = [...byDigest.values()]
    .filter((group) => group.length > 1)
    .map((group) => ({
      kind: "exact" as const,
      assetIds: group.map((entry) => entry.assetId).sort(),
    }))
    .sort(compareGroups);

  const representatives = [...byDigest.values()]
    .map((group) => [...group].sort((left, right) => left.assetId.localeCompare(right.assetId))[0]!)
    .sort((left, right) => left.assetId.localeCompare(right.assetId));
  const parent = new Map(representatives.map((entry) => [entry.assetId, entry.assetId]));
  const find = (assetId: string): string => {
    const current = parent.get(assetId)!;
    if (current === assetId) return current;
    const root = find(current);
    parent.set(assetId, root);
    return root;
  };
  const joinGroups = (left: string, right: string) => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot !== rightRoot) parent.set(rightRoot, leftRoot < rightRoot ? leftRoot : rightRoot);
    if (leftRoot !== rightRoot && leftRoot > rightRoot) parent.set(leftRoot, rightRoot);
  };
  for (let leftIndex = 0; leftIndex < representatives.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < representatives.length; rightIndex += 1) {
      const left = representatives[leftIndex]!;
      const right = representatives[rightIndex]!;
      if (
        cosine(left.vector, right.vector) >= threshold
        && sceneStructureScore(left.scenes, right.scenes) >= MIN_SCENE_STRUCTURE_SCORE
      ) {
        joinGroups(left.assetId, right.assetId);
      }
    }
  }
  const nearByRoot = new Map<string, string[]>();
  for (const entry of representatives) {
    const root = find(entry.assetId);
    const group = nearByRoot.get(root) ?? [];
    group.push(entry.assetId);
    nearByRoot.set(root, group);
  }
  const near = [...nearByRoot.values()]
    .filter((assetIds) => assetIds.length > 1)
    .map((assetIds) => ({ kind: "near" as const, assetIds: assetIds.sort() }))
    .sort(compareGroups);
  return [...exact, ...near];
}

function sceneStructureScore(
  left: readonly Readonly<{ startMs: number; endMs: number }>[],
  right: readonly Readonly<{ startMs: number; endMs: number }>[],
): number {
  const leftDurations = left.map((scene) => scene.endMs - scene.startMs);
  const rightDurations = right.map((scene) => scene.endMs - scene.startMs);
  const paired = Math.min(leftDurations.length, rightDurations.length);
  let overlap = 0;
  let union = 0;
  for (let index = 0; index < paired; index += 1) {
    overlap += Math.min(leftDurations[index]!, rightDurations[index]!);
    union += Math.max(leftDurations[index]!, rightDurations[index]!);
  }
  union += leftDurations.slice(paired).reduce((sum, value) => sum + value, 0);
  union += rightDurations.slice(paired).reduce((sum, value) => sum + value, 0);
  return union === 0 ? 0 : overlap / union;
}

function parseLimit(input: number): number {
  if (!Number.isInteger(input) || input < 1 || input > MAX_ENTRIES) {
    throw new Error("studio-embedding-search-limit-invalid");
  }
  return input;
}

function parseThreshold(input: number): number {
  if (!Number.isFinite(input) || input < 0 || input > 1) {
    throw new Error("studio-embedding-threshold-invalid");
  }
  return input;
}

function roundSimilarity(value: number): number {
  const rounded = Math.round(value * 1_000_000) / 1_000_000;
  return Object.is(rounded, -0) ? 0 : rounded;
}

function compareGroups(left: StudioDuplicateGroup, right: StudioDuplicateGroup): number {
  return left.assetIds.join("\0").localeCompare(right.assetIds.join("\0"));
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonical(value: unknown): string {
  return JSON.stringify(value);
}

function sameEntryBinding(left: StoredEntry, right: StoredEntry): boolean {
  return left.assetId === right.assetId
    && left.sourceDigest === right.sourceDigest
    && sameEmbeddingSpace(left.embeddingSpace, right.embeddingSpace)
    && canonical(left.vector) === canonical(right.vector)
    && canonical(left.scenes) === canonical(right.scenes);
}

function sameEmbeddingSpace(
  left: StudioEmbeddingSpaceIdentity,
  right: StudioEmbeddingSpaceIdentity,
): boolean {
  return left.artifactId === right.artifactId
    && left.modelVersion === right.modelVersion
    && left.checkpointDigest === right.checkpointDigest;
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === code);
}

function enqueueLock<T>(
  queues: Map<string, Promise<void>>,
  key: string,
  work: () => Promise<T>,
): Promise<T> {
  const previous = queues.get(key) ?? Promise.resolve();
  const current = previous.catch(() => undefined).then(work);
  const tail = current.then(() => undefined, () => undefined);
  queues.set(key, tail);
  return current.finally(() => {
    if (queues.get(key) === tail) queues.delete(key);
  });
}

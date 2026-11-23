import { mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  StudioEmbeddingIndex,
  type StudioEmbeddingInput,
  type StudioEmbeddingSpaceIdentity,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};
const embeddingSpace: StudioEmbeddingSpaceIdentity = {
  artifactId: "openclip-vit-b-32",
  modelVersion: "3.3.0-fixture",
  checkpointDigest: "9".repeat(64),
};
const mismatchedEmbeddingSpaces: StudioEmbeddingSpaceIdentity[] = [
  { ...embeddingSpace, artifactId: "other-openclip-vit-b-32" },
  { ...embeddingSpace, modelVersion: "3.3.1-fixture" },
  { ...embeddingSpace, checkpointDigest: "8".repeat(64) },
];

describe("studio embedding index", () => {
  let root: string;
  let index: StudioEmbeddingIndex;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "studio-embedding-index-"));
    index = new StudioEmbeddingIndex(root, {
      now: () => "2026-09-12T08:00:00.000Z",
    });
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("separates exact digest duplicates from structurally compatible visual near duplicates", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [1, 0], [1_000, 2_000]));
    await index.add(scope, embedding("b", "a".repeat(64), [0, 1], [800, 2_200]));
    await index.add(scope, embedding("c", "c".repeat(64), [0.999, 0.02], [1_050, 1_950]));
    await index.add(scope, embedding("d", "d".repeat(64), [1, 0], [200, 2_800]));

    expect(await index.groups(scope, 0.97)).toEqual([
      { kind: "exact", assetIds: ["a", "b"] },
      { kind: "near", assetIds: ["a", "c"] },
    ]);
    expect(await index.groupNearDuplicates(scope, 0.97)).toEqual([
      { kind: "near", assetIds: ["a", "c"] },
    ]);
  });

  it("normalizes stored and query vectors while rejecting non-finite, zero, and mismatched dimensions", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [3, 4], [1_000]));

    expect(await index.search(scope, [10, 0], 10, embeddingSpace)).toEqual([
      { assetId: "a", similarity: 0.6 },
    ]);
    await expect(index.add(scope, embedding("nan", "b".repeat(64), [Number.NaN, 1], [1_000])))
      .rejects.toThrow("studio-embedding-vector-invalid");
    await expect(index.add(scope, embedding("zero", "c".repeat(64), [0, 0], [1_000])))
      .rejects.toThrow("studio-embedding-vector-zero");
    await expect(index.add(scope, embedding("short", "d".repeat(64), [1], [1_000])))
      .rejects.toThrow("studio-embedding-vector-dimension-invalid");
    await expect(index.add(scope, embedding("long", "e".repeat(64), Array(2_049).fill(1), [1_000])))
      .rejects.toThrow("studio-embedding-vector-dimension-invalid");
    await expect(index.add(scope, embedding("mismatch", "f".repeat(64), [1, 0, 0], [1_000])))
      .rejects.toThrow("studio-embedding-vector-dimension-mismatch");
    await expect(index.search(scope, [1, 0, 0], 10, embeddingSpace))
      .rejects.toThrow("studio-embedding-vector-dimension-mismatch");
  });

  it("isolates search and decisions by customer, session, and actor", async () => {
    await index.add(scope, embedding("private-a", "a".repeat(64), [1, 0], [1_000]));
    const otherScopes = [
      { ...scope, customerId: "customer-b" },
      { ...scope, sessionId: "session-b" },
      { ...scope, actorId: "actor-b" },
    ];

    for (const other of otherScopes) {
      await expect(index.search(other, [1, 0], 10, embeddingSpace)).resolves.toEqual([]);
      await expect(index.decisions(other)).resolves.toEqual([]);
    }
  });

  it("validates an empty-index query before returning zero rows", async () => {
    for (const [query, error] of [
      [[Number.NaN, 1], "studio-embedding-vector-invalid"],
      [[0, 0], "studio-embedding-vector-zero"],
      [[1], "studio-embedding-vector-dimension-invalid"],
      [Array(2_049).fill(1), "studio-embedding-vector-dimension-invalid"],
    ] as const) {
      await expect(index.search(scope, query, 10, embeddingSpace)).rejects.toThrow(error);
    }
    await expect(index.search(scope, [1, 0], 10, embeddingSpace)).resolves.toEqual([]);
  });

  it("rejects overlapping or out-of-order scenes on add and reload", async () => {
    for (const [assetId, scenes] of [
      ["overlap", [{ startMs: 0, endMs: 1_000 }, { startMs: 900, endMs: 2_000 }]],
      ["out-of-order", [{ startMs: 1_000, endMs: 2_000 }, { startMs: 0, endMs: 1_000 }]],
    ] as const) {
      await expect(index.add(scope, {
        ...embedding(assetId, "a".repeat(64), [1, 0], [1_000]),
        scenes,
      })).rejects.toThrow("studio-embedding-input-invalid");
    }

    await index.add(scope, embedding("valid", "b".repeat(64), [1, 0], [1_000, 1_000]));
    const [statePath] = await findFiles(root, "embedding-index.json");
    const persisted = JSON.parse(await readFile(statePath!, "utf8"));
    persisted.entries[0].scenes[1].startMs = 900;
    await writeFile(statePath!, `${JSON.stringify(persisted)}\n`);

    await expect(new StudioEmbeddingIndex(root).search(scope, [1, 0], 10, embeddingSpace))
      .rejects.toThrow("studio-embedding-index-state-invalid");
  });

  it("persists an atomically replaceable scoped index and validates it after restart", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [3, 4], [1_000, 2_000]));
    const restarted = new StudioEmbeddingIndex(root);

    await expect(restarted.search(scope, [3, 4], 10, embeddingSpace)).resolves.toEqual([
      { assetId: "a", similarity: 1 },
    ]);
    const stateFiles = await findFiles(root, "embedding-index.json");
    expect(stateFiles).toHaveLength(1);
    expect((await findFiles(root, ".next-", true))).toEqual([]);
    const persisted = JSON.parse(await readFile(stateFiles[0]!, "utf8"));
    expect(persisted).toMatchObject({
      schemaVersion: 1,
      actorId: scope.actorId,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      dimension: 2,
      embeddingSpace,
    });

    persisted.actorId = "actor-swapped";
    await writeFile(stateFiles[0]!, `${JSON.stringify(persisted)}\n`);
    await expect(new StudioEmbeddingIndex(root).search(scope, [1, 0], 10, embeddingSpace))
      .rejects.toThrow("studio-embedding-index-state-invalid");
  });

  it("replays the same asset binding without changing its immutable timestamp", async () => {
    let clock = 0;
    index = new StudioEmbeddingIndex(root, {
      now: () => `2026-09-12T08:00:0${clock++}.000Z`,
    });
    const input = embedding("a", "a".repeat(64), [3, 4], [1_000]);

    await index.add(scope, input);
    const [statePath] = await findFiles(root, "embedding-index.json");
    const before = await readFile(statePath!, "utf8");
    await index.add(scope, input);

    expect(await readFile(statePath!, "utf8")).toBe(before);
  });

  it("keeps the previous complete JSON state when the atomic replace fails", async () => {
    let failReplace = false;
    index = new StudioEmbeddingIndex(root, {
      now: () => "2026-09-12T08:00:00.000Z",
      atomicRename: async (from, to) => {
        if (failReplace) throw new Error("injected-rename-failure");
        await rename(from, to);
      },
    });
    await index.add(scope, embedding("a", "a".repeat(64), [1, 0], [1_000]));
    failReplace = true;

    await expect(index.add(scope, embedding("b", "b".repeat(64), [0, 1], [1_000])))
      .rejects.toThrow("injected-rename-failure");
    await expect(new StudioEmbeddingIndex(root).search(scope, [1, 0], 10, embeddingSpace)).resolves.toEqual([
      { assetId: "a", similarity: 1 },
    ]);
    expect((await findFiles(root, ".next-", true))).toEqual([]);
  });

  it("enforces the 100-video V1 cap without changing the existing index", async () => {
    for (let position = 0; position < 100; position += 1) {
      await index.add(scope, embedding(
        `asset-${position.toString().padStart(3, "0")}`,
        position.toString(16).padStart(64, "0"),
        [1, position + 1],
        [1_000],
      ));
    }
    await expect(index.add(scope, embedding("asset-100", "f".repeat(64), [1, 101], [1_000])))
      .rejects.toThrow("studio-embedding-index-capacity-exceeded");
    expect(await index.search(scope, [1, 1], 100, embeddingSpace)).toHaveLength(100);
  }, 20_000);

  it("records immutable keep decisions without deleting any suggested asset", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [1, 0], [1_000]));
    await index.add(scope, embedding("b", "a".repeat(64), [1, 0], [1_000]));
    const input = {
      operationKey: "keep-choice-1",
      groupAssetIds: ["a", "b"],
      keepAssetIds: ["a"],
    };

    const first = await index.recordKeepDecision(scope, input);
    await expect(index.recordKeepDecision(scope, input)).resolves.toEqual(first);
    await expect(index.recordKeepDecision(scope, { ...input, keepAssetIds: ["b"] }))
      .rejects.toThrow("studio-embedding-decision-conflict");
    expect(await index.groups(scope, 0.97)).toEqual([
      { kind: "exact", assetIds: ["a", "b"] },
    ]);
    expect((await index.search(scope, [1, 0], 10, embeddingSpace)).map((result) => result.assetId))
      .toEqual(["a", "b"]);
    await expect(new StudioEmbeddingIndex(root).decisions(scope)).resolves.toEqual([first]);
  });

  it("rejects an add from a different embedding space even when dimensions match", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [1, 0], [1_000]));

    for (const [position, otherSpace] of mismatchedEmbeddingSpaces.entries()) {
      await expect(index.add(scope, {
        ...embedding(`b-${position}`, "b".repeat(64), [0, 1], [1_000]),
        embeddingSpace: otherSpace,
      })).rejects.toThrow("studio-embedding-space-mismatch");
    }
  });

  it("binds persisted entries and search to one complete embedding space", async () => {
    await index.add(scope, embedding("a", "a".repeat(64), [1, 0], [1_000]));

    for (const otherSpace of mismatchedEmbeddingSpaces) {
      await expect(index.search(scope, [1, 0], 10, otherSpace))
        .rejects.toThrow("studio-embedding-space-mismatch");
    }
    await expect(new StudioEmbeddingIndex(root).search(scope, [1, 0], 10, embeddingSpace))
      .resolves.toEqual([{ assetId: "a", similarity: 1 }]);

    const [statePath] = await findFiles(root, "embedding-index.json");
    const persisted = JSON.parse(await readFile(statePath!, "utf8"));
    expect(persisted.embeddingSpace).toEqual(embeddingSpace);
    expect(persisted.entries[0].embeddingSpace).toEqual(embeddingSpace);
    persisted.entries[0].embeddingSpace = mismatchedEmbeddingSpaces[2];
    await writeFile(statePath!, `${JSON.stringify(persisted)}\n`);
    await expect(new StudioEmbeddingIndex(root).search(scope, [1, 0], 10, embeddingSpace))
      .rejects.toThrow("studio-embedding-index-state-invalid");
  });
});

function embedding(
  assetId: string,
  sourceDigest: string,
  vector: number[],
  sceneDurationsMs: number[],
): StudioEmbeddingInput {
  let cursor = 0;
  return {
    assetId,
    sourceDigest,
    embeddingSpace,
    vector,
    scenes: sceneDurationsMs.map((durationMs) => {
      const scene = { startMs: cursor, endMs: cursor + durationMs };
      cursor += durationMs;
      return scene;
    }),
  };
}

async function findFiles(root: string, match: string, includes = false): Promise<string[]> {
  const found: string[] = [];
  for (const entry of await readdir(root, { withFileTypes: true }).catch(() => [])) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) found.push(...await findFiles(path, match, includes));
    if (entry.isFile() && (includes ? entry.name.includes(match) : entry.name === match)) {
      found.push(path);
    }
  }
  return found;
}

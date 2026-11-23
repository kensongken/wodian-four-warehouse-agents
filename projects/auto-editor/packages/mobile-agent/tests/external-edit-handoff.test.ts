import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { DraftPackage } from "@wearable/contracts";

import {
  ExternalEditHandoffService,
  type CreateExternalEditHandoffInput,
} from "../src";

const parentDraft: DraftPackage = {
  id: "draft-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  revisionProposalId: null,
  briefId: "brief-1",
  blueprintId: "blueprint-1",
  blueprintDigest: "b".repeat(64),
  outputDigest: "c".repeat(64),
  outputPath: "/synthetic-output/draft-1.mp4",
  durationSeconds: 35,
  sourceMap: [{
    shotId: "shot-1",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    reason: "show-process-proof",
  }],
  status: "reviewable",
  capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
  publishClaimed: false,
  createdAt: "2026-09-30T08:00:00.000Z",
};

const scope = { actorId: "actor-a", customerId: "customer-a", sessionId: "session-1" };
const input: CreateExternalEditHandoffInput = {
  parentDraftId: "draft-1",
  parentDraftVersion: 1,
  parentDraftDigest: "c".repeat(64),
  platformVariant: "wechat-channels",
  revisionInstruction: "开头自然一点，保留打磨过程。",
  mustKeep: ["打磨过程"],
  includeSources: true,
  sourceAssetBindings: [{ sourceAssetId: "asset-a", digest: "a".repeat(64) }],
  operationKey: "create-handoff-1",
};

describe("external edit handoff service", () => {
  let root: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "external-edit-handoff-"));
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("creates one customer-bound handoff and reuses the same operation", async () => {
    const service = new ExternalEditHandoffService({
      root,
      now: () => "2026-09-30T08:00:00.000Z",
      resolveParentDraft: async () => parentDraft,
    });

    const first = await service.create(scope, input);
    const retry = await service.create(scope, input);

    expect(first).toMatchObject({
      id: retry.id,
      customerId: "customer-a",
      sessionId: "session-1",
      parentDraftId: "draft-1",
      mode: "file-package",
      status: "preparing",
      publishClaimed: false,
    });
    expect(await service.list(scope)).toHaveLength(1);
  });

  it("allows an adopted draft to continue into external refinement but rejects an unselected draft", async () => {
    const adoptedService = new ExternalEditHandoffService({
      root,
      resolveParentDraft: async () => ({ ...parentDraft, status: "adopted" }),
    });

    await expect(adoptedService.create(scope, input)).resolves.toMatchObject({
      parentDraftId: "draft-1",
      status: "preparing",
    });

    const rejectedService = new ExternalEditHandoffService({
      root: join(root, "rejected"),
      resolveParentDraft: async () => ({ ...parentDraft, status: "rejected" }),
    });

    await expect(rejectedService.create(scope, input)).rejects.toThrow("external-edit-parent-changed");
  });

  it("rejects a parent draft from another customer or session and rejects stale parent evidence", async () => {
    const service = new ExternalEditHandoffService({
      root,
      resolveParentDraft: async () => parentDraft,
    });

    await expect(service.create({ ...scope, customerId: "customer-b" }, input)).rejects.toThrow("external-edit-parent-not-found");
    await expect(service.create({ ...scope, sessionId: "session-b" }, input)).rejects.toThrow("external-edit-parent-not-found");
    await expect(service.create(scope, {
      ...input,
      parentDraftVersion: 2,
    })).rejects.toThrow("external-edit-parent-changed");
    await expect(service.create(scope, {
      ...input,
      parentDraftDigest: "f".repeat(64),
    })).rejects.toThrow("external-edit-parent-changed");
  });

  it("hides another customer's handoff and rejects illegal state transitions", async () => {
    const service = new ExternalEditHandoffService({
      root,
      now: () => "2026-09-30T08:00:00.000Z",
      resolveParentDraft: async () => parentDraft,
    });
    const handoff = await service.create(scope, input);

    await expect(service.get({ ...scope, customerId: "customer-b" }, handoff.id)).rejects.toThrow("external-edit-handoff-not-found");
    const ready = await service.transition(scope, {
      handoffId: handoff.id,
      status: "ready",
      operationKey: "mark-ready-1",
      packageDigest: "d".repeat(64),
    });
    const editing = await service.transition(scope, {
      handoffId: handoff.id,
      status: "editing",
      operationKey: "mark-editing-1",
    });
    expect(ready.status).toBe("ready");
    expect(editing.status).toBe("editing");
    await expect(service.transition(scope, {
      handoffId: handoff.id,
      status: "preparing",
      operationKey: "rewind-1",
    })).rejects.toThrow("external-edit-transition-not-allowed");
  });

  it("recovers append-only events after restart and allows another handoff after cancel", async () => {
    let nextId = 0;
    const options = {
      root,
      now: () => "2026-09-30T08:00:00.000Z",
      resolveParentDraft: async () => parentDraft,
      createId: () => `handoff-fixed-${++nextId}`,
    };
    const firstService = new ExternalEditHandoffService(options);
    const first = await firstService.create(scope, input);
    await firstService.transition(scope, {
      handoffId: first.id,
      status: "ready",
      operationKey: "mark-ready-1",
      packageDigest: "d".repeat(64),
    });
    await firstService.cancel(scope, first.id, "actor-a");

    const recovered = new ExternalEditHandoffService(options);
    expect(await recovered.get(scope, first.id)).toMatchObject({ status: "cancelled", parentDraftVersion: 1 });
    const second = await recovered.create(scope, { ...input, operationKey: "create-handoff-2" });
    expect(second.id).toBe("handoff-fixed-2");
    expect((await readFile(join(root, "handoffs.jsonl"), "utf8")).trim().split("\n")).toHaveLength(4);
  });

  it("rejects reuse of an operation key with a changed request and only permits one active handoff per draft", async () => {
    const service = new ExternalEditHandoffService({
      root,
      resolveParentDraft: async () => parentDraft,
    });
    await service.create(scope, input);
    await expect(service.create(scope, {
      ...input,
      revisionInstruction: "改成完全不同的故事。",
    })).rejects.toThrow("external-edit-operation-key-reused");
    await expect(service.create(scope, {
      ...input,
      operationKey: "create-handoff-2",
    })).rejects.toThrow("external-edit-active-handoff-exists");
  });
});

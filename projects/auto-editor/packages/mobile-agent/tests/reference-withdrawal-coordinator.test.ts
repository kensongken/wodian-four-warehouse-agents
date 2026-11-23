import { describe, expect, it, vi } from "vitest";

import { ReferenceWithdrawalCoordinator } from "../src/reference-withdrawal-coordinator";

const scope = {
  actorId: "server-actor",
  customerId: "customer-a",
  sessionId: "session-a",
};

describe("ReferenceWithdrawalCoordinator", () => {
  it("persists customer withdrawal before invalidating reference state with a stable operation key", async () => {
    const calls: string[] = [];
    const event = {
      id: "withdrawal-1",
      customerId: scope.customerId,
      assetId: "asset-1",
      version: 1 as const,
      action: "withdrawn" as const,
      operationKey: "withdraw-op",
      actorId: scope.actorId,
      createdAt: "2026-09-21T00:00:00.000Z",
    };
    const withdraw = vi.fn(async () => {
      calls.push("library");
      return event;
    });
    const invalidateForAsset = vi.fn(async () => {
      calls.push("reference");
    });
    const coordinator = new ReferenceWithdrawalCoordinator({
      library: { withdraw },
      editor: { invalidateForAsset },
    });

    await expect(coordinator.withdraw(scope, "asset-1", "withdraw-op")).resolves.toEqual(event);
    expect(calls).toEqual(["library", "reference"]);
    expect(withdraw).toHaveBeenCalledWith(scope.actorId, scope.customerId, "asset-1", "withdraw-op");
    expect(invalidateForAsset).toHaveBeenCalledWith(scope, {
      assetId: "asset-1",
      reason: "asset-permission-withdrawn",
    }, "withdraw-op");
  });

  it("does not compensate a durable withdrawal and repairs reference invalidation on exact replay", async () => {
    const event = {
      id: "withdrawal-1",
      customerId: scope.customerId,
      assetId: "asset-1",
      version: 1 as const,
      action: "withdrawn" as const,
      operationKey: "withdraw-repair",
      actorId: scope.actorId,
      createdAt: "2026-09-21T00:00:00.000Z",
    };
    const withdraw = vi.fn(async () => event);
    const invalidateForAsset = vi.fn()
      .mockRejectedValueOnce(new Error("reference-state-conflict"))
      .mockResolvedValueOnce(undefined);
    const coordinator = new ReferenceWithdrawalCoordinator({
      library: { withdraw },
      editor: { invalidateForAsset },
    });

    await expect(coordinator.withdraw(scope, "asset-1", "withdraw-repair"))
      .rejects.toThrow("reference-state-conflict");
    await expect(coordinator.withdraw(scope, "asset-1", "withdraw-repair"))
      .resolves.toEqual(event);
    expect(withdraw).toHaveBeenCalledTimes(2);
    expect(invalidateForAsset).toHaveBeenCalledTimes(2);
  });
});

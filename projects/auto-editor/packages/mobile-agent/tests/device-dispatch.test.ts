import { lstat, mkdtemp, readFile, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  CreativeSession,
  DeviceBrief,
  DirectorCard,
  ScriptPackage,
  ShotPlan,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  consumeLocalSimulatorDeliveries,
  createLocalDispatchRoster,
  dispatchShootingPackage,
  JsonContentStore,
  LocalDispatchMailbox,
  transitionCreativeSession,
} from "../src";
import { getJsonContentStoreCoordinatorStateForTests } from "../src/json-content-store";

const SESSION_ID = "creative_dispatch_01";
const NOW = "2026-09-05T08:00:00.000Z";

describe("three-terminal shooting dispatch", () => {
  let directory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "wearable-device-dispatch-"));
    library = await JsonContentStore.open(join(directory, "library.json"));
    await seedShootingPackage(library);
  });

  afterEach(async () => {
    await rm(directory, { recursive: true, force: true });
  });

  it("requires the customer's explicit start confirmation", async () => {
    const setup = createLocalDispatchRoster({ mailboxes: adapters(), version: dispatchVersion() });
    await expect(dispatchShootingPackage({
      sessionId: SESSION_ID,
      roster: setup.roster,
      actorId: "director_01",
      now: NOW,
    } as never, library)).rejects.toThrow("dispatch-confirmation-required");
  });

  it("requires a ready session", async () => {
    const current = (await library.listCreativeSessions())[0]!;
    await writeSession({ ...current, stage: "awaiting_script_confirmation", updatedAt: NOW }, library);

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("shooting-dispatch-not-allowed");
  });

  it("requires its current shot-plan version", async () => {
    const current = (await library.listCreativeSessions())[0]!;
    await writeSession({ ...current, currentShotPlanVersion: 2, updatedAt: NOW }, library);

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("current-shot-plan-required");
  });

  it("refuses task cards when their current shot plan was invalidated", async () => {
    const plan = (await library.listShotPlans())[0]!;
    await library.putShotPlan({ ...plan, status: "invalidated" });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("current-shot-plan-required");
  });

  it("rejects a package that does not address every local device role", async () => {
    const stoneB = (await library.listDeviceBriefs()).find((item) => item.deviceRole === "main-stone-b")!;
    await library.putDeviceBrief({ ...stoneB, status: "invalidated" });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("all-device-roles-required");
  });

  it("requires every plan step to have exactly its required current cards and bound briefs", async () => {
    const card = (await library.listDirectorCards())[0]!;
    await library.putDirectorCard({ ...card, status: "invalidated" });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("shooting-package-coverage-invalid");
  });

  it("rejects orphan or wrongly bound current device tasks", async () => {
    const brief = (await library.listDeviceBriefs())[0]!;
    await library.putDeviceBrief({
      ...brief,
      id: "brief_orphan",
      stepId: "step_orphan",
      deviceId: "unbound_stone",
    });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("shooting-package-coverage-invalid");
  });

  it("rejects an active plan from a different lineage in the same session", async () => {
    const plan = (await library.listShotPlans())[0]!;
    await library.putShotPlan({
      ...plan,
      id: "plan_orphan",
      shotPlanVersion: 2,
    });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("shooting-package-coverage-invalid");
  });

  it("rejects an incomplete roster before any dispatch receipt or delivery exists", async () => {
    const partialAdapters = adapters();
    delete (partialAdapters as Partial<ReturnType<typeof adapters>>)["main-stone-b"];

    expect(() => createLocalDispatchRoster({
      mailboxes: partialAdapters as ReturnType<typeof adapters>,
      version: dispatchVersion(),
    })).toThrow("trusted-local-adapter-required");
    expect(await library.listDispatchReceipts()).toEqual([]);
    expect(partialAdapters["watch-director"].deliveries).toEqual([]);
    expect(partialAdapters["main-stone-a"].deliveries).toEqual([]);
  });

  it("rejects a non-local adapter instead of merely labeling its receipt simulated", async () => {
    const unsafe = adapters();
    unsafe["main-stone-a"] = {
      async deliver() { return { deviceId: "stone_a_sim_01", receivedAt: NOW }; },
    } as unknown as LocalDispatchMailbox;

    expect(() => createLocalDispatchRoster({ mailboxes: unsafe, version: dispatchVersion() }))
      .toThrow("trusted-local-adapter-required");
  });

  it("never calls a self-labeled lookalike adapter", async () => {
    let externalCalls = 0;
    const lookalike = {
      transport: "simulated" as const,
      localOnly: true as const,
      async deliver() {
        externalCalls += 1;
        return { deviceId: "stone_a_sim_01", receivedAt: NOW, transport: "simulated" as const, localOnly: true as const };
      },
    };
    const unsafe = { ...adapters(), "main-stone-a": lookalike };

    expect(() => createLocalDispatchRoster({
      mailboxes: unsafe as unknown as ReturnType<typeof adapters>,
      version: dispatchVersion(),
    })).toThrow("trusted-local-adapter-required");
    expect(externalCalls).toBe(0);
  });

  it("rejects a Proxy around a trusted mailbox without triggering any Proxy trap", async () => {
    let externalCalls = 0;
    const proxy = new Proxy(new LocalDispatchMailbox("stone_a_sim_01"), {
      get() {
        externalCalls += 1;
        throw new Error("proxy-get-side-effect");
      },
      getPrototypeOf() {
        externalCalls += 1;
        throw new Error("proxy-prototype-side-effect");
      },
      getOwnPropertyDescriptor() {
        externalCalls += 1;
        throw new Error("proxy-own-property-side-effect");
      },
    });
    const unsafe = { ...adapters(), "main-stone-a": proxy };

    expect(() => createLocalDispatchRoster({ mailboxes: unsafe, version: dispatchVersion() }))
      .toThrow("trusted-local-adapter-required");
    expect(externalCalls).toBe(0);
  });

  it("snapshots all three mailboxes into a branded roster before dispatch", async () => {
    const mailboxes = adapters();
    let roleReads = 0;
    const callerContainer = Object.defineProperties({}, {
      "watch-director": {
        configurable: true,
        enumerable: true,
        get: () => { roleReads += 1; return mailboxes["watch-director"]; },
      },
      "main-stone-a": {
        configurable: true,
        enumerable: true,
        get: () => { roleReads += 1; return mailboxes["main-stone-a"]; },
      },
      "main-stone-b": {
        configurable: true,
        enumerable: true,
        get: () => { roleReads += 1; return mailboxes["main-stone-b"]; },
      },
    }) as ReturnType<typeof adapters>;
    const setup = createLocalDispatchRoster({
      mailboxes: callerContainer,
      version: dispatchVersion(),
    });
    for (const role of ["watch-director", "main-stone-a", "main-stone-b"] as const) {
      Object.defineProperty(callerContainer, role, {
        get: () => { throw new Error("caller-container-reread"); },
      });
    }

    await expect(dispatchShootingPackage(dispatchRosterInput(setup.roster), library))
      .resolves.toHaveLength(3);
    expect(roleReads).toBe(3);
  });

  it("rejects a Proxy or lookalike roster with zero traps and zero mailbox delivery", async () => {
    const mailboxes = adapters();
    const setup = createLocalDispatchRoster({ mailboxes, version: dispatchVersion() });
    let traps = 0;
    const proxy = new Proxy(setup.roster, {
      get() { traps += 1; throw new Error("roster-get-trap"); },
      getPrototypeOf() { traps += 1; throw new Error("roster-prototype-trap"); },
      getOwnPropertyDescriptor() { traps += 1; throw new Error("roster-descriptor-trap"); },
    });

    await expect(dispatchShootingPackage(dispatchRosterInput(proxy), library))
      .rejects.toThrow("trusted-local-roster-required");
    await expect(dispatchShootingPackage(dispatchRosterInput({} as typeof setup.roster), library))
      .rejects.toThrow("trusted-local-roster-required");
    expect(traps).toBe(0);
    expect(mailboxes["watch-director"].deliveries).toHaveLength(0);
    expect(mailboxes["main-stone-a"].deliveries).toHaveLength(0);
    expect(mailboxes["main-stone-b"].deliveries).toHaveLength(0);
  });

  it("consumes a capability-bound task once and returns a deeply frozen validated clone", async () => {
    const mailboxes = adapters();
    const setup = createLocalDispatchRoster({ mailboxes, version: dispatchVersion() });
    await dispatchShootingPackage(dispatchRosterInput(setup.roster), library);

    const deliveries = consumeLocalSimulatorDeliveries(setup.pulls["watch-director"]);
    expect(deliveries).toHaveLength(1);
    expect(() => { (deliveries[0]!.task as { purpose: string }).purpose = "tampered"; }).toThrow();
    expect(() => {
      ((deliveries[0]!.task as DirectorCard).availableActions as string[]).push("started");
    }).toThrow();
    expect(() => consumeLocalSimulatorDeliveries(setup.pulls["watch-director"]))
      .toThrow("local-pull-capability-consumed");
  });

  it("does not let a forged or proxied object claim a simulator pull capability", () => {
    const setup = createLocalDispatchRoster({ mailboxes: adapters(), version: dispatchVersion() });
    let traps = 0;
    const proxy = new Proxy(setup.pulls["watch-director"], {
      get() { traps += 1; throw new Error("pull-capability-get-trap"); },
      getPrototypeOf() { traps += 1; throw new Error("pull-capability-prototype-trap"); },
    });

    expect(() => consumeLocalSimulatorDeliveries(proxy))
      .toThrow("trusted-local-pull-capability-required");
    expect(() => consumeLocalSimulatorDeliveries({} as typeof setup.pulls["watch-director"]))
      .toThrow("trusted-local-pull-capability-required");
    expect(traps).toBe(0);
  });

  it("lets an exact V2 pull retire retained V1 tasks without blocking an idempotent retry", async () => {
    const mailboxes = adapters();
    const first = createLocalDispatchRoster({ mailboxes, version: dispatchVersion() });
    await dispatchShootingPackage(dispatchRosterInput(first.roster), library);
    expect(consumeLocalSimulatorDeliveries(first.pulls["watch-director"]))
      .toEqual([expect.objectContaining({ task: expect.objectContaining({ scriptVersion: 1 }) })]);

    await advanceShootingPackageToVersion2(library);
    const version2 = { ...dispatchVersion(), scriptVersion: 2, shotPlanVersion: 2 };
    const second = createLocalDispatchRoster({ mailboxes, version: version2 });
    await dispatchShootingPackage({ ...dispatchRosterInput(second.roster), now: "2026-09-05T08:05:00.000Z" }, library);

    expect(consumeLocalSimulatorDeliveries(second.pulls["watch-director"]))
      .toEqual([expect.objectContaining({ task: expect.objectContaining({ id: "card_02", scriptVersion: 2 }) })]);
    expect(consumeLocalSimulatorDeliveries(second.pulls["main-stone-a"]))
      .toEqual([expect.objectContaining({ task: expect.objectContaining({
        id: "brief_main-stone-a_v2",
        scriptVersion: 2,
      }) })]);
    expect(consumeLocalSimulatorDeliveries(first.pulls["main-stone-a"])).toEqual([]);

    const retry = createLocalDispatchRoster({ mailboxes, version: version2 });
    await expect(dispatchShootingPackage({
      ...dispatchRosterInput(retry.roster),
      now: "2026-09-05T08:06:00.000Z",
    }, library)).rejects.toThrow("shooting-dispatch-not-allowed");
    expect(mailboxes["watch-director"].deliveries.filter((item) => item.taskId === "card_02"))
      .toHaveLength(1);
  });

  it("does not permit the public mailbox API to install a delivery callback", async () => {
    let externalCalls = 0;
    const mailbox = new LocalDispatchMailbox("stone_a_sim_01");
    expect(() => Object.defineProperty(mailbox, "deliver", {
      value: () => { externalCalls += 1; },
    })).toThrow();

    const controlled = { ...adapters(), "main-stone-a": mailbox };
    await expect(dispatchShootingPackage(dispatchInput(controlled), library)).resolves.toBeDefined();
    expect(externalCalls).toBe(0);
  });

  it("binds each one-time pull capability to its captured device role", async () => {
    const setup = createLocalDispatchRoster({ mailboxes: adapters(), version: dispatchVersion() });
    await dispatchShootingPackage(dispatchRosterInput(setup.roster), library);

    expect(consumeLocalSimulatorDeliveries(setup.pulls["watch-director"]))
      .toEqual([expect.objectContaining({
        deviceId: "watch_sim_01",
        deviceRole: "watch-director",
      })]);
    expect(consumeLocalSimulatorDeliveries(setup.pulls["main-stone-a"]))
      .toEqual([expect.objectContaining({
        deviceId: "stone_a_sim_01",
        deviceRole: "main-stone-a",
      })]);
  });

  it("does not expose a task to a mailbox whose device id is not bound", async () => {
    const controlled = adapters();
    const wrongDevice = new LocalDispatchMailbox("other_stone");
    controlled["main-stone-a"] = wrongDevice;

    const setup = createLocalDispatchRoster({ mailboxes: controlled, version: dispatchVersion() });
    await dispatchShootingPackage(dispatchRosterInput(setup.roster), library);

    expect(consumeLocalSimulatorDeliveries(setup.pulls["main-stone-a"])).toEqual([]);
  });

  it("stops after an in-flight delivery when a pause reserves the same session", async () => {
    const controlled = adapters();
    const gate = controlled["watch-director"].holdNextDelivery();

    const dispatching = dispatchShootingPackage(dispatchInput(controlled), library);
    await gate.entered;
    const pausing = transitionCreativeSession({
      sessionId: SESSION_ID, to: "paused", actorId: "director_01", reason: "privacy check", now: NOW,
    }, library);
    await Promise.resolve();
    await Promise.resolve();
    gate.release();

    await expect(dispatching).rejects.toThrow("creative-dispatch-interrupted");
    await expect(pausing).resolves.toMatchObject({ stage: "paused" });
    expect(controlled["watch-director"].deliveries).toHaveLength(1);
    expect(controlled["main-stone-a"].deliveries).toHaveLength(0);
    expect(controlled["main-stone-b"].deliveries).toHaveLength(0);
    expect(await library.listDispatchReceipts()).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "pending" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "pending" }),
    ]));
  });

  it("does not enter shooting when privacy pause arrives during the final delivery", async () => {
    const controlled = adapters();
    const gate = controlled["main-stone-b"].holdNextDelivery();

    const dispatching = dispatchShootingPackage(dispatchInput(controlled), library);
    await gate.entered;
    const pausing = transitionCreativeSession({
      sessionId: SESSION_ID, to: "paused", actorId: "director_01", reason: "privacy check", now: NOW,
    }, library);
    await Promise.resolve();
    await Promise.resolve();
    gate.release();

    await expect(dispatching).rejects.toThrow("creative-dispatch-interrupted");
    await expect(pausing).resolves.toMatchObject({ stage: "paused" });
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    expect((await reopened.listCreativeSessions()).find((item) => item.id === SESSION_ID)?.stage)
      .toBe("paused");
    expect(await reopened.listDispatchReceipts()).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "received" }),
    ]));
    expect((await reopened.listAuditEvents(SESSION_ID)).some((event) =>
      event.action === "creative-session-transitioned" &&
      JSON.stringify(event.after) === JSON.stringify({ stage: "shooting" }),
    )).toBe(false);
  });

  it("does not reuse a receipt when the full task identity differs", async () => {
    await library.putDispatchReceipt({
      id: "dispatch_card_01", sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 1,
      shotPlanVersion: 1, deviceId: "wrong_watch", deviceRole: "watch-director",
      transport: "simulated", idempotencyKey: `${SESSION_ID}:card_01:1`, status: "pending", createdAt: NOW,
    });

    await expect(dispatchShootingPackage(dispatchInput(adapters()), library))
      .rejects.toThrow("dispatch-receipt-identity-conflict");
  });

  it("keeps successful receipts and only retries pending delivery", async () => {
    const fakeAdapters = adapters({ "main-stone-b": ["device-offline", null] });

    const initial = await dispatchShootingPackage(dispatchInput(fakeAdapters), library);
    expect(initial).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received", transport: "simulated" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "received", transport: "simulated" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "pending", transport: "simulated" }),
    ]));
    expect((await library.listCreativeSessions())[0]!.stage).toBe("ready_to_dispatch");

    const retried = await dispatchShootingPackage(dispatchInput(fakeAdapters), library);
    expect(fakeAdapters["watch-director"].deliveries).toHaveLength(1);
    expect(fakeAdapters["main-stone-a"].deliveries).toHaveLength(1);
    expect(fakeAdapters["main-stone-b"].deliveries).toHaveLength(2);
    expect(retried).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "main-stone-b", status: "received" }),
    ]));
    expect((await library.listCreativeSessions())[0]!.stage).toBe("shooting");
  });

  it("coordinates a writer from another store instance before later local mailbox delivery", async () => {
    const secondStore = await JsonContentStore.open(join(directory, "library.json"));
    const controlled = adapters();
    const gate = controlled["watch-director"].holdNextDelivery();
    const dispatching = dispatchShootingPackage(dispatchInput(controlled), library);
    await gate.entered;

    const plan = (await secondStore.listShotPlans())[0]!;
    const invalidating = secondStore.putShotPlan({ ...plan, status: "invalidated" });
    await Promise.resolve();
    await Promise.resolve();
    gate.release();

    await expect(dispatching).rejects.toThrow("creative-dispatch-interrupted");
    await expect(invalidating).rejects.toThrow("stale-content-store");
    expect(controlled["watch-director"].deliveries).toHaveLength(1);
    expect(controlled["main-stone-a"].deliveries).toHaveLength(0);
    expect(controlled["main-stone-b"].deliveries).toHaveLength(0);
    expect(await library.listDispatchReceipts()).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "pending" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "pending" }),
    ]));
  });

  it("does not deliver from a stale second store after another store dispatches", async () => {
    const secondStore = await JsonContentStore.open(join(directory, "library.json"));
    await expect(dispatchShootingPackage(dispatchInput(adapters()), library)).resolves.toBeDefined();

    const staleMailboxes = adapters();
    await expect(dispatchShootingPackage(dispatchInput(staleMailboxes), secondStore))
      .rejects.toThrow("stale-content-store");
    expect(staleMailboxes["watch-director"].deliveries).toHaveLength(0);
    expect(staleMailboxes["main-stone-a"].deliveries).toHaveLength(0);
    expect(staleMailboxes["main-stone-b"].deliveries).toHaveLength(0);
  });

  it("allows reservations for different sessions on the same JSON path", async () => {
    const secondStore = await JsonContentStore.open(join(directory, "library.json"));
    const first = await library.acquireCreativeDispatchReservation(SESSION_ID);
    const second = await secondStore.acquireCreativeDispatchReservation("creative_dispatch_02");
    expect(second.isInterrupted()).toBe(false);
    second.release();
    first.release();
  });

  it("keeps queued same-session acquirers on the current reservation map after cleanup", async () => {
    const path = join(directory, "library.json");
    const queuedStore = await JsonContentStore.open(path);
    const laterStore = await JsonContentStore.open(path);
    const blockerStore = await JsonContentStore.open(path);
    const fenceStore = await JsonContentStore.open(path);
    const held = await library.acquireCreativeDispatchReservation(SESSION_ID);
    const coordinatorGate = holdNextFreshnessCheck(blockerStore);
    const blocking = blockerStore.acquireCreativeDispatchReservation("coordinator-blocker");
    await coordinatorGate.entered;

    const acquired: Array<{
      label: "queued" | "later";
      reservation: Awaited<ReturnType<JsonContentStore["acquireCreativeDispatchReservation"]>>;
    }> = [];
    const queued = queuedStore.acquireCreativeDispatchReservation(SESSION_ID).then((reservation) => {
      acquired.push({ label: "queued", reservation });
      return reservation;
    });
    held.release();
    const later = laterStore.acquireCreativeDispatchReservation(SESSION_ID).then((reservation) => {
      acquired.push({ label: "later", reservation });
      return reservation;
    });
    const fenced = fenceStore.acquireCreativeDispatchReservation("coordinator-fence");
    coordinatorGate.release();
    const blocker = await blocking;
    const fence = await fenced;

    try {
      await Promise.race([queued, later]);
      expect(acquired).toHaveLength(1);
    } finally {
      const released = new Set(acquired.map((item) => item.reservation));
      for (const reservation of released) reservation.release();
      const remaining = acquired[0]?.label === "queued" ? later : queued;
      const follower = await remaining;
      if (!released.has(follower)) follower.release();
      fence.release();
      blocker.release();
    }
  });

  it("serializes full same-session dispatches across queued reservation cleanup", async () => {
    const path = join(directory, "library.json");
    const secondStore = await JsonContentStore.open(path);
    const holderStore = await JsonContentStore.open(path);
    const blockerStore = await JsonContentStore.open(path);
    const fenceStore = await JsonContentStore.open(path);
    const held = await holderStore.acquireCreativeDispatchReservation(SESSION_ID);
    const coordinatorGate = holdNextFreshnessCheck(blockerStore);
    const blocking = blockerStore.acquireCreativeDispatchReservation("dispatch-coordinator-blocker");
    await coordinatorGate.entered;

    const firstMailboxes = adapters();
    const secondMailboxes = adapters();
    const firstDispatch = dispatchShootingPackage(dispatchInput(firstMailboxes), library);
    held.release();
    const secondDispatch = dispatchShootingPackage(dispatchInput(secondMailboxes), secondStore);
    const fenced = fenceStore.acquireCreativeDispatchReservation("dispatch-coordinator-fence");
    coordinatorGate.release();
    const blocker = await blocking;
    const fence = await fenced;

    try {
      const outcomes = await Promise.allSettled([firstDispatch, secondDispatch]);
      const deliveryCounts = [firstMailboxes, secondMailboxes].map((mailboxes) =>
        Object.values(mailboxes).reduce((count, mailbox) => count + mailbox.deliveries.length, 0));
      expect(deliveryCounts.sort((left, right) => left - right)).toEqual([0, 3]);
      expect(outcomes.filter((outcome) => outcome.status === "fulfilled")).toHaveLength(1);

      const deliveredKeys = [firstMailboxes, secondMailboxes]
        .flatMap((mailboxes) => Object.values(mailboxes))
        .flatMap((mailbox) => mailbox.deliveries.map((delivery) => delivery.idempotencyKey))
        .sort();
      const reopened = await JsonContentStore.open(path);
      const receivedKeys = (await reopened.listDispatchReceipts())
        .filter((receipt) => receipt.status === "received")
        .map((receipt) => receipt.idempotencyKey)
        .sort();
      expect(receivedKeys).toEqual(deliveredKeys);
      expect(receivedKeys).toHaveLength(3);
      expect((await reopened.listCreativeSessions()).find((session) => session.id === SESSION_ID)?.stage)
        .toBe("shooting");
    } finally {
      fence.release();
      blocker.release();
    }
  });

  it("lets a session-B writer finish without interrupting session A's reservation", async () => {
    const reservation = await library.acquireCreativeDispatchReservation(SESSION_ID);
    const current = (await library.listCreativeSessions())[0]!;
    let completed = false;
    const writing = Promise.all([
      library.putCreativeSession({
        ...current,
        id: "creative_dispatch_02",
        title: "Independent session",
        stage: "draft_intent",
        currentIntentVersion: 0,
        currentBriefVersion: 0,
        currentScriptVersion: 0,
        currentShotPlanVersion: 0,
        deviceBindings: [],
      }),
      library.putAuditEvent({
        id: "audit_session_b",
        entityType: "creative-session",
        entityId: "creative_dispatch_02",
        actorId: "director_02",
        action: "session-b-audit",
        reason: "independent session",
        before: null,
        after: { stage: "draft_intent" },
        createdAt: NOW,
      }),
    ]).then(() => { completed = true; });

    try {
      await expect(Promise.race([
        writing.then(() => "written" as const),
        new Promise<"timed-out">((resolve) => setTimeout(() => resolve("timed-out"), 250)),
      ])).resolves.toBe("written");
      expect(completed).toBe(true);
      expect(reservation.isInterrupted()).toBe(false);
    } finally {
      reservation.release();
      await writing;
    }
  });

  it("merges session A dispatch accounting after another store writes session B", async () => {
    const secondStore = await JsonContentStore.open(join(directory, "library.json"));
    const controlled = adapters();
    const gate = controlled["watch-director"].holdNextDelivery();
    const dispatching = dispatchShootingPackage(dispatchInput(controlled), library);
    await gate.entered;

    const current = (await secondStore.listCreativeSessions())[0]!;
    await secondStore.putCreativeSession({
      ...current,
      id: "creative_dispatch_02",
      title: "Independent session",
      stage: "draft_intent",
      currentIntentVersion: 0,
      currentBriefVersion: 0,
      currentScriptVersion: 0,
      currentShotPlanVersion: 0,
      deviceBindings: [],
    });
    gate.release();

    await expect(dispatching).resolves.toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "received" }),
    ]));
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    expect((await reopened.listCreativeSessions()).map((item) => [item.id, item.stage]))
      .toEqual(expect.arrayContaining([
        [SESSION_ID, "shooting"],
        ["creative_dispatch_02", "draft_intent"],
      ]));
    expect(await reopened.listDispatchReceipts()).toHaveLength(3);
    expect((await reopened.listCreativeSessionEvents()).filter((event) => event.action === "received"))
      .toHaveLength(3);
  });

  it("fails closed but keeps delivery accounting when session A lineage changes externally", async () => {
    const path = join(directory, "library.json");
    const controlled = adapters();
    const gate = controlled["watch-director"].holdNextDelivery();
    const dispatching = dispatchShootingPackage(dispatchInput(controlled), library);
    await gate.entered;

    const external = JSON.parse(await readFile(path, "utf8"));
    external.creativeSessions[0].currentShotPlanVersion = 2;
    await writeFile(path, `${JSON.stringify(external, null, 2)}\n`);
    gate.release();

    await expect(dispatching).rejects.toThrow("creative-dispatch-lineage-changed");
    const reopened = await JsonContentStore.open(path);
    expect((await reopened.listCreativeSessions())[0]).toMatchObject({
      id: SESSION_ID,
      stage: "ready_to_dispatch",
      currentShotPlanVersion: 2,
    });
    expect(await reopened.listDispatchReceipts()).toHaveLength(3);
    expect((await reopened.listCreativeSessionEvents()).filter((event) =>
      event.action === "disconnected" && event.reason === "creative-dispatch-lineage-changed",
    )).toHaveLength(3);

    const retryMailboxes = adapters();
    const retrySetup = createLocalDispatchRoster({
      mailboxes: retryMailboxes,
      version: { ...dispatchVersion(), shotPlanVersion: 2 },
    });
    await expect(dispatchShootingPackage(dispatchRosterInput(retrySetup.roster), library))
      .rejects.toThrow("current-shot-plan-required");
    expect(retryMailboxes["watch-director"].deliveries).toEqual([]);
  });

  it("treats an ordinary imported asset as a global writer that waits for every reservation", async () => {
    const secondStore = await JsonContentStore.open(join(directory, "library.json"));
    const reservation = await library.acquireCreativeDispatchReservation(SESSION_ID);
    let settled = false;
    const writing = secondStore.putAsset({
      id: "asset_global_01",
      batchId: "batch_import_01",
      sessionId: "capture_import_01",
      digest: "digest-global-01",
      localPath: join(directory, "global-asset.mp4"),
      mediaKind: "video",
      importedAt: NOW,
      permissionState: "allowed",
      origin: "new",
      technicalFlags: [],
    }).then((asset) => {
      settled = true;
      return asset;
    });

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
    expect(reservation.isInterrupted()).toBe(true);
    reservation.release();
    await expect(writing).resolves.toMatchObject({ id: "asset_global_01" });
  });

  it("admits an already-started same-session writer before a later reservation", async () => {
    const lockPath = join(directory, "library.json.lock");
    await writeFile(lockPath, JSON.stringify({ token: "test-lock", pid: process.pid, createdAt: Date.now() }));
    const plan = (await library.listShotPlans())[0]!;
    const order: string[] = [];
    const writing = library.putShotPlan({ ...plan, status: "invalidated" })
      .then(() => { order.push("writer"); });
    await new Promise((resolve) => setImmediate(resolve));
    const acquiring = library.acquireCreativeDispatchReservation(SESSION_ID)
      .then((reservation) => { order.push("reservation"); return reservation; });
    await new Promise((resolve) => setImmediate(resolve));
    expect(order).toEqual([]);

    await unlink(lockPath);
    await writing;
    const reservation = await acquiring;
    expect(order).toEqual(["writer", "reservation"]);
    reservation.release();
  });

  it("shares a reservation through a canonical JSON path alias", async () => {
    const alias = join(directory, "library-alias.json");
    await symlink(join(directory, "library.json"), alias);
    const aliasedStore = await JsonContentStore.open(alias);
    const first = await library.acquireCreativeDispatchReservation(SESSION_ID);
    let acquired = false;
    const waiting = aliasedStore.acquireCreativeDispatchReservation(SESSION_ID).then((reservation) => {
      acquired = true;
      return reservation;
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(acquired).toBe(false);
    first.release();
    const second = await waiting;
    let thirdAcquired = false;
    const thirdWaiting = library.acquireCreativeDispatchReservation(SESSION_ID).then((reservation) => {
      thirdAcquired = true;
      return reservation;
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(thirdAcquired).toBe(false);
    second.release();
    const third = await thirdWaiting;
    third.release();
  });

  it("writes through an existing symlink without replacing the symlink", async () => {
    const alias = join(directory, "library-write-alias.json");
    const target = join(directory, "library.json");
    await symlink(target, alias);
    const aliasedStore = await JsonContentStore.open(alias);
    const plan = (await aliasedStore.listShotPlans())[0]!;

    await aliasedStore.putShotPlan({ ...plan, status: "invalidated" });

    expect((await lstat(alias)).isSymbolicLink()).toBe(true);
    expect(JSON.parse(await readFile(target, "utf8")).shotPlans[0].status).toBe("invalidated");
  });

  it("writes through a dangling symlink target and shares its coordinator", async () => {
    const target = join(directory, "future-library.json");
    const alias = join(directory, "future-library-alias.json");
    await symlink(target, alias);
    const aliasedStore = await JsonContentStore.open(alias);
    const targetStore = await JsonContentStore.open(target);
    const first = await aliasedStore.acquireCreativeDispatchReservation("future-session");
    let acquired = false;
    const waiting = targetStore.acquireCreativeDispatchReservation("future-session").then((reservation) => {
      acquired = true;
      return reservation;
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(acquired).toBe(false);
    first.release();
    const second = await waiting;
    second.release();

    await aliasedStore.putCreativeSession({
      ...(await library.listCreativeSessions())[0]!,
      id: "future-session",
      title: "Future symlink target",
      stage: "draft_intent",
      currentIntentVersion: 0,
      currentBriefVersion: 0,
      currentScriptVersion: 0,
      currentShotPlanVersion: 0,
      deviceBindings: [],
    });

    expect((await lstat(alias)).isSymbolicLink()).toBe(true);
    expect(JSON.parse(await readFile(target, "utf8")).creativeSessions[0].id).toBe("future-session");
  });

  it("cleans empty path coordinators after writers and reservations finish", async () => {
    const path = join(directory, "library.json");
    const reservation = await library.acquireCreativeDispatchReservation("cleanup-session");
    reservation.release();
    await library.putAuditEvent({
      id: "audit_cleanup",
      entityType: "device-event",
      entityId: "cleanup-session",
      actorId: "system_01",
      action: "cleanup-observed",
      reason: null,
      before: null,
      after: null,
      createdAt: NOW,
    });
    await new Promise((resolve) => setImmediate(resolve));

    expect(getJsonContentStoreCoordinatorStateForTests(path)).toEqual({
      reservations: 0,
      admittedWriters: 0,
      coordinatorOperation: false,
      pathOperation: false,
    });
  });
});

function adapters(outcomes: Partial<Record<"watch-director" | "main-stone-a" | "main-stone-b", Array<string | null>>> = {}) {
  const result = {
    "watch-director": new LocalDispatchMailbox("watch_sim_01"),
    "main-stone-a": new LocalDispatchMailbox("stone_a_sim_01"),
    "main-stone-b": new LocalDispatchMailbox("stone_b_sim_01"),
  };
  for (const role of Object.keys(result) as Array<keyof typeof result>) {
    result[role].queueOutcomes(outcomes[role] ?? []);
  }
  return result;
}

function dispatchInput(value: ReturnType<typeof adapters>) {
  const setup = createLocalDispatchRoster({ mailboxes: value, version: dispatchVersion() });
  return dispatchRosterInput(setup.roster);
}

function dispatchRosterInput(value: ReturnType<typeof createLocalDispatchRoster>["roster"]) {
  return {
    sessionId: SESSION_ID,
    confirmStart: true as const,
    roster: value,
    actorId: "director_01",
    now: NOW,
  };
}

function dispatchVersion() {
  return {
    sessionId: SESSION_ID,
    briefVersion: 1,
    scriptVersion: 1,
    shotPlanVersion: 1,
  };
}

function holdNextFreshnessCheck(store: JsonContentStore): {
  entered: Promise<void>;
  release(): void;
} {
  const entered = deferred<void>();
  const released = deferred<void>();
  const assertFresh = store.assertFresh.bind(store);
  vi.spyOn(store, "assertFresh").mockImplementationOnce(async () => {
    entered.resolve();
    await released.promise;
    await assertFresh();
  });
  return { entered: entered.promise, release: released.resolve };
}

type Deferred<T> = {
  promise: Promise<T>;
  resolve(value?: T): void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value?: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = (value) => done(value as T);
  });
  return { promise, resolve };
}

async function seedShootingPackage(library: JsonContentStore): Promise<void> {
  const session: CreativeSession = {
    id: SESSION_ID,
    customerId: "customer_01",
    title: "Pendant repair story",
    stage: "ready_to_dispatch",
    currentIntentVersion: 1,
    currentBriefVersion: 1,
    currentScriptVersion: 1,
    currentShotPlanVersion: 1,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [
      { deviceId: "watch_sim_01", deviceRole: "watch-director", transport: "simulated" },
      { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", transport: "simulated" },
      { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", transport: "simulated" },
    ],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: ["customer faces"],
    status: "active",
    createdAt: "2026-09-05T07:50:00.000Z",
    updatedAt: "2026-09-05T07:55:00.000Z",
  };
  const script: ScriptPackage = {
    id: "script_01", sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 1,
    status: "active", title: "Pendant repair story", platformDirection: "Xiaohongshu",
    opening: "A pendant begins again.", narrativeOrder: ["section_01"],
    sections: [{ id: "section_01", expression: "Show the repair.", source: "customer-input", referencedSegmentIds: [] }],
    confirmedAt: NOW, createdAt: NOW,
  };
  const key = { sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1 };
  const plan: ShotPlan = {
    id: "plan_01", ...key, status: "active", createdAt: NOW,
    steps: [{
      id: "step_01", order: 1, purpose: "Open", expressionSuggestion: "Speak.",
      visualDirection: "Show the bench.", durationSeconds: 8, mustCapture: ["maker hands"],
      optionalGapSuggestions: [], prohibitedCapture: ["customer faces"],
      completionCriteria: ["maker hands visible"],
      deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
    }],
  };
  const card: DirectorCard = {
    id: "card_01", ...key, stepId: "step_01", status: "active", purpose: "Open",
    spokenPrompt: "Speak.", shotDescription: "Show the bench.", durationSeconds: 8,
    mustCapture: ["maker hands"], prohibitedCapture: ["customer faces"],
    availableActions: ["completed", "retake", "skipped", "note", "privacy-paused"], createdAt: NOW,
  };
  const briefs: DeviceBrief[] = (["main-stone-a", "main-stone-b"] as const).map((deviceRole) => ({
    id: `brief_${deviceRole}`, ...key, stepId: "step_01", status: "active",
    deviceId: deviceRole === "main-stone-a" ? "stone_a_sim_01" : "stone_b_sim_01",
    deviceRole, carrier: "pendant", startCondition: "director-card-start:1",
    endCondition: "director-card-stop:1", composition: "product-close-up",
    prohibitedAreas: ["customer faces"], privacyState: "creator", createdAt: NOW,
  }));
  await library.commitCreativeBundle({
    session, scripts: [script], shotPlans: [plan], directorCards: [card], deviceBriefs: briefs, auditEvents: [],
  });
}

async function writeSession(session: CreativeSession, library: JsonContentStore): Promise<void> {
  const prior = (await library.listCreativeSessions()).find((item) => item.id === session.id)!;
  await library.commitCreativeBundle({
    session,
    auditEvents: [{
      id: `audit_${session.stage}_${session.currentShotPlanVersion}`,
      entityType: "creative-session",
      entityId: session.id,
      actorId: "director_01",
      action: prior.stage === session.stage ? "shooting-package-created" : "creative-session-transitioned",
      reason: "test session change",
      before: prior.stage === session.stage
        ? { shotPlanVersion: prior.currentShotPlanVersion }
        : { stage: prior.stage },
      after: prior.stage === session.stage
        ? { shotPlanVersion: session.currentShotPlanVersion }
        : { stage: session.stage },
      createdAt: NOW,
    }],
  });
}

async function advanceShootingPackageToVersion2(library: JsonContentStore): Promise<void> {
  await transitionCreativeSession({
    sessionId: SESSION_ID,
    to: "awaiting_script_confirmation",
    actorId: "director_01",
    reason: "prepare version two",
    now: "2026-09-05T08:02:00.000Z",
  }, library);
  const firstPlan = (await library.listShotPlans())[0]!;
  const firstCard = (await library.listDirectorCards())[0]!;
  const firstBriefs = await library.listDeviceBriefs();
  await library.putShotPlan({ ...firstPlan, status: "invalidated" });
  await library.putDirectorCard({ ...firstCard, status: "invalidated" });
  for (const brief of firstBriefs) {
    await library.putDeviceBrief({ ...brief, status: "invalidated" });
  }
  const current = (await library.listCreativeSessions()).find((item) => item.id === SESSION_ID)!;
  const firstScript = (await library.listScriptPackages())[0]!;
  const version = { sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 2, shotPlanVersion: 2 };
  const script: ScriptPackage = {
    ...firstScript,
    id: "script_02",
    scriptVersion: 2,
    parentScriptId: firstScript.id,
    designerPrompt: "Use version two",
    changeExplanation: "Version rollover fixture",
    confirmedAt: "2026-09-05T08:03:00.000Z",
    createdAt: "2026-09-05T08:03:00.000Z",
  };
  const plan: ShotPlan = {
    ...firstPlan,
    ...version,
    id: "plan_02",
    status: "active",
    createdAt: "2026-09-05T08:03:00.000Z",
  };
  const card: DirectorCard = {
    ...firstCard,
    ...version,
    id: "card_02",
    status: "active",
    createdAt: "2026-09-05T08:03:00.000Z",
  };
  const briefs: DeviceBrief[] = firstBriefs.map((brief) => ({
    ...brief,
    ...version,
    id: `${brief.id}_v2`,
    status: "active",
    createdAt: "2026-09-05T08:03:00.000Z",
  }));
  await library.commitCreativeBundle({
    session: {
      ...current,
      stage: "ready_to_dispatch",
      currentScriptVersion: 2,
      currentShotPlanVersion: 2,
      updatedAt: "2026-09-05T08:03:00.000Z",
    },
    scripts: [script],
    shotPlans: [plan],
    directorCards: [card],
    deviceBriefs: briefs,
    auditEvents: [{
      id: "audit_ready_v2",
      entityType: "creative-session",
      entityId: SESSION_ID,
      actorId: "director_01",
      action: "creative-session-transitioned",
      reason: "version two confirmed",
      before: { stage: "awaiting_script_confirmation" },
      after: { stage: "ready_to_dispatch" },
      createdAt: "2026-09-05T08:03:00.000Z",
    }],
  });
}

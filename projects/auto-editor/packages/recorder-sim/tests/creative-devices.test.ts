import { describe, expect, it } from "vitest";
import type { DirectorCard } from "@wearable/contracts";

import {
  SimulatedCreativeDevice,
  type SimulatedDeviceAction,
} from "../src";

const NOW = "2026-09-05T08:00:00.000Z";

describe("local creative-device simulator", () => {
  it("exposes simulated transport and rejects delivery while disconnected", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setConnected(false);

    await expect(device.deliver(directorCard(), "creative_01:card_01:1"))
      .rejects.toThrow("device-offline");
    expect(device.transport).toBe("simulated");
    expect(device.inbox).toEqual([]);
  });

  it("keeps one receipt for an idempotent same-version retry", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());

    const first = await device.deliver(directorCard(), "creative_01:card_01:1");
    const retry = await device.deliver(directorCard(), "creative_01:card_01:1");

    expect(retry).toEqual(first);
    expect(device.inbox).toEqual([expect.objectContaining({
      taskId: "card_01",
      idempotencyKey: "creative_01:card_01:1",
    })]);
  });

  it("rejects an old script while accepting the current script version", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion({ ...versionKey(), scriptVersion: 2 });

    await expect(device.deliver(directorCard(), "creative_01:card_01:1"))
      .rejects.toThrow("stale-device-task");
    await expect(device.deliver(
      { ...directorCard(), scriptVersion: 2 },
      "creative_01:card_01:2",
    )).resolves.toMatchObject({ deviceId: "watch_sim_01" });
  });

  it("fails closed unless a task is active and exactly matches the current version", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());

    await expect(device.deliver({ ...directorCard(), status: "invalidated" }, "invalidated"))
      .rejects.toThrow("invalidated-device-task");
    for (const task of [
      { ...directorCard(), briefVersion: 2 },
      { ...directorCard(), scriptVersion: 2 },
      { ...directorCard(), shotPlanVersion: 2 },
      { ...directorCard(), sessionId: "creative_other" },
    ]) {
      await expect(device.deliver(task, `wrong-${task.briefVersion}-${task.scriptVersion}-${task.shotPlanVersion}`))
        .rejects.toThrow("stale-device-task");
    }
  });

  it("only emits supported events for the received session and step", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");

    const retake = device.emitAction(deviceAction({ action: "retake", operationId: "retake_01" }));
    const skipped = device.emitAction(deviceAction({
      action: "skipped",
      reason: "the bench is occupied",
      operationId: "skip_01",
    }));

    expect(retake).toMatchObject({ action: "retake", stepId: "step_01" });
    expect(skipped).toMatchObject({ action: "skipped", reason: "the bench is occupied" });
    expect(device.takeFor("step_01")).toBe(2);
    expect(() => device.emitAction(deviceAction({ sessionId: "creative_other", operationId: "other_session" })))
      .toThrow("device-event-task-mismatch");
    expect(() => device.emitAction(deviceAction({ stepId: "step_other", operationId: "other_step" })))
      .toThrow("device-event-task-mismatch");
  });

  it("persists a validated voice rewrite transcript and rejects transcripts on other actions", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver({
      ...directorCard(),
      availableActions: ["voice-rewrite", "note"],
    }, "creative_01:card_01:1");

    expect(device.emitAction(deviceAction({
      action: "voice-rewrite",
      transcript: "Please make the opening calmer.",
      operationId: "voice_01",
    }))).toMatchObject({ transcript: "Please make the opening calmer." });
    expect(() => device.emitAction(deviceAction({
      action: "voice-rewrite", transcript: " ", operationId: "voice_blank",
    }))).toThrow("voice-rewrite-transcript-required");
    expect(() => device.emitAction(deviceAction({
      action: "note", transcript: "not a rewrite", operationId: "note_with_transcript",
    }))).toThrow("unexpected-action-transcript");
  });

  it("uses a caller operation id so replayed retakes do not advance twice", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");

    const first = device.emitAction(deviceAction({ action: "retake", operationId: "retake_01" }));
    const replay = device.emitAction(deviceAction({ action: "retake", operationId: "retake_01" }));
    const firstNote = device.emitAction(deviceAction({ action: "note", operationId: "note_01" }));
    const secondNote = device.emitAction(deviceAction({ action: "note", operationId: "note_02" }));

    expect(replay).toEqual(first);
    expect(device.takeFor("step_01")).toBe(2);
    expect(secondNote.id).not.toBe(firstNote.id);
    expect(() => device.emitAction(deviceAction({ action: "note", operationId: " " })))
      .toThrow("device-action-operation-required");
  });

  it("keeps whitespace-distinct nonblank operation IDs separate", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");

    const plain = device.emitAction(deviceAction({ action: "note", operationId: "op-1" }));
    const asciiSpaced = device.emitAction(deviceAction({ action: "note", operationId: " op-1 " }));
    const unicodeSpaced = device.emitAction(deviceAction({
      action: "note",
      operationId: "\u2003op-1\u3000",
    }));

    expect(new Set([plain.id, asciiSpaced.id, unicodeSpaced.id])).toHaveProperty("size", 3);
    expect(device.events).toHaveLength(3);
  });

  it("rejects an action the delivered director card did not make available", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");

    expect(() => device.emitAction(deviceAction({
      action: "voice-rewrite", transcript: "Change this.", operationId: "voice_01",
    }))).toThrow("device-action-not-available");
  });

  it("does not let an inbox snapshot mutate delivered card actions", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");

    const inbox = device.inbox;
    expect(() => (inbox[0]!.availableActions as string[]).push("voice-rewrite"))
      .toThrow();
    expect(() => device.emitAction(deviceAction({
      action: "voice-rewrite", transcript: "Forged through inbox.", operationId: "voice_01",
    }))).toThrow("device-action-not-available");
  });

  it("syncs only the exact current version and makes rollover retries idempotent", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    const current = { ...versionKey(), scriptVersion: 2 };
    device.setCurrentVersion(current);
    const oldTask = directorCard();
    const currentTask = { ...directorCard(), id: "card_02", scriptVersion: 2 };
    const deliveries = [
      {
        deviceId: "watch_sim_01",
        deviceRole: "watch-director" as const,
        task: oldTask,
        idempotencyKey: "creative_01:card_01:1",
      },
      {
        deviceId: "watch_sim_01",
        deviceRole: "watch-director" as const,
        task: currentTask,
        idempotencyKey: "creative_01:card_02:2",
      },
    ];

    await expect(device.syncLocalDeliveries(deliveries)).resolves.toBeUndefined();
    await expect(device.syncLocalDeliveries(deliveries)).resolves.toBeUndefined();
    expect(device.inbox).toEqual([
      expect.objectContaining({ taskId: "card_02", scriptVersion: 2 }),
    ]);
  });

  it("rejects a local delivery bound to another device or role", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    const delivery = {
      deviceId: "other_watch",
      deviceRole: "watch-director" as const,
      task: directorCard(),
      idempotencyKey: "creative_01:card_01:1",
    };

    await expect(device.syncLocalDeliveries([delivery]))
      .rejects.toThrow("device-delivery-device-mismatch");
    await expect(device.syncLocalDeliveries([{
      ...delivery,
      deviceId: "watch_sim_01",
      deviceRole: "main-stone-a" as const,
    }])).rejects.toThrow("device-role-mismatch");
    expect(device.inbox).toEqual([]);
  });

  it("enters privacy pause after recording the event and blocks later device work", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");
    const paused = device.emitAction(deviceAction({
      action: "privacy-paused",
      operationId: "privacy_01",
      reason: "customer requested privacy",
    }));

    expect(paused.action).toBe("privacy-paused");
    await expect(device.deliver(directorCard(), "creative_01:card_01:retry"))
      .rejects.toThrow("privacy-paused");
    await expect(device.syncLocalDeliveries([])).rejects.toThrow("privacy-paused");
    expect(() => device.emitAction(deviceAction({ action: "note", operationId: "note_after_pause" })))
      .toThrow("privacy-paused");

    const events = device.events;
    expect(() => events.push(paused)).toThrow();
    expect(() => { (events[0] as { reason: string | null }).reason = "tampered"; }).toThrow();
    expect(device.events[0]).toMatchObject({ reason: "customer requested privacy" });
  });

  it("resumes a physically paused simulator before accepting more field work", async () => {
    const device = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    device.setCurrentVersion(versionKey());
    await device.deliver(directorCard(), "creative_01:card_01:1");
    device.emitAction(deviceAction({
      action: "privacy-paused",
      operationId: "privacy_resume_01",
      reason: "customer requested privacy",
    }));

    expect(device.isPrivacyPaused()).toBe(true);
    device.resumePrivacy();

    expect(device.isPrivacyPaused()).toBe(false);
    expect(() => device.emitAction(deviceAction({
      action: "note",
      operationId: "note_after_resume",
    }))).not.toThrow();
  });
});

function versionKey() {
  return {
    sessionId: "creative_01",
    briefVersion: 1,
    scriptVersion: 1,
    shotPlanVersion: 1,
  };
}

function directorCard(): DirectorCard {
  return {
    id: "card_01",
    ...versionKey(),
    stepId: "step_01",
    status: "active" as const,
    purpose: "Open the repair story",
    spokenPrompt: "Show the maker at the bench.",
    shotDescription: "Wide bench shot.",
    durationSeconds: 8,
    mustCapture: ["maker hands"],
    prohibitedCapture: ["customer faces"],
    availableActions: ["completed", "retake", "skipped", "note", "privacy-paused"],
    createdAt: NOW,
  };
}

function deviceAction(
  patch: Partial<SimulatedDeviceAction> = {},
): SimulatedDeviceAction {
  return {
    ...versionKey(),
    stepId: "step_01",
    action: "completed",
    actorId: "director_01",
    reason: null,
    operationId: "completed_01",
    now: NOW,
    ...patch,
  };
}

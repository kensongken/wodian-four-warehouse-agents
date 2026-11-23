import { describe, expect, it } from "vitest";
import { EncryptedSpool } from "../src/encrypted-spool";
import { Recorder } from "../src/recorder";

describe("privacy-aware recorder", () => {
  it("refuses bytes after physical privacy pause", () => {
    const recorder = new Recorder();

    recorder.start("daily-pendant");
    recorder.pausePrivacy();

    expect(() => recorder.appendSegment(Buffer.from("video"))).toThrow("privacy-paused");
  });

  it("shows green while an owner recording is active", () => {
    const recorder = new Recorder();

    recorder.start("daily-pendant");

    expect(recorder.events).toContainEqual({ type: "led", color: "green" });
  });

  it("resumes confirmed visitor recording with yellow LED and a prompt tone", () => {
    const recorder = new Recorder();

    recorder.start("daily-pendant");
    recorder.pausePrivacy();
    recorder.resumeVisitor("confirmed");

    expect(recorder.state).toBe("recording");
    expect(recorder.events).toContainEqual({ type: "led", color: "yellow" });
    expect(recorder.events).toContainEqual({ type: "prompt-tone" });
  });
});

describe("encrypted offline spool", () => {
  it("does not retain plaintext while phone is unavailable", async () => {
    const spool = new EncryptedSpool("test-key");

    await spool.queue("cap_001", Buffer.from("segment-01"));

    expect(await spool.rawStorageContains("segment-01")).toBe(false);
  });

  it("restores protected short files as one logical session", async () => {
    const spool = new EncryptedSpool("test-key");

    await spool.queue("cap_002", Buffer.from("A"));
    await spool.queue("cap_002", Buffer.from("B"));

    expect((await spool.restore("cap_002")).toString()).toBe("AB");
  });
});

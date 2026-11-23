import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, readdir, rm, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import ffmpegPath from "ffmpeg-static";
import request from "supertest";
import { afterEach, describe, expect, it } from "vitest";
import type {
  ModelAnalysisJobV1,
  ModelAnalysisReceiptV1,
} from "@wearable/contracts";
import {
  PlatformAutomaticEditorialAdapter,
  type EditorialDecisionAdapter,
  type StudioModelWorker,
} from "@wearable/mobile-agent";

import {
  createStudioServer,
  listenStudioServer,
  parseStudioPort,
} from "../src/studio-server";
import { normalizeUploadedFileName } from "../src/studio-material-import";

const ORIGIN = "http://127.0.0.1";
const servers = new Set<ReturnType<typeof createStudioServer>>();
const roots: string[] = [];
const execFileAsync = promisify(execFile);

describe("private content studio API", () => {
  afterEach(async () => {
    for (const server of servers) {
      if (server.listening) {
        await new Promise<void>((resolve, reject) =>
          server.close((error) => error ? reject(error) : resolve()));
      }
    }
    servers.clear();
    await Promise.all(roots.splice(0).map((root) =>
      rm(root, { recursive: true, force: true })));
  });

  it("repairs legacy UTF-8 filename mojibake without changing normal names", () => {
    const mojibake = Buffer.from("微信视频2026-10-02.mp4", "utf8").toString("latin1");
    expect(normalizeUploadedFileName(mojibake)).toBe("微信视频2026-10-02.mp4");
    expect(normalizeUploadedFileName("微信视频2026-10-02.mp4")).toBe("微信视频2026-10-02.mp4");
    expect(normalizeUploadedFileName("cafe-video.mp4")).toBe("cafe-video.mp4");
  });

  it("never accepts customer scope from an authenticated body", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");

    for (const body of [
      { customerId: "customer-b" },
      { customerId: session.customerId },
      { actorId: "another-actor" },
      { actorId: session.actorId },
      { sessionId: "another-session" },
      { sessionId: session.sessionId },
    ]) {
      const response = await studioPost(
        api,
        "/api/studio/inventory",
        body,
        session.token,
      );

      expect(response.status).toBe(403);
      expect(response.body).toEqual({ error: "studio-customer-scope-forbidden" });
    }
  }, 120_000);

  it("exports an empty caption feedback candidate dataset without claiming training approval", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");

    const response = await studioPost(
      api,
      "/api/studio/captions/feedback-dataset",
      {},
      session.token,
    );

    expect(response.status).toBe(200);
    expect(response.body.dataset).toMatchObject({
      schemaVersion: 1,
      counts: {
        total: 0,
        adopted: 0,
        rejected: 0,
        changesRequested: 0,
        unreviewed: 0,
        excludedInvalidatedDrafts: 0,
        excludedMissingParents: 0,
        excludedBindingMismatches: 0,
      },
      readiness: {
        status: "empty",
        trainingEligible: false,
        automaticTraining: false,
        releaseEligible: false,
        nextGate: "human-dataset-review-required",
      },
      boundary: "versioned-caption-candidate-dataset-not-training-approval",
    });
    expect(response.body.dataset.entries).toEqual([]);
    expect(response.body.dataset.datasetId).toMatch(/^caption-feedback-v1-[a-f0-9]{12}$/);
    expect(response.body.dataset.datasetDigest).toMatch(/^[a-f0-9]{64}$/);
  });

  it("keeps customer-created material tags private and supports several tags per material", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token);
    const materialCardId = inventory.body.materials[0]?.id;
    expect(materialCardId).toBeTypeOf("string");

    const october = await studioPost(api, "/api/studio/material-tags", {
      label: "十月拍摄",
      operationKey: "create-october-tag",
    }, session.token);
    expect(october.status).toBe(201);
    const editing = await studioPost(api, "/api/studio/material-tags", {
      label: "待剪辑",
      operationKey: "create-editing-tag",
    }, session.token);
    expect(editing.status).toBe(201);
    const tagIds = editing.body.materialTags.tags.map((tag: { id: string }) => tag.id);

    const assigned = await studioPost(api, "/api/studio/material-tags/assign", {
      materialCardId,
      tagIds,
      operationKey: "assign-two-tags",
    }, session.token);
    expect(assigned.status).toBe(200);
    expect(assigned.body.materialTags.assignments).toEqual([expect.objectContaining({
      materialCardId,
      tagIds: [...tagIds].sort(),
    })]);

    const repeated = await studioPost(api, "/api/studio/material-tags/assign", {
      materialCardId,
      tagIds,
      operationKey: "assign-two-tags",
    }, session.token);
    expect(repeated.status).toBe(200);
    expect(repeated.body.materialTags.version).toBe(3);

    const otherCustomer = await openSession(api, "customer-b");
    const privateList = await studioPost(api, "/api/studio/material-tags/list", {}, otherCustomer.token);
    expect(privateList.body.materialTags).toMatchObject({ version: 0, tags: [], assignments: [] });
    const crossCustomerAssignment = await studioPost(api, "/api/studio/material-tags/assign", {
      materialCardId,
      tagIds: [],
      operationKey: "cross-customer-assignment",
    }, otherCustomer.token);
    expect(crossCustomerAssignment.status).toBe(404);
    expect(crossCustomerAssignment.body).toEqual({ error: "material-card-not-found" });
  }, 120_000);

  it("uploads an existing MP4 into the material library without creating a work", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-uploaded-material-"));
    roots.push(fixtureRoot);
    const fixture = join(fixtureRoot, "customer-v1.mp4");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=blue:s=64x96:r=1:d=2",
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", fixture,
    ], { timeout: 30_000 });

    const uploaded = await api.post("/api/studio/materials/import")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "jewelry-making-process")
      .field("operationKey", "upload-existing-material")
      .attach("file", fixture, { filename: "微信视频2026-10-02_110736_447.mp4", contentType: "video/mp4" });

    expect(uploaded.status, JSON.stringify(uploaded.body)).toBe(201);
    expect(uploaded.body.material).toMatchObject({
      version: 1,
      contentType: "jewelry-making-process",
      technicalUsable: true,
      observations: ["客户上传素材：微信视频2026-10-02_110736_447.mp4"],
      mediaUrl: `/api/studio/materials/${uploaded.body.material.sourceAssetId}/media`,
      fileName: "微信视频2026-10-02_110736_447.mp4",
      displayName: "微信视频2026-10-02_110736_447.mp4",
      uploadSource: "手机／本机上传",
      uploadedAt: expect.any(String),
    });
    expect(uploaded.body.material.uploadedAt).toBe("2026-09-10T08:00:00.000Z");
    expect(uploaded.body.material.libraryPath).toMatch(
      /^素材库 \/ 手机／本机上传 \/ \d{4}-\d{2}-\d{2} \/ 微信视频2026-10-02_110736_447\.mp4$/,
    );
    const expectedUploadDate = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
    }).format(new Date(uploaded.body.material.uploadedAt));
    expect(uploaded.body.material.libraryPath).toContain(` / ${expectedUploadDate} / `);
    expect(uploaded.body.inventory.items).toContainEqual(expect.objectContaining({
      materialCardId: uploaded.body.material.id,
      sourceAssetId: uploaded.body.material.sourceAssetId,
    }));
    expect(uploaded.body.materials).toContainEqual(uploaded.body.material);
    const refreshed = await studioPost(api, "/api/studio/inventory", {}, session.token);
    expect(refreshed.body.inventory.items).toContainEqual(expect.objectContaining({
      materialCardId: uploaded.body.material.id,
    }));
    expect(refreshed.body.materials).toContainEqual(uploaded.body.material);
    const fullMedia = await authenticatedMedia(
      api,
      uploaded.body.material.mediaUrl,
      session.token,
    );
    expect(fullMedia.status).toBe(200);
    expect(fullMedia.headers["content-type"]).toContain("video/mp4");
    expect(Buffer.byteLength(fullMedia.body)).toBeGreaterThan(32);
    const partialMedia = await authenticatedMedia(
      api,
      uploaded.body.material.mediaUrl,
      session.token,
    ).set("range", "bytes=0-31");
    expect(partialMedia.status).toBe(206);
    expect(partialMedia.headers["accept-ranges"]).toBe("bytes");
    expect(partialMedia.headers["content-range"]).toMatch(/^bytes 0-31\//);
    expect(Buffer.byteLength(partialMedia.body)).toBe(32);
    const otherSession = await openSession(api, "customer-a");
    const crossSessionMedia = await authenticatedMedia(
      api,
      uploaded.body.material.mediaUrl,
      otherSession.token,
    );
    expect(crossSessionMedia.status).toBe(404);
    expect(crossSessionMedia.body).toEqual({ error: "material-media-not-found" });
    const history = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(history.body.history.drafts).toEqual([]);
  }, 120_000);

  it("uploads, selects, and clears a rights-bound local music track", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-uploaded-music-"));
    roots.push(fixtureRoot);
    const fixture = join(fixtureRoot, "customer-music.m4a");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
      "sine=frequency=330:sample_rate=44100:duration=2", "-c:a", "aac", "-y", fixture,
    ], { timeout: 30_000 });

    const uploaded = await api.post("/api/studio/music/import")
      .set("host", "127.0.0.1").set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("operationKey", "upload-owned-music")
      .field("sourceKind", "customer-owned")
      .field("licenseNote", "客户原创，仅授权本项目本地预览")
      .field("publishUseAllowed", "false")
      .attach("file", fixture, { filename: "客户原创配乐.m4a", contentType: "audio/mp4" });
    expect(uploaded.status, JSON.stringify(uploaded.body)).toBe(201);
    expect(uploaded.body.selectedTrackId).toBe(uploaded.body.track.assetId);
    expect(uploaded.body.track.rights).toMatchObject({
      sourceKind: "customer-owned", previewUseAllowed: true, publishUseAllowed: false,
      licenseNote: "客户原创，仅授权本项目本地预览",
    });
    expect(uploaded.body.track.audioProfile).toMatchObject({
      analyzer: "local-audio-energy-v1",
      energy: expect.stringMatching(/^(soft|balanced|strong)$/),
      highlightStartMs: expect.any(Number),
      waveform: expect.any(Array),
      reason: expect.stringContaining("本地分析建议"),
    });
    expect(uploaded.body.track.audioProfile.waveform.length).toBeGreaterThan(10);

    const listed = await studioPost(api, "/api/studio/music/list", { briefId: null }, session.token);
    expect(listed.body.tracks).toHaveLength(1);
    expect(listed.body.tracks[0]).toMatchObject({
      mediaUrl: `/api/studio/music/${uploaded.body.track.assetId}/media`,
      mixPreference: expect.objectContaining({ originalGainDb: 0, musicGainDb: -16 }),
    });
    const media = await authenticatedMedia(api, listed.body.tracks[0].mediaUrl, session.token);
    expect(media.status).toBe(200);
    expect(media.headers["content-type"]).toContain("audio/mp4");
    const nextProject = await openSession(api, "customer-a");
    const reusable = await studioPost(api, "/api/studio/music/list", { briefId: null }, nextProject.token);
    expect(reusable.body.selectedTrackId).toBeNull();
    expect(reusable.body.tracks).toHaveLength(1);
    expect(reusable.body.tracks[0]).toMatchObject({
      assetId: uploaded.body.track.assetId,
      digest: uploaded.body.track.digest,
      rights: { licenseNote: "客户原创，仅授权本项目本地预览", publishUseAllowed: false },
    });
    const reusableMedia = await authenticatedMedia(api, reusable.body.tracks[0].mediaUrl, nextProject.token);
    expect(reusableMedia.status).toBe(200);
    const otherCustomer = await openSession(api, "customer-b");
    const isolated = await studioPost(api, "/api/studio/music/list", { briefId: null }, otherCustomer.token);
    expect(isolated.body.tracks).toEqual([]);
    const mixed = await studioPost(api, "/api/studio/music/mix", {
      briefId: null,
      trackId: uploaded.body.track.assetId,
      trackStartMs: 500,
      originalGainDb: -3,
      musicGainDb: -20,
    }, session.token);
    expect(mixed.body.tracks[0].mixPreference).toEqual({
      trackStartMs: 500, originalGainDb: -3, musicGainDb: -20,
    });
    const cleared = await studioPost(api, "/api/studio/music/select", {
      briefId: null, trackId: null,
    }, session.token);
    expect(cleared.body.selectedTrackId).toBeNull();
  }, 120_000);

  it("pairs one iPhone gateway and imports its video into the bound customer library", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    const api = await createApi({ hardwareBaseUrl: "http://192.168.1.20:4273" });
    const session = await openSession(api, "customer-a");
    const created = await studioPost(api, "/api/studio/hardware/pairings", {}, session.token);
    expect(created.status).toBe(201);
    expect(created.body.pairing).toMatchObject({
      status: "waiting",
      pairingUrl: expect.stringMatching(/^http:\/\/192\.168\.1\.20:4273\/hardware\/pair\?code=/),
      qrDataUrl: expect.stringMatching(/^data:image\/png;base64,/),
    });
    const pairingUrl = new URL(created.body.pairing.pairingUrl);
    const code = pairingUrl.searchParams.get("code");
    expect(code).toBeTruthy();

    const page = await api.get(`${pairingUrl.pathname}${pairingUrl.search}`)
      .set("host", "192.168.1.20:4273");
    expect(page.status).toBe(200);
    expect(page.text).toContain("连接这台 Mac");
    expect(page.text).toContain("硬件收件箱");
    expect(page.text).toContain("本地模拟，不代表真实硬件已接入");
    expect(page.text).toContain("确认进入素材库");

    const claimed = await api.post("/api/hardware/pair")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .send({ code, deviceName: "测试 iPhone" });
    expect(claimed.status).toBe(201);
    expect(claimed.body.binding).toMatchObject({
      deviceToken: expect.stringMatching(/^hardware_/),
      deviceId: expect.stringMatching(/^iphone_gateway_/),
      deviceName: "测试 iPhone",
    });
    const secondClaim = await api.post("/api/hardware/pair")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .send({ code, deviceName: "另一台 iPhone" });
    expect(secondClaim.status).toBe(400);
    expect(secondClaim.body).toEqual({ error: "hardware-pairing-code-already-used" });

    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-hardware-material-"));
    roots.push(fixtureRoot);
    const fixture = join(fixtureRoot, "iphone-capture.mp4");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=green:s=64x96:r=1:d=2",
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", fixture,
    ], { timeout: 30_000 });
    const captureMetadata = JSON.stringify({
      captureId: "sim_capture_test_one",
      deviceId: "simulated-main-stone-local-v1",
      deviceName: "模拟智能主石",
      capturedAt: "2026-09-10T07:59:00.000Z",
      simulated: true,
    });
    const received = await api.post("/api/hardware/inbox/receive")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .set("authorization", `Bearer ${claimed.body.binding.deviceToken}`)
      .field("contentType", "single-product")
      .field("operationKey", "hardware-import-one")
      .field("captureMetadata", captureMetadata)
      .attach("file", fixture, { filename: "iPhone拍摄-戒指.mp4", contentType: "video/mp4" });
    expect(received.status, JSON.stringify(received.body)).toBe(201);
    expect(received.body.item).toMatchObject({
      id: expect.stringMatching(/^hardware_inbox_[a-f0-9]{64}$/),
      fileName: "iPhone拍摄-戒指.mp4",
      status: "received",
      materialCardId: null,
      captureMetadata: expect.objectContaining({ simulated: true, deviceName: "模拟智能主石" }),
    });

    const beforeConfirmation = await studioPost(api, "/api/studio/inventory", {}, session.token);
    expect(beforeConfirmation.body.materials).not.toContainEqual(expect.objectContaining({
      observations: ["硬件接入素材：iPhone拍摄-戒指.mp4"],
    }));
    const inbox = await api.get("/api/hardware/inbox")
      .set("host", "192.168.1.20:4273")
      .set("authorization", `Bearer ${claimed.body.binding.deviceToken}`);
    expect(inbox.status).toBe(200);
    expect(inbox.body.items).toEqual([expect.objectContaining({ id: received.body.item.id, status: "received" })]);

    const secondPairing = await studioPost(api, "/api/studio/hardware/pairings", {}, session.token);
    const secondCode = new URL(secondPairing.body.pairing.pairingUrl).searchParams.get("code");
    const secondDevice = await api.post("/api/hardware/pair")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .send({ code: secondCode, deviceName: "另一台 iPhone" });
    expect(secondDevice.status).toBe(201);
    const otherDeviceInbox = await api.get("/api/hardware/inbox")
      .set("host", "192.168.1.20:4273")
      .set("authorization", `Bearer ${secondDevice.body.binding.deviceToken}`);
    expect(otherDeviceInbox.body.items).toEqual([]);
    const otherDeviceConfirmation = await api.post("/api/hardware/inbox/confirm")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .set("authorization", `Bearer ${secondDevice.body.binding.deviceToken}`)
      .send({ id: received.body.item.id });
    expect(otherDeviceConfirmation.status).toBe(404);
    expect(otherDeviceConfirmation.body).toEqual({ error: "hardware-inbox-item-not-found" });

    const retryReceive = await api.post("/api/hardware/inbox/receive")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .set("authorization", `Bearer ${claimed.body.binding.deviceToken}`)
      .field("contentType", "single-product")
      .field("operationKey", "hardware-import-one")
      .field("captureMetadata", captureMetadata)
      .attach("file", fixture, { filename: "iPhone拍摄-戒指.mp4", contentType: "video/mp4" });
    expect(retryReceive.status).toBe(201);
    expect(retryReceive.body.item.id).toBe(received.body.item.id);

    const uploaded = await api.post("/api/hardware/inbox/confirm")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .set("authorization", `Bearer ${claimed.body.binding.deviceToken}`)
      .send({ id: received.body.item.id });
    expect(uploaded.status, JSON.stringify(uploaded.body)).toBe(200);
    expect(uploaded.body.item).toMatchObject({ status: "delivered", materialCardId: expect.any(String) });
    expect(uploaded.body.material).toMatchObject({
      displayName: "iPhone拍摄-戒指.mp4",
      uploadSource: "硬件自动接入",
      observations: ["硬件接入素材：iPhone拍摄-戒指.mp4"],
      technicalFlags: expect.arrayContaining([
        "接入设备：模拟智能主石（模拟）",
        "接入方式：模拟硬件经 iPhone",
      ]),
      pendingFacts: ["设备真实性：本地模拟，待真实硬件验证"],
    });
    expect(uploaded.body.material.libraryPath).toContain("素材库 / 硬件自动接入 /");

    const retry = await api.post("/api/hardware/inbox/confirm")
      .set("host", "192.168.1.20:4273")
      .set("origin", "http://192.168.1.20:4273")
      .set("authorization", `Bearer ${claimed.body.binding.deviceToken}`)
      .send({ id: received.body.item.id });
    expect(retry.status, JSON.stringify(retry.body)).toBe(200);
    expect(retry.body.material.id).toBe(uploaded.body.material.id);

    const inventory = await studioPost(api, "/api/studio/inventory", {}, session.token);
    expect(inventory.body.materials).toContainEqual(expect.objectContaining({
      id: uploaded.body.material.id,
      uploadSource: "硬件自动接入",
    }));
    expect(inventory.body.materials.filter((material: { id: string }) =>
      material.id === uploaded.body.material.id)).toHaveLength(1);
    const paired = await studioPost(api, "/api/studio/hardware/pairings/status", {
      pairingId: created.body.pairing.id,
    }, session.token);
    expect(paired.body.pairing).toMatchObject({ status: "connected", deviceName: "测试 iPhone" });
  }, 120_000);

  it("does not issue a phone QR code when the server is in Mac-only mode", async () => {
    const api = await createApi({ hardwarePairingEnabled: false });
    const session = await openSession(api, "customer-a");
    const response = await studioPost(api, "/api/studio/hardware/pairings", {}, session.token);

    expect(response.status).toBe(400);
    expect(response.body).toEqual({ error: "hardware-lan-disabled" });
  });

  it("accepts an iPhone-compatible MOV upload into the material library", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-uploaded-mobile-material-"));
    roots.push(fixtureRoot);
    const fixture = join(fixtureRoot, "iphone-library.mov");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=green:s=64x96:r=1:d=2",
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", fixture,
    ], { timeout: 30_000 });

    const uploaded = await api.post("/api/studio/materials/import")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "single-product")
      .field("operationKey", "upload-mobile-material")
      .attach("file", fixture, { filename: "iphone-library.mov", contentType: "video/quicktime" });

    expect(uploaded.status, JSON.stringify(uploaded.body)).toBe(201);
    expect(uploaded.body.material).toMatchObject({
      contentType: "single-product",
      technicalUsable: true,
      observations: ["客户上传素材：iphone-library.mov"],
    });
    const history = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(history.body.history.drafts).toEqual([]);
  }, 120_000);

  it("records the shortest decodable stream duration instead of a longer container duration", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-uploaded-stream-duration-"));
    roots.push(fixtureRoot);
    const fixture = join(fixtureRoot, "video-shorter-than-audio.mp4");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", "color=c=blue:s=64x96:r=25:d=2",
      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
      "-map", "0:v:0", "-map", "1:a:0",
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", fixture,
    ], { timeout: 30_000 });

    const uploaded = await api.post("/api/studio/materials/import")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "jewelry-making-process")
      .field("operationKey", "upload-stream-duration-material")
      .attach("file", fixture, { filename: "video-shorter-than-audio.mp4", contentType: "video/mp4" });

    expect(uploaded.status, JSON.stringify(uploaded.body)).toBe(201);
    expect(uploaded.body.material.sourceEndMs).toBeGreaterThanOrEqual(1_990);
    expect(uploaded.body.material.sourceEndMs).toBeLessThanOrEqual(2_010);
  }, 120_000);

  it("creates a scoped external edit handoff, returns only a package fallback, and accepts an explicit candidate only", async () => {
    const openedTargets: Array<{ kind: "jianying" | "folder"; target: string }> = [];
    const api = await createApi({
      findJianyingApp: async () => ({ appVersion: "11.5.3", appPath: "/Applications/VideoFusion-macOS.app", draftRoot: "/private/test-drafts" }),
      openExternalEditTarget: async (target) => { openedTargets.push(target); },
    });
    const session = await openSession(api, "customer-a");
    const ready = await createDemandAndMatch(api, session.token, "external-edit-api");
    const created = await createBriefAndDrafts(api, session.token, ready.demand, ready.assessment, "external-edit-api");
    const parent = created.drafts[0];

    const injectedPath = await studioPost(api, "/api/studio/external-edit-handoffs", {
      parentDraftId: parent.id,
      parentDraftVersion: parent.version,
      parentDraftDigest: parent.outputDigest,
      platformVariant: "editorial-master",
      revisionInstruction: "节奏再紧一些，但保留设计师讲返工原因的原话",
      mustKeep: ["设计师讲返工原因的原话"],
      includeSources: false,
      sourceAssetIds: [],
      operationKey: "external-edit-api-path-injection",
      sourcePath: "/etc/passwd",
      returnPath: "/tmp/return.mp4",
    }, session.token);
    expect(injectedPath.status).toBe(403);

    const createdHandoff = await studioPost(api, "/api/studio/external-edit-handoffs", {
      parentDraftId: parent.id,
      parentDraftVersion: parent.version,
      parentDraftDigest: parent.outputDigest,
      platformVariant: "editorial-master",
      revisionInstruction: "节奏再紧一些，但保留设计师讲返工原因的原话",
      mustKeep: ["设计师讲返工原因的原话"],
      includeSources: false,
      sourceAssetIds: [],
      operationKey: "external-edit-api-create",
      customerId: "customer-b",
    }, session.token);
    expect(createdHandoff.status).toBe(403);

    const handoffResponse = await studioPost(api, "/api/studio/external-edit-handoffs", {
      parentDraftId: parent.id,
      parentDraftVersion: parent.version,
      parentDraftDigest: parent.outputDigest,
      platformVariant: "editorial-master",
      revisionInstruction: "节奏再紧一些，但保留设计师讲返工原因的原话",
      mustKeep: ["设计师讲返工原因的原话"],
      includeSources: false,
      sourceAssetIds: [],
      operationKey: "external-edit-api-create",
    }, session.token);
    expect(handoffResponse.status).toBe(201);
    expect(handoffResponse.body.handoff.customerId).toBe("customer-a");
    expect(handoffResponse.body.handoff.publishClaimed).toBe(false);
    const handoffId = handoffResponse.body.handoff.id as string;

    const listed = await api.get("/api/studio/external-edit-handoffs")
      .set("host", "127.0.0.1").set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`);
    expect(listed.status).toBe(200);
    expect(listed.body.handoffs.map((item: any) => item.id)).toContain(handoffId);

    const prepared = await studioPost(api, `/api/studio/external-edit-handoffs/${handoffId}/prepare`, {}, session.token);
    expect(prepared.status).toBe(200);
    expect(prepared.body.mode).toBe("file-package");
    expect(prepared.body.health).toBe("unsupported-version");
    expect(prepared.body.degradedReason).toBe("jianying-version-not-verified");
    expect(prepared.body).not.toHaveProperty("packageRoot");
    expect(JSON.stringify(prepared.body)).not.toContain("/Users/");

    const opened = await studioPost(api, `/api/studio/external-edit-handoffs/${handoffId}/open`, {}, session.token);
    expect(opened.status).toBe(200);
    expect(opened.body).toMatchObject({
      opened: true, targetKind: "jianying", jianyingOpened: true, packageFolderOpened: true, returnFolderOpened: true,
    });
    expect(openedTargets.map(({ kind }) => kind)).toEqual(["jianying", "folder", "folder"]);
    expect(JSON.stringify(opened.body)).not.toContain("/private/");

    const candidates = await api.get(`/api/studio/external-edit-handoffs/${handoffId}/candidates`)
      .set("host", "127.0.0.1").set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`);
    expect(candidates.status).toBe(200);
    expect(candidates.body.candidates).toEqual([]);

    const forgedCandidate = await studioPost(api, `/api/studio/external-edit-handoffs/${handoffId}/candidates/fake-candidate/accept`, {
      operationKey: "external-edit-api-forged-return",
    }, session.token);
    expect(forgedCandidate.status).toBe(404);

    const invalidUpload = await api
      .post(`/api/studio/external-edit-handoffs/${handoffId}/candidates/import`)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "designer-viewpoint")
      .field("operationKey", "external-edit-invalid-return-upload")
      .attach("file", Buffer.from("not-a-video"), { filename: "损坏视频.mp4", contentType: "video/mp4" });
    expect(invalidUpload.status).toBe(422);
    expect(invalidUpload.body).toEqual({ error: "external-edit-upload-media-invalid" });

    const exportedFixtureRoot = await mkdtemp(join(tmpdir(), "studio-external-return-upload-"));
    roots.push(exportedFixtureRoot);
    const exportedFixture = join(exportedFixtureRoot, "manual-jianying-export.mp4");
    const parentMedia = await authenticatedMedia(api, parent.mediaUrl, session.token);
    expect(parentMedia.status).toBe(200);
    await writeFile(exportedFixture, parentMedia.body);
    const uploadedCandidate = await api
      .post(`/api/studio/external-edit-handoffs/${handoffId}/candidates/import`)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "designer-viewpoint")
      .field("operationKey", "external-edit-manual-return-upload")
      .attach("file", exportedFixture, { filename: "剪映导出.mp4", contentType: "video/mp4" });
    expect(uploadedCandidate.status, JSON.stringify(uploadedCandidate.body)).toBe(201);
    expect(uploadedCandidate.body.candidate).toMatchObject({
      handoffId,
      parentDraftId: parent.id,
      status: "pending-review",
      mediaUrl: expect.stringMatching(/\/candidates\/.+\/media$/u),
    });
    expect(uploadedCandidate.body.candidate).not.toHaveProperty("externalEditor");
    const retriedCandidate = await api
      .post(`/api/studio/external-edit-handoffs/${handoffId}/candidates/import`)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`)
      .field("contentType", "designer-viewpoint")
      .field("operationKey", "external-edit-manual-return-upload")
      .attach("file", exportedFixture, { filename: "再次选择同一文件.mp4", contentType: "video/mp4" });
    expect(retriedCandidate.status, JSON.stringify(retriedCandidate.body)).toBe(201);
    expect(retriedCandidate.body.candidate.id).toBe(uploadedCandidate.body.candidate.id);
    const candidatesAfterRetry = await api.get(`/api/studio/external-edit-handoffs/${handoffId}/candidates`)
      .set("host", "127.0.0.1").set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`);
    expect(candidatesAfterRetry.body.candidates).toHaveLength(1);
    const uploadedMedia = await authenticatedMedia(
      api,
      uploadedCandidate.body.candidate.mediaUrl,
      session.token,
    );
    expect(uploadedMedia.status).toBe(200);
    expect(createHash("sha256").update(uploadedMedia.body).digest("hex"))
      .toBe(uploadedCandidate.body.candidate.digest);
    const acceptedCandidate = await studioPost(
      api,
      `/api/studio/external-edit-handoffs/${handoffId}/candidates/${uploadedCandidate.body.candidate.id}/accept`,
      { operationKey: "external-edit-manual-return-accept" },
      session.token,
    );
    expect(acceptedCandidate.status, JSON.stringify(acceptedCandidate.body)).toBe(201);
    expect(acceptedCandidate.body).toMatchObject({
      lineageConfidence: "parent-only",
      publishClaimed: false,
      draft: {
        version: parent.version + 1,
        externalEdit: {
          externalEditor: "unverified-external-file",
          declaredEditor: "jianying-desktop",
          editorProvenance: "return-folder-only",
          parentDraftId: parent.id,
        },
      },
    });

    const secondSession = await openSession(api, "customer-b");
    const crossCustomer = await api.get(`/api/studio/external-edit-handoffs/${handoffId}`)
      .set("host", "127.0.0.1").set("origin", ORIGIN)
      .set("authorization", `Bearer ${secondSession.token}`);
    expect(crossCustomer.status).toBe(404);
  }, 120_000);

  it("allows an explicit LAN listener but rejects public bind addresses and invalid ports", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-listen-"));
    roots.push(root);
    const server = createStudioServer({ rootDir: root });
    servers.add(server);

    await listenStudioServer(server, 0, "0.0.0.0");
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    const rejectedServer = createStudioServer({ rootDir: root });
    servers.add(rejectedServer);
    await expect(listenStudioServer(rejectedServer, 4273, "8.8.8.8"))
      .rejects.toThrow("studio-listen-host-required");
    expect(() => parseStudioPort("70000")).toThrow("invalid-studio-port");
    expect(parseStudioPort("4273")).toBe(4273);
  });

  it("serves /studio only for a loopback Host header", async () => {
    const api = await createApi();

    const local = await api.get("/studio").set("host", "127.0.0.1");
    const external = await api.get("/studio").set("host", "studio.example");

    expect(local.status).toBe(200);
    expect(local.text).toContain("私人内容工作台");
    expect(external.status).toBe(403);
    expect(external.body).toEqual({ error: "studio-loopback-host-required" });
  });

  it("resumes the same in-memory session from its HttpOnly cookie after a page reload", async () => {
    const api = await createApi();
    const root = roots.at(-1)!;
    const opened = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .send({ customerId: "customer-a" })
      .expect(201);
    const setCookie = opened.headers["set-cookie"];
    const cookie = (Array.isArray(setCookie) ? setCookie[0] : setCookie)?.split(";")[0];
    expect(cookie).toMatch(/^studio_session=studio_/);
    const session = opened.body.session as { token: string; sessionId: string };
    expect(JSON.parse(await readFile(join(root, "active-session.json"), "utf8"))).toMatchObject({
      schemaVersion: 1,
      token: session.token,
      customerId: "customer-a",
      customerLabel: "合成客户 A",
      sessionId: session.sessionId,
    });
    const demand = await studioPost(api, "/api/studio/demands", demandBody("resume-cookie"), session.token);
    expect(demand.status).toBe(201);

    const resumed = await api
      .post("/api/studio/session/resume")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", cookie!)
      .send({});
    expect(resumed.status).toBe(200);
    expect(resumed.body).toMatchObject({
      resumed: true,
      session: { token: session.token, sessionId: session.sessionId, customerId: "customer-a" },
      automaticPublishing: false,
    });

    const history = await api
      .post("/api/studio/history")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", cookie!)
      .send({});
    expect(history.status).toBe(200);
    expect(history.body.history.demands).toContainEqual(expect.objectContaining({
      id: demand.body.demand.id,
      status: "current",
    }));
  }, 120_000);

  it("connects an authenticated local session to the browser with a one-time URL", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const created = await studioPost(
      api,
      "/api/studio/browser-connections",
      {},
      session.token,
    );

    expect(created.status).toBe(201);
    expect(created.body).toMatchObject({
      oneTime: true,
      connectUrl: expect.stringMatching(/^\/studio\/connect\?code=[0-9a-f-]{36}$/u),
    });
    expect(JSON.stringify(created.body)).not.toContain(session.token);

    const connected = await api
      .get(created.body.connectUrl)
      .set("host", "127.0.0.1")
      .redirects(0);
    expect(connected.status).toBe(303);
    expect(connected.headers.location).toBe("/studio");
    const setCookie = connected.headers["set-cookie"];
    const cookie = (Array.isArray(setCookie) ? setCookie[0] : setCookie)?.split(";")[0];
    expect(cookie).toBe(`studio_session=${session.token}`);

    const resumed = await api
      .post("/api/studio/session/resume")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", cookie!)
      .send({});
    expect(resumed.status).toBe(200);
    expect(resumed.body.session).toMatchObject({
      token: session.token,
      customerId: "customer-a",
      sessionId: session.sessionId,
    });

    const reused = await api
      .get(created.body.connectUrl)
      .set("host", "127.0.0.1")
      .redirects(0);
    expect(reused.status).toBe(404);
    expect(reused.body).toEqual({ error: "studio-browser-connection-not-found" });
  });

  it("advertises the four-platform automatic editor on create and resume", async () => {
    const api = await createApi({ editorial: new PlatformAutomaticEditorialAdapter() });
    const created = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .send({ customerId: "customer-a" })
      .expect(201);

    expect(created.body.capability).toMatchObject({
      mode: "provider",
      label: "本地平台自动剪辑 v1",
      provider: "local-rule:platform-automatic-editor-v1",
    });
    const resumed = await studioPost(api, "/api/studio/session/resume", {}, created.body.session.token);
    expect(resumed.status).toBe(200);
    expect(resumed.body.capability).toEqual(created.body.capability);
  }, 120_000);

  it("restores the local workflow ledger and draft media after the server process restarts", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-restart-resume-"));
    roots.push(root);
    const serverOptions = {
      rootDir: root,
      customers: [
        { id: "customer-a", label: "合成客户 A" },
        { id: "customer-b", label: "合成客户 B" },
      ],
      now: () => "2026-09-10T08:00:00.000Z",
      openExternalEditTarget: async () => undefined,
      findJianyingApp: async () => null,
      modelWorker: isolatedApiTestModelWorker(),
    } as const;
    const firstServer = createStudioServer(serverOptions);
    servers.add(firstServer);
    const firstApi = request(firstServer);
    const journey = await runStudioApiJourney(firstApi);
    const cookie = `studio_session=${journey.token}`;

    const restartedServer = createStudioServer(serverOptions);
    servers.add(restartedServer);
    const restartedApi = request(restartedServer);
    const resumed = await restartedApi
      .post("/api/studio/session/resume")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", cookie)
      .send({});
    expect(resumed.status, JSON.stringify(resumed.body)).toBe(200);
    expect(resumed.body.session).toMatchObject({
      token: journey.token,
      customerId: "customer-a",
    });

    const history = await restartedApi
      .post("/api/studio/history")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", cookie)
      .send({});
    expect(history.status, JSON.stringify(history.body)).toBe(200);
    expect(history.body.history.demands).toHaveLength(1);
    expect(history.body.history.briefs).toHaveLength(1);
    expect(history.body.history.drafts).toHaveLength(2);
    expect(history.body.history.drafts).toContainEqual(expect.objectContaining({
      id: journey.drafts[0].id,
      status: "changes-requested",
    }));

    const media = await restartedApi
      .get(history.body.history.drafts[0].mediaUrl)
      .set("host", "127.0.0.1")
      .set("referer", `${ORIGIN}/studio`)
      .set("cookie", cookie);
    expect(media.status).toBe(200);
    expect(media.headers["content-type"]).toContain("video/mp4");
    expect(Buffer.byteLength(media.body)).toBeGreaterThan(0);
  }, 420_000);

  it("requires same-origin application/json for every JSON write", async () => {
    const api = await createApi();

    const wrongOrigin = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", "https://outside.example")
      .send({ customerId: "customer-a" });
    const wrongType = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("content-type", "text/plain")
      .send("customer-a");

    expect(wrongOrigin.status).toBe(403);
    expect(wrongOrigin.body).toEqual({ error: "studio-same-origin-required" });
    expect(wrongType.status).toBe(415);
    expect(wrongType.body).toEqual({ error: "studio-json-content-type-required" });
  });

  it("returns stable 400 errors for studio input overflows and accepts every exact boundary", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const items = Array.from({ length: 20 }, (_, index) =>
      `${String(index).padStart(2, "0")}${"项".repeat(198)}`);
    const boundaryDemand = await studioPost(api, "/api/studio/demands", {
      ...demandBody("boundary"),
      goal: "g".repeat(500),
      targetAudience: "a".repeat(200),
      desiredEffect: "e".repeat(500),
      mustInclude: items,
      mustAvoid: items.map((item) => item.replace("项", "避")),
      operationKey: "k".repeat(200),
    }, session.token);
    expect(boundaryDemand.status).toBe(201);

    const demandFailures = [
      [{ ...demandBody("goal-over"), goal: "g".repeat(501) }, "demand-goal-too-long"],
      [{ ...demandBody("audience-over"), targetAudience: "a".repeat(201) }, "demand-target-audience-too-long"],
      [{ ...demandBody("effect-over"), desiredEffect: "e".repeat(501) }, "demand-desired-effect-too-long"],
      [{ ...demandBody("count-over"), mustInclude: Array.from({ length: 21 }, (_, index) => `item-${index}`) }, "demand-must-include-too-many"],
      [{ ...demandBody("item-over"), mustAvoid: ["x".repeat(201)] }, "demand-must-avoid-item-too-long"],
      [{ ...demandBody("key-over"), operationKey: "k".repeat(201) }, "demand-operation-key-too-long"],
    ] as const;
    for (const [body, error] of demandFailures) {
      const response = await studioPost(api, "/api/studio/demands", body, session.token);
      expect(response.status).toBe(400);
      expect(response.body).toEqual({ error });
    }

    const regular = await createDemandAndMatch(api, session.token, "feedback-boundary");
    const created = await createBriefAndDrafts(api, session.token, regular.demand, regular.assessment, "feedback-boundary");
    const boundaryFeedback = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: created.drafts[0].id,
      satisfaction: "partially-satisfied",
      issueCategories: ["pacing"],
      comment: "c".repeat(1000),
      mustKeep: items,
      mustRemove: items.map((item) => item.replace("项", "删")),
      needsReshoot: items.map((item) => item.replace("项", "拍")),
      submittedBy: "customer-self-declared",
      operationKey: "f".repeat(200),
    }, session.token);
    expect(boundaryFeedback.status).toBe(201);

    const feedbackFailures = [
      [{ comment: "c".repeat(1001) }, "customer-feedback-comment-too-long"],
      [{ mustKeep: Array.from({ length: 21 }, (_, index) => `item-${index}`) }, "customer-feedback-must-keep-too-many"],
      [{ mustRemove: ["x".repeat(201)] }, "customer-feedback-must-remove-item-too-long"],
      [{ needsReshoot: ["x".repeat(201)] }, "customer-feedback-needs-reshoot-item-too-long"],
      [{ operationKey: "k".repeat(201) }, "customer-feedback-operation-key-too-long"],
    ] as const;
    for (const [override, error] of feedbackFailures) {
      const response = await studioPost(api, "/api/studio/customer-feedback", {
        draftId: created.drafts[0].id,
        satisfaction: "partially-satisfied",
        issueCategories: ["pacing"],
        comment: "节奏太慢",
        mustKeep: [], mustRemove: [], needsReshoot: [],
        submittedBy: "customer-self-declared",
        operationKey: `feedback-${error}`,
        ...override,
      }, session.token);
      expect(response.status).toBe(400);
      expect(response.body).toEqual({ error });
    }
  }, 120_000);

  it("rejects repeated structured demand and feedback items before persistence", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const fields = [
      {
        path: "/api/studio/demands",
        field: "mustInclude",
        tooManyError: "demand-must-include-too-many",
        duplicateError: "duplicate-must-include",
      },
      {
        path: "/api/studio/demands",
        field: "mustAvoid",
        tooManyError: "demand-must-avoid-too-many",
        duplicateError: "duplicate-must-avoid",
      },
      {
        path: "/api/studio/customer-feedback",
        field: "mustKeep",
        tooManyError: "customer-feedback-must-keep-too-many",
        duplicateError: "duplicate-must-keep",
      },
      {
        path: "/api/studio/customer-feedback",
        field: "mustRemove",
        tooManyError: "customer-feedback-must-remove-too-many",
        duplicateError: "duplicate-must-remove",
      },
      {
        path: "/api/studio/customer-feedback",
        field: "needsReshoot",
        tooManyError: "customer-feedback-needs-reshoot-too-many",
        duplicateError: "duplicate-needs-reshoot",
      },
    ] as const;
    const scenarios = [
      {
        name: "21 repeated items",
        values: Array.from({ length: 21 }, () => "重复项"),
        errorKey: "tooManyError",
      },
      {
        name: "two repeated items",
        values: ["重复项", "重复项"],
        errorKey: "duplicateError",
      },
      {
        name: "trim-normalized repeated items",
        values: ["重复项", "  重复项  "],
        errorKey: "duplicateError",
      },
    ] as const;

    for (const field of fields) {
      for (const scenario of scenarios) {
        const operationKey = `${field.field}-${scenario.name}`;
        const body = field.path === "/api/studio/demands"
          ? { ...demandBody(operationKey), [field.field]: scenario.values }
          : {
            draftId: "draft-not-created",
            satisfaction: "partially-satisfied",
            issueCategories: ["pacing"],
            comment: "节奏太慢",
            mustKeep: [],
            mustRemove: [],
            needsReshoot: [],
            submittedBy: "customer-self-declared",
            operationKey,
            [field.field]: scenario.values,
          };
        const response = await studioPost(api, field.path, body, session.token);
        expect(response.status, `${field.field}: ${scenario.name}`).toBe(400);
        expect(response.body).toEqual({ error: field[scenario.errorKey] });

        const history = await studioPost(api, "/api/studio/history", {}, session.token);
        expect(history.status).toBe(200);
        expect(history.body.history.demands).toEqual([]);
        expect(history.body.history.customerFeedback).toEqual([]);
      }
    }
  }, 120_000);

  it("walks session, inventory, opportunity, brief, drafts, and feedback", async () => {
    const api = await createApi();

    const result = await runStudioApiJourney(api);

    expect(result.inventory.items).toHaveLength(4);
    expect(result.materials.flatMap((material: any) => material.observations).sort()).toEqual([
      "纯色合成商品夹具 1",
      "纯色合成商品夹具 2",
      "纯色合成商品夹具 3",
      "纯色合成商品夹具 4",
    ]);
    expect(result.materials.every((material: any) =>
      material.pendingFacts.join("") === "example-attribute: 示例属性字段待人工确认",
    )).toBe(true);
    expect(result.opportunities).toHaveLength(1);
    expect(result.assessment.verdict).toBe("ready");
    expect(result.brief.status).toBe("confirmed");
    expect(result.drafts).toHaveLength(2);
    expect(result.drafts.every((draft: any) => !("outputPath" in draft))).toBe(true);
    expect(result.drafts[0].blueprintDigest)
      .not.toBe(result.drafts[1].blueprintDigest);
    expect(result.drafts.every((draft: any) =>
      draft.publishClaimed === false &&
      draft.capability.label === "离线/模拟分析" &&
      ["passed", "needs-review"].includes(draft.qualityReport?.status) &&
      draft.qualityReport?.checks?.length === 6 &&
      draft.mediaUrl.startsWith("/api/studio/drafts/") &&
      draft.sourceMap.every((source: any) => source.sourceDigest && source.reason),
    )).toBe(true);
    expect(result.feedback).toMatchObject({
      action: "request-changes",
      reason: "请让设计师观点更突出",
      publishClaimed: false,
    });
  }, 120_000);

  it("creates an immutable playable draft after editing caption text and style", async () => {
    const automatic = new PlatformAutomaticEditorialAdapter();
    const captionedEditorial: EditorialDecisionAdapter = {
      capability: automatic.capability,
      createBlueprints: (input) => automatic.createBlueprints({
          ...input,
          transcriptSpans: input.cards.flatMap((card) => [
            {
              sourceAssetId: card.sourceAssetId,
              sourceDigest: card.sourceDigest,
              startMs: card.sourceStartMs + 1_000,
              endMs: card.sourceStartMs + 3_000,
              text: "这是自动生成的待修改字幕",
            },
          ]),
        }),
    };
    const api = await createApi({ editorial: captionedEditorial });
    const result = await runStudioApiJourney(api, {
      mode: "provider",
      label: "本地平台自动剪辑 v1",
    });
    const parent = result.drafts.find((draft: any) => draft.captionCues?.length > 0);
    expect(parent).toBeTruthy();
    const captionTexts = parent.captionCues.map((cue: any, index: number) => ({
      id: cue.id,
      text: index === 0 ? "这句字幕已经人工修正" : cue.text,
    }));
    const response = await studioPost(
      api,
      `/api/studio/drafts/${encodeURIComponent(parent.id)}/captions`,
      {
        captionTexts,
        style: {
          ...parent.captionStyle,
          fontSizePx: 60,
          primaryColor: "#00FF00",
          bottomMarginPx: 220,
        },
        operationKey: "caption-revision-api-1",
      },
      result.token,
    );

    expect(response.status).toBe(201);
    expect(response.body.draft.id).not.toBe(parent.id);
    expect(response.body.draft.outputDigest).not.toBe(parent.outputDigest);
    expect(response.body.draft.captionCues[0].text).toBe("这句字幕已经人工修正");
    expect(response.body.draft.captionStyle).toMatchObject({
      fontSizePx: 60,
      primaryColor: "#00FF00",
      bottomMarginPx: 220,
    });
    expect(response.body.draft.captionRevision).toEqual({
      parentDraftId: parent.id,
      changedCueIds: [parent.captionCues[0].id],
      styleChanged: true,
    });
    expect(response.body.draft.qualityReport.status).not.toBe("failed");
    expect(response.body.draft.mediaUrl).toContain(response.body.draft.id);

  // This exercise performs the normal draft render and then a second ffmpeg
  // subtitle burn-in. On a busy local Mac that verified path can exceed two
  // minutes; the assertions remain unchanged and still require a playable,
  // immutable child draft.
  }, 300_000);

  it("installs a human-approved subtitle chapter as a separate local draft", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const inventoryResponse = await studioPost(api, "/api/studio/inventory", {}, session.token);
    const source = inventoryResponse.body.materials[0];
    const { demand, assessment } = await createDemandAndMatch(
      api,
      session.token,
      "approved-caption-import",
    );
    expect(assessment.verdict).toBe("ready");
    const opportunities = await studioPost(api, "/api/studio/opportunities", {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    }, session.token);
    expect(opportunities.status).toBe(200);
    const briefResponse = await studioPost(api, "/api/studio/briefs", {
      opportunityId: opportunities.body.opportunities[0].id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: inventoryResponse.body.inventory.items.map((item: any) => item.sourceAssetId),
      confirmedFactIds: ["example-attribute: 示例属性字段待人工确认"],
      reviewRequirements: ["发布前人工审核"],
      operationKey: "approved-caption-brief",
    }, session.token);
    expect(briefResponse.status).toBe(201);

    // These two terms and windows come from the user's human-reviewed local
    // jewelry sample. They are regression bindings only: preserving them does
    // not claim that the speech model learned or generalized the corrections.
    const captionCues = [{
      id: "approved-cue-jinhong",
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: 0,
      sourceEndMs: 3_400,
      timelineStartMs: 0,
      timelineEndMs: 3_400,
      text: "这条是咱们东山的一个锦红",
      reviewStatus: "human-approved",
    }, {
      id: "approved-cue-five-millimeters",
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: 5_560,
      sourceEndMs: 7_120,
      timelineStartMs: 5_560,
      timelineEndMs: 7_120,
      text: "然后五毫米左右这样子的一个",
      reviewStatus: "human-approved",
    }];
    const subtitleDigest = createHash("sha256").update("approved-subtitle-v1").digest("hex");
    const response = await studioPost(api, "/api/studio/approved-caption-drafts", {
      briefId: briefResponse.body.brief.id,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: 0,
      sourceEndMs: 10_000,
      subtitleDigest,
      approvedAt: "2026-09-10T08:00:00.000Z",
      captionCues,
      style: {
        template: "clean-outline",
        fontSizePx: 52,
        primaryColor: "#FFFFFF",
        outlineColor: "#111111",
        outlineWidthPx: 4,
        bottomMarginPx: 180,
        maxCharsPerLine: 14,
      },
      operationKey: "approved-caption-import-1",
    }, session.token);

    expect(response.status, JSON.stringify(response.body)).toBe(201);
    expect(response.body.publishClaimed).toBe(false);
    expect(response.body.draft).toMatchObject({
      status: "reviewable",
      platformVariant: "editorial-master",
      publishClaimed: false,
      approvedCaptionImport: {
        sourceDigest: source.sourceDigest,
        subtitleDigest,
        sourceStartMs: 0,
        sourceEndMs: 10_000,
        approvalScope: "local-human-approved-candidate",
        productionApproved: false,
      },
    });
    expect(response.body.draft.captionCues).toEqual(captionCues);
    expect(response.body.draft.mediaUrl).toMatch(/^\/api\/studio\/drafts\/.+\/media$/u);
    expect(response.body.draft).not.toHaveProperty("outputPath");
    const media = await authenticatedMedia(api, response.body.draft.mediaUrl, session.token);
    expect(media.status).toBe(200);
    expect(media.headers["content-type"]).toBe("video/mp4");

    const history = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(history.body.history.drafts).toContainEqual(response.body.draft);

    const handoff = await studioPost(api, "/api/studio/external-edit-handoffs", {
      parentDraftId: response.body.draft.id,
      parentDraftVersion: response.body.draft.version,
      parentDraftDigest: response.body.draft.outputDigest,
      platformVariant: "editorial-master",
      revisionInstruction: "保留已验收字幕，只做画面节奏精修，不要发布。",
      mustKeep: ["保留已验收字幕", "保留原声"],
      includeSources: false,
      sourceAssetIds: [],
      operationKey: "approved-caption-handoff-1",
    }, session.token);
    expect(handoff.status).toBe(201);
    const prepared = await studioPost(
      api,
      `/api/studio/external-edit-handoffs/${encodeURIComponent(handoff.body.handoff.id)}/prepare`,
      {},
      session.token,
    );
    expect(prepared.status).toBe(200);
    expect(prepared.body).toMatchObject({
      mode: "file-package",
      health: "uninstalled",
      degradedReason: "jianying-not-installed",
      handoff: { status: "degraded" },
    });
    expect(prepared.body.package.files).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: "parent-video", digest: response.body.draft.outputDigest }),
      expect.objectContaining({ role: "subtitle", relativePath: "support/subtitle.srt" }),
      expect.objectContaining({ role: "instructions", relativePath: "README.txt" }),
    ]));

    const revised = await studioPost(
      api,
      `/api/studio/drafts/${encodeURIComponent(response.body.draft.id)}/captions`,
      {
        // The API stores a complete immutable subtitle snapshot. The UI may
        // edit one visible row, but it must submit the untouched rows too so a
        // retry cannot silently drop a previously approved caption.
        captionTexts: [
          { id: "approved-cue-jinhong", text: "改过之后必须重新人工确认" },
          { id: "approved-cue-five-millimeters", text: "然后五毫米左右这样子的一个" },
        ],
        style: {
          template: "clean-outline",
          fontSizePx: 52,
          primaryColor: "#FFFFFF",
          outlineColor: "#111111",
          outlineWidthPx: 4,
          bottomMarginPx: 180,
          maxCharsPerLine: 14,
        },
        operationKey: "approved-caption-revision-1",
      },
      session.token,
    );
    expect(revised.status).toBe(201);
    expect(revised.body.draft).not.toHaveProperty("approvedCaptionImport");
    expect(revised.body.draft.captionCues).toEqual([
      expect.objectContaining({
        id: "approved-cue-jinhong",
        text: "改过之后必须重新人工确认",
        reviewStatus: "model-suggestion",
      }),
      expect.objectContaining({
        id: "approved-cue-five-millimeters",
        text: "然后五毫米左右这样子的一个",
        reviewStatus: "human-approved",
      }),
    ]);
  }, 120_000);

  it("requires demand, green match, customer feedback, and internal confirmation before V2", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const { demand, assessment } = await createDemandAndMatch(
      api,
      session.token,
      "full-journey",
    );
    expect(assessment.verdict).toBe("ready");

    const v1 = await createBriefAndDrafts(api, session.token, demand, assessment, "full-journey");
    const feedbackResponse = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: v1.drafts[0].id,
      satisfaction: "partially-satisfied",
      issueCategories: ["style"],
      comment: "请让设计师观点更突出",
      mustKeep: ["成品结尾"],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-full-journey",
    }, session.token);
    expect(feedbackResponse.status).toBe(201);

    const proposalResponse = await studioPost(api, "/api/studio/revision-proposals", {
      feedbackId: feedbackResponse.body.feedback.id,
      operationKey: "proposal-full-journey",
    }, session.token);
    expect(proposalResponse.status).toBe(201);
    expect(proposalResponse.body.proposal.status).toBe("proposed");

    const historyResponse = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(historyResponse.status).toBe(200);
    expect(historyResponse.body.history.briefs).toHaveLength(1);
    expect(historyResponse.body.history.drafts.every((draft: any) =>
      !("outputPath" in draft) && draft.mediaUrl.startsWith("/api/studio/drafts/"),
    )).toBe(true);

    const confirmedResponse = await studioPost(api, "/api/studio/revision-proposals/confirm", {
      proposalId: proposalResponse.body.proposal.id,
      operationKey: "confirm-full-journey",
    }, session.token);
    expect(confirmedResponse.status).toBe(201);
    expect(confirmedResponse.body.brief).toMatchObject({
      version: 2,
      parentBriefId: v1.brief.id,
      feedbackId: feedbackResponse.body.feedback.id,
      revisionProposalId: proposalResponse.body.proposal.id,
    });

    const staleFeedback = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: v1.drafts[1].id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-stale-v1",
    }, session.token);
    expect(staleFeedback.status).toBe(409);
    expect(staleFeedback.body).toEqual({ error: "brief-version-not-current" });
  }, 120_000);

  it("maps a satisfied feedback revision request to an exact business conflict", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const ready = await createDemandAndMatch(api, session.token, "satisfied-feedback");
    const v1 = await createBriefAndDrafts(
      api,
      session.token,
      ready.demand,
      ready.assessment,
      "satisfied-feedback",
    );
    const feedback = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: v1.drafts[0].id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-satisfied",
    }, session.token);
    expect(feedback.status).toBe(201);

    const proposal = await studioPost(api, "/api/studio/revision-proposals", {
      feedbackId: feedback.body.feedback.id,
      operationKey: "proposal-satisfied",
    }, session.token);

    expect(proposal.status).toBe(409);
    expect(proposal.body).toEqual({ error: "feedback-not-revision-request" });
  }, 120_000);

  it("persists local distribution actions without claiming platform delivery", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const ready = await createDemandAndMatch(api, session.token, "distribution-ledger");
    const created = await createBriefAndDrafts(
      api, session.token, ready.demand, ready.assessment, "distribution-ledger",
    );
    const draft = created.drafts[0];

    const blocked = await studioPost(api, "/api/studio/distribution-events", {
      draftId: draft.id,
      action: "download-started",
      operationKey: "distribution-before-adopt",
    }, session.token);
    expect(blocked.status).toBe(409);
    expect(blocked.body).toEqual({ error: "distribution-draft-not-adopted" });

    const satisfaction = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: draft.id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "distribution-satisfied",
    }, session.token);
    expect(satisfaction.status).toBe(201);
    const adoption = await studioPost(api, "/api/studio/feedback", {
      draftId: draft.id,
      action: "adopt",
      reason: null,
      operationKey: "distribution-adopt",
    }, session.token);
    expect(adoption.status).toBe(201);

    const recorded = await studioPost(api, "/api/studio/distribution-events", {
      draftId: draft.id,
      action: "system-share-invoked",
      operationKey: "distribution-share-1",
    }, session.token);
    expect(recorded.status).toBe(201);
    expect(recorded.body.event).toMatchObject({
      draftId: draft.id,
      action: "system-share-invoked",
      platformVariant: draft.platformVariant ?? null,
      publishClaimed: false,
      externalDeliveryClaimed: false,
    });

    const history = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(history.status).toBe(200);
    expect(history.body.history.distributionEvents).toEqual([recorded.body.event]);

    const restartedServer = createStudioServer({
      rootDir: roots.at(-1)!,
      customers: [
        { id: "customer-a", label: "合成客户 A" },
        { id: "customer-b", label: "合成客户 B" },
      ],
      now: () => "2026-09-10T08:00:00.000Z",
      openExternalEditTarget: async () => undefined,
      findJianyingApp: async () => null,
      modelWorker: isolatedApiTestModelWorker(),
    });
    servers.add(restartedServer);
    const restartedApi = request(restartedServer);
    const resumed = await studioPost(restartedApi, "/api/studio/session/resume", {}, session.token);
    expect(resumed.status).toBe(200);
    const restoredHistory = await studioPost(restartedApi, "/api/studio/history", {}, session.token);
    expect(restoredHistory.status).toBe(200);
    expect(restoredHistory.body.history.distributionEvents).toEqual([recorded.body.event]);
  }, 300_000);

  it("creates a separate vertical share version only from an adopted draft", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const ready = await createDemandAndMatch(api, session.token, "share-version");
    const created = await createBriefAndDrafts(
      api, session.token, ready.demand, ready.assessment, "share-version",
    );
    const parent = created.drafts[0];
    const parentMediaBefore = await api
      .get(parent.mediaUrl)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`);
    expect(parentMediaBefore.status).toBe(200);
    const parentDigestBefore = createHash("sha256").update(parentMediaBefore.body).digest("hex");

    const blocked = await studioPost(api, `/api/studio/drafts/${parent.id}/share-version`, {
      operationKey: "share-version-before-adopt",
    }, session.token);
    expect(blocked.status).toBe(409);
    expect(blocked.body).toEqual({ error: "share-optimization-parent-not-adopted" });

    const satisfaction = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: parent.id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "share-version-satisfied",
    }, session.token);
    expect(satisfaction.status).toBe(201);
    const adoption = await studioPost(api, "/api/studio/feedback", {
      draftId: parent.id,
      action: "adopt",
      reason: null,
      operationKey: "share-version-adopt",
    }, session.token);
    expect(adoption.status).toBe(201);

    const shared = await studioPost(api, `/api/studio/drafts/${parent.id}/share-version`, {
      operationKey: "share-version-create",
    }, session.token);
    expect(shared.status, JSON.stringify(shared.body)).toBe(201);
    expect(shared.body.draft).toMatchObject({
      status: "reviewable",
      publishClaimed: false,
      shareOptimization: {
        parentDraftId: parent.id,
        targetWidth: 720,
        targetHeight: 1280,
        mode: "fit-pad-no-crop",
      },
    });
    expect(shared.body.draft.id).not.toBe(parent.id);
    expect(shared.body.draft.outputDigest).not.toBe(parent.outputDigest);
    expect(shared.body.draft.outputPath).toBeUndefined();

    const repeat = await studioPost(api, `/api/studio/drafts/${parent.id}/share-version`, {
      operationKey: "share-version-create",
    }, session.token);
    expect(repeat.status).toBe(201);
    expect(repeat.body.draft.id).toBe(shared.body.draft.id);

    const parentMediaAfter = await api
      .get(parent.mediaUrl)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${session.token}`);
    expect(parentMediaAfter.status).toBe(200);
    expect(createHash("sha256").update(parentMediaAfter.body).digest("hex")).toBe(parentDigestBefore);

    const history = await studioPost(api, "/api/studio/history", {}, session.token);
    expect(history.status).toBe(200);
    expect(history.body.history.drafts).toContainEqual(expect.objectContaining({
      id: parent.id,
      status: "adopted",
      outputDigest: parent.outputDigest,
    }));
    expect(history.body.history.drafts).toContainEqual(expect.objectContaining({
      id: shared.body.draft.id,
      status: "reviewable",
      shareOptimization: expect.objectContaining({ parentDraftId: parent.id }),
    }));
  }, 300_000);

  it("rejects malformed structured customer feedback fields with exact validation errors", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const valid = {
      draftId: "draft-unknown",
      satisfaction: "partially-satisfied",
      issueCategories: ["style"],
      comment: "需要调整风格",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-validation",
    };
    const { comment: _comment, ...withoutComment } = valid;
    const cases: ReadonlyArray<readonly [
      string,
      Record<string, unknown>,
      string,
    ]> = [
      ["invalid issue category enum", { ...valid, issueCategories: ["not-a-category"] }, "customer-feedback-issue-categories-invalid"],
      ["issue categories null", { ...valid, issueCategories: null }, "customer-feedback-issue-categories-invalid"],
      ["issue categories string", { ...valid, issueCategories: "style" }, "customer-feedback-issue-categories-invalid"],
      ["must keep null", { ...valid, mustKeep: null }, "customer-feedback-must-keep-invalid"],
      ["must keep string", { ...valid, mustKeep: "成品结尾" }, "customer-feedback-must-keep-invalid"],
      ["must remove null", { ...valid, mustRemove: null }, "customer-feedback-must-remove-invalid"],
      ["must remove string", { ...valid, mustRemove: "某个镜头" }, "customer-feedback-must-remove-invalid"],
      ["needs reshoot null", { ...valid, needsReshoot: null }, "customer-feedback-needs-reshoot-invalid"],
      ["needs reshoot string", { ...valid, needsReshoot: "补拍场景" }, "customer-feedback-needs-reshoot-invalid"],
      ["invalid submitter", { ...valid, submittedBy: "browser-actor" }, "studio-demand-submitter-invalid"],
      ["missing comment", withoutComment, "customer-feedback-comment-invalid"],
      ["numeric comment", { ...valid, comment: 42 }, "customer-feedback-comment-invalid"],
      ["empty comment", { ...valid, comment: "" }, "customer-feedback-comment-invalid"],
      ["blank comment", { ...valid, comment: "   " }, "customer-feedback-comment-invalid"],
    ];

    for (const [label, body, error] of cases) {
      const response = await studioPost(
        api,
        "/api/studio/customer-feedback",
        { ...body, operationKey: `feedback-validation-${label}` },
        session.token,
      );
      expect(response.status, label).toBe(400);
      expect(response.body, label).toEqual({ error });
    }
  }, 120_000);

  it("maps validation, scope, match, and capture-gap boundaries exactly", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");

    const invalidDemand = await studioPost(api, "/api/studio/demands", {
      ...demandBody("invalid-demand"),
      contentType: "automatic-publishing",
    }, session.token);
    const invalidFeedback = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: "draft-unknown",
      satisfaction: "mostly-satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "invalid-feedback",
    }, session.token);
    const spoofedHistory = await studioPost(
      api,
      "/api/studio/history",
      { customerId: "customer-b" },
      session.token,
    );
    const missingDemandBinding = await studioPost(api, "/api/studio/opportunities", {
      matchAssessmentId: "match-unknown",
    }, session.token);
    const missingMatchBinding = await studioPost(api, "/api/studio/opportunities", {
      demandId: "demand-unknown",
    }, session.token);

    const partial = await createDemandAndMatch(api, session.token, "partial", {
      mustInclude: ["不存在的特定镜头"],
    });
    expect(partial.assessment.verdict).toBe("partial");
    const partialOpportunity = await studioPost(api, "/api/studio/opportunities", {
      demandId: partial.demand.id,
      matchAssessmentId: partial.assessment.id,
    }, session.token);

    const blocked = await createDemandAndMatch(api, session.token, "blocked", {
      mustAvoid: ["纯色合成商品夹具"],
    });
    expect(blocked.assessment.verdict).toBe("blocked");
    const blockedOpportunity = await studioPost(api, "/api/studio/opportunities", {
      demandId: blocked.demand.id,
      matchAssessmentId: blocked.assessment.id,
    }, session.token);

    const ready = await createDemandAndMatch(api, session.token, "capture-gap");
    const v1 = await createBriefAndDrafts(api, session.token, ready.demand, ready.assessment, "capture-gap");
    const missingBriefMatchBinding = await studioPost(api, "/api/studio/briefs", {
      opportunityId: v1.opportunities[0].id,
    }, session.token);
    const gapFeedback = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: v1.drafts[0].id,
      satisfaction: "unsatisfied",
      issueCategories: ["shot-selection"],
      comment: "需要新场景",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: ["补一个新场景"],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-capture-gap",
    }, session.token);
    expect(gapFeedback.status).toBe(201);
    const gapProposal = await studioPost(api, "/api/studio/revision-proposals", {
      feedbackId: gapFeedback.body.feedback.id,
      operationKey: "proposal-capture-gap",
    }, session.token);
    expect(gapProposal.status).toBe(201);
    const gapConfirmation = await studioPost(api, "/api/studio/revision-proposals/confirm", {
      proposalId: gapProposal.body.proposal.id,
      operationKey: "confirm-capture-gap",
    }, session.token);

    for (const [label, response, status, error] of [
      ["invalid demand enum", invalidDemand, 400, "studio-content-type-invalid"],
      ["invalid feedback enum", invalidFeedback, 400, "customer-feedback-satisfaction-invalid"],
      ["body customer spoof", spoofedHistory, 403, "studio-customer-scope-forbidden"],
      ["opportunity demand binding", missingDemandBinding, 400, "demand-id-required"],
      ["opportunity match binding", missingMatchBinding, 400, "match-assessment-id-required"],
      ["brief match binding", missingBriefMatchBinding, 400, "match-assessment-id-required"],
      ["partial match", partialOpportunity, 409, "match-not-ready"],
      ["blocked match", blockedOpportunity, 409, "match-not-ready"],
      ["capture gap", gapConfirmation, 409, "revision-proposal-has-unresolved-gaps"],
    ] as const) {
      expect(response.status, label).toBe(status);
      expect(response.body, label).toEqual({ error });
    }
  }, 120_000);

  it("hides demand, match, feedback, and proposal IDs from a second customer", async () => {
    const api = await createApi();
    const sessionA = await openSession(api, "customer-a");
    const readyA = await createDemandAndMatch(api, sessionA.token, "customer-a");
    const v1A = await createBriefAndDrafts(api, sessionA.token, readyA.demand, readyA.assessment, "customer-a");
    const feedbackA = await studioPost(api, "/api/studio/customer-feedback", {
      draftId: v1A.drafts[0].id,
      satisfaction: "partially-satisfied",
      issueCategories: ["style"],
      comment: "请让成品结尾更突出",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-customer-a",
    }, sessionA.token);
    const proposalA = await studioPost(api, "/api/studio/revision-proposals", {
      feedbackId: feedbackA.body.feedback.id,
      operationKey: "proposal-customer-a",
    }, sessionA.token);

    const sessionB = await openSession(api, "customer-b");
    const readyB = await createDemandAndMatch(api, sessionB.token, "customer-b");
    const probes = [
      ["demand", "/api/studio/matches", {
        demandId: readyA.demand.id,
        operationKey: "cross-customer-demand",
      }, "demand-not-found"],
      ["match", "/api/studio/opportunities", {
        demandId: readyB.demand.id,
        matchAssessmentId: readyA.assessment.id,
      }, "match-assessment-not-found"],
      ["feedback", "/api/studio/revision-proposals", {
        feedbackId: feedbackA.body.feedback.id,
        operationKey: "cross-customer-feedback",
      }, "customer-feedback-not-found"],
      ["proposal", "/api/studio/revision-proposals/confirm", {
        proposalId: proposalA.body.proposal.id,
        operationKey: "cross-customer-proposal",
      }, "revision-proposal-not-found"],
    ] as const;

    for (const [label, path, body, error] of probes) {
      const response = await studioPost(api, path, body, sessionB.token);
      expect(response.status, label).toBe(404);
      expect(response.body, label).toEqual({ error });
    }
  }, 120_000);

  it("binds operation keys to normalized request bodies", async () => {
    const api = await createApi();
    const session = await openSession(api, "customer-a");
    const first = await studioPost(
      api,
      "/api/studio/demands",
      demandBody("operation-conflict"),
      session.token,
    );
    expect(first.status).toBe(201);

    const changedBody = await studioPost(api, "/api/studio/demands", {
      ...demandBody("operation-conflict"),
      desiredEffect: "改成另一种效果",
    }, session.token);

    expect(changedBody.status).toBe(409);
    expect(changedBody.body).toEqual({ error: "demand-operation-key-conflict" });
  }, 120_000);

  it("rejects oversized and invalid JSON bodies without leaking server paths", async () => {
    const api = await createApi();
    const oversized = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("content-type", "application/json")
      .send(JSON.stringify({ customerId: "x".repeat(1024 * 1024) }));
    const invalid = await api
      .post("/api/studio/session")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("content-type", "application/json")
      .send('{"customerId":');

    expect(oversized.status).toBe(400);
    expect(oversized.body).toEqual({ error: "studio-request-body-too-large" });
    expect(invalid.status).toBe(400);
    expect(invalid.body).toEqual({ error: "studio-invalid-json-body" });
    expect(JSON.stringify([oversized.body, invalid.body])).not.toContain(roots.at(-1)!);
  });

  it("serves only the bound customer's reviewable draft and exposes no publish route", async () => {
    const api = await createApi();
    const result = await runStudioApiJourney(api);
    const otherSession = await openSession(api, "customer-b");
    const media = await api
      .get(result.drafts[0].mediaUrl)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${result.token}`);
    const crossCustomerMedia = await api
      .get(result.drafts[0].mediaUrl)
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("authorization", `Bearer ${otherSession.token}`);
    const publish = await studioPost(
      api,
      "/api/studio/publish",
      { draftId: result.drafts[0].id },
      result.token,
    );

    expect(media.status).toBe(200);
    expect(media.headers["content-type"]).toBe("video/mp4");
    expect(Number(media.headers["content-length"])).toBeGreaterThan(0);
    expect(crossCustomerMedia.status).toBe(404);
    expect(crossCustomerMedia.body).toEqual({ error: "draft-not-found" });
    expect(publish.status).toBe(404);
    expect(publish.body).toEqual({ error: "studio-route-not-found" });
  }, 120_000);

  it("refuses media bytes after a three-byte overwrite or missing-file replacement", async () => {
    const api = await createApi();
    const root = roots.at(-1)!;
    const result = await runStudioApiJourney(api);
    const overwrittenPath = await findFileByDigest(root, result.drafts[0].outputDigest);
    const missingPath = await findFileByDigest(root, result.drafts[1].outputDigest);

    await writeFile(overwrittenPath, Buffer.from([0x00, 0x01, 0x02]));
    await unlink(missingPath);

    const overwrittenMedia = await authenticatedMedia(api, result.drafts[0].mediaUrl, result.token);
    const missingMedia = await authenticatedMedia(api, result.drafts[1].mediaUrl, result.token);

    expect(overwrittenMedia.status).not.toBe(200);
    expect(overwrittenMedia.body).toEqual({ error: "studio-render-output-digest-mismatch" });
    expect(missingMedia.status).not.toBe(200);
    expect(missingMedia.body).toEqual({ error: "studio-render-output-unavailable" });
  }, 120_000);
});

async function createApi(options: {
  findJianyingApp?: () => Promise<{ appVersion: string; appPath: string; draftRoot: string } | null>;
  openExternalEditTarget?: (input: { kind: "jianying" | "folder"; target: string }) => Promise<void>;
  hardwareBaseUrl?: string;
  hardwarePairingEnabled?: boolean;
  editorial?: EditorialDecisionAdapter;
  modelWorker?: StudioModelWorker;
} = {}) {
  const root = await mkdtemp(join(tmpdir(), "studio-api-"));
  roots.push(root);
  const server = createStudioServer({
    rootDir: root,
    customers: [
      { id: "customer-a", label: "合成客户 A" },
      { id: "customer-b", label: "合成客户 B" },
    ],
    now: () => "2026-09-10T08:00:00.000Z",
    openExternalEditTarget: options.openExternalEditTarget ?? (async () => undefined),
    findJianyingApp: options.findJianyingApp ?? (async () => null),
    hardwareBaseUrl: options.hardwareBaseUrl,
    hardwarePairingEnabled: options.hardwarePairingEnabled,
    editorial: options.editorial,
    // This API suite verifies studio behavior, not installed model binaries.
    // Keeping the worker explicit makes the suite deterministic even when a
    // formal local runtime happens to be installed on the developer machine.
    modelWorker: options.modelWorker ?? isolatedApiTestModelWorker(),
  });
  servers.add(server);
  return request(server);
}

function isolatedApiTestModelWorker(): StudioModelWorker {
  return Object.freeze({
    async health() {
      return {
        mode: "offline-fallback" as const,
        installed: false as const,
        modelName: null,
        checkpointDigest: null,
      };
    },
    async run(job: ModelAnalysisJobV1): Promise<ModelAnalysisReceiptV1> {
      const { requestedAt: _requestedAt, ...binding } = job;
      return {
        ...binding,
        adapterName: "studio-api-test-isolated-worker",
        modelName: job.layer === "embedding" ? "openclip-test-fixture" : `${job.layer}-test-fixture`,
        modelVersion: "fixture-v1",
        checkpointDigest: null,
        inputDigest: job.sourceDigest,
        promptVersion: "studio-api-test-isolated-v1",
        observations: [],
        suggestedTags: [],
        qualitySignals: {},
        confidence: 0,
        warnings: ["test-worker-isolated-from-installed-runtime"],
        startedAt: "2026-09-10T08:00:00.000Z",
        completedAt: "2026-09-10T08:00:00.000Z",
        runtimeMs: 0,
        status: "failed-manual-review",
        failure: { code: "studio-api-test-worker-disabled" },
        synthetic: true,
        providerMode: "offline-fallback",
      };
    },
  });
}

async function openSession(
  api: ReturnType<typeof request>,
  customerId: string,
  expectedCapability: Readonly<{ mode: string; label: string }> = {
    mode: "offline-fallback",
    label: "离线/模拟分析",
  },
) {
  const response = await api
    .post("/api/studio/session")
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .send({ customerId })
    .expect(201);
  expect(response.body.session.customerId).toBe(customerId);
  expect(response.body.capability).toMatchObject(expectedCapability);
  expect(response.body.release).toEqual({
    version: "1.0.0",
    label: "本地合成候选",
    boundary: "local-synthetic-candidate",
    production: false,
    deployed: false,
  });
  return response.body.session as {
    token: string;
    actorId: string;
    customerId: string;
    sessionId: string;
  };
}

async function runStudioApiJourney(
  api: ReturnType<typeof request>,
  expectedCapability?: Readonly<{ mode: string; label: string }>,
) {
  const session = await openSession(api, "customer-a", expectedCapability);
  const inventoryResponse = await studioPost(
    api,
    "/api/studio/inventory",
    {},
    session.token,
  );
  expect(inventoryResponse.status).toBe(200);
  const { inventory, materials } = inventoryResponse.body;

  const { demand, assessment } = await createDemandAndMatch(api, session.token, "legacy-journey");
  const created = await createBriefAndDrafts(
    api,
    session.token,
    demand,
    assessment,
    "legacy-journey",
    inventory.items.map((item: any) => item.sourceAssetId),
  );

  const feedbackResponse = await studioPost(api, "/api/studio/feedback", {
    draftId: created.drafts[0].id,
    action: "request-changes",
    reason: "请让设计师观点更突出",
    operationKey: "feedback-op-api-1",
  }, session.token);
  expect(feedbackResponse.status).toBe(201);

  return {
    token: session.token,
    inventory,
    materials,
    demand,
    assessment,
    opportunities: created.opportunities,
    brief: created.brief,
    drafts: created.drafts,
    feedback: feedbackResponse.body.feedback,
  };
}

function demandBody(operationKey: string) {
  return {
    contentType: "single-product",
    goal: "用旧素材讲清楚纯色合成商品夹具",
    targetAudience: "珠宝顾客",
    desiredEffect: "清楚可信",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "customer-self-declared",
    operationKey: `demand-${operationKey}`,
  };
}

async function createDemandAndMatch(
  api: ReturnType<typeof request>,
  token: string,
  key: string,
  overrides: Record<string, unknown> = {},
) {
  const demandResponse = await studioPost(api, "/api/studio/demands", {
    ...demandBody(key),
    ...overrides,
  }, token);
  expect(demandResponse.status).toBe(201);
  const demand = demandResponse.body.demand;
  const matchResponse = await studioPost(api, "/api/studio/matches", {
    demandId: demand.id,
    operationKey: `match-${key}`,
  }, token);
  expect(matchResponse.status).toBe(201);
  return { demand, assessment: matchResponse.body.assessment };
}

async function createBriefAndDrafts(
  api: ReturnType<typeof request>,
  token: string,
  demand: any,
  assessment: any,
  key: string,
  selectedAssetIds?: string[],
) {
  const opportunityResponse = await studioPost(api, "/api/studio/opportunities", {
    demandId: demand.id,
    matchAssessmentId: assessment.id,
  }, token);
  expect(opportunityResponse.status).toBe(200);
  const opportunities = opportunityResponse.body.opportunities;
  const opportunity = opportunities[0];
  const inventoryResponse = selectedAssetIds === undefined
    ? await studioPost(api, "/api/studio/inventory", {}, token)
    : null;
  const assetIds = selectedAssetIds ?? inventoryResponse!.body.inventory.items
    .map((item: any) => item.sourceAssetId);
  const briefResponse = await studioPost(api, "/api/studio/briefs", {
    opportunityId: opportunity.id,
    matchAssessmentId: assessment.id,
    selectedAssetIds: assetIds,
    confirmedFactIds: ["example-attribute: 示例属性字段待人工确认"],
    reviewRequirements: ["采用前人工复核"],
    operationKey: `brief-${key}`,
  }, token);
  expect(briefResponse.status).toBe(201);
  const draftResponse = await studioPost(api, "/api/studio/drafts", {
    briefId: briefResponse.body.brief.id,
    operationKey: `draft-${key}`,
  }, token);
  expect(draftResponse.status).toBe(201);
  return {
    opportunities,
    brief: briefResponse.body.brief,
    drafts: draftResponse.body.drafts,
  };
}

function studioPost(
  api: ReturnType<typeof request>,
  path: string,
  body: Record<string, unknown>,
  token: string,
) {
  return api
    .post(path)
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("authorization", `Bearer ${token}`)
    .send(body);
}

function authenticatedMedia(
  api: ReturnType<typeof request>,
  path: string,
  token: string,
) {
  return api
    .get(path)
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("authorization", `Bearer ${token}`);
}

async function findFileByDigest(root: string, expectedDigest: string): Promise<string> {
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) {
      const match = await findFileByDigest(path, expectedDigest).catch(() => null);
      if (match) return match;
    } else if (entry.isFile() && createHash("sha256").update(await readFile(path)).digest("hex") === expectedDigest) {
      return path;
    }
  }
  throw new Error(`test-draft-output-not-found:${expectedDigest}`);
}

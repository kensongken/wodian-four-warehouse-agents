import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { request as httpRequest } from "node:http";
import { chmod, mkdtemp, readFile, readdir, rm, stat, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

import ffmpegPath from "ffmpeg-static";
import request from "supertest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReferenceMobileReviewV1Schema } from "@wearable/contracts";
import {
  JsonReferenceSampleStore,
  ReferenceSampleService,
  referenceEditorOperationMetadata,
  referenceWorkflowDigest,
  type ReferenceEditorOperation,
  type StudioModelWorker,
} from "@wearable/mobile-agent";

import {
  createStudioServer,
  type StudioReferenceRuntimeFactory,
} from "../src/studio-server";
import type { StudioReferenceRuntime } from "../src/studio-reference-api";

const ORIGIN = "http://127.0.0.1";
const execFileAsync = promisify(execFile);
const roots: string[] = [];
const servers = new Set<ReturnType<typeof createStudioServer>>();

describe("studio reference API", () => {
  afterEach(async () => {
    for (const server of servers) {
      if (server.listening) {
        await new Promise<void>((resolve, reject) =>
          server.close((error) => error ? reject(error) : resolve()));
      }
    }
    servers.clear();
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("streams one decoded local video and derives the import scope from the HttpOnly session", async () => {
    const fixture = await makeFixture();
    const imports: Array<Record<string, unknown>> = [];
    const { api } = await createApi(runtimeFactory({ imports }));
    const session = await openSession(api);

    const response = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_1")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mov", contentType: "video/quicktime" });

    expect(response.status).toBe(201);
    expect(response.body).toEqual({
      sample: { id: "reference-sample-imported", sourceKind: "external-style-only" },
    });
    expect(imports).toHaveLength(1);
    expect(imports[0]).toMatchObject({
      scope: {
        actorId: session.actorId,
        customerId: session.customerId,
        sessionId: session.sessionId,
      },
      requestId: "reference_import_1",
      fileName: "sample.mov",
      sourceKind: "external-style-only",
      operationKey: "reference_import_1",
    });
    expect((imports[0]?.bytes as Uint8Array).byteLength).toBeGreaterThan(0);
  }, 120_000);

  it("never accepts a caller target path and removes server-owned staging bytes", async () => {
    const fixture = await makeFixture();
    const imports: Array<Record<string, unknown>> = [];
    const { api, root } = await createApi(runtimeFactory({ imports }));
    const session = await openSession(api);

    const response = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_target_path")
      .field("sourceKind", "external-style-only")
      .field("targetPath", "/tmp/attacker-selected")
      .attach("file", fixture, { filename: "sample.mov", contentType: "video/quicktime" });

    expect(response.status).toBe(400);
    expect(response.body).toEqual({ error: "reference-import-field-not-allowed" });
    expect(imports).toEqual([]);
    expect(await listReferenceStagingFiles(root)).toEqual([]);
  }, 120_000);

  it("rejects an oversized upload and removes staging bytes", async () => {
    const fixture = await makeFixture();
    const imports: Array<Record<string, unknown>> = [];
    const { api, root } = await createApi(runtimeFactory({ imports }), { maxUploadBytes: 1_024 });
    const session = await openSession(api);

    const response = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_oversized")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });

    expect(response.status).toBe(413);
    expect(response.body).toEqual({ error: "reference-import-too-large" });
    expect(imports).toEqual([]);
    expect(await listReferenceStagingFiles(root)).toEqual([]);
    expect(await readImportAudits(root)).toEqual([
      expect.objectContaining({
        operationKey: "reference_import_oversized",
        errorCode: "reference-import-too-large",
      }),
    ]);
  }, 120_000);

  it("accepts exactly the byte ceiling and rejects only max plus one", async () => {
    const fixture = await makeFixture();
    const fixtureBytes = await readFile(fixture);
    const over = join(await makeRoot("studio-reference-over-limit-"), "over.mp4");
    await writeFile(over, Buffer.concat([fixtureBytes, Buffer.from([0])]));
    const imports: Array<Record<string, unknown>> = [];
    const { api } = await createApi(runtimeFactory({ imports }), {
      maxUploadBytes: fixtureBytes.byteLength,
    });
    const session = await openSession(api);

    const exact = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_exact_limit")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "exact.mp4", contentType: "video/mp4" });
    const tooLarge = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_limit_plus_one")
      .field("sourceKind", "external-style-only")
      .attach("file", over, { filename: "over.mp4", contentType: "video/mp4" });

    expect(exact.status).toBe(201);
    expect(tooLarge.status).toBe(413);
    expect(tooLarge.body).toEqual({ error: "reference-import-too-large" });
    expect(imports).toHaveLength(1);
  }, 120_000);

  it("uses Busboy's hard multipart header ceiling and records a content-free failure audit", async () => {
    const { api, root } = await createApi(runtimeFactory());
    const session = await openSession(api);
    const boundary = "reference-hard-header-boundary";
    const hugeHeaderName = `x-${"a".repeat(17 * 1024)}`;
    const body = [
      `--${boundary}\r\n`,
      `Content-Disposition: form-data; name="operationKey"\r\n\r\n`,
      `header_limit_operation\r\n`,
      `--${boundary}\r\n`,
      `${hugeHeaderName}: value\r\n`,
      `Content-Disposition: form-data; name="sourceKind"\r\n\r\n`,
      `external-style-only\r\n`,
      `--${boundary}--\r\n`,
    ].join("");

    const response = await api.post("/api/studio/reference-samples/import")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", session.cookie)
      .set("content-type", `multipart/form-data; boundary=${boundary}`)
      .send(body);

    expect(response.status).toBe(400);
    expect(response.body).toEqual({ error: "reference-import-multipart-invalid" });
    const audits = await readImportAudits(root);
    expect(audits).toHaveLength(1);
    expect(audits[0]).toMatchObject({
      customerId: session.customerId,
      sessionId: session.sessionId,
      actorId: session.actorId,
      operationKey: "header_limit_operation",
      errorCode: "reference-import-multipart-invalid",
    });
    expect(audits[0]).toMatchObject({ id: expect.stringMatching(/^reference_import_failure_/), createdAt: expect.any(String) });
    expect(JSON.stringify(audits[0])).not.toMatch(/bytes|path|stderr|x-a{20}/i);
  }, 120_000);

  it("settles and cleans repeated client-aborted uploads without retained staging bytes or Mac file handles", async () => {
    const { api, root, server } = await createApi(runtimeFactory());
    const session = await openSession(api);
    if (!server.listening) {
      await new Promise<void>((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
      });
    }
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("test-server-address-unavailable");

    for (let index = 0; index < 5; index += 1) {
      await abortMultipartRequest(address.port, session.cookie, index);
    }

    await expect.poll(() => listReferenceStagingFiles(root), { timeout: 10_000 }).toEqual([]);
    const audits = await readImportAudits(root);
    expect(audits.filter((row) => row.errorCode === "reference-import-request-aborted")).toHaveLength(5);
    if (process.platform === "darwin") {
      const lsof = await execFileAsync("lsof", ["+D", root], { timeout: 10_000 }).catch((error: any) => ({
        stdout: error?.stdout ?? "",
      }));
      expect(String(lsof.stdout)).not.toContain("reference-upload.part");
    }
  }, 120_000);

  it("observes an abort that lands while the staging directory is being prepared", async () => {
    const setupEntered = deferred<void>();
    const releaseSetup = deferred<void>();
    const { api, root, server } = await createApi(
      runtimeFactory(), undefined, undefined, undefined,
      { afterDirectoryCreate: async () => { setupEntered.resolve(); await releaseSetup.promise; } },
    );
    const session = await openSession(api);
    if (!server.listening) {
      await new Promise<void>((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
      });
    }
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("test-server-address-unavailable");
    const partial = openAbortableMultipartRequest(address.port, session.cookie, 99);
    await setupEntered.promise;
    partial.abort();
    releaseSetup.resolve();
    await partial.closed;

    await expect.poll(() => readImportAudits(root), { timeout: 10_000 }).toEqual([
      expect.objectContaining({ errorCode: "reference-import-request-aborted" }),
    ]);
    expect(await listReferenceStagingFiles(root)).toEqual([]);
  }, 120_000);

  it("cleans staging before persistence and audits a cleanup failure without claiming success", async () => {
    const fixture = await makeFixture();
    const imports: Array<Record<string, unknown>> = [];
    const { api, root } = await createApi(
      runtimeFactory({ imports }), undefined, undefined, undefined,
      { removeDirectory: async () => { throw new Error("private-cleanup-path"); } },
    );
    const session = await openSession(api);

    const response = await multipart(api, session.cookie)
      .field("operationKey", "reference_cleanup_failure")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });

    expect(response.status).toBe(500);
    expect(response.body).toEqual({ error: "reference-import-staging-cleanup-failed" });
    expect(imports).toEqual([]);
    expect((await listReferenceStagingFiles(root)).length).toBeGreaterThan(0);
    expect(await readImportAudits(root)).toEqual([
      expect.objectContaining({
        operationKey: "reference_cleanup_failure",
        errorCode: "reference-import-staging-cleanup-failed",
      }),
    ]);
    expect(JSON.stringify(await readImportAudits(root))).not.toContain("private-cleanup-path");
  }, 120_000);

  it("rejects media longer than the ffprobe duration limit and removes staging bytes", async () => {
    const fixture = await makeFixture();
    const imports: Array<Record<string, unknown>> = [];
    const { api, root } = await createApi(runtimeFactory({ imports }), { maxDurationMs: 100 });
    const session = await openSession(api);

    const response = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_too_long")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });

    expect(response.status).toBe(422);
    expect(response.body).toEqual({ error: "reference-import-duration-too-long" });
    expect(imports).toEqual([]);
    expect(await listReferenceStagingFiles(root)).toEqual([]);
  }, 120_000);

  it("rejects a second file and a corrupt MOV without importing either", async () => {
    const fixture = await makeFixture();
    const corrupt = join(await makeRoot("studio-reference-corrupt-"), "corrupt.mov");
    await writeFile(corrupt, Buffer.from("not a movie"));
    const imports: Array<Record<string, unknown>> = [];
    const { api, root } = await createApi(runtimeFactory({ imports }));
    const session = await openSession(api);

    const twoFiles = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_two_files")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "one.mp4", contentType: "video/mp4" })
      .attach("file", fixture, { filename: "two.mp4", contentType: "video/mp4" });
    const invalid = await multipart(api, session.cookie)
      .field("operationKey", "reference_import_corrupt")
      .field("sourceKind", "external-style-only")
      .attach("file", corrupt, { filename: "corrupt.mov", contentType: "video/quicktime" });

    expect(twoFiles.status).toBe(400);
    expect(twoFiles.body).toEqual({ error: "reference-import-file-count-invalid" });
    expect(invalid.status).toBe(422);
    expect(invalid.body).toEqual({ error: "reference-import-media-invalid" });
    expect(imports).toEqual([]);
    expect(await listReferenceStagingFiles(root)).toEqual([]);
    expect(await readImportAudits(root)).toEqual(expect.arrayContaining([
      expect.objectContaining({
        operationKey: "reference_import_corrupt",
        errorCode: "reference-import-media-invalid",
      }),
    ]));
  }, 120_000);

  it("keeps multipart behind loopback, same-origin, and authenticated-session gates", async () => {
    const { api } = await createApi(runtimeFactory());
    const session = await openSession(api);
    const emptyMultipart = "--reference-boundary--\r\n";

    const wrongOrigin = await api.post("/api/studio/reference-samples/import")
      .set("host", "127.0.0.1")
      .set("origin", "https://outside.example")
      .set("cookie", session.cookie)
      .set("content-type", "multipart/form-data; boundary=reference-boundary")
      .send(emptyMultipart);
    const noSession = await api.post("/api/studio/reference-samples/import")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("content-type", "multipart/form-data; boundary=reference-boundary")
      .send(emptyMultipart);

    expect(wrongOrigin.status).toBe(403);
    expect(wrongOrigin.body).toEqual({ error: "studio-same-origin-required" });
    expect(noSession.status).toBe(401);
    expect(noSession.body).toEqual({ error: "studio-session-token-required" });
  }, 120_000);

  it("exposes the exact Mac JSON workflow while refusing client scope and extra fields", async () => {
    const calls: Array<{ method: string; values: unknown[] }> = [];
    const { api } = await createApi(runtimeFactory({
      calls,
      snapshot: (scope) => mobileWorkflowSnapshot(scope),
    }));
    const session = await openSession(api);

    const requests = [
      ["/api/studio/reference-samples/from-history", { assetId: "asset-1", sourceKind: "self-owned", operationKey: "history_1" }, "sample"],
      ["/api/studio/reference-rights", { referenceSampleId: "sample-1", sourceKind: "self-owned", reusableElements: ["video-clips"], operationKey: "rights_1" }, "rights"],
      ["/api/studio/reference-analyses", { referenceSampleId: "sample-1", operationKey: "analysis_1" }, "analysis"],
      ["/api/studio/reference-style-selections", { analysisId: "analysis-1", selectedRuleIds: ["rule-1"], operationKey: "selection_1" }, "selection"],
      ["/api/studio/reference-mapping-plans", { referenceSampleId: "sample-1", operationKey: "mapping_1" }, "mappingPlan"],
      ["/api/studio/reference-mapping-plans/confirm", { mappingPlanId: "mapping-1", operationKey: "mapping_confirm_1" }, "mappingPlan"],
      ["/api/studio/reference-drafts", { mappingPlanId: "mapping-1", operationKey: "render_1" }, "report"],
    ] as const;
    for (const [path, body, responseKey] of requests) {
      const response = await jsonPost(api, path, body, session.cookie);
      expect(response.status, path).toBe(201);
      expect(response.body).toHaveProperty(responseKey);
      expect(JSON.stringify(response.body)).not.toMatch(/sourcePath|outputPath|rawModelOutput/);
    }

    const samples = await jsonGet(api, "/api/studio/reference-samples", session.cookie);
    const report = await jsonGet(api, "/api/studio/reference-execution-reports/report-mobile", session.cookie);
    expect(samples.status).toBe(200);
    expect(samples.body).toEqual({ samples: [{ id: "sample-listed" }] });
    expect(report.status).toBe(200);
    expect(report.body).toMatchObject({ report: { id: "report-mobile", status: "reviewable" } });
    expect(calls.every((call) => {
      const scope = call.values[0] as Record<string, unknown>;
      return scope.actorId === session.actorId
        && scope.customerId === session.customerId
        && scope.sessionId === session.sessionId;
    })).toBe(true);

    for (const forbidden of [
      { customerId: session.customerId },
      { customerId: "customer-b" },
      { sessionId: session.sessionId },
      { actorId: session.actorId },
      { targetPath: "/tmp/forged" },
      { extra: true },
    ]) {
      const response = await jsonPost(api, "/api/studio/reference-analyses", {
        referenceSampleId: "sample-1",
        operationKey: `forged_${Object.keys(forbidden)[0]}`,
        ...forbidden,
      }, session.cookie);
      expect(response.status, JSON.stringify(forbidden)).toBe(403);
      expect(response.body).toEqual({ error: "studio-customer-scope-forbidden" });
    }
  }, 120_000);

  it("binds prompt revision to the draft path and replays only the exact operation intent", async () => {
    const calls: Array<{ method: string; values: unknown[] }> = [];
    const actionLedger = new Map<string, { intent: string; result: unknown }>();
    const { api } = await createApi(runtimeFactory({ calls, actionLedger }));
    const session = await openSession(api);
    const body = {
      reportId: "report-mobile",
      reportVersion: 9,
      draftVersion: 8,
      draftDigest: createHash("sha256").update("output-1").digest("hex"),
      prompt: "开头缩短 1 秒",
      operationKey: "reference_revise_xhs_exact",
    };

    const first = await jsonPost(
      api, "/api/studio/reference-drafts/draft-xiaohongshu/revise", body, session.cookie,
    );
    const replay = await jsonPost(
      api, "/api/studio/reference-drafts/draft-xiaohongshu/revise", body, session.cookie,
    );
    const conflict = await jsonPost(
      api, "/api/studio/reference-drafts/draft-xiaohongshu/revise",
      { ...body, prompt: "改成更慢的开头" }, session.cookie,
    );
    const pathOverride = await jsonPost(
      api, "/api/studio/reference-drafts/draft-xiaohongshu/revise",
      { ...body, draftId: "draft-attacker-selected", operationKey: "reference_revise_path_override" },
      session.cookie,
    );

    expect(first.status).toBe(201);
    expect(first.body).toEqual({ report: { id: "report-revised", version: 10, status: "reviewable" } });
    expect(replay.status).toBe(201);
    expect(replay.body).toEqual(first.body);
    expect(conflict.status).toBe(409);
    expect(conflict.body).toEqual({ error: "reference-operation-key-conflict" });
    expect(pathOverride.status).toBe(403);
    expect(pathOverride.body).toEqual({ error: "studio-customer-scope-forbidden" });
    expect(calls.filter((call) => call.method === "reviseWithPrompt")[0]?.values).toEqual([
      expect.objectContaining({
        actorId: session.actorId,
        customerId: session.customerId,
        sessionId: session.sessionId,
      }),
      {
        draftId: "draft-xiaohongshu",
        reportId: body.reportId,
        reportVersion: body.reportVersion,
        draftVersion: body.draftVersion,
        draftDigest: body.draftDigest,
        prompt: body.prompt,
      },
      body.operationKey,
    ]);
  }, 120_000);

  it("approves the path-bound exact draft version and rejects prompt or same-key intent drift", async () => {
    const calls: Array<{ method: string; values: unknown[] }> = [];
    const actionLedger = new Map<string, { intent: string; result: unknown }>();
    const { api } = await createApi(runtimeFactory({ calls, actionLedger }));
    const session = await openSession(api);
    const body = {
      reportId: "report-mobile",
      reportVersion: 9,
      draftVersion: 8,
      draftDigest: createHash("sha256").update("output-0").digest("hex"),
      operationKey: "reference_approve_master_exact",
    };

    const first = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/approve", body, session.cookie,
    );
    const replay = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/approve", body, session.cookie,
    );
    const conflict = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/approve",
      { ...body, draftVersion: 9 }, session.cookie,
    );
    const unexpectedPrompt = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/approve",
      { ...body, operationKey: "reference_approve_prompt_extra", prompt: "调整一下" }, session.cookie,
    );

    expect(first.status).toBe(201);
    expect(first.body).toMatchObject({
      approval: { id: "approval-reference", status: "active", publishClaimed: false },
      review: { publishClaimed: false },
    });
    expect(replay.status).toBe(201);
    expect(replay.body).toEqual(first.body);
    expect(conflict.status).toBe(409);
    expect(conflict.body).toEqual({ error: "reference-operation-key-conflict" });
    expect(unexpectedPrompt.status).toBe(403);
    expect(unexpectedPrompt.body).toEqual({ error: "studio-customer-scope-forbidden" });
    expect(calls.filter((call) => call.method === "approveDraft")[0]?.values).toEqual([
      expect.objectContaining({
        actorId: session.actorId,
        customerId: session.customerId,
        sessionId: session.sessionId,
      }),
      {
        draftId: "draft-editorial-master",
        reportId: body.reportId,
        reportVersion: body.reportVersion,
        draftVersion: body.draftVersion,
        draftDigest: body.draftDigest,
      },
      body.operationKey,
    ]);
  }, 120_000);

  it("withdraws a path-bound asset with server scope and replays only the exact operation", async () => {
    const calls: Array<{ method: string; values: unknown[] }> = [];
    let firstAssetId = "";
    let secondAssetId = "";
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      for (const [index, bytes] of ["withdraw-one", "withdraw-two"].entries()) {
        const asset = await library.importOriginal(scope.actorId, scope.customerId, {
          requestId: `withdraw-api-${scope.sessionId}-${index}`,
          bytes: Buffer.from(bytes),
          mediaKind: "video",
          fileName: `withdraw-${index}.mp4`,
          capturedAt: null,
          sessionId: scope.sessionId,
          viewPermission: "allowed",
          usePermission: "allowed",
        });
        if (index === 0) firstAssetId = asset.id;
        else secondAssetId = asset.id;
      }
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot, {
          now: () => "2026-09-19T08:00:00.000Z",
        }),
        store,
        editor: { ...stubEditor(), ...actionEditor({ calls }) },
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);

    const first = await jsonPost(
      api, `/api/studio/reference-assets/${firstAssetId}/withdraw`,
      { operationKey: "reference_withdraw_exact" }, session.cookie,
    );
    const replay = await jsonPost(
      api, `/api/studio/reference-assets/${firstAssetId}/withdraw`,
      { operationKey: "reference_withdraw_exact" }, session.cookie,
    );
    const conflict = await jsonPost(
      api, `/api/studio/reference-assets/${secondAssetId}/withdraw`,
      { operationKey: "reference_withdraw_exact" }, session.cookie,
    );
    const forgedScope = await jsonPost(
      api, `/api/studio/reference-assets/${firstAssetId}/withdraw`,
      { operationKey: "reference_withdraw_forged", actorId: "attacker" }, session.cookie,
    );

    expect(first.status).toBe(201);
    expect(first.body.withdrawal).toMatchObject({
      customerId: session.customerId,
      assetId: firstAssetId,
      operationKey: "reference_withdraw_exact",
      actorId: session.actorId,
      action: "withdrawn",
    });
    expect(replay.status).toBe(201);
    expect(replay.body).toEqual(first.body);
    expect(conflict.status).toBe(409);
    expect(conflict.body).toEqual({ error: "withdrawal-request-conflict" });
    expect(forgedScope.status).toBe(403);
    expect(forgedScope.body).toEqual({ error: "studio-customer-scope-forbidden" });
    expect(calls.filter((call) => call.method === "invalidateForAsset").map((call) => call.values))
      .toEqual([
        [expect.objectContaining({
          actorId: session.actorId,
          customerId: session.customerId,
          sessionId: session.sessionId,
        }), { assetId: firstAssetId, reason: "asset-permission-withdrawn" }, "reference_withdraw_exact"],
        [expect.objectContaining({
          actorId: session.actorId,
          customerId: session.customerId,
          sessionId: session.sessionId,
        }), { assetId: firstAssetId, reason: "asset-permission-withdrawn" }, "reference_withdraw_exact"],
      ]);
  }, 120_000);

  it("fails revision and approval closed after the bound asset is withdrawn", async () => {
    const calls: Array<{ method: string; values: unknown[] }> = [];
    const withdrawn = { value: false };
    let assetId = "";
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      const asset = await library.importOriginal(scope.actorId, scope.customerId, {
        requestId: `withdraw-before-actions-${scope.sessionId}`,
        bytes: Buffer.from("withdraw-before-reference-actions"),
        mediaKind: "video",
        fileName: "withdraw-before-actions.mp4",
        capturedAt: null,
        sessionId: scope.sessionId,
        viewPermission: "allowed",
        usePermission: "allowed",
      });
      assetId = asset.id;
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot, {
          now: () => "2026-09-19T08:00:00.000Z",
        }),
        store,
        editor: { ...stubEditor(), ...actionEditor({ calls, withdrawn }) },
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);
    const withdrawnResponse = await jsonPost(
      api, `/api/studio/reference-assets/${assetId}/withdraw`,
      { operationKey: "reference_withdraw_before_actions" }, session.cookie,
    );
    expect(withdrawnResponse.status).toBe(201);

    const binding = {
      reportId: "report-mobile",
      reportVersion: 9,
      draftVersion: 8,
      draftDigest: createHash("sha256").update("output-0").digest("hex"),
    };
    const revise = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/revise",
      { ...binding, prompt: "开头再快一点", operationKey: "reference_revise_after_withdraw" },
      session.cookie,
    );
    const approve = await jsonPost(
      api, "/api/studio/reference-drafts/draft-editorial-master/approve",
      { ...binding, operationKey: "reference_approve_after_withdraw" }, session.cookie,
    );

    expect(revise.status).toBe(409);
    expect(revise.body).toEqual({ error: "reference-workflow-asset-withdrawn" });
    expect(approve.status).toBe(409);
    expect(approve.body).toEqual({ error: "reference-workflow-asset-withdrawn" });
  }, 120_000);

  it("fails mobile review and draft media closed while withdrawal is durable but workflow invalidation lags", async () => {
    let assetId = "";
    let masterDraftId = "";
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const asset = await library.importOriginal(scope.actorId, scope.customerId, {
        requestId: `reference-auth-race-${scope.sessionId}`,
        bytes: Buffer.from("reference"),
        mediaKind: "video",
        fileName: "reference-auth-race.mp4",
        capturedAt: null,
        sessionId: scope.sessionId,
        viewPermission: "allowed",
        usePermission: "allowed",
      });
      assetId = asset.id;
      const rebound = JSON.parse(
        JSON.stringify(mobileWorkflowSnapshot(scope)).replaceAll("asset-reference", asset.id),
      );
      masterDraftId = rebound.executionReports[0].artifacts[0].draftId;
      const renderRoot = join(sessionRoot, "reference-renders");
      const masterPath = join(renderRoot, "authorized-master.mp4");
      const { mkdir } = await import("node:fs/promises");
      await mkdir(renderRoot, { recursive: true });
      await writeFile(masterPath, Buffer.from("output-0"));
      rebound.drafts[0].outputPath = masterPath;
      const store: StudioReferenceRuntime["store"] = {
        snapshot: async () => rebound,
        withCurrentSnapshot: async (work) => work(rebound),
      };
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot),
        editor: stubEditor(),
        store,
        enforceMaterialAuthorization: true,
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);

    expect((await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie)).status).toBe(200);
    expect((await jsonGet(
      api, `/api/studio/reference-drafts/${masterDraftId}/media`, session.cookie,
    )).status).toBe(200);
    expect((await jsonPost(
      api,
      `/api/studio/reference-assets/${assetId}/withdraw`,
      { operationKey: "reference_auth_race_withdraw" },
      session.cookie,
    )).status).toBe(201);

    const mobile = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);
    const media = await jsonGet(
      api, `/api/studio/reference-drafts/${masterDraftId}/media`, session.cookie,
    );
    const actionBinding = {
      reportId: "report-mobile",
      reportVersion: 9,
      draftVersion: 8,
      draftDigest: createHash("sha256").update("output-0").digest("hex"),
    };
    const revise = await jsonPost(
      api, `/api/studio/reference-drafts/${masterDraftId}/revise`, {
        ...actionBinding,
        prompt: "开头缩短 1 秒",
        operationKey: "reference_auth_race_revise",
      }, session.cookie,
    );
    const approve = await jsonPost(
      api, `/api/studio/reference-drafts/${masterDraftId}/approve`, {
        ...actionBinding,
        operationKey: "reference_auth_race_approve",
      }, session.cookie,
    );
    expect([mobile.status, media.status, revise.status, approve.status]).toEqual([409, 409, 409, 409]);
    expect([mobile.body, media.body, revise.body, approve.body]).toEqual([
      { error: "reference-workflow-asset-withdrawn" },
      { error: "reference-workflow-asset-withdrawn" },
      { error: "reference-workflow-asset-withdrawn" },
      { error: "reference-workflow-asset-withdrawn" },
    ]);
  }, 120_000);

  it("produces real local scene transcript and vision receipts for a review-required reference", async () => {
    const fixture = await makeFixture();
    const worker = referenceWorker();
    const { api, root } = await createDefaultApi({ modelWorker: worker });
    const session = await openSession(api);
    const imported = await multipart(api, session.cookie)
      .field("operationKey", "reference_real_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "reference.mp4", contentType: "video/mp4" });
    expect(imported.status).toBe(201);
    expect(imported.body.sample.usePermission).toBe("review-required");
    const rights = await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: imported.body.sample.id,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
      operationKey: "reference_real_rights",
    }, session.cookie);
    expect(rights.status).toBe(201);

    const analyzed = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: "reference_real_analysis",
    }, session.cookie);

    expect(analyzed.status).toBe(201);
    expect(analyzed.body.analysis).toMatchObject({ status: "degraded" });
    expect(worker.run).toHaveBeenCalledTimes(3);
    expect(worker.run.mock.calls.map(([job]) => job.layer)).toEqual(["scene", "transcript", "vision"]);
    expect(await findJsonValue(root, imported.body.sample.sourceAssetId, "usePermission"))
      .toBe("review-required");
  }, 120_000);

  it("returns a degraded analysis while retaining optional failure receipts outside grammar evidence", async () => {
    const fixture = await makeFixture();
    const worker = referenceWorker((layer) => layer === "scene" ? "succeeded" : "failed-manual-review");
    const { api, root } = await createDefaultApi({ modelWorker: worker });
    const session = await openSession(api);
    const imported = await multipart(api, session.cookie)
      .field("operationKey", "reference_degraded_import")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "degraded.mp4", contentType: "video/mp4" });
    await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: imported.body.sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
      operationKey: "reference_degraded_rights",
    }, session.cookie);

    const analyzed = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: "reference_degraded_analysis",
    }, session.cookie);

    expect(analyzed.status).toBe(201);
    expect(analyzed.body.analysis).toMatchObject({
      status: "degraded",
      warnings: expect.arrayContaining([
        "reference-transcript-evidence-missing",
        "reference-vision-evidence-missing",
      ]),
    });
    const state = await findAnalysisState(root, imported.body.sample.sourceAssetId);
    expect(state.jobs[0]).toMatchObject({ status: "succeeded", attempt: 1 });
    expect(state.receipts.map(({ layer, status }: any) => ({ layer, status }))).toEqual([
      { layer: "scene", status: "succeeded" },
      { layer: "transcript", status: "failed-manual-review" },
      { layer: "vision", status: "failed-manual-review" },
    ]);
    expect(analyzed.body.analysis.rules.map((rule: any) => rule.kind)).toEqual(["rhythm"]);
    expect(analyzed.body.analysis.confidence).toBe(0.9);
  }, 120_000);

  it.each([
    ["failed-retryable", 503, "reference-analysis-temporarily-unavailable"],
    ["failed-manual-review", 422, "reference-analysis-manual-review-required"],
  ] as const)("maps a %s required scene receipt without leaking worker detail", async (
    sceneStatus,
    expectedStatus,
    expectedError,
  ) => {
    const fixture = await makeFixture();
    const worker = referenceWorker(() => sceneStatus);
    const { api } = await createDefaultApi({ modelWorker: worker });
    const session = await openSession(api);
    const imported = await multipart(api, session.cookie)
      .field("operationKey", `reference_scene_${sceneStatus}_import`)
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "scene-failure.mp4", contentType: "video/mp4" });
    await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: imported.body.sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
      operationKey: `reference_scene_${sceneStatus}_rights`,
    }, session.cookie);

    const analyzed = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: `reference_scene_${sceneStatus}_analysis`,
    }, session.cookie);

    expect(analyzed.status).toBe(expectedStatus);
    expect(analyzed.body).toEqual({ error: expectedError });
    expect(JSON.stringify(analyzed.body)).not.toMatch(/worker|sourcePath|private|stderr/i);
    expect(worker.run).toHaveBeenCalledTimes(1);
  }, 120_000);

  it.each([
    ["reference-scene-evidence-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => {
      if (layer !== "scene") return signals;
      const scene = (signals.scenes as Array<Record<string, unknown>>)[0]!;
      return { ...signals, scenes: [{ ...scene, representativeFrameMs: scene.startMs }] };
    }],
    ["reference-transition-evidence-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => layer === "scene" ? { ...signals, transitionEvidence: "malformed" } : signals],
    ["reference-audio-evidence-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => layer === "scene" ? { ...signals, beatMarkers: [-1] } : signals],
    ["reference-evidence-millisecond-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => {
      if (layer !== "scene") return signals;
      const scene = (signals.scenes as Array<Record<string, unknown>>)[0]!;
      return { ...signals, scenes: [{ ...scene, representativeFrameMs: "malformed" }] };
    }],
    ["reference-vision-evidence-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => layer === "vision" ? { ...signals, visualObservations: "malformed" } : signals],
    ["reference-bounded-evidence-flag-invalid", (
      layer: ReferenceWorkerLayer,
      signals: Record<string, unknown>,
    ) => layer === "scene" ? { ...signals, audioEvidenceTruncated: "malformed" } : signals],
  ] as const)("maps production grammar integrity error %s to HTTP 500", async (
    expectedError,
    qualitySignalsForLayer,
  ) => {
    const fixture = await makeFixture();
    const worker = referenceWorker(() => "succeeded", qualitySignalsForLayer);
    const { api } = await createDefaultApi({ modelWorker: worker });
    const session = await openSession(api);
    const operationSuffix = expectedError.replaceAll("-", "_");
    const imported = await multipart(api, session.cookie)
      .field("operationKey", `${operationSuffix}_import`)
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "malformed-evidence.mp4", contentType: "video/mp4" });
    await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: imported.body.sample.id,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
      operationKey: `${operationSuffix}_rights`,
    }, session.cookie);

    const analyzed = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: `${operationSuffix}_analysis`,
    }, session.cookie);

    expect(analyzed.status, JSON.stringify(analyzed.body)).toBe(500);
    expect(analyzed.body).toEqual({ error: expectedError });
    expect(JSON.stringify(analyzed.body)).not.toMatch(/worker|sourcePath|private|stderr/i);
    expect(worker.run).toHaveBeenCalledTimes(3);
  }, 120_000);

  it("retries one failed reference analysis attempt on the same endpoint without duplicate success receipts", async () => {
    const fixture = await makeFixture();
    const worker = referenceWorker();
    const successfulRun = worker.run.getMockImplementation()!;
    worker.run.mockImplementationOnce(async () => {
      throw new Error("studio-model-worker-timeout");
    }).mockImplementation(successfulRun);
    const { api, root } = await createDefaultApi({ modelWorker: worker });
    const session = await openSession(api);
    const imported = await multipart(api, session.cookie)
      .field("operationKey", "reference_retry_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "retry.mp4", contentType: "video/mp4" });
    const rights = await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: imported.body.sample.id,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
      operationKey: "reference_retry_rights",
    }, session.cookie);
    expect(rights.status).toBe(201);

    const first = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: "reference_retry_analysis",
    }, session.cookie);
    const second = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: imported.body.sample.id,
      operationKey: "reference_retry_analysis",
    }, session.cookie);

    expect(first.status).not.toBe(201);
    expect(second.status).toBe(201);
    expect(worker.run).toHaveBeenCalledTimes(4);
    const state = await findAnalysisState(root, imported.body.sample.sourceAssetId);
    expect(state.jobs).toHaveLength(1);
    expect(state.jobs[0]).toMatchObject({ status: "succeeded", attempt: 2 });
    expect(state.receipts).toHaveLength(3);
    expect(new Set(state.receipts.map((receipt: any) => receipt.jobId)).size).toBe(3);
  }, 120_000);

  it("persists historical assets across same-customer sessions without treating current-session assets as history", async () => {
    let firstAssetId = "";
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      const sampleService = new ReferenceSampleService(library, sessionRoot, {
        now: () => "2026-09-19T08:00:00.000Z",
      });
      const current = await library.importOriginal(scope.actorId, scope.customerId, {
        requestId: `history-seed-${scope.sessionId}`,
        bytes: Buffer.from(`history-source-${scope.customerId}-${scope.sessionId}`),
        mediaKind: "video",
        fileName: "history.mp4",
        capturedAt: null,
        sessionId: scope.sessionId,
        viewPermission: "allowed",
        usePermission: "allowed",
      });
      if (!firstAssetId && scope.customerId === "customer-a") firstAssetId = current.id;
      return { sampleService, store, editor: stubEditor() };
    };
    const { api, root } = await createApi(factory);
    const first = await openSession(api);
    const currentDenied = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: firstAssetId, sourceKind: "self-owned", operationKey: "current_is_not_history",
    }, first.cookie);
    expect(currentDenied.status).toBe(400);
    expect(currentDenied.body).toEqual({ error: "reference-history-asset-current-session" });

    const second = await openSession(api);
    const priorAllowed = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: firstAssetId, sourceKind: "self-owned", operationKey: "prior_session_history",
    }, second.cookie);
    expect(priorAllowed.status).toBe(201);

    const otherCustomer = await openSession(api, "customer-b");
    const crossCustomer = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: firstAssetId, sourceKind: "self-owned", operationKey: "cross_customer_history",
    }, otherCustomer.cookie);
    expect(crossCustomer.status).toBe(400);
    expect(crossCustomer.body).toEqual({ error: "reference-history-asset-not-visible" });
  }, 120_000);

  it("analyzes a same-customer prior-session historical reference without granting output use", async () => {
    const fixture = await makeFixture();
    const worker = referenceWorker();
    const { api, root } = await createDefaultApi({ modelWorker: worker });
    const first = await openSession(api);
    const imported = await multipart(api, first.cookie)
      .field("operationKey", "historical_analysis_source")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "historical.mp4", contentType: "video/mp4" });
    expect(imported.status).toBe(201);

    const second = await openSession(api);
    const historical = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: imported.body.sample.sourceAssetId,
      sourceKind: "self-owned",
      operationKey: "historical_analysis_pointer",
    }, second.cookie);
    expect(historical.status).toBe(201);
    const rights = await jsonPost(api, "/api/studio/reference-rights", {
      referenceSampleId: historical.body.sample.id,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
      operationKey: "historical_analysis_rights",
    }, second.cookie);
    expect(rights.status).toBe(201);

    const analysis = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: historical.body.sample.id,
      operationKey: "historical_analysis_run",
    }, second.cookie);

    expect(analysis.status).toBe(201);
    expect(worker.run).toHaveBeenCalledTimes(3);
    expect(await findJsonValue(root, imported.body.sample.sourceAssetId, "usePermission"))
      .toBe("review-required");
  }, 120_000);

  it("streams only current scoped non-invalidated reference draft bytes", async () => {
    const draftId = "reference-draft-readable";
    let runtimeCount = 0;
    const factory: StudioReferenceRuntimeFactory = async (runtimeInput) => {
      const { scope, sessionRoot } = runtimeInput;
      runtimeCount += 1;
      const outputPath = join(sessionRoot, "reference-renders", "draft.mp4");
      const { mkdir } = await import("node:fs/promises");
      await mkdir(join(sessionRoot, "reference-renders"), { recursive: true });
      await writeFile(outputPath, Buffer.from("reference-draft-media"));
      const draft = runtimeCount === 1
        ? { id: draftId, sessionId: scope.sessionId, outputPath, status: "reviewable" }
        : { id: "reference-draft-invalidated", sessionId: scope.sessionId, outputPath, status: "invalidated" };
      return runtimeFactory({ draft })(runtimeInput);
    };
    const { api, root } = await createApi(factory);
    const first = await openSession(api);
    const normal = await jsonGet(api, `/api/studio/reference-drafts/${draftId}/media`, first.cookie);
    expect(normal.status).toBe(200);
    expect(normal.headers["content-type"]).toBe("video/mp4");
    expect(normal.body).toEqual(Buffer.from("reference-draft-media"));

    const second = await openSession(api);
    const crossSession = await jsonGet(api, `/api/studio/reference-drafts/${draftId}/media`, second.cookie);
    expect(crossSession.status).toBe(404);
    expect(crossSession.body).toEqual({ error: "reference-draft-not-found" });
    const invalidated = await jsonGet(api, "/api/studio/reference-drafts/reference-draft-invalidated/media", second.cookie);
    expect(invalidated.status).toBe(409);
    expect(invalidated.body).toEqual({ error: "reference-draft-invalidated" });
    const unknown = await jsonGet(api, "/api/studio/reference-drafts/unknown/media", first.cookie);
    expect(unknown.status).toBe(404);
    expect(unknown.body).toEqual({ error: "reference-draft-not-found" });
  }, 120_000);

  it("denies the original media URL after the workflow appends an invalidation tombstone", async () => {
    const originalId = "reference-draft-old-url";
    const factory: StudioReferenceRuntimeFactory = async (runtimeInput) => {
      const outputPath = join(runtimeInput.sessionRoot, "reference-renders", "old-url.mp4");
      const { mkdir } = await import("node:fs/promises");
      await mkdir(join(runtimeInput.sessionRoot, "reference-renders"), { recursive: true });
      await writeFile(outputPath, Buffer.from("reference-draft-media"));
      const base = {
        customerId: runtimeInput.scope.customerId,
        sessionId: runtimeInput.scope.sessionId,
        version: 1,
        outputPath,
        outputDigest: createHash("sha256").update("reference-draft-media").digest("hex"),
      };
      return {
        ...await runtimeFactory()(runtimeInput),
        store: {
          snapshot: async () => ({
            executionReports: [],
            drafts: [
              { ...base, id: originalId, status: "reviewable" },
              {
                ...base,
                id: `${originalId}-invalidated-0123456789ab`,
                status: "invalidated",
              },
            ],
          }) as never,
        },
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);

    const response = await jsonGet(
      api,
      `/api/studio/reference-drafts/${originalId}/media`,
      session.cookie,
    );

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-draft-invalidated" });
  }, 120_000);

  it("fails a draft media read when invalidation commits after the first snapshot and before return", async () => {
    const originalId = "reference-draft-race";
    const markerId = "reference-sample-draft-race-marker";
    const firstSnapshotRead = deferred<void>();
    const invalidationCommitted = deferred<void>();
    let realStore!: JsonReferenceSampleStore;
    let realScope!: { actorId: string; customerId: string; sessionId: string };
    const factory: StudioReferenceRuntimeFactory = async (runtimeInput) => {
      realScope = runtimeInput.scope;
      const outputPath = join(runtimeInput.sessionRoot, "reference-renders", "race.mp4");
      const { mkdir } = await import("node:fs/promises");
      await mkdir(join(runtimeInput.sessionRoot, "reference-renders"), { recursive: true });
      await writeFile(outputPath, Buffer.from("reference-draft-media"));
      realStore = await JsonReferenceSampleStore.open(runtimeInput.sessionRoot, runtimeInput.scope);
      const markerDigest = createHash("sha256").update("draft-race-marker").digest("hex");
      await realStore.putSample({
        id: markerId,
        customerId: runtimeInput.scope.customerId,
        sessionId: runtimeInput.scope.sessionId,
        version: 1,
        sourceAssetId: `asset_${markerDigest}`,
        sourceDigest: markerDigest,
        sourceKind: "self-owned",
        collectionMode: "historical-pointer",
        candidateScope: "reference-only",
        status: "pending-rights",
        viewPermission: "allowed",
        usePermission: "review-required",
        createdAt: "2026-09-19T08:00:00.000Z",
      });
      const base = {
        customerId: runtimeInput.scope.customerId,
        sessionId: runtimeInput.scope.sessionId,
        version: 1,
        outputPath,
        outputDigest: createHash("sha256").update("reference-draft-media").digest("hex"),
      };
      return {
        ...await runtimeFactory()(runtimeInput),
        store: {
          snapshot: async () => {
            const snapshot = await realStore.snapshot();
            firstSnapshotRead.resolve();
            await invalidationCommitted.promise;
            return { ...snapshot, drafts: [{ ...base, id: originalId, status: "reviewable" }] } as never;
          },
          withCurrentSnapshot: (work) => realStore.withCurrentSnapshot((snapshot) => {
            const invalidated = snapshot.samples.some((sample) =>
              sample.id === markerId && sample.status === "invalidated");
            return work({
              ...snapshot,
              drafts: invalidated
                ? [
                    { ...base, id: originalId, status: "reviewable" },
                    {
                      ...base,
                      id: `${originalId}-invalidated-abcdef012345`,
                      status: "invalidated",
                    },
                  ]
                : [{ ...base, id: originalId, status: "reviewable" }],
            } as never);
          }),
        },
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);

    const pendingResponse = jsonGet(
      api,
      `/api/studio/reference-drafts/${originalId}/media`,
      session.cookie,
    ).then((response) => response);
    await firstSnapshotRead.promise;
    const beforeInvalidation = await realStore.snapshot();
    const marker = beforeInvalidation.samples.find((sample) => sample.id === markerId)!;
    await commitSampleInvalidation(
      realStore,
      beforeInvalidation,
      marker,
      realScope,
      "draft-media-race-invalidation",
    );
    invalidationCommitted.resolve();
    const response = await pendingResponse;

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-draft-invalidated" });
  }, 120_000);

  it("lists safe same-customer historical videos while excluding the current session", async () => {
    const sessionlessByCustomer = new Map<string, string>();
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const sessionless = await library.importOriginal(scope.actorId, scope.customerId, {
        requestId: `sessionless-catalog-${scope.customerId}`,
        bytes: Buffer.from(`sessionless-catalog-${scope.customerId}`),
        mediaKind: "video",
        fileName: "/Users/test-user/private/sessionless-history.mov",
        capturedAt: null,
        sessionId: null,
        viewPermission: "allowed",
        usePermission: "review-required",
      });
      sessionlessByCustomer.set(scope.customerId, sessionless.id);
      if (scope.customerId === "customer-a") {
        await library.importOriginal(scope.actorId, scope.customerId, {
          requestId: "sessionless-catalog-trailing-private-directory",
          bytes: Buffer.from("sessionless-catalog-trailing-private-directory"),
          mediaKind: "video",
          fileName: "C:\\Users\\alice\\SecretMatter\\",
          capturedAt: null,
          sessionId: null,
          viewPermission: "allowed",
          usePermission: "review-required",
        });
      }
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot, {
          now: () => "2026-09-19T08:00:00.000Z",
        }),
        editor: stubEditor(),
        store,
        prepareAnalysis: async () => undefined,
      };
    };
    const { api } = await createApi(factory);
    const first = await openSession(api, "customer-a");
    const firstList = await jsonGet(api, "/api/studio/reference-history", first.cookie);
    expect(firstList.status).toBe(200);
    expect(firstList.body.assets.map((asset: any) => asset.assetId))
      .toContain(sessionlessByCustomer.get("customer-a"));
    expect(firstList.body.assets).toHaveLength(2);
    expect(firstList.body.assets.map((asset: any) => asset.fileName).sort())
      .toEqual(["reference-video", "sessionless-history.mov"]);
    expect(JSON.stringify(firstList.body)).not.toMatch(/sessionId|customerId|sourcePath|outputPath|blobId/);
    expect(JSON.stringify(firstList.body)).not.toContain("/Users/test-user/private");

    const second = await openSession(api, "customer-a");
    const secondList = await jsonGet(api, "/api/studio/reference-history", second.cookie);
    expect(secondList.status).toBe(200);
    expect(secondList.body.assets.some((asset: any) => asset.fileName.includes("synthetic-color")))
      .toBe(true);
    expect(secondList.body.assets).toHaveLength(6);
    expect(JSON.stringify(secondList.body)).not.toContain(second.sessionId);

    const other = await openSession(api, "customer-b");
    const otherList = await jsonGet(api, "/api/studio/reference-history", other.cookie);
    expect(otherList.status).toBe(200);
    expect(otherList.body.assets.map((asset: any) => asset.assetId))
      .not.toContain(sessionlessByCustomer.get("customer-a"));
  }, 120_000);

  it("previews dedicated and historical reference samples without exposing paths and fails closed after withdrawal", async () => {
    const fixture = await makeFixture();
    const runtimeBySession = new Map<string, { library: any; store: JsonReferenceSampleStore; scope: any }>();
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      runtimeBySession.set(scope.sessionId, { library, store, scope });
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot, {
          now: () => "2026-09-19T08:00:00.000Z",
        }),
        editor: stubEditor(),
        store,
        prepareAnalysis: async () => undefined,
      };
    };
    const { api, root } = await createApi(factory);
    const first = await openSession(api, "customer-a");
    const imported = await multipart(api, first.cookie)
      .field("operationKey", "preview_dedicated_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "preview.mov", contentType: "video/quicktime" });
    expect(imported.status).toBe(201);
    const dedicatedPreview = await jsonGet(
      api, `/api/studio/reference-samples/${imported.body.sample.id}/media`, first.cookie,
    );
    expect(dedicatedPreview.status).toBe(200);
    expect(dedicatedPreview.headers["content-type"]).toBe("video/quicktime");
    expect(dedicatedPreview.body).toEqual(await readFile(fixture));

    const firstRuntime = runtimeBySession.get(first.sessionId)!;
    const beforeInvalidation = await firstRuntime.store.snapshot();
    const currentSample = beforeInvalidation.samples
      .filter((sample) => sample.id === imported.body.sample.id)
      .sort((left, right) => right.version - left.version)[0]!;
    await commitSampleInvalidation(
      firstRuntime.store,
      beforeInvalidation,
      currentSample,
      firstRuntime.scope,
      "preview-dedicated-invalidation",
    );
    const invalidated = await jsonGet(
      api, `/api/studio/reference-samples/${imported.body.sample.id}/media`, first.cookie,
    );
    expect(invalidated.status).toBe(409);
    expect(invalidated.body).toEqual({ error: "reference-sample-invalidated" });

    const second = await openSession(api, "customer-a");
    const historical = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: imported.body.sample.sourceAssetId,
      sourceKind: "self-owned",
      operationKey: "preview_history_pointer",
    }, second.cookie);
    expect(historical.status).toBe(201);
    const historicalPreview = await jsonGet(
      api, `/api/studio/reference-samples/${historical.body.sample.id}/media`, second.cookie,
    );
    expect(historicalPreview.status).toBe(200);
    expect(historicalPreview.body).toEqual(await readFile(fixture));

    const crossSession = await jsonGet(
      api, `/api/studio/reference-samples/${historical.body.sample.id}/media`, first.cookie,
    );
    const other = await openSession(api, "customer-b");
    const crossCustomer = await jsonGet(
      api, `/api/studio/reference-samples/${historical.body.sample.id}/media`, other.cookie,
    );
    const unknown = await jsonGet(api, "/api/studio/reference-samples/unknown/media", second.cookie);
    expect([crossSession.status, crossCustomer.status, unknown.status]).toEqual([404, 404, 404]);
    expect(JSON.stringify([crossSession.body, crossCustomer.body, unknown.body])).not.toContain("/private/");

    const secondRuntime = runtimeBySession.get(second.sessionId)!;
    await secondRuntime.library.withdraw(
      second.actorId,
      second.customerId,
      imported.body.sample.sourceAssetId,
      "preview-source-withdrawn",
    );
    const withdrawn = await jsonGet(
      api, `/api/studio/reference-samples/${historical.body.sample.id}/media`, second.cookie,
    );
    expect(withdrawn.status).toBe(409);
    expect(withdrawn.body).toEqual({ error: "reference-sample-source-withdrawn" });

    const escapeImport = await multipart(api, second.cookie)
      .field("operationKey", "preview_escape_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "escape.mp4", contentType: "video/mp4" });
    expect(escapeImport.status).toBe(201);
    const record = await findAssetRecord(root, escapeImport.body.sample.sourceAssetId);
    const customerRoot = dirname(dirname(dirname(record.path)));
    const blobPath = join(customerRoot, "blobs", record.value.blobId, "original.bin");
    const outsideRoot = await makeRoot("reference-preview-outside-");
    const outsidePath = join(outsideRoot, "outside.mp4");
    await writeFile(outsidePath, await readFile(fixture));
    await unlink(blobPath);
    await symlink(outsidePath, blobPath);
    const escaped = await jsonGet(
      api, `/api/studio/reference-samples/${escapeImport.body.sample.id}/media`, second.cookie,
    );
    expect(escaped.status).toBe(409);
    expect(escaped.body).toEqual({ error: "reference-sample-source-binding-invalid" });
  }, 120_000);

  it("linearizes sample preview against a real store invalidation and customer-library withdrawal", async () => {
    const fixture = await makeFixture();
    let runtime!: {
      library: any;
      store: JsonReferenceSampleStore;
      scope: { actorId: string; customerId: string; sessionId: string };
    };
    const factory: StudioReferenceRuntimeFactory = async ({ scope, sessionRoot, library }) => {
      const store = await JsonReferenceSampleStore.open(sessionRoot, scope);
      runtime = { library, store, scope };
      return {
        sampleService: new ReferenceSampleService(library, sessionRoot, {
          now: () => "2026-09-19T08:00:00.000Z",
        }),
        editor: stubEditor(),
        store,
        prepareAnalysis: async () => undefined,
      };
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);
    const imported = await multipart(api, session.cookie)
      .field("operationKey", "preview_invalidation_race_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "race.mp4", contentType: "video/mp4" });
    expect(imported.status).toBe(201);

    const originalRead = runtime.library.withReferenceOriginal.bind(runtime.library);
    const bytesRead = deferred<void>();
    const releaseRead = deferred<void>();
    runtime.library.withReferenceOriginal = async (
      actorId: string,
      customerId: string,
      assetId: string,
      work: (snapshot: unknown) => Promise<unknown>,
    ) => originalRead(actorId, customerId, assetId, async (snapshot: unknown) => {
      bytesRead.resolve();
      await releaseRead.promise;
      return work(snapshot);
    });
    const pendingInvalidatedPreview = jsonGet(
      api, `/api/studio/reference-samples/${imported.body.sample.id}/media`, session.cookie,
    ).then((response) => response);
    await bytesRead.promise;
    const beforeInvalidation = await runtime.store.snapshot();
    const current = beforeInvalidation.samples.find((sample) => sample.id === imported.body.sample.id)!;
    await commitSampleInvalidation(
      runtime.store,
      beforeInvalidation,
      current,
      runtime.scope,
      "preview-invalidation-race",
    );
    releaseRead.resolve();
    const invalidatedPreview = await pendingInvalidatedPreview;
    expect(invalidatedPreview.status).toBe(409);
    expect(invalidatedPreview.body).toEqual({ error: "reference-sample-invalidated" });

    const second = await multipart(api, session.cookie)
      .field("operationKey", "preview_withdrawal_race_import")
      .field("sourceKind", "self-owned")
      .attach("file", fixture, { filename: "withdrawal-race.mp4", contentType: "video/mp4" });
    expect(second.status).toBe(201);
    const secondBytesRead = deferred<void>();
    const releaseSecondRead = deferred<void>();
    runtime.library.withReferenceOriginal = async (
      actorId: string,
      customerId: string,
      assetId: string,
      work: (snapshot: unknown) => Promise<unknown>,
    ) => originalRead(actorId, customerId, assetId, async (snapshot: unknown) => {
      secondBytesRead.resolve();
      await releaseSecondRead.promise;
      return work(snapshot);
    });
    const pendingAuthorizedPreview = jsonGet(
      api, `/api/studio/reference-samples/${second.body.sample.id}/media`, session.cookie,
    ).then((response) => response);
    await secondBytesRead.promise;
    let withdrawalCommitted = false;
    const withdrawal = runtime.library.withdraw(
      session.actorId,
      session.customerId,
      second.body.sample.sourceAssetId,
      "withdrawal-raced-with-preview",
    ).then(() => { withdrawalCommitted = true; });
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(withdrawalCommitted).toBe(false);
    releaseSecondRead.resolve();
    expect((await pendingAuthorizedPreview).status).toBe(200);
    await withdrawal;
    const afterWithdrawal = await jsonGet(
      api, `/api/studio/reference-samples/${second.body.sample.id}/media`, session.cookie,
    );
    expect(afterWithdrawal.status).toBe(409);
    expect(afterWithdrawal.body).toEqual({ error: "reference-sample-source-withdrawn" });
  }, 120_000);

  it("rejects a reference draft path outside the server-owned render root without leaking it", async () => {
    const outsideRoot = await makeRoot("studio-reference-outside-draft-");
    const outsidePath = join(outsideRoot, "outside.mp4");
    await writeFile(outsidePath, Buffer.from("outside-reference-media"));
    const factory: StudioReferenceRuntimeFactory = async (runtimeInput) => {
      const { mkdir } = await import("node:fs/promises");
      await mkdir(join(runtimeInput.sessionRoot, "reference-renders"), { recursive: true });
      return runtimeFactory({
        draft: {
          id: "reference-draft-outside",
          sessionId: runtimeInput.scope.sessionId,
          outputPath: outsidePath,
          status: "reviewable",
        },
      })(runtimeInput);
    };
    const { api } = await createApi(factory);
    const session = await openSession(api);

    const response = await jsonGet(
      api, "/api/studio/reference-drafts/reference-draft-outside/media", session.cookie,
    );

    expect(response.status).toBe(500);
    expect(response.body).toEqual({ error: "reference-draft-output-binding-invalid" });
    expect(JSON.stringify(response.body)).not.toContain(outsideRoot);
  }, 120_000);

  it("maps media-tool infrastructure failures separately from invalid user media and audits both", async () => {
    const fixture = await makeFixture();
    const { api, root } = await createApi(runtimeFactory(), undefined, {
      ffprobePath: "/definitely/missing/reference-ffprobe",
      ffmpegPath: ffmpegPath!,
    });
    const session = await openSession(api);
    const unavailable = await multipart(api, session.cookie)
      .field("operationKey", "reference_missing_tool")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });

    expect(unavailable.status).toBe(503);
    expect(unavailable.body).toEqual({ error: "reference-import-media-tool-unavailable" });
    expect(JSON.stringify(unavailable.body)).not.toContain("/definitely/");
    expect(await listReferenceStagingFiles(root)).toEqual([]);
    expect(await readImportAudits(root)).toEqual([
      expect.objectContaining({
        operationKey: "reference_missing_tool",
        errorCode: "reference-import-media-tool-unavailable",
      }),
    ]);
  }, 120_000);

  it("returns stable infrastructure errors for staging I/O, tool output overflow, and tool timeout", async () => {
    const fixture = await makeFixture();

    const staging = await createApi(runtimeFactory());
    const stagingSession = await openSession(staging.api);
    const [sessionDirectory] = await readdir(join(staging.root, "sessions"));
    await writeFile(
      join(staging.root, "sessions", sessionDirectory!, "reference-upload-staging"),
      "blocks-directory-creation",
    );
    const stagingFailure = await multipart(staging.api, stagingSession.cookie)
      .field("operationKey", "reference_staging_io")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });
    expect(stagingFailure.status).toBe(500);
    expect(stagingFailure.body).toEqual({ error: "reference-import-staging-io-failed" });

    const scriptRoot = await makeRoot("studio-reference-tool-scripts-");
    const overflowTool = join(scriptRoot, "overflow.sh");
    const timeoutTool = join(scriptRoot, "timeout.sh");
    const signaledTool = join(scriptRoot, "signaled.sh");
    await writeFile(overflowTool, "#!/bin/sh\nhead -c 70000 /dev/zero\n");
    await writeFile(timeoutTool, "#!/bin/sh\nsleep 1\n");
    await writeFile(signaledTool, "#!/bin/sh\nkill -9 $$\n");
    await chmod(overflowTool, 0o700);
    await chmod(timeoutTool, 0o700);
    await chmod(signaledTool, 0o700);

    const overflow = await createApi(runtimeFactory(), undefined, {
      ffprobePath: overflowTool, ffmpegPath: ffmpegPath!,
    });
    const overflowSession = await openSession(overflow.api);
    const overflowResponse = await multipart(overflow.api, overflowSession.cookie)
      .field("operationKey", "reference_tool_overflow")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });
    expect(overflowResponse.status).toBe(500);
    expect(overflowResponse.body).toEqual({ error: "reference-import-media-output-too-large" });

    const timeout = await createApi(runtimeFactory(), undefined, {
      ffprobePath: timeoutTool, ffmpegPath: ffmpegPath!,
    }, { timeoutMs: 25 });
    const timeoutSession = await openSession(timeout.api);
    const timeoutResponse = await multipart(timeout.api, timeoutSession.cookie)
      .field("operationKey", "reference_tool_timeout")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });
    expect(timeoutResponse.status).toBe(503);
    expect(timeoutResponse.body).toEqual({ error: "reference-import-media-tool-timeout" });

    const signaled = await createApi(runtimeFactory(), undefined, {
      ffprobePath: signaledTool, ffmpegPath: ffmpegPath!,
    });
    const signaledSession = await openSession(signaled.api);
    const signaledResponse = await multipart(signaled.api, signaledSession.cookie)
      .field("operationKey", "reference_tool_signaled")
      .field("sourceKind", "external-style-only")
      .attach("file", fixture, { filename: "sample.mp4", contentType: "video/mp4" });
    expect(signaledResponse.status).toBe(503);
    expect(signaledResponse.body).toEqual({ error: "reference-import-media-tool-crashed" });
  }, 120_000);

  it("returns a deterministic review-safe mobile contract without mutating workflow state", async () => {
    let snapshot!: ReturnType<typeof mobileWorkflowSnapshot>;
    let reads = 0;
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => {
        snapshot ??= mobileWorkflowSnapshot(scope, { includeUnsafePrivateFields: true });
        reads += 1;
        return snapshot;
      },
    }));
    const session = await openSession(api);
    snapshot ??= mobileWorkflowSnapshot(session, { includeUnsafePrivateFields: true });
    const before = structuredClone(snapshot);

    const first = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);
    const second = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(second.body).toEqual(first.body);
    const review = ReferenceMobileReviewV1Schema.parse(first.body.review);
    expect(review.artifacts.map((artifact) => artifact.kind)).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
    expect(review.artifacts.map((artifact) => artifact.mediaRoute)).toEqual([
      { kind: "reference-draft-media", version: 1, draftId: "draft-editorial-master" },
      { kind: "reference-draft-media", version: 1, draftId: "draft-xiaohongshu" },
      { kind: "reference-draft-media", version: 1, draftId: "draft-douyin" },
      { kind: "reference-draft-media", version: 1, draftId: "draft-wechat-channels" },
    ]);
    expect(review.styleSummary).toMatchObject({
      status: "degraded",
      capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1 },
      warningCodes: ["reference-audio-evidence-missing"],
    });
    expect(review.styleSummary.capability.version).not.toBe(830_001);
    expect(JSON.stringify(first.body)).not.toContain("830001");
    expect(review.executionReport).toMatchObject({
      rhythm: { status: "matched" },
      transitions: { status: "substituted" },
      narrative: { status: "matched" },
      audio: { status: "unachievable" },
    });
    expect(review.approval).toEqual({ status: "pending", approvedArtifactKinds: [] });
    expect(review.publishClaimed).toBe(false);
    expect(JSON.stringify(first.body)).not.toMatch(
      /sourcePath|outputPath|originalBytes|externalTranscript|externalContent|rawModelOutput|\/Users\/operator/,
    );
    expect(snapshot).toEqual(before);
    expect(reads).toBe(2);
  }, 120_000);

  it("does not project a legacy approval onto a newer execution report", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => {
        const current = mobileWorkflowSnapshot(scope);
        const report = current.executionReports[0]!;
        const mapping = current.mappingPlans[0]!;
        const sample = current.samples[0]!;
        const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;
        const priorReport = {
          ...report,
          version: report.version - 1,
          status: "invalidated" as const,
        };
        const legacyApproval = {
          id: "legacy-approval-master",
          storyId: sample.id,
          revisionId: mapping.id,
          revision: mapping.version,
          artifactId: master.draftId,
          artifactDigest: master.outputDigest,
          coverDigest: createHash("sha256").update("legacy-cover").digest("hex"),
          stillDigests: [],
          renderInputDigest: mapping.contentDigest,
          variant: "editorial-master" as const,
          reviewerId: scope.actorId,
          approvedAt: "2026-09-19T08:01:00.000Z",
          status: "active" as const,
        };
        return {
          ...current,
          executionReports: [priorReport, report],
          approvals: [legacyApproval],
        };
      },
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(200);
    expect(response.body.review.artifacts.find(
      (artifact: { kind: string }) => artifact.kind === "editorial-master",
    )?.status).toBe("reviewable");
    expect(response.body.review.approval).toEqual({ status: "pending", approvedArtifactKinds: [] });
  }, 120_000);

  it("selects the unique highest-version report across ids without inheriting a legacy approval", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => {
        const current = mobileWorkflowSnapshot(scope);
        const report = current.executionReports[0]!;
        const mapping = current.mappingPlans[0]!;
        const sample = current.samples[0]!;
        const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;
        return {
          ...current,
          executionReports: [{
            ...report,
            id: "report-mobile-prior-id",
            version: report.version - 1,
          }, report],
          approvals: [{
            id: "legacy-approval-prior-report",
            storyId: sample.id,
            revisionId: mapping.id,
            revision: mapping.version,
            artifactId: master.draftId,
            artifactDigest: master.outputDigest,
            coverDigest: createHash("sha256").update("legacy-prior-cover").digest("hex"),
            stillDigests: [],
            renderInputDigest: mapping.contentDigest,
            variant: "editorial-master" as const,
            reviewerId: scope.actorId,
            approvedAt: "2026-09-19T08:02:00.000Z",
            status: "active" as const,
          }],
        };
      },
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(200);
    expect(response.body.review.bindings.executionReport)
      .toMatchObject({ id: "report-mobile", version: 9 });
    expect(response.body.review.artifacts.find(
      (artifact: { kind: string }) => artifact.kind === "editorial-master",
    )?.status).toBe("reviewable");
  }, 120_000);

  it("fails closed when two report ids tie for the highest current version", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => {
        const current = mobileWorkflowSnapshot(scope);
        const report = current.executionReports[0]!;
        return {
          ...current,
          executionReports: [report, { ...report, id: "report-mobile-equal-highest" }],
        };
      },
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-mobile-review-ambiguous" });
  }, 120_000);

  it.each([
    "/Volumes/private/reference.mov",
    "/opt/private/reference.mov",
    "C:/Users/test-user/private.mov",
    "C:\\Users\\operator\\private.mov",
    "\\\\server\\share\\private.mov",
    "\\\\?\\C:\\private.mov",
    "file:///Volumes/private/reference.mov",
    "%252FVolumes%252Fprivate%252Freference.mov",
    "模型位于 /Users/test-user/model",
    "错误来源 file:///private/model.json",
    "前缀 C:/Users/test-user/private.mov",
    "前缀 %252Fopt%252Fprivate.mov",
    "/api/studio/foo?debug=/private/draft.mov",
    "/api/studio/foo;debug=/private/draft.mov",
    "/api/studio/foo&debug=/private/draft.mov",
    "/api/studio/foo#debug=/private/draft.mov",
    "/api/studio/foo\\private\\draft.mov",
    "/api/studio/foo%3Fdebug%3D%2Fprivate%2Fdraft.mov",
    "/api/studio/foo%2Fprivate%2Fdraft.mov",
    "路径：/tmp",
    "路径：/Users/test-user/video.mov",
    "路径：/Users/test-user/video.mov",
    "路径：%2FUsers%2F木泽%2Fvideo.mov",
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "路径%25EF%25BC%259A%252FUsers%252F%25E6%259C%25A8%25E6%25B3%25BD%252Fvideo.mov",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("projects unsafe internal capability text to fixed mobile-safe values: %s", async (unsafePath) => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope, { unsafeCapabilityLabel: unsafePath }),
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(200);
    expect(response.body.review.styleSummary.capability).toEqual({
      mode: "provider",
      label: "移动审片：已配置模型服务 A/B，节奏/转场",
      version: 1,
      provider: "configured-provider",
    });
    expect(response.body.review.styleSummary.capability.version).not.toBe(930_001);
    expect(JSON.stringify(response.body)).not.toContain("930001");
    expect(JSON.stringify(response.body)).not.toContain(unsafePath);
  }, 120_000);

  it.each([
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fprovider.json",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("does not echo an unsafe internal provider when the internal label is ordinary: %s", async (unsafeProvider) => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope, { unsafeCapabilityProvider: unsafeProvider }),
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(200);
    expect(response.body.review.styleSummary.capability).toEqual({
      mode: "provider",
      label: "移动审片：已配置模型服务 A/B，节奏/转场",
      version: 1,
      provider: "configured-provider",
    });
    expect(response.body.review.styleSummary.capability.version).not.toBe(930_001);
    expect(JSON.stringify(response.body)).not.toContain("930001");
    expect(JSON.stringify(response.body)).not.toContain(unsafeProvider);
  }, 120_000);

  it.each([
    "/Volumes/private/draft.mov",
    "C:/private/draft.mov",
    "\\\\server\\share\\draft.mov",
    "file:///Volumes/private/draft.mov",
    "%5C%5Cserver%5Cshare%5Cdraft.mov",
    "draft at /private/draft.mov",
    "路径：/Users/test-user/draft.mov",
    "路径：/Users/test-user/draft.mov",
    "路径%EF%BC%9A%2FUsers%2F%E6%9C%A8%E6%B3%BD%2Fdraft.mov",
    "路径/木泽/video.mov",
    "%E8%B7%AF%E5%BE%84%2F%E6%9C%A8%E6%B3%BD%2Fvideo.mov",
    "%25E8%25B7%25AF%25E5%25BE%2584%252F%25E6%259C%A8%25E6%25B3%25BD%252Fvideo.mov",
  ])("fails closed for coordinated unsafe draft and media identities: %s", async (unsafePath) => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope, { unsafeDraftId: unsafePath }),
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-mobile-review-incomplete" });
    expect(JSON.stringify(response.body)).not.toContain(unsafePath);
  }, 120_000);

  it.each([
    ["invalidated", { invalidated: true }, 409, "reference-mobile-review-invalidated"],
    ["incomplete", { incomplete: true }, 409, "reference-mobile-review-incomplete"],
    ["ambiguous", { ambiguous: true }, 409, "reference-mobile-review-ambiguous"],
    ["cross-session", { crossSession: true }, 404, "reference-mobile-review-not-found"],
  ] as const)("fails closed for %s mobile workflow state", async (_label, options, status, error) => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope, options),
    }));
    const session = await openSession(api);

    const response = await jsonGet(api, "/api/studio/reference-mobile-review", session.cookie);

    expect(response.status).toBe(status);
    expect(response.body).toEqual({ error });
  }, 120_000);

  it("does not return an invalidated execution report as a reviewable UI source", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope, { invalidated: true }),
    }));
    const session = await openSession(api);

    const response = await jsonGet(
      api,
      "/api/studio/reference-execution-reports/report-mobile",
      session.cookie,
    );

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-execution-report-not-current" });
  }, 120_000);

  it("does not return a reviewable historical report when an injected runtime omits the authorization wrapper", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => {
        const current = mobileWorkflowSnapshot(scope);
        const historical = {
          ...current.executionReports[0]!,
          id: "report-historical-reviewable",
          version: 1,
        };
        return { ...current, executionReports: [...current.executionReports, historical] };
      },
    }));
    const session = await openSession(api);

    const response = await jsonGet(
      api,
      "/api/studio/reference-execution-reports/report-historical-reviewable",
      session.cookie,
    );

    expect(response.status).toBe(409);
    expect(response.body).toEqual({ error: "reference-execution-report-not-current" });
  }, 120_000);

  it("keeps the mobile read behind loopback, same-origin, session, and empty-body gates", async () => {
    const { api } = await createApi(runtimeFactory({
      snapshot: (scope) => mobileWorkflowSnapshot(scope),
    }));
    const session = await openSession(api);

    const wrongHost = await api.get("/api/studio/reference-mobile-review")
      .set("host", "outside.example")
      .set("origin", "http://outside.example")
      .set("cookie", session.cookie);
    const wrongOrigin = await api.get("/api/studio/reference-mobile-review")
      .set("host", "127.0.0.1")
      .set("origin", "https://outside.example")
      .set("cookie", session.cookie);
    const noSession = await api.get("/api/studio/reference-mobile-review")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN);
    const bodyScope = await api.get("/api/studio/reference-mobile-review")
      .set("host", "127.0.0.1")
      .set("origin", ORIGIN)
      .set("cookie", session.cookie)
      .send({ customerId: session.customerId });

    expect(wrongHost.body).toEqual({ error: "studio-loopback-host-required" });
    expect(wrongOrigin.body).toEqual({ error: "studio-same-origin-required" });
    expect(noSession.body).toEqual({ error: "studio-session-token-required" });
    expect(bodyScope.status).toBe(403);
    expect(bodyScope.body).toEqual({ error: "studio-read-body-forbidden" });
  }, 120_000);

  it("maps validation, stable conflicts, and unavailable verified evidence without leaking detail", async () => {
    const { api } = await createApi(runtimeFactory({
      registerHistoricalError: "reference-operation-key-conflict:/private/reference/store.json",
      analysisError: "reference-analysis-receipts-not-found:/private/model/output.json",
    }));
    const session = await openSession(api);

    const invalid = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: "asset-1",
      sourceKind: "forged-source-kind",
      operationKey: "invalid_source_kind",
    }, session.cookie);
    const conflict = await jsonPost(api, "/api/studio/reference-samples/from-history", {
      assetId: "asset-1",
      sourceKind: "self-owned",
      operationKey: "conflict_1",
    }, session.cookie);
    const unavailable = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: "sample-1",
      operationKey: "analysis_unavailable",
    }, session.cookie);

    expect(invalid.status).toBe(400);
    expect(invalid.body).toEqual({ error: "reference-source-kind-invalid" });
    expect(conflict.status).toBe(409);
    expect(conflict.body).toEqual({ error: "reference-operation-key-conflict" });
    expect(unavailable.status).toBe(422);
    expect(unavailable.body).toEqual({ error: "reference-analysis-receipts-not-found" });
    expect(JSON.stringify([invalid.body, conflict.body, unavailable.body])).not.toContain("/private/");
  }, 120_000);

  it.each([
    ["reference-analysis-temporarily-unavailable", 503],
    ["reference-analysis-manual-review-required", 422],
    ["reference-receipt-real-local-evidence-required", 422],
    ["reference-receipt-binding-mismatch", 409],
    ["reference-render-required", 409],
    ["reference-analysis-final-attempt-layer-invalid", 500],
    ["reference-analysis-final-attempt-not-succeeded", 500],
    ["reference-analysis-scene-not-succeeded", 500],
    ["reference-scene-evidence-invalid", 500],
    ["reference-transition-evidence-invalid", 500],
    ["reference-audio-evidence-invalid", 500],
    ["reference-transcript-evidence-invalid", 500],
    ["reference-vision-evidence-invalid", 500],
    ["reference-bounded-evidence-flag-invalid", 500],
    ["reference-evidence-millisecond-invalid", 500],
  ] as const)("maps reference runtime error %s to HTTP %i", async (error, status) => {
    const { api } = await createApi(runtimeFactory({ analysisError: `${error}:/private/model/output.json` }));
    const session = await openSession(api);

    const response = await jsonPost(api, "/api/studio/reference-analyses", {
      referenceSampleId: "sample-1",
      operationKey: `runtime_error_${status}`,
    }, session.cookie);

    expect(response.status).toBe(status);
    expect(response.body).toEqual({ error });
    expect(JSON.stringify(response.body)).not.toContain("/private/");
  }, 120_000);
});

async function commitSampleInvalidation(
  store: JsonReferenceSampleStore,
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
  sample: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>["samples"][number],
  scope: { actorId: string; customerId: string; sessionId: string },
  operationKey: string,
): Promise<void> {
  const affectedSamples = [{
    id: sample.id,
    version: sample.version,
    sourceDigest: sample.sourceDigest,
  }];
  const invalidatedSample = {
    ...sample,
    version: sample.version + 1,
    status: "invalidated" as const,
  };
  const invalidatedSamples = [{
    id: invalidatedSample.id,
    version: invalidatedSample.version,
    sourceDigest: invalidatedSample.sourceDigest,
  }];
  const reason = "asset-permission-withdrawn";
  const intent = {
    method: "invalidateForAsset" as const,
    assetId: sample.sourceAssetId,
    reason,
    affectedSamples,
    affectedMappings: [],
  };
  const result = {
    kind: "invalidation" as const,
    assetId: sample.sourceAssetId,
    reason,
    affectedSamples,
    invalidatedSamples,
    affectedMappings: [],
    invalidatedMappings: [],
  };
  const operation = {
    ...referenceEditorOperationMetadata(scope, "invalidateForAsset", operationKey, intent),
    result,
    resultDigest: createHash("sha256").update(canonicalJsonForTest(result)).digest("hex"),
  } as ReferenceEditorOperation;
  const { digest: _digest, ...payload } = snapshot;
  const next = {
    ...payload,
    samples: [...snapshot.samples, invalidatedSample],
    editorOperations: [...snapshot.editorOperations, operation],
  };
  await store.commit({ ...next, digest: referenceWorkflowDigest(next) }, snapshot.digest);
}

function canonicalJsonForTest(value: unknown): string {
  return JSON.stringify(canonicalizeForTest(value));
}

function canonicalizeForTest(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalizeForTest);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, child]) => [key, canonicalizeForTest(child)]));
}

function runtimeFactory(state: Readonly<{
  imports?: Array<Record<string, unknown>>;
  calls?: Array<{ method: string; values: unknown[] }>;
  actionLedger?: Map<string, { intent: string; result: unknown }>;
  withdrawn?: { value: boolean };
  registerHistoricalError?: string;
  analysisError?: string;
  draft?: Readonly<{ id: string; sessionId: string; outputPath: string; status: string }>;
  snapshot?: (scope: StudioReferenceRuntimeFactory extends (input: infer T) => unknown
    ? T extends { scope: infer S } ? S : never
    : never) => unknown;
}> = {}): StudioReferenceRuntimeFactory {
  return async ({ scope }) => {
    const sampleService: StudioReferenceRuntime["sampleService"] = {
      importDedicated: async (receivedScope, input) => {
        state.imports?.push({ scope: receivedScope, ...input });
        return {
          asset: { id: "asset-imported" },
          sample: { id: "reference-sample-imported", sourceKind: input.sourceKind },
        } as never;
      },
      registerHistorical: async (receivedScope, input) => {
        if (state.registerHistoricalError) throw new Error(state.registerHistoricalError);
        state.calls?.push({ method: "registerHistorical", values: [receivedScope, input] });
        return { asset: { id: input.assetId }, sample: { id: "sample-history" } } as never;
      },
      listSamples: async (receivedScope) => {
        if (receivedScope !== scope) throw new Error("test-runtime-scope-object-replaced");
        return [{ id: "sample-listed" }] as never;
      },
    };
    const editor: StudioReferenceRuntime["editor"] = {
      declareRights: async (...values) => record(state, "declareRights", values, { id: "rights-1" }),
      analyze: async (...values) => {
        if (state.analysisError) throw new Error(state.analysisError);
        return record(state, "analyze", values, { id: "analysis-1" });
      },
      confirmStyle: async (...values) => record(state, "confirmStyle", values, { id: "selection-1" }),
      createMappingPlan: async (...values) => record(state, "createMappingPlan", values, { id: "mapping-1" }),
      confirmMappingPlan: async (...values) => record(state, "confirmMappingPlan", values, { id: "mapping-1", status: "confirmed" }),
      render: async (...values) => record(state, "render", values, { id: "report-1", status: "reviewable" }),
      ...actionEditor(state),
    };
    const store: StudioReferenceRuntime["store"] = {
      snapshot: async () => state.snapshot
        ? state.snapshot(scope) as never
        : state.actionLedger
          ? mobileWorkflowSnapshot(scope) as never
          : ({
              executionReports: [{ id: "report-1", status: "reviewable" }],
              drafts: state.draft ? [{
                id: state.draft.id,
                customerId: scope.customerId,
                sessionId: state.draft.sessionId,
                version: 1,
                outputPath: state.draft.outputPath,
                outputDigest: createHash("sha256").update("reference-draft-media").digest("hex"),
                status: state.draft.status,
              }] : [],
            }) as never,
    };
    return { sampleService, editor, store, prepareAnalysis: async () => undefined };
  };
}

function actionEditor(state: Readonly<{
  calls?: Array<{ method: string; values: unknown[] }>;
  actionLedger?: Map<string, { intent: string; result: unknown }>;
  withdrawn?: { value: boolean };
}>): Pick<StudioReferenceRuntime["editor"], "reviseWithPrompt" | "approveDraft" | "invalidateForAsset"> {
  return {
    reviseWithPrompt: async (...values) => referenceAction(state, "reviseWithPrompt", values, {
      id: "report-revised",
      version: 10,
      status: "reviewable",
    }),
    approveDraft: async (...values) => referenceAction(state, "approveDraft", values, {
      id: "approval-reference",
      status: "active",
      publishClaimed: false,
    }),
    invalidateForAsset: async (...values) => {
      state.calls?.push({ method: "invalidateForAsset", values });
      if (state.withdrawn) state.withdrawn.value = true;
    },
  };
}

function referenceAction(
  state: Readonly<{
    calls?: Array<{ method: string; values: unknown[] }>;
    actionLedger?: Map<string, { intent: string; result: unknown }>;
    withdrawn?: { value: boolean };
  }>,
  method: "reviseWithPrompt" | "approveDraft",
  values: unknown[],
  result: unknown,
) {
  state.calls?.push({ method, values });
  if (state.withdrawn?.value) throw new Error("reference-workflow-asset-withdrawn");
  const operationKey = String(values[2]);
  const ledgerKey = `${method}:${operationKey}`;
  const intent = JSON.stringify(values[1]);
  const existing = state.actionLedger?.get(ledgerKey);
  if (existing) {
    if (existing.intent !== intent) throw new Error("reference-operation-key-conflict");
    return existing.result as never;
  }
  state.actionLedger?.set(ledgerKey, { intent, result });
  return result as never;
}

function mobileWorkflowSnapshot(
  scopeValue: { customerId: string; sessionId: string },
  options: Readonly<{
    invalidated?: boolean;
    incomplete?: boolean;
    ambiguous?: boolean;
    crossSession?: boolean;
    includeUnsafePrivateFields?: boolean;
    unsafeCapabilityLabel?: string;
    unsafeCapabilityProvider?: string;
    unsafeDraftId?: string;
  }> = {},
) {
  const scope = options.crossSession
    ? { customerId: scopeValue.customerId, sessionId: "another-session" }
    : scopeValue;
  const sha = (value: string) => createHash("sha256").update(value).digest("hex");
  const createdAt = "2026-09-19T08:00:00.000Z";
  const sample = {
    id: "sample-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 2,
    sourceAssetId: "asset-reference", sourceDigest: sha("reference"),
    sourceKind: "external-style-only", collectionMode: "dedicated-upload",
    candidateScope: "reference-only", status: options.invalidated ? "invalidated" : "ready",
    viewPermission: "allowed", usePermission: "review-required", createdAt,
  } as const;
  const sampleBinding = {
    id: sample.id, version: sample.version, customerId: sample.customerId, sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId, sourceDigest: sample.sourceDigest,
    sourceKind: sample.sourceKind, collectionMode: sample.collectionMode, candidateScope: sample.candidateScope,
  };
  const rights = {
    id: "rights-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 3,
    contentDigest: sha("rights"), sample: sampleBinding, sourceKind: "external-style-only",
    reusableElements: [], actorId: "actor-mobile", createdAt,
  } as const;
  const rightsBinding = {
    id: rights.id, version: rights.version, customerId: rights.customerId, sessionId: rights.sessionId,
    referenceSampleId: sample.id, referenceSampleVersion: sample.version,
    referenceSampleDigest: sample.sourceDigest, sourceKind: rights.sourceKind,
    reusableElements: [], contentDigest: rights.contentDigest,
  };
  const analysis = {
    id: "analysis-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 4,
    contentDigest: sha("analysis"), bindings: { sample: sampleBinding, rights: rightsBinding },
    rules: [
      { id: "rule-rhythm", kind: "rhythm" },
      { id: "rule-transition", kind: "transition" },
      { id: "rule-narrative", kind: "narrative" },
      { id: "rule-audio", kind: "audio" },
    ],
    warnings: ["reference-audio-evidence-missing"], status: "degraded",
    capability: options.unsafeCapabilityLabel || options.unsafeCapabilityProvider ? {
      mode: "provider", label: options.unsafeCapabilityLabel ?? "configured model",
      version: 930_001, provider: options.unsafeCapabilityProvider ?? options.unsafeCapabilityLabel!,
    } : {
      mode: "offline-fallback", label: "离线/模拟分析", version: 830_001, provider: null,
    },
    createdAt,
  } as const;
  const analysisBinding = {
    id: analysis.id, version: analysis.version, customerId: analysis.customerId,
    sessionId: analysis.sessionId, contentDigest: analysis.contentDigest,
  };
  const confirmation = { receiptId: "receipt-mobile", actorId: "actor-mobile", confirmedAt: createdAt };
  const selection = {
    id: "selection-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 5,
    contentDigest: sha("selection"),
    bindings: { sample: sampleBinding, rights: rightsBinding, analysis: analysisBinding },
    selectedRuleIds: analysis.rules.map((rule) => rule.id), actorId: "actor-mobile",
    confirmationReceiptId: confirmation.receiptId, confirmedAt: createdAt, status: "confirmed", createdAt,
  } as const;
  const selectionBinding = {
    id: selection.id, version: selection.version, customerId: selection.customerId,
    sessionId: selection.sessionId, contentDigest: selection.contentDigest, status: "confirmed",
    confirmation,
  };
  const permission = {
    id: "permission-mobile", version: 1, epoch: 1, customerId: scope.customerId,
    sessionId: scope.sessionId, sourceAssetId: "asset-customer", sourceDigest: sha("customer-source"),
  };
  const mapping = {
    id: "mapping-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 6,
    contentDigest: sha("mapping"), permissionSetDigest: sha("permissions"),
    bindings: { sample: sampleBinding, rights: rightsBinding, analysis: analysisBinding, styleSelection: selectionBinding },
    mappings: [{
      id: "mapping-row-mobile",
      permission: { ...permission, view: "allowed", use: "allowed" },
    }],
    status: "confirmed", confirmedByActorId: "actor-mobile",
    confirmationReceiptId: "mapping-receipt-mobile", confirmedAt: createdAt, createdAt,
  } as const;
  const mappingBinding = {
    id: mapping.id, version: mapping.version, customerId: mapping.customerId,
    sessionId: mapping.sessionId, contentDigest: mapping.contentDigest,
    permissionSetDigest: mapping.permissionSetDigest, permissions: [permission], status: "confirmed",
    confirmation: { ...confirmation, receiptId: "mapping-receipt-mobile" },
  };
  const bindings = {
    sample: sampleBinding, rights: rightsBinding, analysis: analysisBinding,
    styleSelection: selectionBinding, mappingPlan: mappingBinding,
  };
  const kinds = ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const;
  const artifacts = kinds.map((kind, index) => ({
    kind, variantPlanId: `variant-${kind}`, variantPlanVersion: 7,
    draftId: index === 0 && options.unsafeDraftId ? options.unsafeDraftId : `draft-${kind}`,
    draftVersion: 8, outputDigest: sha(`output-${index}`),
  }));
  const section = (status: "matched" | "substituted" | "unachievable", name: string) => ({
    status, summaryCode: `reference.${name}.${status}`, evidenceRefs: [`evidence-${name}`],
  });
  const report = {
    id: "report-mobile", customerId: scope.customerId, sessionId: scope.sessionId, version: 9,
    bindings, artifacts, rhythm: section("matched", "rhythm"),
    transitions: section("substituted", "transition"), narrative: section("matched", "narrative"),
    audio: section("unachievable", "audio"),
    mappingCounts: { matched: 2, substituted: 1, unachievable: 1 },
    platformChanges: [
      { kind: "xiaohongshu", changeCodes: ["shorter-opening"] },
      { kind: "douyin", changeCodes: ["tighten-cut-boundaries"] },
      { kind: "wechat-channels", changeCodes: ["longer-context"] },
    ],
    status: options.invalidated ? "invalidated" : "reviewable", publishClaimed: false, createdAt,
  } as const;
  const variantPlans = artifacts.map((artifact) => ({
    id: artifact.variantPlanId, customerId: scope.customerId, sessionId: scope.sessionId,
    version: artifact.variantPlanVersion, kind: artifact.kind, bindings,
    status: options.invalidated ? "invalidated" : "planned",
  }));
  const drafts = artifacts.map((artifact) => ({
    id: artifact.draftId, customerId: scope.customerId, sessionId: scope.sessionId,
    version: artifact.draftVersion,
    bindings: {
      mappingPlan: mappingBinding,
      variantPlan: {
        id: artifact.variantPlanId, version: artifact.variantPlanVersion,
        customerId: scope.customerId, sessionId: scope.sessionId, kind: artifact.kind,
      },
    },
    outputDigest: artifact.outputDigest,
    outputPath: options.includeUnsafePrivateFields ? `/Users/test-user/private/${artifact.kind}.mp4` : `/tmp/${artifact.kind}.mp4`,
    originalBytes: options.includeUnsafePrivateFields ? "private-bytes" : undefined,
    externalTranscript: options.includeUnsafePrivateFields ? "private transcript" : undefined,
    rawModelOutput: options.includeUnsafePrivateFields ? { hidden: true } : undefined,
    status: options.invalidated ? "invalidated" : "reviewable",
  }));
  return {
    samples: [sample], rightsDeclarations: [rights], analyses: [analysis], styleSelections: [selection],
    mappingPlans: options.ambiguous ? [mapping, {
      ...mapping,
      id: "mapping-mobile-second",
      contentDigest: sha("mapping-second"),
    }] : [mapping],
    variantPlans,
    drafts: options.incomplete ? drafts.slice(0, 2) : drafts,
    executionReports: [report],
    approvals: [], mobileReviews: [], editorOperations: [], digest: sha("snapshot"),
  };
}

function stubEditor(): StudioReferenceRuntime["editor"] {
  return {
    declareRights: async () => ({ id: "rights" }) as never,
    analyze: async () => ({ id: "analysis" }) as never,
    confirmStyle: async () => ({ id: "selection" }) as never,
    createMappingPlan: async () => ({ id: "mapping" }) as never,
    confirmMappingPlan: async () => ({ id: "mapping", status: "confirmed" }) as never,
    render: async () => ({ id: "report" }) as never,
    reviseWithPrompt: async () => ({ id: "report-revised" }) as never,
    approveDraft: async () => ({ id: "approval-reference" }) as never,
    invalidateForAsset: async () => undefined,
  };
}

function record(
  state: Readonly<{ calls?: Array<{ method: string; values: unknown[] }> }>,
  method: string,
  values: unknown[],
  result: unknown,
) {
  state.calls?.push({ method, values });
  return result as never;
}

async function createApi(
  referenceRuntimeFactory: StudioReferenceRuntimeFactory,
  uploadLimits?: Readonly<{ maxUploadBytes?: number; maxDurationMs?: number }>,
  referenceMediaTools?: Readonly<{ ffprobePath: string; ffmpegPath: string }>,
  referenceMediaProcessLimits?: Readonly<{ timeoutMs?: number; maxOutputBytes?: number }>,
  referenceStagingHooks?: Readonly<{
    afterDirectoryCreate?: () => Promise<void>;
    removeDirectory?: (directory: string) => Promise<void>;
  }>,
) {
  const root = await makeRoot("studio-reference-api-");
  const server = createStudioServer({
    rootDir: root,
    customers: [
      { id: "customer-a", label: "合成客户 A" },
      { id: "customer-b", label: "合成客户 B" },
    ],
    now: () => "2026-09-19T08:00:00.000Z",
    referenceRuntimeFactory,
    referenceUploadLimits: uploadLimits,
    referenceMediaTools,
    referenceMediaProcessLimits,
    referenceStagingHooks,
  });
  servers.add(server);
  return { api: request(server), root, server };
}

async function createDefaultApi(options: Readonly<{ modelWorker: StudioModelWorker }>) {
  const root = await makeRoot("studio-reference-default-api-");
  const server = createStudioServer({
    rootDir: root,
    customers: [
      { id: "customer-a", label: "合成客户 A" },
      { id: "customer-b", label: "合成客户 B" },
    ],
    now: () => "2026-09-19T08:00:00.000Z",
    modelWorker: options.modelWorker,
  });
  servers.add(server);
  return { api: request(server), root, server };
}

async function openSession(api: ReturnType<typeof request>, customerId = "customer-a") {
  const response = await api.post("/api/studio/session")
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .send({ customerId })
    .expect(201);
  const setCookie = response.headers["set-cookie"];
  const cookie = (Array.isArray(setCookie) ? setCookie[0] : setCookie)?.split(";")[0];
  expect(cookie).toMatch(/^studio_session=/);
  expect(Array.isArray(setCookie) ? setCookie[0] : setCookie).toContain("HttpOnly");
  return { ...response.body.session, cookie } as {
    token: string;
    actorId: string;
    customerId: string;
    sessionId: string;
    cookie: string;
  };
}

function multipart(api: ReturnType<typeof request>, cookie: string) {
  return api.post("/api/studio/reference-samples/import")
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("cookie", cookie);
}

function jsonPost(
  api: ReturnType<typeof request>,
  path: string,
  body: Record<string, unknown>,
  cookie: string,
) {
  return api.post(path)
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("cookie", cookie)
    .send(body);
}

function jsonGet(api: ReturnType<typeof request>, path: string, cookie: string) {
  return api.get(path)
    .set("host", "127.0.0.1")
    .set("origin", ORIGIN)
    .set("cookie", cookie);
}

async function makeFixture(): Promise<string> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const root = await makeRoot("studio-reference-fixture-");
  const path = join(root, "fixture.mp4");
  await execFileAsync(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=25:d=0.24",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=0.24",
    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", path,
  ], { timeout: 30_000 });
  return path;
}

async function makeRoot(prefix: string): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), prefix));
  roots.push(root);
  return root;
}

async function listReferenceStagingFiles(root: string): Promise<string[]> {
  const files: string[] = [];
  async function visit(path: string, insideStaging: boolean): Promise<void> {
    const entries = await readdir(path, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const child = join(path, entry.name);
      const inStaging = insideStaging || entry.name === "reference-upload-staging";
      if (entry.isDirectory()) await visit(child, inStaging);
      else if (inStaging) files.push(child);
    }
  }
  await visit(root, false);
  return files.sort();
}

async function readImportAudits(root: string): Promise<Array<Record<string, any>>> {
  const paths: string[] = [];
  async function visit(path: string): Promise<void> {
    const entries = await readdir(path, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.name === "reference-import-failures.jsonl") paths.push(child);
    }
  }
  await visit(root);
  const rows: Array<Record<string, any>> = [];
  for (const path of paths.sort()) {
    const text = await readFile(path, "utf8");
    rows.push(...text.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line)));
  }
  return rows;
}

async function abortMultipartRequest(port: number, cookie: string, index: number): Promise<void> {
  const boundary = `reference-abort-${index}`;
  const prefix = [
    `--${boundary}\r\nContent-Disposition: form-data; name="operationKey"\r\n\r\nabort_${index}\r\n`,
    `--${boundary}\r\nContent-Disposition: form-data; name="sourceKind"\r\n\r\nexternal-style-only\r\n`,
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="abort.mp4"\r\n`,
    "Content-Type: video/mp4\r\n\r\n",
  ].join("");
  await new Promise<void>((resolve) => {
    const outgoing = httpRequest({
      host: "127.0.0.1",
      port,
      method: "POST",
      path: "/api/studio/reference-samples/import",
      headers: {
        host: `127.0.0.1:${port}`,
        origin: `http://127.0.0.1:${port}`,
        cookie,
        "content-type": `multipart/form-data; boundary=${boundary}`,
        "content-length": String(Buffer.byteLength(prefix) + 1024 * 1024),
      },
    });
    outgoing.on("error", () => resolve());
    outgoing.write(prefix);
    outgoing.write(Buffer.alloc(32 * 1024, index), () => {
      setTimeout(() => outgoing.destroy(), 10);
    });
    outgoing.once("close", resolve);
  });
}

function openAbortableMultipartRequest(port: number, cookie: string, index: number) {
  const boundary = `reference-controlled-abort-${index}`;
  const prefix = [
    `--${boundary}\r\nContent-Disposition: form-data; name="operationKey"\r\n\r\ncontrolled_abort_${index}\r\n`,
    `--${boundary}\r\nContent-Disposition: form-data; name="sourceKind"\r\n\r\nexternal-style-only\r\n`,
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="abort.mp4"\r\n`,
    "Content-Type: video/mp4\r\n\r\n",
  ].join("");
  let resolveClosed!: () => void;
  const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });
  const outgoing = httpRequest({
    host: "127.0.0.1",
    port,
    method: "POST",
    path: "/api/studio/reference-samples/import",
    headers: {
      host: `127.0.0.1:${port}`,
      origin: `http://127.0.0.1:${port}`,
      cookie,
      "content-type": `multipart/form-data; boundary=${boundary}`,
      "content-length": String(Buffer.byteLength(prefix) + 1024 * 1024),
    },
  });
  outgoing.on("error", resolveClosed);
  outgoing.once("close", resolveClosed);
  outgoing.write(prefix);
  outgoing.write(Buffer.alloc(32 * 1024, index));
  return { abort: () => outgoing.destroy(), closed };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function findJsonValue(root: string, containing: string, key: string): Promise<unknown> {
  async function visit(path: string): Promise<unknown> {
    const entries = await readdir(path, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) {
        const found = await visit(child);
        if (found !== undefined) return found;
      } else if (entry.name.endsWith(".json")) {
        const text = await readFile(child, "utf8");
        if (!text.includes(containing)) continue;
        const value = JSON.parse(text);
        if (key in value) return value[key];
      }
    }
    return undefined;
  }
  return visit(root);
}

async function findAnalysisState(root: string, sourceAssetId: string): Promise<any> {
  async function visit(path: string): Promise<any> {
    for (const entry of await readdir(path, { withFileTypes: true }).catch(() => [])) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) {
        const found = await visit(child);
        if (found) return found;
      } else if (entry.name === "analysis-state.json") {
        const value = JSON.parse(await readFile(child, "utf8"));
        if (value.jobs?.some((job: any) => job.sourceAssetId === sourceAssetId)) return value;
      }
    }
    return null;
  }
  const state = await visit(root);
  if (!state) throw new Error("analysis-state-not-found");
  return state;
}

async function findAssetRecord(root: string, assetId: string): Promise<{ path: string; value: any }> {
  async function visit(path: string): Promise<{ path: string; value: any } | null> {
    for (const entry of await readdir(path, { withFileTypes: true }).catch(() => [])) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) {
        const found = await visit(child);
        if (found) return found;
      } else if (entry.name === "asset.json") {
        const value = JSON.parse(await readFile(child, "utf8"));
        if (value.id === assetId) return { path: child, value };
      }
    }
    return null;
  }
  const record = await visit(root);
  if (!record) throw new Error("asset-record-not-found");
  return record;
}

type ReferenceWorkerLayer = "technical" | "scene" | "transcript" | "embedding" | "vision";

function referenceWorker(
  statusForLayer: (
    layer: ReferenceWorkerLayer,
  ) => "succeeded" | "failed-retryable" | "failed-manual-review" = () => "succeeded",
  qualitySignalsForLayer: (
    layer: ReferenceWorkerLayer,
    signals: Record<string, unknown>,
  ) => Record<string, unknown> = (_layer, signals) => signals,
) {
  const worker: StudioModelWorker = {
    run: vi.fn(async (job) => {
      const defaultQualitySignals: Record<string, unknown> = job.layer === "scene"
        ? {
            scenes: [{
              startMs: job.sourceStartMs,
              endMs: job.sourceEndMs,
              representativeFrameMs: Math.floor((job.sourceStartMs + job.sourceEndMs) / 2),
              representativeFrameDigest: createHash("sha256").update("frame").digest("hex"),
            }],
            transitionEvidence: [],
            beatMarkers: [],
          }
        : job.layer === "transcript"
          ? { spans: [] }
          : { visualObservations: [] };
      const qualitySignals = qualitySignalsForLayer(job.layer, defaultQualitySignals);
      const { requestedAt: _requestedAt, ...binding } = job;
      const status = statusForLayer(job.layer);
      return {
        ...binding,
        adapterName: `local-${job.layer}`,
        modelName: `mock-${job.layer}`,
        modelVersion: "v1",
        checkpointDigest: "c".repeat(64),
        inputDigest: job.sourceDigest,
        promptVersion: "reference-v1",
        observations: [],
        suggestedTags: [],
        qualitySignals,
        confidence: status === "succeeded" ? 0.9 : 0,
        warnings: [],
        startedAt: "2026-09-19T08:00:00.000Z",
        completedAt: "2026-09-19T08:00:00.100Z",
        runtimeMs: 100,
        status,
        failure: status === "succeeded" ? null : { code: `fixture-${status}` },
        synthetic: status !== "succeeded",
        providerMode: status === "succeeded" ? "local" as const : "offline-fallback" as const,
      };
    }),
    health: vi.fn(async () => ({
      mode: "offline-fallback" as const,
      installed: false as const,
      modelName: null,
      checkpointDigest: null,
    })),
  };
  return worker as StudioModelWorker & { run: ReturnType<typeof vi.fn> };
}

import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import ffmpegPath from "ffmpeg-static";
import { chromium, type Locator } from "playwright-core";
import { defaultStudioBrowserExecutable } from "./studio-browser-pilot";
import { createStudioServer, listenStudioServer } from "./studio-server";

// Isolated, synthetic, local browser check. Does not invoke a model or the real external editor.
const output = await mkdtemp(join(resolve(process.argv[2] || "/tmp"), "studio-simple-flow-"));
const fixture = join(output, "制作过程测试.mp4");
assert(ffmpegPath);
await promisify(execFile)(ffmpegPath, ["-hide_banner", "-loglevel", "error",
  "-f", "lavfi", "-i", "color=c=blue:s=160x240:r=10:d=2",
  "-c:v", "libx264", "-pix_fmt", "yuv420p", fixture], { timeout: 30_000 });
const launches: string[] = [];
const sourceFiles = [
  "packages/mobile-agent/src/customer-material-library.ts",
  "packages/mobile-agent/src/private-content-studio.ts",
  "packages/review-app/src/studio-server.ts",
  "packages/review-app/src/studio-material-import.ts",
  "packages/review-app/public/studio.html",
  "packages/review-app/public/studio.js",
  "packages/review-app/public/studio.css",
  "packages/review-app/src/run-studio-simple-flow-check.ts",
];
async function sourceDigests() {
  return Object.fromEntries(await Promise.all(sourceFiles.map(async (path) => [
    path, createHash("sha256").update(await readFile(resolve(path))).digest("hex"),
  ])));
}
const testedSourceDigests = await sourceDigests();
const server = createStudioServer({ rootDir: join(output, "runtime"),
  now: () => "2026-10-10T08:00:00.000Z",
  findJianyingApp: async () => null,
  openExternalEditTarget: async (input) => { launches.push(input.kind); },
});
await listenStudioServer(server, 0, "127.0.0.1");
const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
const browser = await chromium.launch({ executablePath: defaultStudioBrowserExecutable(), headless: true });
const context = await browser.newContext({ viewport: { width: 1100, height: 820 } });
const page = await context.newPage();
const errors: string[] = [];
const requests: string[] = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("request", (request) => requests.push(new URL(request.url()).pathname));
await context.route("**/*", (route) => new URL(route.request().url()).origin === origin
  ? route.continue() : route.abort());
async function play(video: Locator) {
  // Install observers before play: a short video may finish before Node polls.
  // Keep callbacks inline so tsx does not inject a Node-only naming helper.
  const evidence = await video.evaluate(async (element) => {
    const media = element as HTMLVideoElement;
    const advanced = new Promise<{ time: number; readyState: number; error: number | null }>((resolvePlay, rejectPlay) => {
      media.addEventListener("timeupdate", () => {
        if (media.currentTime > 0.1 && media.readyState >= 3 && !media.error) {
          resolvePlay({ time: media.currentTime, readyState: media.readyState, error: null });
        }
      });
      media.addEventListener("ended", () => {
        if (media.currentTime > 0.1 && media.readyState >= 3 && !media.error) {
          resolvePlay({ time: media.currentTime, readyState: media.readyState, error: null });
        } else rejectPlay(new Error("Video ended without playback evidence"));
      }, { once: true });
      media.addEventListener("error", () => rejectPlay(new Error("Video playback failed")), { once: true });
      setTimeout(() => rejectPlay(new Error("Video playback did not advance")), 30_000);
    });
    media.muted = true;
    media.currentTime = 0;
    await media.play();
    const result = await advanced;
    media.pause();
    return result;
  });
  assert(evidence && evidence.time > 0.1 && evidence.readyState >= 3 && evidence.error === null,
    "Playback must produce actual time/decode evidence");
  return evidence;
}
try {
  await page.goto(`${origin}/studio`);
  await page.waitForFunction(() => document.querySelector("#create-material-summary")?.textContent?.includes("4 段"));
  await page.screenshot({ path: join(output, "desktop-create.png"), fullPage: true });
  await page.getByRole("button", { name: "上传视频", exact: true }).click();
  await page.locator('#material-upload-form input[name="file"]').setInputFiles(fixture);
  const uploadedResponse = page.waitForResponse((response) => response.url().endsWith("/api/studio/materials/import"));
  await page.locator('#material-upload-form button[type="submit"]').click();
  const uploaded = await (await uploadedResponse).json();
  assert(uploaded.inventory.items.some((item: { materialCardId: string }) => item.materialCardId === uploaded.material.id));
  assert.equal(uploaded.material.displayName, "制作过程测试.mp4");
  await page.waitForFunction(() => document.querySelector("#studio-status")?.textContent?.includes("已上传"));
  const uploadedCard = page.locator(".material-card").filter({ hasText: "制作过程测试.mp4" });
  assert.equal(await uploadedCard.count(), 1);
  const uploadPlayback = await play(uploadedCard.locator("video"));
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileLibraryLayout = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
  assert(mobileLibraryLayout.content <= mobileLibraryLayout.viewport);
  await page.screenshot({ path: join(output, "mobile-library.png"), fullPage: true });
  await page.locator("#continue-from-library").click();
  await page.locator('#studio-quick-demand-form textarea').fill("展示这枚戒指的细节");
  await page.locator('#studio-quick-demand-form button[type="submit"]').click();
  await page.waitForFunction(() => document.querySelector(".studio-shell")?.getAttribute("data-current-step") === "brief");
  assert.equal(await page.locator(".studio-hero").isVisible(), false);
  await page.setViewportSize({ width: 1100, height: 820 });
  const desktopConfirmLayout = await page.locator('[data-studio-step="brief"]').evaluate((step) => {
    const content = step.querySelector<HTMLElement>(".step-content");
    const stepRect = step.getBoundingClientRect();
    const contentRect = content?.getBoundingClientRect();
    return {
      gridColumns: getComputedStyle(step).gridTemplateColumns,
      stepWidth: stepRect.width,
      contentWidth: contentRect?.width ?? 0,
    };
  });
  assert(desktopConfirmLayout.contentWidth >= desktopConfirmLayout.stepWidth * 0.85,
    "Desktop confirmation content must use the main panel width");
  await page.screenshot({ path: join(output, "desktop-confirm.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const materialPlanPlayback = await play(page.locator("#brief video").first());
  await page.screenshot({ path: join(output, "mobile-confirm.png"), fullPage: true });
  const draftsResponse = page.waitForResponse((response) => response.url().endsWith("/api/studio/drafts"), { timeout: 120_000 });
  await page.locator("#confirm-brief").click();
  const draftsV1 = (await (await draftsResponse).json()).drafts;
  await page.waitForFunction(() => document.querySelectorAll("#drafts .draft-card").length === 2);
  const draftPlayback = await play(page.locator("#drafts .draft-card video").first());
  await page.screenshot({ path: join(output, "mobile-review.png"), fullPage: true });

  await page.locator(".draft-card .revision-options summary").first().click();
  await page.locator("[data-open-external-edit]").first().click();
  const handoffForm = page.locator("[data-external-edit-form]").first();
  await handoffForm.locator('[name="revisionInstruction"]').fill("缩短开头，保留产品近景");
  await handoffForm.locator('[name="platformVariant"]').selectOption("xiaohongshu");
  const handoffResponse = page.waitForResponse((response) => response.url().endsWith("/prepare"), { timeout: 120_000 });
  await handoffForm.locator('button[type="submit"]').click();
  const handoff = await (await handoffResponse).json();
  assert.equal(handoff.mode, "file-package");
  assert.equal(handoff.handoff.platformVariant, "xiaohongshu");
  await page.waitForFunction(() => document.querySelector("[data-external-progress]")?.textContent?.includes("手动导入"));

  await page.locator(".customer-feedback-drawer summary").first().click();
  let feedback = page.locator("[data-customer-feedback]").first();
  await feedback.locator('[name="satisfaction"]').selectOption("partially-satisfied");
  await feedback.locator('input[value="style"]').check();
  await feedback.locator('[name="comment"]').fill("请让设计师观点更突出");
  await feedback.locator('button[type="submit"]').click();
  await page.waitForFunction(() => document.querySelector(".studio-shell")?.getAttribute("data-current-step") === "revision");
  await page.locator("#confirm-revision-proposal").click();
  await page.waitForFunction(() => !(document.querySelector("#make-revision-drafts") as HTMLButtonElement)?.disabled);
  const revisionResponse = page.waitForResponse((response) => response.url().endsWith("/api/studio/drafts"), { timeout: 120_000 });
  await page.locator("#make-revision-drafts").click();
  const draftsV2 = (await (await revisionResponse).json()).drafts;
  assert.equal(draftsV2.length, 2);
  assert(draftsV2.every((draft: { version: number }) => draft.version === 2));
  await page.waitForFunction(() => document.querySelector(".studio-shell")?.getAttribute("data-current-step") === "drafts");
  await play(page.locator("#drafts .draft-card video").first());
  await page.locator(".customer-feedback-drawer summary").first().click();
  feedback = page.locator("[data-customer-feedback]").first();
  await feedback.locator('[name="satisfaction"]').selectOption("satisfied");
  await feedback.locator('button[type="submit"]').click();
  await page.waitForFunction(() => !(document.querySelector("[data-adopt-draft]") as HTMLButtonElement)?.disabled);
  const adoptionResponse = page.waitForResponse((response) => response.url().endsWith("/api/studio/feedback"));
  await page.locator("[data-adopt-draft]").first().click();
  const adoption = await (await adoptionResponse).json();
  assert.equal(adoption.feedback.publishClaimed, false);
  const historyResponse = await context.request.post(`${origin}/api/studio/history`, {
    headers: { origin }, data: {},
  });
  assert(historyResponse.ok());
  const history = (await historyResponse.json()).history;
  for (const original of draftsV1) {
    const retained = history.drafts.find((draft: { id: string }) => draft.id === original.id);
    assert(retained, "V1 must remain in history after V2 adoption");
    assert.equal(retained.version, 1);
    assert.equal(retained.outputDigest, original.outputDigest);
    const mediaResponse = await context.request.get(`${origin}${retained.mediaUrl}`, { headers: { origin } });
    assert(mediaResponse.ok(), `Original V1 must remain playable: ${mediaResponse.status()}`);
    assert.equal(createHash("sha256").update(await mediaResponse.body()).digest("hex"), original.outputDigest);
  }
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#create-material-summary")?.textContent?.includes("5 段"));
  await page.locator('.studio-bottom-nav [data-studio-tab="library"]').click();
  assert.equal(await page.locator(".material-card").filter({ hasText: "制作过程测试.mp4" }).count(), 1);
  await page.setViewportSize({ width: 150, height: 844 });
  const narrowLayout = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
  assert(narrowLayout.content <= narrowLayout.viewport);
  assert.equal(requests.filter((path) => path.includes("/analysis/jobs") || path.includes("publish")).length, 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(await sourceDigests(), testedSourceDigests, "Source files changed while the check was running");
  const baseGitSha = (await promisify(execFile)("git", ["rev-parse", "HEAD"])).stdout.trim();
  const report = { passed: true, syntheticOnly: true, realModelExecuted: false, automaticPublishing: false,
    baseGitSha, testedSourceDigests, workingTreeNotRelease: true,
    jianyingPhysicalRoundtrip: false, uploadName: uploaded.material.displayName, inventoryCount: uploaded.inventory.items.length,
    uploadPlayback, materialPlanPlayback, draftPlayback, mobileLibraryLayout, desktopConfirmLayout, narrowLayout,
    normalDraftsV1: draftsV1.length, normalDraftsV2: draftsV2.length, handoffMode: handoff.mode,
    handoffPlatform: handoff.handoff.platformVariant, originalVersionPreserved: true, pageErrors: errors,
    originalDraftEvidence: draftsV1.map((draft: { id: string; outputDigest: string }) => ({
      id: draft.id, sha256: draft.outputDigest, historyRetained: true, originalMediaVerified: true,
    })),
    outputDirectory: output };
  await writeFile(join(output, "report.json"), JSON.stringify(report, null, 2));
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} catch (error) {
  await page.screenshot({ path: join(output, "failure.png"), fullPage: true });
  process.stderr.write(`Browser evidence: ${output}\n`);
  throw error;
} finally {
  await browser.close();
  await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
}

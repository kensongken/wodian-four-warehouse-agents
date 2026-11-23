import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import type { IncomingMessage } from "node:http";
import { basename, extname, join } from "node:path";

import {
  MaterialCardSchema,
  StudioContentTypeSchema,
  type MaterialCard,
  type StudioContentType,
} from "@wearable/contracts";
import {
  OFFLINE_STUDIO_CAPABILITY,
  type CustomerMaterialLibrary,
  type StudioScope,
} from "@wearable/mobile-agent";
import Busboy from "busboy";

export const STUDIO_MATERIAL_UPLOAD_MAX_BYTES = 268_435_456;
export const STUDIO_MATERIAL_UPLOAD_MAX_DURATION_SECONDS = 3_600;
const PROCESS_TIMEOUT_MS = 120_000;
const MAX_PROCESS_OUTPUT_BYTES = 64 * 1024;
const SAFE_OPERATION_KEY = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;
const ALLOWED_EXTENSIONS = new Set([".mp4", ".mov"]);

export type StudioMaterialImportApi = Readonly<{
  importMultipart(request: IncomingMessage, scope: StudioScope): Promise<MaterialCard>;
  importHardwareUpload(
    upload: StudioMaterialUpload,
    scope: StudioScope,
    hardware: Readonly<{ pairingId: string; deviceId: string; deviceName: string }>,
  ): Promise<MaterialCard>;
}>;

export type StudioMaterialUpload = Readonly<{
  bytes: Buffer;
  fileName: string;
  operationKey: string;
  contentType: StudioContentType;
  captureMetadata: HardwareCaptureMetadata | null;
}>;

export type HardwareCaptureMetadata = Readonly<{
  captureId: string;
  deviceId: string;
  deviceName: string;
  capturedAt: string;
  simulated: boolean;
}>;

type MediaFacts = Readonly<{
  durationSeconds: number;
  width: number;
  height: number;
}>;

export function createStudioMaterialImportApi(input: Readonly<{
  stagingRoot: string;
  library: CustomerMaterialLibrary;
  ffprobePath: string;
  ffmpegPath: string;
  now?: () => string;
}>): StudioMaterialImportApi {
  if (!input.stagingRoot.trim()) throw new Error("material-upload-staging-root-required");
  if (!input.ffprobePath.trim() || !input.ffmpegPath.trim()) {
    throw new Error("material-upload-media-tools-unavailable");
  }
  const now = input.now ?? (() => new Date().toISOString());
  const importUpload = async (
    upload: StudioMaterialUpload,
    scope: StudioScope,
    hardware: Readonly<{ pairingId: string; deviceId: string; deviceName: string }> | null,
  ): Promise<MaterialCard> => {
      await mkdir(input.stagingRoot, { recursive: true, mode: 0o700 });
      const stagingDirectory = await mkdtemp(join(input.stagingRoot, "material-"));
      const stagedPath = join(stagingDirectory, `upload${extname(upload.fileName).toLowerCase()}`);
      try {
        if (!hardware && upload.captureMetadata) throw new Error("hardware-capture-metadata-forbidden");
        await writeFile(stagedPath, upload.bytes, { flag: "wx", mode: 0o600 });
        const facts = await verifyMedia(stagedPath, input.ffprobePath, input.ffmpegPath);
        const sourceKind = hardware ? "hardware" : "upload";
        const requestedAt = now();
        const declaredDevice = upload.captureMetadata;
        const requestId = `material-${sourceKind}:${scope.sessionId}:${upload.operationKey}`;
        const asset = hardware
          ? await input.library.importCaptureEnvelope(scope.actorId, scope.customerId, {
              id: declaredDevice?.captureId ?? `${hardware.pairingId}:${upload.operationKey}`,
              customerId: scope.customerId,
              sessionId: scope.sessionId,
              version: 1,
              requestId,
              mediaKind: "video",
              fileName: upload.fileName,
              bytes: new Uint8Array(upload.bytes),
              binding: { customerId: scope.customerId, sessionId: scope.sessionId, verified: true },
              adapter: {
                name: declaredDevice
                  ? `${declaredDevice.simulated ? "simulated" : "declared"}-hardware-via-iphone:${declaredDevice.deviceId}`
                  : `iphone-gateway:${hardware.deviceId}`,
                version: "local-v1",
              },
              capturedAt: declaredDevice?.capturedAt ?? requestedAt,
              viewPermission: "allowed",
              usePermission: "allowed",
            })
          : await input.library.importOriginal(scope.actorId, scope.customerId, {
              requestId,
              bytes: upload.bytes,
              mediaKind: "video",
              fileName: upload.fileName,
              capturedAt: null,
              sessionId: scope.sessionId,
              viewPermission: "allowed",
              usePermission: "allowed",
            });
        // Inventory uses receipt time; a device clock must not hide a received video.
        const cardCreatedAt = asset.importedAt;
        const effectiveDeviceName = declaredDevice?.deviceName ?? hardware?.deviceName ?? null;
        const sourceEndMs = Math.round(facts.durationSeconds * 1_000);
        const card = MaterialCardSchema.parse({
          id: `card_${sourceKind}_${createHash("sha256").update(`${scope.sessionId}\0${asset.id}\0${upload.operationKey}`).digest("hex")}`,
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          version: 1,
          sourceAssetId: asset.id,
          sourceDigest: asset.digest,
          sourceStartMs: 0,
          sourceEndMs,
          contentType: upload.contentType,
          technicalUsable: true,
          technicalFlags: [
            `本机检查：${facts.width}x${facts.height}`,
            `完整时长：${facts.durationSeconds.toFixed(3)}秒`,
            ...(effectiveDeviceName ? [`接入设备：${effectiveDeviceName}${declaredDevice?.simulated ? "（模拟）" : ""}`] : []),
            ...(declaredDevice?.simulated ? ["接入方式：模拟硬件经 iPhone"] : []),
          ],
          observations: [`${hardware ? "硬件接入" : "客户上传"}素材：${upload.fileName}`],
          confirmedFacts: [],
          pendingFacts: declaredDevice?.simulated ? ["设备真实性：本地模拟，待真实硬件验证"] : [],
          capability: OFFLINE_STUDIO_CAPABILITY,
          createdAt: cardCreatedAt,
        });
        return await input.library.appendMaterialCard(scope.actorId, scope.customerId, card);
      } finally {
        await rm(stagingDirectory, { recursive: true, force: true });
      }
  };
  return {
    importMultipart: async (request, scope) => importUpload(await readStudioMaterialMultipart(request), scope, null),
    importHardwareUpload: (upload, scope, hardware) => importUpload(upload, scope, hardware),
  };
}

export async function readStudioMaterialMultipart(request: IncomingMessage): Promise<StudioMaterialUpload> {
  const contentTypeHeader = request.headers["content-type"] ?? "";
  if (!/^multipart\/form-data\s*;/i.test(contentTypeHeader)) {
    throw new Error("material-upload-multipart-content-type-required");
  }
  let parser: ReturnType<typeof Busboy>;
  try {
    parser = Busboy({
      headers: request.headers,
      defParamCharset: "utf8",
      limits: { files: 1, fields: 3, parts: 5, fieldSize: 1_024, fileSize: STUDIO_MATERIAL_UPLOAD_MAX_BYTES + 1 },
    });
  } catch {
    throw new Error("material-upload-multipart-invalid");
  }
  const fields = new Map<string, string>();
  const chunks: Buffer[] = [];
  let fileName = "";
  let fileCount = 0;
  let byteLength = 0;
  let failure: Error | null = null;
  const fail = (code: string) => { failure ??= new Error(code); };
  parser.on("field", (name, value, info) => {
    if ((name !== "contentType" && name !== "operationKey" && name !== "captureMetadata") || fields.has(name)) {
      fail("material-upload-field-invalid");
      return;
    }
    if (info.nameTruncated || info.valueTruncated) fail("material-upload-field-invalid");
    else fields.set(name, value);
  });
  parser.on("file", (name, stream, info) => {
    fileCount += 1;
    if (name !== "file" || fileCount !== 1) {
      fail("material-upload-file-count-invalid");
      stream.resume();
      return;
    }
    const normalized = basename(normalizeUploadedFileName(info.filename).normalize("NFC")).trim();
    if (!normalized || normalized.length > 255 || !ALLOWED_EXTENSIONS.has(extname(normalized).toLowerCase())) {
      fail("material-upload-file-name-invalid");
    } else {
      fileName = normalized;
    }
    stream.on("limit", () => fail("material-upload-too-large"));
    stream.on("data", (chunk: Buffer | string) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      byteLength += bytes.byteLength;
      if (byteLength > STUDIO_MATERIAL_UPLOAD_MAX_BYTES) fail("material-upload-too-large");
      else chunks.push(bytes);
    });
  });
  parser.once("filesLimit", () => fail("material-upload-file-count-invalid"));
  parser.once("fieldsLimit", () => fail("material-upload-field-invalid"));
  parser.once("partsLimit", () => fail("material-upload-field-invalid"));
  await new Promise<void>((resolve, reject) => {
    parser.once("error", () => reject(new Error("material-upload-multipart-invalid")));
    parser.once("close", resolve);
    request.once("aborted", () => reject(new Error("material-upload-request-aborted")));
    request.pipe(parser);
  });
  if (failure) throw failure;
  if (fileCount !== 1 || !fileName || byteLength < 1) throw new Error("material-upload-file-required");
  const operationKey = String(fields.get("operationKey") ?? "").trim();
  if (!SAFE_OPERATION_KEY.test(operationKey)) throw new Error("material-upload-operation-key-invalid");
  const contentType = StudioContentTypeSchema.safeParse(fields.get("contentType"));
  if (!contentType.success) throw new Error("material-upload-content-type-invalid");
  return {
    bytes: Buffer.concat(chunks),
    fileName,
    operationKey,
    contentType: contentType.data,
    captureMetadata: parseHardwareCaptureMetadata(fields.get("captureMetadata")),
  };
}

function parseHardwareCaptureMetadata(value: string | undefined): HardwareCaptureMetadata | null {
  if (value === undefined) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("hardware-capture-metadata-invalid");
  }
  if (!isRecord(parsed) || Object.keys(parsed).sort().join(",") !==
      "captureId,capturedAt,deviceId,deviceName,simulated") {
    throw new Error("hardware-capture-metadata-invalid");
  }
  const captureId = boundedMetadataText(parsed.captureId, 160);
  const deviceId = boundedMetadataText(parsed.deviceId, 120);
  const deviceName = boundedMetadataText(parsed.deviceName, 80);
  const capturedAt = boundedMetadataText(parsed.capturedAt, 64);
  if (!Number.isFinite(Date.parse(capturedAt)) || typeof parsed.simulated !== "boolean") {
    throw new Error("hardware-capture-metadata-invalid");
  }
  return { captureId, deviceId, deviceName, capturedAt, simulated: parsed.simulated };
}

function boundedMetadataText(value: unknown, maxLength: number): string {
  const normalized = typeof value === "string" ? value.normalize("NFC").trim() : "";
  if (!normalized || normalized.length > maxLength) throw new Error("hardware-capture-metadata-invalid");
  return normalized;
}

/**
 * Some multipart clients send UTF-8 filename bytes through the legacy latin1
 * header path. Repair only the unmistakable case: the original has no CJK,
 * the latin1-to-UTF8 candidate gains CJK, and decoding introduces no loss.
 */
export function normalizeUploadedFileName(value: string): string {
  const original = String(value || "");
  if (!/[\u0080-\u00ff]/u.test(original) || /[\u3400-\u9fff]/u.test(original)) return original;
  const candidate = Buffer.from(original, "latin1").toString("utf8");
  return /[\u3400-\u9fff]/u.test(candidate) && !candidate.includes("\ufffd") ? candidate : original;
}

async function verifyMedia(path: string, ffprobePath: string, ffmpegPath: string): Promise<MediaFacts> {
  let probe: Record<string, unknown>;
  try {
    const result = await runProcess(ffprobePath, [
      "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_type,width,height,duration", "-of", "json", path,
    ]);
    probe = JSON.parse(result.stdout) as Record<string, unknown>;
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("material-upload-media-tool-")) throw error;
    throw new Error("material-upload-media-invalid");
  }
  const format = isRecord(probe.format) ? probe.format : null;
  const streams = Array.isArray(probe.streams) ? probe.streams : [];
  const video = streams.find((stream) => isRecord(stream) && stream.codec_type === "video");
  const audio = streams.find((stream) => isRecord(stream) && stream.codec_type === "audio");
  const formatNames = typeof format?.format_name === "string" ? new Set(format.format_name.split(",")) : new Set<string>();
  const formatDurationSeconds = Number(format?.duration);
  const videoDurationSeconds = isRecord(video) ? Number(video.duration) : NaN;
  const audioDurationSeconds = isRecord(audio) ? Number(audio.duration) : NaN;
  const durationSeconds = Math.min(
    formatDurationSeconds,
    Number.isFinite(videoDurationSeconds) && videoDurationSeconds > 0
      ? videoDurationSeconds
      : formatDurationSeconds,
    Number.isFinite(audioDurationSeconds) && audioDurationSeconds > 0
      ? audioDurationSeconds
      : formatDurationSeconds,
  );
  const width = isRecord(video) ? Number(video.width) : NaN;
  const height = isRecord(video) ? Number(video.height) : NaN;
  if ((!formatNames.has("mov") && !formatNames.has("mp4")) || !Number.isFinite(formatDurationSeconds) ||
      !Number.isFinite(durationSeconds) ||
      durationSeconds <= 0 || durationSeconds > STUDIO_MATERIAL_UPLOAD_MAX_DURATION_SECONDS ||
      !Number.isInteger(width) || width < 1 || !Number.isInteger(height) || height < 1) {
    throw new Error(durationSeconds > STUDIO_MATERIAL_UPLOAD_MAX_DURATION_SECONDS
      ? "material-upload-duration-too-long"
      : "material-upload-media-invalid");
  }
  try {
    await runProcess(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-xerror", "-i", path,
      "-map", "0:v", "-map", "0:a?", "-f", "null", "-",
    ]);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("material-upload-media-tool-")) throw error;
    throw new Error("material-upload-media-invalid");
  }
  return { durationSeconds, width, height };
}

async function runProcess(command: string, args: readonly string[]): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    let settled = false;
    let pending: Error | null = null;
    let timer: ReturnType<typeof setTimeout>;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve({ stdout: Buffer.concat(stdout).toString("utf8"), stderr: Buffer.concat(stderr).toString("utf8") });
    };
    const capture = (target: Buffer[]) => (chunk: Buffer | string) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      outputBytes += bytes.byteLength;
      if (outputBytes > MAX_PROCESS_OUTPUT_BYTES) {
        pending = new Error("material-upload-media-tool-output-too-large");
        child.kill("SIGKILL");
      } else target.push(bytes);
    };
    child.stdout.on("data", capture(stdout));
    child.stderr.on("data", capture(stderr));
    child.once("error", (error: NodeJS.ErrnoException) => finish(new Error(
      error.code === "ENOENT" ? "material-upload-media-tool-unavailable" : "material-upload-media-tool-failed",
    )));
    child.once("close", (status, signal) => pending
      ? finish(pending)
      : signal !== null || status !== 0
        ? finish(new Error("material-upload-media-rejected"))
        : finish());
    timer = setTimeout(() => {
      pending = new Error("material-upload-media-tool-timeout");
      child.kill("SIGKILL");
    }, PROCESS_TIMEOUT_MS);
    timer.unref();
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";

import QRCode from "qrcode";

import type { StudioScope } from "@wearable/mobile-agent";

const PAIRING_TTL_MS = 5 * 60 * 1_000;
const DEVICE_TTL_MS = 12 * 60 * 60 * 1_000;

export type StudioHardwarePairingView = Readonly<{
  id: string;
  pairingUrl: string;
  qrDataUrl: string;
  expiresAt: string;
  status: "waiting" | "connected" | "expired";
  deviceName: string | null;
}>;

export type StudioHardwareDeviceBinding = Readonly<{
  pairingId: string;
  runtimeToken: string;
  scope: StudioScope;
  deviceId: string;
  deviceName: string;
  expiresAt: string;
}>;

type PairingRecord = {
  id: string;
  codeHash: string;
  runtimeToken: string;
  scope: StudioScope;
  pairingUrl: string;
  qrDataUrl: string;
  expiresAt: string;
  claimedAt: string | null;
  deviceName: string | null;
  deviceId: string | null;
  deviceTokenHash: string | null;
  deviceExpiresAt: string | null;
};

export class StudioHardwarePairingStore {
  private readonly pairings = new Map<string, PairingRecord>();

  constructor(
    private readonly hardwareBaseUrl: string,
    private readonly now: () => string = () => new Date().toISOString(),
  ) {
    const parsed = new URL(hardwareBaseUrl);
    if (parsed.protocol !== "http:" || !parsed.hostname || parsed.username || parsed.password) {
      throw new Error("hardware-base-url-invalid");
    }
  }

  async create(runtimeToken: string, scope: StudioScope): Promise<StudioHardwarePairingView> {
    this.expireOldPairings(runtimeToken);
    const id = `hardware_pairing_${randomUUID()}`;
    const code = randomBytes(24).toString("base64url");
    const pairingUrl = `${this.hardwareBaseUrl.replace(/\/$/u, "")}/hardware/pair?code=${encodeURIComponent(code)}`;
    const record: PairingRecord = {
      id,
      codeHash: digest(code),
      runtimeToken,
      scope: { ...scope },
      pairingUrl,
      qrDataUrl: await QRCode.toDataURL(pairingUrl, { errorCorrectionLevel: "M", margin: 1, width: 320 }),
      expiresAt: new Date(Date.parse(this.now()) + PAIRING_TTL_MS).toISOString(),
      claimedAt: null,
      deviceName: null,
      deviceId: null,
      deviceTokenHash: null,
      deviceExpiresAt: null,
    };
    this.pairings.set(id, record);
    return this.view(record);
  }

  status(runtimeToken: string, pairingId: string): StudioHardwarePairingView {
    const record = this.pairings.get(pairingId);
    if (!record || record.runtimeToken !== runtimeToken) throw new Error("hardware-pairing-not-found");
    return this.view(record);
  }

  claim(code: string, deviceNameValue: string): Readonly<{
    deviceToken: string;
    deviceId: string;
    deviceName: string;
    expiresAt: string;
  }> {
    const codeHash = digest(requireBoundedText(code, "hardware-pairing-code-required", 256));
    const record = [...this.pairings.values()].find((candidate) => safeEqual(candidate.codeHash, codeHash));
    if (!record || this.isPairingExpired(record)) throw new Error("hardware-pairing-code-invalid");
    if (record.claimedAt) throw new Error("hardware-pairing-code-already-used");
    const deviceName = requireBoundedText(deviceNameValue, "hardware-device-name-required", 80);
    const deviceToken = `hardware_${randomBytes(32).toString("base64url")}`;
    const claimedAt = this.now();
    record.claimedAt = claimedAt;
    record.deviceName = deviceName;
    record.deviceId = `iphone_gateway_${randomUUID()}`;
    record.deviceTokenHash = digest(deviceToken);
    record.deviceExpiresAt = new Date(Date.parse(claimedAt) + DEVICE_TTL_MS).toISOString();
    return {
      deviceToken,
      deviceId: record.deviceId,
      deviceName,
      expiresAt: record.deviceExpiresAt,
    };
  }

  authorize(value: string | undefined): StudioHardwareDeviceBinding {
    const token = bearerToken(value);
    const tokenHash = digest(token);
    const record = [...this.pairings.values()].find((candidate) =>
      candidate.deviceTokenHash && safeEqual(candidate.deviceTokenHash, tokenHash));
    if (!record || !record.claimedAt || !record.deviceId || !record.deviceName || !record.deviceExpiresAt) {
      throw new Error("hardware-device-token-invalid");
    }
    if (Date.parse(record.deviceExpiresAt) <= Date.parse(this.now())) {
      throw new Error("hardware-device-token-expired");
    }
    return {
      pairingId: record.id,
      runtimeToken: record.runtimeToken,
      scope: { ...record.scope },
      deviceId: record.deviceId,
      deviceName: record.deviceName,
      expiresAt: record.deviceExpiresAt,
    };
  }

  private view(record: PairingRecord): StudioHardwarePairingView {
    const status = record.claimedAt
      ? "connected"
      : this.isPairingExpired(record)
        ? "expired"
        : "waiting";
    return {
      id: record.id,
      pairingUrl: record.pairingUrl,
      qrDataUrl: record.qrDataUrl,
      expiresAt: record.expiresAt,
      status,
      deviceName: record.deviceName,
    };
  }

  private expireOldPairings(runtimeToken: string): void {
    for (const record of this.pairings.values()) {
      if (record.runtimeToken === runtimeToken && !record.claimedAt) record.expiresAt = this.now();
    }
  }

  private isPairingExpired(record: PairingRecord): boolean {
    return Date.parse(record.expiresAt) <= Date.parse(this.now());
  }
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function safeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left);
  const rightBytes = Buffer.from(right);
  return leftBytes.byteLength === rightBytes.byteLength && timingSafeEqual(leftBytes, rightBytes);
}

function bearerToken(value: string | undefined): string {
  const match = /^Bearer\s+([^\s]+)$/u.exec(value ?? "");
  if (!match) throw new Error("hardware-device-token-required");
  return match[1]!;
}

function requireBoundedText(value: string, code: string, maxLength: number): string {
  const normalized = String(value || "").normalize("NFC").trim();
  if (!normalized || normalized.length > maxLength) throw new Error(code);
  return normalized;
}

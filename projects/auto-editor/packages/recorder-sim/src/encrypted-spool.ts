import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";

type ProtectedSegment = Readonly<{
  iv: Buffer;
  tag: Buffer;
  ciphertext: Buffer;
}>;

/**
 * In-memory simulation of the device's local encrypted spool. It deliberately
 * has no phone, network, or publishing responsibilities.
 */
export class EncryptedSpool {
  private readonly key: Buffer;
  private readonly protectedSegments = new Map<string, ProtectedSegment[]>();

  constructor(keyMaterial: string) {
    this.key = createHash("sha256").update(keyMaterial).digest();
  }

  async queue(captureId: string, segment: Buffer): Promise<void> {
    if (!captureId) {
      throw new Error("capture-id-required");
    }

    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    const ciphertext = Buffer.concat([cipher.update(segment), cipher.final()]);
    const protectedSegment: ProtectedSegment = {
      iv,
      tag: cipher.getAuthTag(),
      ciphertext,
    };

    const existing = this.protectedSegments.get(captureId) ?? [];
    this.protectedSegments.set(captureId, [...existing, protectedSegment]);
  }

  async restore(captureId: string): Promise<Buffer> {
    const segments = this.protectedSegments.get(captureId) ?? [];
    const restored = segments.map(({ iv, tag, ciphertext }) => {
      const decipher = createDecipheriv("aes-256-gcm", this.key, iv);
      decipher.setAuthTag(tag);
      return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    });

    return Buffer.concat(restored);
  }

  async rawStorageContains(plaintext: string): Promise<boolean> {
    return JSON.stringify(
      [...this.protectedSegments.entries()].map(([captureId, segments]) => ({
        captureId,
        segments: segments.map(({ iv, tag, ciphertext }) => ({
          iv: iv.toString("base64"),
          tag: tag.toString("base64"),
          ciphertext: ciphertext.toString("base64"),
        })),
      })),
    ).includes(plaintext);
  }
}

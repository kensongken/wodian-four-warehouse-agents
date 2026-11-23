const encoder = new TextEncoder();

function hex(bytes: Uint8Array): string {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function decodeHex(value: string): Uint8Array<ArrayBuffer> | undefined {
  if (!/^[0-9a-f]{64}$/u.test(value)) return undefined;
  const bytes = new Uint8Array(new ArrayBuffer(32));
  value.match(/.{2}/gu)!.forEach((pair, index) => {
    bytes[index] = Number.parseInt(pair, 16);
  });
  return bytes;
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(value));
  return hex(new Uint8Array(digest));
}

export async function verifyProductImageEngineSignature(input: Readonly<{
  secret: string;
  timestamp: number;
  nowSeconds: number;
  sourceSystem: string;
  tenantId: string;
  method: string;
  path: string;
  body: string;
  suppliedSignature: string;
}>): Promise<boolean> {
  if (
    !Number.isSafeInteger(input.timestamp)
    || Math.abs(input.nowSeconds - input.timestamp) > 300
  ) return false;
  const supplied = decodeHex(input.suppliedSignature);
  if (!supplied) return false;
  const bodySha256 = await sha256Hex(input.body);
  const message = [
    input.timestamp,
    input.sourceSystem,
    input.tenantId,
    input.method.toUpperCase(),
    input.path,
    bodySha256,
  ].join("\n");
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(input.secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify("HMAC", key, supplied, encoder.encode(message));
}

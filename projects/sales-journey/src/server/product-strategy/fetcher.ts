import {
  canonicalSalesStrategyInputSha256,
  SalesStrategyInputV1Schema,
  type SalesStrategyInputV1,
} from "@/domain/product-strategy/input-schema";
import { sha256Hex } from "./signature";

const encoder = new TextEncoder();

export class ProductStrategyFetchError extends Error {
  constructor(
    public readonly code: "unavailable" | "invalid",
    message: string,
  ) {
    super(message);
    this.name = "ProductStrategyFetchError";
  }
}

export interface ProductStrategyInputFetcher {
  fetch(tenantId: string, productCode: string, version: number): Promise<SalesStrategyInputV1>;
}

async function hmacHex(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export class HttpProductStrategyInputFetcher implements ProductStrategyInputFetcher {
  constructor(
    private readonly baseUrl: string,
    private readonly signingSecret: string,
    private readonly nowSeconds: () => number,
    private readonly request: typeof fetch = fetch,
  ) {}

  async fetch(tenantId: string, productCode: string, version: number): Promise<SalesStrategyInputV1> {
    const path = `/v0.1/sales-strategy-inputs/${encodeURIComponent(productCode)}`;
    const timestamp = this.nowSeconds();
    const message = [timestamp, "sales", tenantId, "GET", path, await sha256Hex("")].join("\n");
    let response: Response;
    try {
      response = await this.request(`${this.baseUrl.replace(/\/$/u, "")}${path}?version=${version}`, {
        headers: {
          "X-Source-System": "sales",
          "X-Tenant-Id": tenantId,
          "X-Signature-Timestamp": String(timestamp),
          "X-Signature": await hmacHex(this.signingSecret, message),
        },
      });
    } catch (error) {
      throw new ProductStrategyFetchError("unavailable", `Strategy source unavailable: ${String(error)}`);
    }
    if (response.status >= 500 || response.status === 429) {
      throw new ProductStrategyFetchError("unavailable", `Strategy source returned ${response.status}.`);
    }
    if (!response.ok) {
      throw new ProductStrategyFetchError("invalid", `Strategy source rejected request with ${response.status}.`);
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ProductStrategyFetchError("invalid", "Strategy source returned invalid JSON.");
    }
    const parsed = SalesStrategyInputV1Schema.safeParse(payload);
    if (!parsed.success) {
      throw new ProductStrategyFetchError("invalid", "Strategy source returned an invalid contract.");
    }
    if (await canonicalSalesStrategyInputSha256(parsed.data) !== parsed.data.content_sha256) {
      throw new ProductStrategyFetchError("invalid", "Strategy source content digest mismatch.");
    }
    return parsed.data;
  }
}

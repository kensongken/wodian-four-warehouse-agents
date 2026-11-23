import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { purgeExpiredVaultAssets } from "@/server/supervision/retention-job";
import { requireVaultStorage, vaultErrorResponse } from "@/server/supervision/vault";

function equalSecret(received: string, expected: string): boolean {
  const receivedBytes = new TextEncoder().encode(received);
  const expectedBytes = new TextEncoder().encode(expected);
  let difference = receivedBytes.length ^ expectedBytes.length;
  const comparisons = Math.max(receivedBytes.length, expectedBytes.length);
  for (let index = 0; index < comparisons; index += 1) {
    difference |= (receivedBytes[index] ?? 0) ^ (expectedBytes[index] ?? 0);
  }
  return difference === 0;
}

export async function POST(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    if (!equalSecret(
      request.headers.get("authorization") ?? "",
      `Bearer ${runtime.retentionSchedulerSecret}`,
    )) {
      return Response.json({ error: "AUTHENTICATION_REQUIRED" }, {
        status: 401,
        headers: { "Cache-Control": "no-store" },
      });
    }
    const result = await purgeExpiredVaultAssets({
      now: runtime.now?.() ?? new Date().toISOString(),
      repository: runtime.repository,
      vault: requireVaultStorage(runtime.vault),
      createId: runtime.createId,
    });
    return Response.json(result, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return vaultErrorResponse(error) ?? supervisionApiErrorResponse(error);
  }
}

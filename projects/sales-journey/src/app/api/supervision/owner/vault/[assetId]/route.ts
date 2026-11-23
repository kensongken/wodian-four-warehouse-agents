import { requireOwnerFromD1 } from "@/server/auth/authorization";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import {
  readOwnerVaultAsset,
  requireVaultStorage,
  vaultErrorResponse,
} from "@/server/supervision/vault";

type RouteContext = Readonly<{
  params: Promise<Readonly<{ assetId: string }>>;
}>;

function responseBody(
  body: Uint8Array<ArrayBuffer> | ReadableStream<Uint8Array<ArrayBuffer>>,
): BodyInit {
  return body instanceof ReadableStream ? body : Uint8Array.from(body).buffer;
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireOwnerFromD1(request.headers, runtime.d1);
    const { assetId } = await context.params;
    const { object } = await readOwnerVaultAsset({
      actor,
      vaultAssetId: assetId,
      repository: runtime.repository,
      vault: requireVaultStorage(runtime.vault),
      now: runtime.now,
      createId: runtime.createId,
    });
    return new Response(responseBody(object.body), {
      headers: {
        "Cache-Control": "no-store, private",
        "Content-Length": String(object.byteSize),
        "Content-Type": object.contentType,
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (error) {
    const response = vaultErrorResponse(error) ?? supervisionApiErrorResponse(error);
    response.headers.set("Cache-Control", "no-store, private");
    return response;
  }
}

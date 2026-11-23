import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import {
  MAX_VAULT_UPLOAD_REQUEST_BYTES,
  requireVaultStorage,
  uploadVaultImages,
  VaultUploadError,
  vaultErrorResponse,
} from "@/server/supervision/vault";

function isUploadFile(value: FormDataEntryValue): value is File {
  return typeof value === "object"
    && value !== null
    && typeof value.name === "string"
    && typeof value.type === "string"
    && typeof value.size === "number"
    && typeof value.arrayBuffer === "function";
}

function uploadForm(form: FormData): { customerId: string; files: File[] } {
  const customerId = form.get("customerId");
  const files = form.getAll("files");
  if (
    typeof customerId !== "string"
    || customerId.trim().length === 0
    || files.some((file) => !isUploadFile(file))
  ) {
    throw new SyntaxError("invalid vault upload form");
  }
  return { customerId: customerId.trim(), files: files as File[] };
}

export async function POST(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    const declaredLength = request.headers.get("content-length");
    if (
      declaredLength !== null
      && (!/^\d+$/u.test(declaredLength)
        || Number(declaredLength) > MAX_VAULT_UPLOAD_REQUEST_BYTES)
    ) {
      throw new VaultUploadError("VAULT_REQUEST_LIMIT_EXCEEDED", 413);
    }
    let form: FormData;
    try {
      form = await request.formData();
    } catch {
      throw new SyntaxError("invalid vault upload form");
    }
    const parsed = uploadForm(form);
    const assets = await uploadVaultImages({
      actor,
      customerId: parsed.customerId,
      files: parsed.files,
      repository: runtime.repository,
      vault: requireVaultStorage(runtime.vault),
      now: runtime.now,
      createId: runtime.createId,
    });
    return Response.json({
      assets: assets.map((asset) => ({
        id: asset.id,
        kind: asset.kind,
        uploadedAt: asset.uploadedAt,
        deleteAfter: asset.deleteAfter,
      })),
    }, {
      status: 201,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    if (error instanceof SyntaxError) {
      return Response.json({ error: "INVALID_VAULT_UPLOAD" }, {
        status: 400,
        headers: { "Cache-Control": "no-store" },
      });
    }
    return vaultErrorResponse(error) ?? supervisionApiErrorResponse(error);
  }
}

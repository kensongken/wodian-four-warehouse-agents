import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";

export const dynamic = "force-dynamic";

function trustedIdentity(request: Request): { id: string; email: string } | undefined {
  const id = request.headers.get("oai-authenticated-user-id")?.trim();
  const email = request.headers.get("oai-authenticated-user-email")?.trim().toLowerCase();
  return id && email ? { id, email } : undefined;
}

/** One-shot initialization for a brand-new, owner-only Site. */
export async function POST(request: Request): Promise<Response> {
  const identity = trustedIdentity(request);
  if (!identity) return Response.json({ error: "AUTHENTICATION_REQUIRED" }, { status: 401 });

  const { d1 } = getSupervisionApiRuntime();
  const existing = await d1.prepare(
    "SELECT id, email, role FROM users ORDER BY created_at ASC LIMIT 1",
  ).first<Record<string, unknown>>();
  if (existing) {
    const isOwner = existing.id === identity.id
      && String(existing.email).toLowerCase() === identity.email
      && existing.role === "owner";
    if (isOwner && request.headers.get("accept")?.includes("text/html")) {
      return Response.redirect(new URL("/owner", request.url), 303);
    }
    return Response.json(
      { initialized: isOwner, alreadyInitialized: true },
      { status: isOwner ? 200 : 409, headers: { "Cache-Control": "no-store" } },
    );
  }

  const tenantId = `tenant_${identity.id}`;
  const result = await d1.prepare(
    `INSERT INTO users (id, tenant_id, email, role, active, version, created_at)
     SELECT ?, ?, ?, 'owner', 1, 1, ?
     WHERE NOT EXISTS (SELECT 1 FROM users)`,
  ).bind(identity.id, tenantId, identity.email, new Date().toISOString()).run();
  if (!result.success) return Response.json({ error: "INITIALIZATION_FAILED" }, { status: 503 });
  if (request.headers.get("accept")?.includes("text/html")) {
    return Response.redirect(new URL("/owner", request.url), 303);
  }
  return Response.json(
    { initialized: true, alreadyInitialized: false },
    { status: 201, headers: { "Cache-Control": "no-store" } },
  );
}

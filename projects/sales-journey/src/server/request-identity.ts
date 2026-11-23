export type RequestIdentity = {
  tenantId: string;
  operatorId: string;
};

const DEMO_REQUEST_IDENTITY: RequestIdentity = {
  tenantId: "demo-tenant",
  operatorId: "demo-operator",
};

/**
 * Local vertical-slice identity. It is deliberately server-owned and does not
 * inspect caller-provided headers, query parameters, or request bodies.
 */
export function resolveRequestIdentity(): RequestIdentity {
  return DEMO_REQUEST_IDENTITY;
}

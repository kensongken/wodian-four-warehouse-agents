import { z } from "zod";

import { sha256Hex } from "@/server/product-strategy/signature";

const Sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/u);
const CandidateFactSchema = z.object({
  value: z.string().min(1).max(64),
  confirmation_status: z.literal("candidate"),
}).strict();

export const SalesStrategyInputV1Schema = z.object({
  contract_version: z.literal("1-sales-strategy-input"),
  tenant_id: z.string().uuid(),
  product_code: z.string().min(1).max(64).regex(/^[A-Za-z0-9_-]+$/u),
  strategy_input_version: z.number().int().min(1),
  product_data_version: z.number().int().min(1),
  source_album_event_id: z.string().uuid(),
  source_material_package_version: z.number().int().min(1),
  source_image_set_sha256: Sha256HexSchema,
  images: z.array(z.object({
    asset_id: z.string().uuid(),
    sha256: Sha256HexSchema,
    mime_type: z.enum(["image/heic", "image/jpeg", "image/png"]),
    role: z.enum(["main_candidate", "gallery", "source_original"]),
  }).strict()).min(1).max(3_000),
  material: CandidateFactSchema,
  form: CandidateFactSchema,
  inventory_status: z.literal("pending"),
  visual_observations: z.array(z.string()).max(100),
  selling_point_candidates: z.array(z.string()).max(100),
  price_status: z.literal("pending"),
  service_rule_summaries: z.array(z.string()).max(100),
  unverified_items: z.array(z.string()).max(100),
  forbidden_expressions: z.array(z.string()).max(100),
  status: z.literal("preview_only"),
  created_at: z.string().datetime({ offset: true }),
  content_sha256: Sha256HexSchema,
  automatic_customer_contact_allowed: z.literal(false),
  automatic_publication_allowed: z.literal(false),
  automatic_wechat_send_allowed: z.literal(false),
  automatic_quote_allowed: z.literal(false),
}).strict();

export type SalesStrategyInputV1 = z.infer<typeof SalesStrategyInputV1Schema>;

function sortedJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedJsonValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortedJsonValue(item)]),
    );
  }
  return value;
}

export async function canonicalSalesStrategyInputSha256(
  input: SalesStrategyInputV1,
): Promise<string> {
  const {
    content_sha256: _contentSha256,
    strategy_input_version: _strategyInputVersion,
    created_at: _createdAt,
    source_album_event_id: _sourceAlbumEventId,
    ...canonicalBody
  } = input;
  return sha256Hex(JSON.stringify(sortedJsonValue(canonicalBody)));
}

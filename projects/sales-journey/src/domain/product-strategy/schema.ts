import { z } from "zod";

const Sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/u);
const ProductCodeSchema = z.string().min(1).max(64).regex(/^[A-Za-z0-9_-]+$/u);

export const SalesStrategyInputUpdatedV1Schema = z.object({
  schema_version: z.literal(1),
  event_id: z.string().uuid(),
  occurred_at: z.string().datetime({ offset: true }),
  tenant_id: z.string().uuid(),
  product_code: ProductCodeSchema,
  strategy_input_version: z.number().int().min(1),
  product_data_version: z.number().int().min(1),
  status: z.literal("preview_only"),
  content_sha256: Sha256HexSchema,
  automatic_customer_contact_allowed: z.literal(false),
  automatic_publication_allowed: z.literal(false),
  automatic_wechat_send_allowed: z.literal(false),
  automatic_quote_allowed: z.literal(false),
}).strict();

export type SalesStrategyInputUpdatedV1 = z.infer<
  typeof SalesStrategyInputUpdatedV1Schema
>;

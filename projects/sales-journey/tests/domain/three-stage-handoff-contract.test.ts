import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

import {
  canonicalSalesStrategyInputSha256,
  SalesStrategyInputV1Schema,
} from "@/domain/product-strategy/input-schema";

it("accepts the sales snapshot derived from the pinned album candidate", async () => {
  const input = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
    process.cwd(),
    "fixtures/integrations/sales-strategy-input.three-stage.synthetic.json",
  ), "utf8")));

  expect(input.source_album_event_id).toBe("fa2ee0c2-d145-5e1b-b304-91a731e145c5");
  expect(input.images[0]?.sha256).toBe("a".repeat(64));
  expect(await canonicalSalesStrategyInputSha256(input)).toBe(input.content_sha256);
  expect(input.status).toBe("preview_only");
});

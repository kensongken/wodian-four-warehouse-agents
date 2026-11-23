import { describe, expect, it } from "vitest";

import { calculateDeleteAfter } from "@/domain/supervision/retention";

describe("supervision vault retention", () => {
  it("expires raw material exactly thirty days after upload", () => {
    expect(calculateDeleteAfter("2026-08-30T00:00:00.000Z"))
      .toBe("2026-09-29T00:00:00.000Z");
  });

  it("rejects an invalid upload timestamp instead of inventing an expiry", () => {
    expect(() => calculateDeleteAfter("not-a-timestamp")).toThrow(RangeError);
  });
});

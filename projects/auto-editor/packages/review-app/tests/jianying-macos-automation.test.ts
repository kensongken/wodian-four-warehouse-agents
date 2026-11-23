import { describe, expect, it } from "vitest";

import { isVerifiedJianyingVersion } from "../src/jianying-macos-automation";

describe("Jianying macOS automation version gate", () => {
  it("allows only the two manually verified Jianying versions", () => {
    expect(isVerifiedJianyingVersion("11.5.3")).toBe(true);
    expect(isVerifiedJianyingVersion("11.6.0")).toBe(true);
    expect(isVerifiedJianyingVersion("11.6.1")).toBe(false);
    expect(isVerifiedJianyingVersion("12.0.0")).toBe(false);
  });
});

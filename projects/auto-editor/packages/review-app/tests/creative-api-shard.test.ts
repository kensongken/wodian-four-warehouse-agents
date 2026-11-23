import { describe, expect, it } from "vitest";

import {
  parseCreativeApiShard,
  shardForCreativeApiTest,
} from "./helpers/creative-api-shard";

describe("creative API deterministic sharding", () => {
  it("assigns every test title to exactly one stable shard", () => {
    const titles = [
      "ordinary receipt lifecycle",
      "renders a four-video handoff",
      "rejects withdrawn source authority",
      "projects multilingual identities",
    ];
    const first = titles.map((title) => shardForCreativeApiTest(title, 4));
    const second = titles.map((title) => shardForCreativeApiTest(title, 4));

    expect(first).toEqual(second);
    expect(first.every((index) => index >= 1 && index <= 4)).toBe(true);
    for (const [titleIndex, title] of titles.entries()) {
      expect([1, 2, 3, 4].filter((index) =>
        shardForCreativeApiTest(title, 4) === index,
      )).toEqual([first[titleIndex]]);
    }
  });

  it("uses all tests without an environment shard and rejects malformed shard requests", () => {
    expect(parseCreativeApiShard(undefined)).toBeNull();
    expect(parseCreativeApiShard("2/4")).toEqual({ index: 2, count: 4 });
    expect(() => parseCreativeApiShard("0/4")).toThrow("creative-api-shard-invalid");
    expect(() => parseCreativeApiShard("2/0")).toThrow("creative-api-shard-invalid");
    expect(() => parseCreativeApiShard("five")).toThrow("creative-api-shard-invalid");
  });
});

export type CreativeApiShard = Readonly<{ index: number; count: number }>;

export function parseCreativeApiShard(value: string | undefined): CreativeApiShard | null {
  if (value === undefined || value === "") return null;
  const match = /^(\d+)\/(\d+)$/.exec(value);
  if (!match) throw new Error("creative-api-shard-invalid");
  const index = Number(match[1]);
  const count = Number(match[2]);
  if (!Number.isSafeInteger(index) || !Number.isSafeInteger(count) ||
      count < 1 || index < 1 || index > count) {
    throw new Error("creative-api-shard-invalid");
  }
  return { index, count };
}

export function shardForCreativeApiTest(title: string, count: number): number {
  if (!Number.isSafeInteger(count) || count < 1) throw new Error("creative-api-shard-invalid");
  let hash = 2_166_136_261;
  for (const byte of new TextEncoder().encode(title)) {
    hash ^= byte;
    hash = Math.imul(hash, 16_777_619) >>> 0;
  }
  return (hash % count) + 1;
}

export function creativeApiTestBelongsToShard(
  title: string,
  shard: CreativeApiShard | null,
): boolean {
  return shard === null || shardForCreativeApiTest(title, shard.count) === shard.index;
}

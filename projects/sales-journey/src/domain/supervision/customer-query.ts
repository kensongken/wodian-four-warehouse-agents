export function normalizeOwnerCustomerQuery(value: string): string {
  return value.trim().toLocaleLowerCase("zh-CN").replace(/\s+/gu, "");
}

export function ownerCustomerMatches(alias: string, number: string, query: string): boolean {
  return normalizeOwnerCustomerQuery(`${alias}${number}`).includes(normalizeOwnerCustomerQuery(query));
}

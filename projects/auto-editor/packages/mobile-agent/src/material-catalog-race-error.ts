const materialCatalogRaceBrand = Symbol("material-catalog-race");

class MaterialCatalogRaceError extends Error {
  readonly code = "material-catalog-race";
  readonly [materialCatalogRaceBrand] = true;

  constructor() {
    super("material-no-longer-eligible");
    this.name = "MaterialCatalogRaceError";
  }
}

export function createMaterialCatalogRaceError(): Error {
  return new MaterialCatalogRaceError();
}

export function hasMaterialCatalogRaceCause(value: unknown): boolean {
  const visited = new WeakSet<object>();
  let current = value;
  while (isObjectLike(current)) {
    try {
      if (visited.has(current)) return false;
      visited.add(current);
      if (current instanceof MaterialCatalogRaceError &&
        current.code === "material-catalog-race" &&
        current[materialCatalogRaceBrand] === true) {
        return true;
      }
      current = Reflect.get(current, "cause");
    } catch {
      return false;
    }
  }
  return false;
}

function isObjectLike(value: unknown): value is object {
  return (typeof value === "object" && value !== null) || typeof value === "function";
}

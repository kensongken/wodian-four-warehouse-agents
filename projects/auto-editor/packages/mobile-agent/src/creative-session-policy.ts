import type { CreativeSession, CreativeSessionStage } from "@wearable/contracts";

export const allowedCreativeSessionTransitions: Record<
  CreativeSessionStage,
  readonly CreativeSessionStage[]
> = {
  draft_intent: ["awaiting_material_consent", "paused", "cancelled", "invalidated"],
  awaiting_material_consent: ["reviewing_materials", "materials_declined", "paused", "cancelled", "invalidated"],
  reviewing_materials: ["awaiting_script_confirmation", "paused", "cancelled", "invalidated"],
  materials_declined: ["awaiting_script_confirmation", "paused", "cancelled", "invalidated"],
  awaiting_script_confirmation: ["ready_to_dispatch", "paused", "cancelled", "invalidated"],
  ready_to_dispatch: ["shooting", "awaiting_script_confirmation", "paused", "cancelled", "invalidated"],
  shooting: ["ingesting", "awaiting_script_confirmation", "paused", "cancelled", "invalidated"],
  ingesting: ["editorial_review", "paused", "cancelled", "invalidated"],
  editorial_review: ["completed", "paused", "cancelled", "invalidated"],
  completed: [],
  paused: ["draft_intent", "awaiting_material_consent", "reviewing_materials", "materials_declined", "awaiting_script_confirmation", "ready_to_dispatch", "shooting", "ingesting", "editorial_review", "cancelled", "invalidated"],
  cancelled: [],
  invalidated: [],
};

export function isCreativeSessionTransitionAllowed(
  from: CreativeSessionStage,
  to: CreativeSessionStage,
): boolean {
  return allowedCreativeSessionTransitions[from].includes(to);
}

export function hasConsistentCreativeSessionStatus(session: CreativeSession): boolean {
  return (session.status === "invalidated") === (session.stage === "invalidated");
}

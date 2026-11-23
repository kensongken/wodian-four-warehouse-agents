import { z } from "zod";
import { SALES_SCENARIOS, SALES_STAGES } from "@/domain/decision-card/types";

const NonBlank = z.string().trim().min(1);

export const SalesJourneyContextSchema = z.object({
  tenantId: NonBlank,
  journeyId: NonBlank,
  customerId: NonBlank,
  subjectId: NonBlank,
  /** Legacy UI-only field. Server-derived journey snapshots deliberately omit it. */
  operatorId: NonBlank.optional(),
  scenario: z.enum(SALES_SCENARIOS).optional(),
  approvedEvidenceIds: z.array(NonBlank.max(120)).max(20).optional(),
  currentStage: z.enum(SALES_STAGES),
  customerCategory: NonBlank,
  confirmedNeeds: z.array(NonBlank).max(20),
  pendingQuestions: z.array(NonBlank).max(20),
  commitments: z.array(NonBlank).max(20),
  openTasks: z.array(NonBlank).max(20),
  recentConversation: z.array(z.object({
    speaker: z.enum(["customer", "operator"]),
    text: NonBlank.max(1000),
    occurredAt: z.string().datetime().optional(),
  })).max(12),
});

export const JourneyWriteBackSchema = z.object({
  journeyId: NonBlank,
  tenantId: NonBlank,
  operatorId: NonBlank,
  proposedStage: z.enum(SALES_STAGES),
  nextAction: NonBlank,
  confirmedByOperator: z.literal(true),
});

export type SalesJourneyContext = z.infer<typeof SalesJourneyContextSchema>;
export type JourneyWriteBack = z.infer<typeof JourneyWriteBackSchema>;

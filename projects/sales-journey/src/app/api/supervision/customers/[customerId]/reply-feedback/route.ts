import { ReplyFeedbackAcceptedResponseSchema } from "@/domain/reply-feedback/schema";
import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";

type RouteContext = Readonly<{
  params: Promise<Readonly<{ customerId: string }>>;
}>;

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "INVALID_SUPERVISION_REQUEST" }, { status: 400 });
    }
    const result = await createCustomerService(runtime).saveReplyFeedback(
      actor,
      (await context.params).customerId,
      body,
    );
    return Response.json(ReplyFeedbackAcceptedResponseSchema.parse({
      accepted: true,
      outcome: result.outcome,
      privacyStatus: result.record.privacyStatus,
      privacyReasonCodes: result.record.privacyReasonCodes,
    }), { status: result.outcome === "created" ? 201 : 200 });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}

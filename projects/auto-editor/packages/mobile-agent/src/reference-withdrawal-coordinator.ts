import type {
  CustomerMaterialLibrary,
  MaterialWithdrawalEvent,
} from "./customer-material-library";
import type { ReferenceGuidedEditor } from "./reference-guided-editor";
import type { StudioScope } from "./studio-capabilities";

export class ReferenceWithdrawalCoordinator {
  constructor(private readonly dependencies: Readonly<{
    library: Pick<CustomerMaterialLibrary, "withdraw">;
    editor: Pick<ReferenceGuidedEditor, "invalidateForAsset">;
  }>) {}

  async withdraw(
    scope: StudioScope,
    assetId: string,
    operationKey: string,
  ): Promise<MaterialWithdrawalEvent> {
    // This ordering is deliberate. Once the customer-level withdrawal is
    // durable, every read must fail closed even if the session ledger needs a
    // retry to append its audit tombstones. Never compensate the withdrawal.
    const event = await this.dependencies.library.withdraw(
      scope.actorId,
      scope.customerId,
      assetId,
      operationKey,
    );
    await this.dependencies.editor.invalidateForAsset(scope, {
      assetId,
      reason: "asset-permission-withdrawn",
    }, operationKey);
    return event;
  }
}

"""Read-only API route for a customer's portable style evidence export."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.services.exports import StyleExportService

router = APIRouter(tags=["style-export"])
SessionDependency = Annotated[Session, Depends(get_session)]


@router.get("/v1/style-export")
def get_style_export(
    customer_id: CustomerId, session: SessionDependency
) -> dict[str, Any]:
    """Return only this customer's stable, safe style history."""
    return StyleExportService(session).build(customer_id)

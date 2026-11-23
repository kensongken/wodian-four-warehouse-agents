"""Shared request dependencies for customer-scoped API routes."""

from collections.abc import Generator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from opc_style.db import SessionLocal
from opc_style.domain.background_generator import BackgroundGenerator
from opc_style.domain.product_foreground import ProductForegroundExtractor
from opc_style.services.foreground_benchmark_workbench import PrivateBenchmarkDraftStore


def get_session(request: Request) -> Generator[Session, None, None]:
    """Use an app-owned local-test factory when explicitly configured."""
    factory = getattr(request.app.state, "session_factory", SessionLocal)
    assert isinstance(factory, sessionmaker)
    with factory() as session:
        yield session


CustomerId = Annotated[UUID, Header(alias="X-Customer-Id")]


def get_product_foreground_extractor(
    request: Request,
) -> ProductForegroundExtractor | None:
    """Return the explicitly configured local extractor, never an implicit fallback."""
    return getattr(request.app.state, "product_foreground_extractor", None)


ProductForegroundExtractorDependency = Annotated[
    ProductForegroundExtractor | None, Depends(get_product_foreground_extractor)
]


def get_background_generator(request: Request) -> BackgroundGenerator | None:
    """Return only the explicitly enabled product-blind background adapter."""
    return getattr(request.app.state, "background_generator", None)


BackgroundGeneratorDependency = Annotated[
    BackgroundGenerator | None, Depends(get_background_generator)
]


def get_foreground_benchmark_workbench_store(
    request: Request,
) -> PrivateBenchmarkDraftStore | None:
    """Return only the explicitly enabled repository-external private store."""
    return getattr(request.app.state, "foreground_benchmark_workbench_store", None)


ForegroundBenchmarkWorkbenchStoreDependency = Annotated[
    PrivateBenchmarkDraftStore | None,
    Depends(get_foreground_benchmark_workbench_store),
]

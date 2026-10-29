"""CY2026 ordinary, LUPA, partial-period, late-NOA and outlier payments with explicit inputs."""

from fastapi import APIRouter, HTTPException, Request
import jpype

from bridge.hha_models import HhaRequest, HhaResponse
from bridge.hha_release import release_info

router = APIRouter(prefix="/api/v1/hha", tags=["HHA"])


def readiness(processor) -> dict:
    return {"enabled": processor is not None,
            "ready": processor is not None and jpype.isJVMStarted(), **release_info()}


@router.get("/ready")
def ready(request: Request):
    processor = getattr(request.app.state, "hha_processor", None)
    if not readiness(processor)["ready"]:
        raise HTTPException(503, "HHA pricing is disabled or unavailable.")
    return {"status": "ready", **readiness(processor)}


@router.post("/process", response_model=HhaResponse)
def process(payload: HhaRequest, request: Request):
    processor = getattr(request.app.state, "hha_processor", None)
    if not readiness(processor)["ready"]:
        raise HTTPException(503, "HHA pricing is disabled or unavailable.")
    try:
        return processor.process(payload)
    except Exception:
        # Raw Java, SQL and claim details belong neither in errors nor logs.
        raise HTTPException(503, "HHA pricing failed; retry after checking service availability.") from None

"""HH ordinary, LUPA, partial-period, late-NOA and outlier payments with explicit inputs."""

from fastapi import APIRouter, HTTPException, Request
import jpype

from bridge.hha_models import HhaRequest, HhaResponse
from bridge.hha_release import release_info

router = APIRouter(prefix="/api/v1/hha", tags=["HHA"])


def readiness(processors) -> dict:
    result = {"enabled": bool(processors), "ready": bool(processors) and jpype.isJVMStarted(), **release_info()}
    # The existing top-level reference fields describe the most recent loaded release.
    processor = processors[max(processors)] if processors else None
    catalog = getattr(processor, 'geography_catalog', None)
    if catalog is not None:
        result['geographyReference'] = catalog.reference_info
    if processor is not None and hasattr(processor, 'a9272_rates'):
        rates = processor.a9272_rates
        result['a9272Reference'] = dict(ready=rates is not None, store='MyelinDatabase',
            catalogSha256=rates.sha256 if rates is not None else None)
    return result


@router.get("/ready")
def ready(request: Request):
    processors = getattr(request.app.state, "hha_processors", {})
    if not readiness(processors)["ready"]:
        raise HTTPException(503, "HHA pricing is disabled or unavailable.")
    return {"status": "ready", **readiness(processors)}


@router.post("/process", response_model=HhaResponse)
def process(payload: HhaRequest, request: Request):
    processors = getattr(request.app.state, "hha_processors", {})
    if not readiness(processors)["ready"]:
        raise HTTPException(503, "HHA pricing is disabled or unavailable.")
    processor = processors.get(payload.release.payment_year)
    if processor is None:
        raise HTTPException(503, "The approved HH release is not loaded.")
    try:
        return processor.process(payload)
    except Exception:
        # Raw Java, SQL and claim details belong neither in errors nor logs.
        raise HTTPException(503, "HHA pricing failed; retry after checking service availability.") from None

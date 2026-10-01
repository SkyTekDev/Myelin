from fastapi import APIRouter, HTTPException, Request
from bridge.snf_models import SnfRequest, SnfResponse
from bridge.snf_release import PRICER_RELEASE, CALCULATION_VERSIONS, FIRST_SERVICE_DATE, LAST_SERVICE_DATE

router = APIRouter(prefix="/api/v1/snf", tags=["SNF"])


@router.get("/ready")
def ready(request: Request):
    if getattr(request.app.state, "snf_processor", None) is None:
        raise HTTPException(503, "SNF pricing is disabled or unavailable.")
    return {"status": "ready", "calculationYears": list(CALCULATION_VERSIONS),
            "pricerRelease": PRICER_RELEASE, "calculationVersions": CALCULATION_VERSIONS,
            "firstServiceDate": FIRST_SERVICE_DATE.isoformat(), "lastServiceDate": LAST_SERVICE_DATE.isoformat()}


@router.post("/process", response_model=SnfResponse)
def process(payload: SnfRequest, request: Request):
    processor = getattr(request.app.state, "snf_processor", None)
    if processor is None:
        raise HTTPException(503, "SNF pricing is disabled or unavailable.")
    try:
        return processor.process(payload)
    except Exception:
        # Do not include claim payloads, database errors or Java exceptions.
        raise HTTPException(503, "SNF pricing failed; retry after checking service availability.") from None

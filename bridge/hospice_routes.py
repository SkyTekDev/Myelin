import logging

import jpype
from fastapi import APIRouter, HTTPException, Request

from bridge.hospice_models import HospiceClaimRequest, HospiceResponse
from bridge.hospice_release import SUPPORTED_FISCAL_YEARS, fiscal_year_start, release_info

router = APIRouter(prefix="/api/v1/hospice", tags=["Hospice"])
logger = logging.getLogger("qon_ioce_opps_api.hospice")


def readiness(processor) -> dict:
    through = getattr(processor, "records_through", None)
    exported = getattr(processor, "export_date", None)
    return {"enabled": processor is not None, "ready": processor is not None and jpype.isJVMStarted(), **release_info(),
            "providerFileExportDate": exported.isoformat() if exported else None,
            # The latest hospice record effective on or before the export date (later-dated records do not count).
            "providerFileHospiceRecordsThrough": through.isoformat() if through else None,
            # Fiscal years whose quality reporting results the loaded provider file can hold; other years price at full rates.
            "qualityIndicatorFiscalYears": [year for year in SUPPORTED_FISCAL_YEARS
                                            if through is not None and through >= fiscal_year_start(year)]}


@router.get("/ready")
def ready(request: Request):
    processor = getattr(request.app.state, "hospice_processor", None)
    if not readiness(processor)["ready"]:
        raise HTTPException(503, "Hospice pricing is disabled or unavailable.")
    return {"status": "ready", **readiness(processor)}


@router.post("/process", response_model=HospiceResponse)
def process(payload: HospiceClaimRequest, request: Request):
    processor = getattr(request.app.state, "hospice_processor", None)
    lock = getattr(request.app.state, "processing_lock", None)
    # A processor exists only after the JVM started and the pinned jar loaded.
    if processor is None or lock is None:
        raise HTTPException(503, "Hospice pricing is disabled or unavailable.")
    try:
        return processor.process(payload, lock, request.app.version)
    except Exception as ex:
        # Neither the claim payload nor the Java exception text leaves the bridge.
        logger.error("Hospice pricing failed for claimId=%s (%s)", payload.claimId, type(ex).__name__)
        raise HTTPException(503, "Hospice pricing failed; retry after checking service availability.") from None

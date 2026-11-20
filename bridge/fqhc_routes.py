import logging
import re

import jpype
from fastapi import APIRouter, HTTPException, Request

from bridge.fqhc_models import FqhcClaimRequest, FqhcResponse
from bridge.fqhc_release import TELEHEALTH_IOCE_MINIMUM, release_info

router = APIRouter(prefix="/api/v1/fqhc", tags=["FQHC"])
logger = logging.getLogger("qon_ioce_opps_api.fqhc")


def readiness(processor) -> dict:
    ioce_jar = getattr(processor, "ioce_jar", None)
    match = re.match(r"^ioce-standalone-(\d+)\.(\d+)", ioce_jar or "")
    return {"enabled": processor is not None, "ready": processor is not None and jpype.isJVMStarted(), **release_info(),
            "ioceJar": ioce_jar,
            # October 2026 telehealth billing (modifier 93/95) needs I/OCE 27.3 or later (CR 14468).
            "telehealthCodeLines": bool(match) and (int(match.group(1)), int(match.group(2))) >= TELEHEALTH_IOCE_MINIMUM}


@router.get("/ready")
def ready(request: Request):
    processor = getattr(request.app.state, "fqhc_processor", None)
    if not readiness(processor)["ready"]:
        raise HTTPException(503, "FQHC pricing is disabled or unavailable.")
    return {"status": "ready", **readiness(processor)}


@router.post("/process", response_model=FqhcResponse)
def process(payload: FqhcClaimRequest, request: Request):
    processor = getattr(request.app.state, "fqhc_processor", None)
    ioce_client = getattr(request.app.state, "ioce_client", None)
    lock = getattr(request.app.state, "processing_lock", None)
    # A processor exists only after the JVM started and the pinned jar loaded.
    if processor is None or ioce_client is None or lock is None:
        raise HTTPException(503, "FQHC pricing is disabled or unavailable.")
    try:
        return processor.process(payload, ioce_client, lock, request.app.version)
    except Exception as ex:
        # Neither the claim payload nor the Java exception text leaves the bridge.
        logger.error("FQHC pricing failed for claimId=%s (%s)", payload.claimId, type(ex).__name__)
        raise HTTPException(503, "FQHC pricing failed; retry after checking service availability.") from None

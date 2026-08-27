from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from bridge.ipps_mapper import map_ipps_request
from bridge.ipps_models import IppsClaimRequest, IppsClaimResponse


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/ipps",
    tags=["IPPS"],
)


@router.get("/ready")
def ipps_ready(request: Request):
    processor = getattr(
        request.app.state,
        "ipps_processor",
        None,
    )

    if processor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IPPS processor is not initialized.",
        )

    return processor.readiness()


@router.post(
    "/process",
    response_model=IppsClaimResponse,
)
def process_ipps(
    payload: IppsClaimRequest,
    request: Request,
) -> IppsClaimResponse:
    processor = getattr(
        request.app.state,
        "ipps_processor",
        None,
    )

    if processor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IPPS processor is not initialized.",
        )

    try:
        claim = map_ipps_request(payload)
        result = processor.process(claim)
        return IppsClaimResponse.model_validate(result)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception(
            "IPPS processing failed for claim %s.",
            payload.claimId,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"IPPS processing failed: {exc}",
        ) from exc

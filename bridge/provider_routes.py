from datetime import date as DateType
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from myelin.helpers.utils import ProviderDataError
from myelin.input.claim import Provider
from myelin.pricers.ipsf import IPSFProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/provider", tags=["Provider"])


class IpsfProviderResponse(BaseModel):
    providerCcn: str
    effectiveDate: int
    terminationDate: int | None
    providerType: str | None
    waiverIndicator: str | None
    npi: str | None


@router.get("/ipsf", response_model=IpsfProviderResponse)
def get_ipsf_provider(
    request: Request,
    date: DateType,
    ccn: str = Query(pattern=r"^[A-Za-z0-9]{6}$"),
) -> IpsfProviderResponse:
    engine = getattr(request.app.state, "myelin_engine", None)
    database = getattr(getattr(engine, "db_manager", None), "engine", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Provider database is not initialized.")

    try:
        provider = IPSFProvider()
        # Use precisely the effective-date lookup and sentinel normalization used
        # by IPPS pricing. The explicit session also closes on a missing record.
        with Session(database) as session:
            provider.from_db(
                database,
                Provider(other_id=ccn.upper()),
                date.year * 10000 + date.month * 100 + date.day,
                session=session,
            )
        return IpsfProviderResponse(
            providerCcn=provider.provider_ccn,
            effectiveDate=provider.effective_date,
            terminationDate=provider.termination_date,
            providerType=provider.provider_type,
            waiverIndicator=provider.waiver_indicator,
            npi=provider.national_provider_identifier,
        )
    except ProviderDataError as exc:
        if exc.code == "P0003":
            raise HTTPException(status_code=404, detail="No IPSF provider record for this CCN and date.") from exc
        logger.exception("IPSF provider lookup failed")
        raise HTTPException(status_code=503, detail="Provider lookup is unavailable.") from exc
    except Exception as exc:
        logger.exception("IPSF provider database error")
        raise HTTPException(status_code=503, detail="Provider lookup is unavailable.") from exc

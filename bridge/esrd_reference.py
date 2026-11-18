"""Dated provider and immutable database-rate references for the ESRD benchmark."""
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from bridge.esrd_catalog import EsrdCatalog
from bridge.esrd_supersessions import evidence
from myelin.pricers.opsf import DATATYPES, OPSF


class EsrdCatalogBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    serviceDate: date
    catalogSha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class EsrdReferenceSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['Current', 'AsOf', 'Pinned'] = 'Current'
    asOfUtc: datetime | None = None
    catalogs: list[EsrdCatalogBinding] = Field(default_factory=list, max_length=31)

    @field_validator('asOfUtc', mode='before')
    @classmethod
    def explicit_timestamp(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError('Supply an explicit UTC timestamp.')
        return value

    @model_validator(mode='after')
    def selection_shape(self):
        if self.asOfUtc is not None and (self.asOfUtc.tzinfo is None or self.asOfUtc.utcoffset().total_seconds() != 0
                or self.asOfUtc > datetime.now(timezone.utc)):
            raise ValueError('Supply a UTC historical timestamp that is not in the future.')
        if ((self.mode == 'Current' and (self.asOfUtc is not None or self.catalogs))
                or (self.mode == 'AsOf' and (self.asOfUtc is None or self.catalogs))
                or (self.mode == 'Pinned' and (self.asOfUtc is not None or not self.catalogs))
                or len({row.serviceDate for row in self.catalogs}) != len(self.catalogs)):
            raise ValueError('Invalid ESRD reference selection.')
        return self


class EsrdReferenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    providerCcn: str | None = Field(default=None, pattern=r'^[0-9]{6}$')
    npi: str | None = Field(default=None, pattern=r'^[0-9]{10}$')
    serviceDates: list[date] = Field(min_length=1, max_length=31)
    selection: EsrdReferenceSelection | None = None

    @field_validator('serviceDates', mode='before')
    @classmethod
    def calendar_dates(cls, values):
        if not isinstance(values, list) or any(
            not (type(value) is date or (isinstance(value, str) and len(value) == 10 and value[4] == '-' and value[7] == '-'))
            for value in values
        ):
            raise ValueError('Supply ISO calendar dates, not timestamps or numeric dates.')
        return values

    @model_validator(mode='after')
    def supported(self):
        if not self.providerCcn and not self.npi:
            raise ValueError('Supply an ESRD CCN or NPI.')
        if len(set(self.serviceDates)) != len(self.serviceDates):
            raise ValueError('Service dates must be distinct.')
        if self.selection and self.selection.mode == 'Pinned' and {row.serviceDate for row in self.selection.catalogs} != set(self.serviceDates):
            raise ValueError('Pinned catalogs must bind every requested service date exactly once.')
        return self


class EsrdReferenceEntry(BaseModel):
    serviceDate: date
    providerSource: str
    providerReferenceSource: str
    provider: dict[str, Any]
    cbsa: str
    geographicWageIndex: Decimal
    wageIndex: Decimal
    wageIndexSource: str
    baseRate: Decimal
    laborShare: Decimal
    nonLaborShare: Decimal
    rateSet: dict[str, Any]
    supersession: dict[str, Any] | None = None


class EsrdReferenceResponse(BaseModel):
    status: str
    reasonCode: str = ''
    reason: str = ''
    referenceVersion: str = ''
    jarSha256: str = ''
    wageTableSha256: str = ''
    entries: list[EsrdReferenceEntry] = Field(default_factory=list)
    selection: dict[str, Any] | None = None


class EsrdReferenceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(valid, code, message):
    if not valid:
        raise EsrdReferenceError(code, message)


def number(value):
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except Exception:
        pass
    raise EsrdReferenceError('ESRD_PROVIDER_WAGE_MISSING', 'A required ESRD wage input is missing or invalid.')


class EsrdReferenceProcessor:
    def __init__(self, engine):
        self.engine = engine
        self.catalog = EsrdCatalog(engine)

    @staticmethod
    def _latest(session, ccn, as_of):
        rows = session.scalars(select(OPSF).where(OPSF.provider_ccn == ccn, OPSF.effective_date <= as_of)
            .order_by(OPSF.effective_date.desc())).all()
        require(bool(rows), 'ESRD_PROVIDER_NOT_FOUND', 'No effective OPSF record for the ESRD facility and service date.')
        require(sum(row.effective_date == rows[0].effective_date for row in rows) == 1,
            'ESRD_PROVIDER_AMBIGUOUS', 'Duplicate OPSF records on the latest provider effective date.')
        return rows[0]

    def _provider(self, session, request, service_date):
        as_of = int(service_date.strftime('%Y%m%d'))
        if request.providerCcn:
            row = self._latest(session, request.providerCcn, as_of)
            require(not request.npi or (row.national_provider_identifier or '').strip() == request.npi,
                'ESRD_PROVIDER_IDENTITY_CONFLICT', 'Submitted CCN and NPI do not match the effective OPSF record.')
        else:
            ccns = session.scalars(select(OPSF.provider_ccn).where(
                OPSF.national_provider_identifier == request.npi, OPSF.effective_date <= as_of).distinct()).all()
            candidates = []
            for ccn in ccns:
                candidate = self._latest(session, ccn, as_of)
                if ((candidate.national_provider_identifier or '').strip() == request.npi
                        and (candidate.provider_type or '').strip() in ('40', '41')
                        and (candidate.termination_date in (None, 0, 19000101, 99991231) or candidate.termination_date > as_of)):
                    candidates.append(candidate)
            require(len(candidates) == 1, 'ESRD_PROVIDER_AMBIGUOUS' if candidates else 'ESRD_PROVIDER_NOT_FOUND',
                'NPI must identify exactly one active, dated ESRD facility in OPSF.')
            row = candidates[0]
        require((row.provider_type or '').strip() in ('40', '41'), 'ESRD_PROVIDER_TYPE_UNSUPPORTED',
            'The effective OPSF provider is not an ESRD facility.')
        require(row.termination_date in (None, 0, 19000101, 99991231) or row.termination_date > as_of,
            'ESRD_PROVIDER_TERMINATED', 'The ESRD facility is terminated on the service date.')
        require(service_date.year * 10000 + 101 <= row.effective_date <= as_of, 'ESRD_PROVIDER_YEAR_MISSING',
            'A current-year ESRD OPSF record is required to establish wage inputs.')
        return row

    def resolve(self, request):
        entries = []
        selected = request.selection or EsrdReferenceSelection()
        resolved_at = datetime.now(timezone.utc)
        as_of = selected.asOfUtc or resolved_at
        pins = {row.serviceDate: row.catalogSha256 for row in selected.catalogs}
        try:
            with Session(self.engine) as session:
                for service_date in sorted(request.serviceDates):
                    try:
                        document, rates = self.catalog.for_date(service_date, as_of=as_of.isoformat(), pinned=pins.get(service_date))
                    except ValueError:
                        raise EsrdReferenceError('ESRD_RATE_REFERENCE_MISSING', 'No unique loaded ESRD rate release covers the service date.') from None
                    row = self._provider(session, request, service_date)
                    cbsa = (row.cbsa_actual_geographic_location or '').strip()
                    require(cbsa in document['entries'], 'ESRD_WAGE_INDEX_NOT_FOUND', 'ESRD CBSA is missing from the selected dated wage table.')
                    require(not (row.cbsa_wage_index_location or '').strip() or row.cbsa_wage_index_location.strip() == cbsa,
                        'ESRD_WAGE_LOCATION_UNSUPPORTED', 'The provider has a wage-location override outside this benchmark.')
                    require(row.special_payment_indicator is not None and row.special_payment_indicator.strip() in ('', '0'),
                        'ESRD_SPECIAL_PAYMENT_UNSUPPORTED', 'ESRD special wage/payment status is missing or unsupported.')
                    indicator = row.supplemental_wage_index_indicator
                    require(indicator is not None and indicator.strip() in ('', '0', '1', '2'),
                        'ESRD_WAGE_CAP_STATUS_MISSING', 'ESRD supplemental wage-cap status is missing or unsupported.')
                    geographic = Decimal(document['entries'][cbsa]['wageIndex'])
                    wage = max(Decimal(rates['wageFloor']), geographic)
                    if indicator.strip() in ('1', '2'):
                        prior = number(row.supplemental_wage_index)
                        require(0 < prior <= 5, 'ESRD_PROVIDER_WAGE_MISSING', 'Missing or invalid supplemental wage index.')
                        # CMS ApplySupplementalWageIndex2025 truncates the change from the prior-year index to the
                        # wage index's four decimals and replaces the index only below the permitted decrease.
                        multiplier = Decimal(rates['wageCapMultiplier'])
                        change = ((wage - prior) / prior).quantize(Decimal('0.0001'), rounding=ROUND_DOWN)
                        if change < multiplier - 1:
                            wage = (prior * multiplier).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
                    provider = {name: getattr(row, name) for name in DATATYPES}
                    provider = {name: Decimal(str(value)) if isinstance(value, float) else value for name, value in provider.items()}
                    entries.append(EsrdReferenceEntry(serviceDate=service_date,
                        providerSource='SubmittedCcn' if request.providerCcn else 'DatedOPSFNpi',
                        providerReferenceSource=rates['providerSourceUrl'], provider=provider, cbsa=cbsa,
                        geographicWageIndex=geographic, wageIndex=wage,
                        wageIndexSource='OPSF_WAGE_CAP' if wage > max(Decimal(rates['wageFloor']), geographic) else 'ESRD_REFERENCE_TABLE',
                        baseRate=Decimal(rates['baseRate']), laborShare=Decimal(rates['laborShare']), nonLaborShare=Decimal(rates['nonLaborShare']), rateSet=rates,
                        supersession=evidence(self.catalog.supersessions.get(rates['catalogSha256']))))
            require(len({entry.provider['provider_ccn'] for entry in entries}) == 1,
                'ESRD_PROVIDER_IDENTITY_CONFLICT', 'The dated NPI resolves to different ESRD facilities within the claim.')
            def summary(key):
                values = {entry.rateSet[key] for entry in entries}
                return values.pop() if len(values) == 1 else 'MULTIPLE'
            return EsrdReferenceResponse(status='Resolved', referenceVersion=summary('referenceVersion'),
                jarSha256=summary('jarSha256'), wageTableSha256=summary('wageTableSha256'), entries=entries,
                selection=dict(mode=selected.mode, resolvedAtUtc=resolved_at.isoformat(),
                    asOfUtc=as_of.isoformat() if selected.mode != 'Pinned' else None,
                    catalogs=[dict(serviceDate=entry.serviceDate.isoformat(), catalogSha256=entry.rateSet['catalogSha256']) for entry in entries]))
        except EsrdReferenceError as error:
            return EsrdReferenceResponse(status='UnableToPrice', reasonCode=error.code, reason=str(error))

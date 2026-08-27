from __future__ import annotations

from datetime import date, datetime, time

from myelin.input.claim import (
    Claim,
    DiagnosisCode,
    DxType,
    LineItem,
    Patient,
    PoaType,
    Provider,
    ValueCode,
)

from bridge.api_models import DiagnosisRequest, IoceClaimRequest


def _as_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min)


def _poa(value: str) -> PoaType:
    try:
        return PoaType(value)
    except ValueError:
        return PoaType.BLANK


def _diagnosis(
    request: DiagnosisRequest,
    diagnosis_type: DxType,
) -> DiagnosisCode:
    return DiagnosisCode(
        code=request.code,
        poa=_poa(request.poa),
        dx_type=diagnosis_type,
    )


def to_myelin_claim(request: IoceClaimRequest) -> Claim:
    if request.fromDate is None or request.throughDate is None:
        raise ValueError(
            "Claim fromDate and throughDate are required."
        )

    lines = [
        LineItem(
            service_date=_as_datetime(line.dateOfService),
            revenue_code=line.revenueCode,
            hcpcs=line.hcpcs or "",
            modifiers=line.modifiers,
            units=float(line.units),
            charges=float(line.chargeAmount),
        )
        for line in request.claimLines
    ]

    return Claim(
        claimid=str(request.claimId),
        from_date=_as_datetime(request.fromDate),
        thru_date=_as_datetime(request.throughDate),
        bill_type=request.billType,
        patient_status=request.patientStatus,
        total_charges=float(
            sum(line.chargeAmount for line in request.claimLines)
        ),
        cond_codes=request.conditionCodes,
        value_codes=[
            ValueCode(
                code=value_code.code,
                amount=float(value_code.amount),
            )
            for value_code in request.valueCodes
        ],
        principal_dx=_diagnosis(
            request.principalDiagnosis,
            DxType.PRIMARY,
        ),
        secondary_dxs=[
            _diagnosis(diagnosis, DxType.SECONDARY)
            for diagnosis in request.secondaryDiagnoses
        ],
        billing_provider=Provider(
            npi=request.npi or "",
            other_id=request.providerNumber,
        ),
        patient=Patient(
            age=request.patientAge,
            sex=request.patientSex,
        ),
        lines=lines,
        opps_flag=request.oppsFlag,
        additional_data={
            "patientControlNumber": request.patientControlNumber,
            "lineIdentifiers": [
                {
                    "claimLineId": str(line.claimLineId),
                    "lineNumber": line.lineNumber,
                }
                for line in request.claimLines
            ],
        },
    )

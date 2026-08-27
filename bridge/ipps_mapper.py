from __future__ import annotations

from datetime import date, datetime, time

from myelin.input.claim import (
    Claim,
    DiagnosisCode,
    LineItem,
    Patient,
    PoaType,
    ProcedureCode,
    Provider,
    ValueCode,
)

from bridge.ipps_models import IppsClaimRequest


def _to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _poa(value: str) -> PoaType:
    for poa_type in PoaType:
        if poa_type.value == value:
            return poa_type
    return PoaType.BLANK


def _calculate_age(date_of_birth: date, as_of: date) -> int:
    return (
        as_of.year
        - date_of_birth.year
        - (
            (as_of.month, as_of.day)
            < (date_of_birth.month, date_of_birth.day)
        )
    )


def map_ipps_request(request: IppsClaimRequest) -> Claim:
    if request.patientAge is not None:
        patient_age = request.patientAge
    elif request.patientDateOfBirth is not None:
        patient_age = _calculate_age(
            request.patientDateOfBirth,
            request.throughDate,
        )
    else:
        # Request validation prevents this branch.
        raise ValueError(
            "Either patientAge or patientDateOfBirth is required"
        )

    if request.lengthOfStay is not None:
        length_of_stay = request.lengthOfStay
    else:
        length_of_stay = (
            request.throughDate - request.admitDate
        ).days

    principal_dx = DiagnosisCode(
        code=request.principalDiagnosis.code,
        poa=_poa(request.principalDiagnosis.poa),
    )

    admit_source = request.admitDiagnosis or request.principalDiagnosis
    admit_dx = DiagnosisCode(
        code=admit_source.code,
        poa=_poa(admit_source.poa),
    )

    secondary_dxs = [
        DiagnosisCode(
            code=diagnosis.code,
            poa=_poa(diagnosis.poa),
        )
        for diagnosis in request.secondaryDiagnoses
    ]

    inpatient_procedures = [
        ProcedureCode(
            code=procedure.code,
            modifier=procedure.modifier,
            date=_to_datetime(procedure.date),
        )
        for procedure in request.inpatientProcedures
    ]

    lines = [
        LineItem(
            service_date=_to_datetime(line.dateOfService),
            revenue_code=line.revenueCode,
            hcpcs=line.hcpcs,
            modifiers=line.modifiers,
            units=line.units,
            charges=line.chargeAmount,
        )
        for line in request.claimLines
    ]

    value_codes = [
        ValueCode(
            code=value_code.code,
            amount=value_code.amount,
        )
        for value_code in request.valueCodes
    ]

    patient = Patient(
        patient_id=request.patientControlNumber or "",
        medical_record_number=request.patientControlNumber or "",
        date_of_birth=_to_datetime(request.patientDateOfBirth),
        age=patient_age,
        sex=request.patientSex,
    )

    billing_provider = Provider(
        npi=request.npi or "",
        other_id=request.providerNumber,
    )

    claim = Claim(
        claimid=request.claimId,
        admit_date=_to_datetime(request.admitDate),
        from_date=_to_datetime(request.fromDate),
        thru_date=_to_datetime(request.throughDate),
        los=length_of_stay,
        bill_type=request.billType,
        patient_status=request.patientStatus,
        total_charges=request.totalCharges,
        cond_codes=request.conditionCodes,
        value_codes=value_codes,
        secondary_dxs=secondary_dxs,
        principal_dx=principal_dx,
        admit_dx=admit_dx,
        inpatient_pxs=inpatient_procedures,
        lines=lines,
        non_covered_days=request.nonCoveredDays,
        billing_provider=billing_provider,
        patient=patient,
        admission_source=request.admissionSource,
        hmo=request.hmo,
        additional_data={
            "ipps": {
                "review_code": request.reviewCode,
                "lifetime_reserve_days": request.lifetimeReserveDays,
                "midnight_adjustment_geolocation": (
                    request.midnightAdjustmentGeolocation
                ),
            }
        },
    )

    return claim

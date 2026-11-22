"""Maps an accepted claim to a FHIR R4B Claim resource and validates it with fhir.resources.

US payers exchange R4 (Da Vinci, CARIN); R4B has the same Claim resource. Only what this
pipeline knows is filled in: patient and coverage are references to resources owned by the
member system, and the provider is identified by NPI.
"""

from __future__ import annotations

from datetime import datetime

from fhir.resources.R4B.claim import Claim as FhirClaim

from .models import Claim

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
HCPCS_SYSTEM = "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"
POS_SYSTEM = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"
CLAIM_ID_SYSTEM = "urn:example:claims-intake:claim-id"


def _money(cents: int) -> dict:
    return {"value": cents / 100, "currency": "USD"}


def to_fhir(c: Claim, created: datetime) -> dict:
    resource = {
        "resourceType": "Claim",
        "id": c.claim_id.lower().replace("_", "-"),
        "identifier": [{"system": CLAIM_ID_SYSTEM, "value": c.claim_id}],
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "claim",
        "patient": {"reference": f"Patient/{c.member_id}"},
        "created": created.isoformat(),
        "provider": {"identifier": {"system": NPI_SYSTEM, "value": c.provider_npi}},
        "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority", "code": "normal"}]},
        "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/{c.member_id}"}}],
        "diagnosis": [
            {"sequence": i, "diagnosisCodeableConcept": {"coding": [{"system": ICD10_SYSTEM, "code": dx}]}}
            for i, dx in enumerate(c.diagnosis_codes, start=1)
        ],
        "item": [
            {
                "sequence": i,
                "diagnosisSequence": list(range(1, len(c.diagnosis_codes) + 1)),
                "productOrService": {
                    "coding": [{"system": CPT_SYSTEM if ln.code[0].isdigit() else HCPCS_SYSTEM, "code": ln.code}]
                },
                **({"modifier": [{"coding": [{"code": ln.modifier}]}]} if ln.modifier else {}),
                "servicedDate": c.service_date.isoformat(),
                "locationCodeableConcept": {"coding": [{"system": POS_SYSTEM, "code": c.place_of_service}]},
                "quantity": {"value": ln.units},
                "net": _money(ln.charge_cents),
            }
            for i, ln in enumerate(c.lines, start=1)
        ],
        "total": _money(c.total_charge_cents),
    }
    FhirClaim.model_validate(resource)  # raises if the resource is not valid R4B
    return resource

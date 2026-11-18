"""Manual repair for existing patient-history indexes, including skipped old patches.

Requires the compatible vobiz_ai maintenance helper on the same bench.
Does not enqueue a backfill or change the existing migration patch.
"""
from vobiz_ai.maintenance.lookup_indexes import repair_specs

TARGETS = {
    "encounter_timeline": (
        "tabPatient Encounter", "idx_patient_encounter_patient_timeline",
        ("patient", "encounter_date", "encounter_time", "modified"),
    ),
    "medical_record_timeline": (
        "tabPatient Medical Record", "idx_patient_medical_record_patient_timeline",
        ("patient", "communication_date", "modified"),
    ),
}


def repair(target=None, apply=False):
    return repair_specs(TARGETS, target, apply)

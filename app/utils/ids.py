"""ID generation utilities.

UUID4 is used for the MVP. Generation is kept behind these functions so a
later switch to UUID7 (or another sortable scheme) touches one file instead
of every call site.
"""

from __future__ import annotations

import uuid


def generate_ingestion_id() -> str:
    return f"ing_{uuid.uuid4()}"


def generate_session_id() -> str:
    return f"sess_{uuid.uuid4()}"


def generate_validation_id() -> str:
    return f"val_{uuid.uuid4()}"


def generate_integrity_id() -> str:
    return f"integ_{uuid.uuid4()}"


def generate_normalization_id() -> str:
    return f"norm_{uuid.uuid4()}"


def generate_synchronization_id() -> str:
    return f"sync_{uuid.uuid4()}"


def generate_cleaning_id() -> str:
    return f"clean_{uuid.uuid4()}"


def generate_transformation_id() -> str:
    return f"xform_{uuid.uuid4()}"


def generate_qc_id() -> str:
    return f"qc_{uuid.uuid4()}"


def generate_package_id() -> str:
    return f"pkg_{uuid.uuid4()}"

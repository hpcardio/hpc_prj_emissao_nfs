from __future__ import annotations

from datetime import date

import pytest

from nfs_fortaleza.maida_extraction import (
    MaidaExtractionConfigurationError,
    MaidaExtractionPayload,
    _competency_range,
)
from nfs_fortaleza.maida_portal import MaidaCompetency


def test_payload_accepts_single_and_multiple_competencies() -> None:
    single = MaidaExtractionPayload.from_mapping({"competencia": "01/2026"})
    multiple = MaidaExtractionPayload.from_mapping(
        {"competencias": ["03/2026", "2026-02", "03/2026"]}
    )

    assert single.competencies == (MaidaCompetency(2026, 1),)
    assert multiple.competencies == (
        MaidaCompetency(2026, 2),
        MaidaCompetency(2026, 3),
    )


def test_payload_rejects_invalid_competency() -> None:
    with pytest.raises(MaidaExtractionConfigurationError):
        MaidaExtractionPayload.from_mapping({"competencia": "13/2026"})


def test_competency_range_starts_in_january_2026() -> None:
    values = list(
        _competency_range(MaidaCompetency(2026, 1), MaidaCompetency(2026, 4))
    )
    assert [value.first_day for value in values] == [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
        date(2026, 4, 1),
    ]

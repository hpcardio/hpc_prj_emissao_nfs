from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nfs_fortaleza.maida_extraction import (
    MaidaExtractionConfigurationError,
    MaidaExtractionPayload,
    _competency_range,
    _parse_glosa_records,
)
from nfs_fortaleza.maida_portal import (
    DownloadedMaidaDocument,
    MaidaCompetency,
    MaidaDocument,
)
from nfs_fortaleza.maida_xlsx import MaidaSpreadsheetError


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


def test_parse_glosa_records_ignores_document_without_glosa_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    downloaded = DownloadedMaidaDocument(
        document=MaidaDocument(
            competency=MaidaCompetency(2026, 6),
            source="lotes",
            document_id="doc-sem-glosa",
            file_name="Demonstrativo_Lote26921.xlsx",
            remote_path="/arquivo/Demonstrativo_Lote26921.xlsx",
        ),
        path=tmp_path / "Demonstrativo_Lote26921.xlsx",
    )

    def invalid_workbook(_downloaded: DownloadedMaidaDocument):
        raise MaidaSpreadsheetError("layout sem colunas de glosa")

    monkeypatch.setattr(
        "nfs_fortaleza.maida_extraction.parse_maida_xlsx",
        invalid_workbook,
    )

    assert _parse_glosa_records(downloaded) == []
    assert "Demonstrativo_Lote26921.xlsx" in caplog.text

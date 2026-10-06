from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from nfs_fortaleza.maida_config import MaidaSettings
from nfs_fortaleza.maida_portal import (
    MaidaCompetency,
    MaidaDocument,
    MaidaCompetencyResult,
    _jwt_subject,
    MaidaPortalClient,
)


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return _Response({"maidaToken": "token-sem-expor-credencial"})

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/demonstrativo-prestador/visao-prestador"):
            return _Response([])
        if url.endswith("/lotes-analisados/visao-prestador"):
            selected = kwargs["params"]
            lot_id = (
                "nao-recursado"
                if selected["lotesGlosasNaoRecursadas"]
                else "recursado"
            )
            return _Response(
                {
                    "content": [{"loteId": lot_id, "tipoLote": "COBRANCA"}],
                    "totalPages": 1,
                }
            )
        if "/detalhes-lote" in url:
            lot_id = url.split("/")[-2]
            return _Response(
                {
                    "dataEnvio": "2026-01-10T10:00:00",
                    "dataProcessamento": "2026-01-11T10:00:00",
                    "dataEnvioRecurso": "2026-01-12T10:00:00",
                    "demonstrativos": [
                        {
                            "documentoId": f"doc-{lot_id}",
                            "nomeArquivo": f"{lot_id}.xlsx",
                            "pathArquivo": f"/arquivos/{lot_id}.xlsx",
                        }
                    ],
                }
            )
        raise AssertionError(f"Endpoint inesperado: {url}")


def test_jwt_subject_is_read_without_logging_token() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user-123"}).encode())
    token = "header." + payload.decode().rstrip("=") + ".signature"
    assert _jwt_subject(token) == "user-123"


def test_summary_xlsx_has_precedence_over_lot_documents() -> None:
    competency = MaidaCompetency(2026, 1)
    summary = MaidaDocument(
        competency, "resumo", "summary", "resumo.xlsx", "/resumo.xlsx"
    )
    lot = MaidaDocument(
        competency, "lote_glosa", "lot", "lote.xlsx", "/lote.xlsx"
    )
    result = MaidaCompetencyResult(competency, (summary,), (lot,), ())
    assert result.selected_documents == (summary,)


def test_lot_xlsx_is_fallback_when_summary_has_no_xlsx() -> None:
    competency = MaidaCompetency(2026, 1)
    summary_pdf = MaidaDocument(
        competency, "resumo", "pdf", "resumo.pdf", "/resumo.pdf"
    )
    lot = MaidaDocument(
        competency, "lote_glosa", "lot", "lote.xlsx", "/lote.xlsx"
    )
    result = MaidaCompetencyResult(competency, (summary_pdf,), (lot,), ())
    assert result.selected_documents == (lot,)


def test_collect_competency_uses_both_analyzed_lot_filters(tmp_path: Path) -> None:
    settings = MaidaSettings(
        login="login",
        password="password",
        database_url="postgresql://example",
        provider_id="provider-1",
    )
    client = MaidaPortalClient(settings, downloads_dir=tmp_path)
    session = _Session()
    client.session = session  # type: ignore[assignment]

    result = client.collect_competency(MaidaCompetency(2026, 1))

    assert len(result.lots) == 2
    assert len(result.selected_documents) == 2
    assert {item.source for item in result.selected_documents} == {
        "lote_glosa_nao_recursado",
        "lote_glosa_recursado",
    }
    list_calls = [
        call for call in session.calls if call[1].endswith("visao-prestador")
        and "lotes-analisados" in call[1]
    ]
    assert list_calls[0][2]["params"]["lotesGlosasNaoRecursadas"] is True
    assert list_calls[1][2]["params"]["lotesGlosasRecursadas"] is True
    assert session.headers["Authorization"].startswith("Bearer ")

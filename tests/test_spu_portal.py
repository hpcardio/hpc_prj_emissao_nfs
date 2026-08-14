from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from nfs_fortaleza.spu_portal import (
    SpuMaterializedTreeUnavailable,
    SpuPortalClient,
    SpuProfileInUseError,
    SpuSessionExpiredError,
    _is_target_document,
    _is_tramitando_report_document,
    canonical_spu_sector,
    parse_process_card,
    spu_profile_lock,
)


def test_parse_process_card_extracts_requested_listing_fields() -> None:
    process = parse_process_card(
        {
            "numero": "P193251/2026",
            "cabecalho": "P193251/2026 FINALIZADO",
            "tipo": "TIPO DE PROCESSO/ASSUNTO: Pagamento de prestador",
            "data": "DATA DE ABERTURA: 04/08/2026",
            "texto": (
                "P193251/2026 FINALIZADO TIPO DE PROCESSO/ASSUNTO: "
                "Pagamento de prestador DATA DE ABERTURA: 04/08/2026 "
                "MOTIVO DA FINALIZAÇÃO: Processo pago VISUALIZAR PROCESSO"
            ),
            "motivo": "Processo pago",
            "href": (
                "https://spumaterializar.sepog.fortaleza.ce.gov.br/"
                "processos/P193251_2026/visualizar_folder"
            ),
        }
    )

    assert process.numero_processo == "P193251/2026"
    assert process.status_processo == "FINALIZADO"
    assert process.tipo_processo_assunto == "Pagamento de prestador"
    assert process.data_abertura == date(2026, 8, 4)
    assert process.motivo_finalizacao == "Processo pago"
    assert process.finalizado is True


def test_parse_process_card_accepts_underscore_number() -> None:
    process = parse_process_card(
        {
            "numero": "P193252_2026",
            "cabecalho": "P193252_2026 TRAMITANDO",
            "tipo": "Cobrança administrativa",
            "data": "05/08/2026",
            "texto": "P193252_2026 TRAMITANDO 05/08/2026",
            "href": None,
        }
    )

    assert process.numero_processo == "P193252/2026"
    assert process.tipo_processo_assunto == "Cobrança administrativa"
    assert process.finalizado is False


def test_finalized_process_without_view_has_no_inferred_folder_url() -> None:
    process = parse_process_card(
        {
            "numero": "P483030/2023",
            "cabecalho": "P483030/2023 FINALIZADO",
            "tipo": "Pagamento",
            "data": "01/08/2023",
            "texto": "P483030/2023 FINALIZADO",
            "href": None,
        }
    )
    client = SpuPortalClient(object(), downloads_dir=Path("."))  # type: ignore[arg-type]

    with pytest.raises(SpuMaterializedTreeUnavailable, match="sem o botao"):
        client._open_folder_tree(None, process)  # type: ignore[arg-type]


def test_parse_process_card_keeps_process_with_unknown_status() -> None:
    process = parse_process_card(
        {
            "numero": "P236162/2022",
            "cabecalho": "P236162/2022",
            "tipo": "Processo legado",
            "data": "01/02/2022",
            "texto": "P236162/2022 Processo legado 01/02/2022",
            "href": None,
        }
    )

    assert process.numero_processo == "P236162/2022"
    assert process.status_processo == "DESCONHECIDO"
    assert process.finalizado is False


def test_parse_process_card_recognizes_unarchived_status() -> None:
    process = parse_process_card(
        {
            "numero": "P260758/2025",
            "cabecalho": "NÚMERO P260758/2025 DESARQUIVADO",
            "tipo": "Revisão de pagamento de contas médicas",
            "data": "26/06/2025",
            "texto": (
                "P260758/2025 DESARQUIVADO Revisão de pagamento "
                "DESARQUIVADO PELO SEGUINTE MOTIVO: Equívoco"
            ),
            "motivo": "“EQUIVOCO “ VISUALIZAR MOVIMENTAÇÕES",
            "href": None,
        }
    )

    assert process.status_processo == "DESARQUIVADO"
    assert process.motivo_finalizacao == "EQUIVOCO"
    assert process.finalizado is False


def test_parse_process_card_preserves_status_from_header() -> None:
    process = parse_process_card(
        {
            "numero": "P387860/2024",
            "cabecalho": "NÚMERO P387860/2024",
            "tipo": "Revisão de pagamento",
            "data": "25/09/2024",
            "texto": (
                "P387860/2024 ABERTURA REPROVADA "
                "ABERTURA REPROVADA PELO SEGUINTE MOTIVO: Certidão vencida"
            ),
            "href": None,
        }
    )

    assert process.status_processo == "ABERTURA REPROVADA"
    assert process.finalizado is False


def test_parse_process_card_does_not_use_whole_card_as_reason() -> None:
    process = parse_process_card(
        {
            "numero": "P248911/2026",
            "cabecalho": "NÚMERO P248911/2026 TRAMITANDO",
            "tipo": "Pagamento de contas médicas",
            "data": "10/06/2026",
            "texto": (
                "NÚMERO P248911/2026 TRAMITANDO SOLICITAÇÃO DE "
                "FORNECEDORES/PRESTADORES PAGAMENTO DE CONTAS MÉDICAS"
            ),
            "href": None,
        }
    )

    assert process.status_processo == "TRAMITANDO"
    assert process.motivo_finalizacao is None


def test_canonical_spu_sector_accepts_historical_cogestao_name() -> None:
    assert canonical_spu_sector("IPM/SAUDECOGESTAO") == "ipm/saudecogestao"
    assert canonical_spu_sector("IPM/CO-GESTORA") == "ipm/saudecogestao"
    assert canonical_spu_sector("IPM/NUEXO") == "ipm/nuexo"


def test_target_document_filter_skips_dispatches_and_certificates() -> None:
    assert _is_target_document("ipm/saudecogestao", "RELATORIO.pdf")
    assert not _is_target_document(
        "ipm/saudecogestao",
        "RELATÓRIO_18259_45584518.pdf",
    )
    assert not _is_target_document(
        "ipm/saudecogestao",
        "RELATORIO_18624_22833231.pdf",
    )
    assert not _is_target_document("ipm/saudecogestao", "FOLHA DE DESPACHO.pdf")
    assert _is_target_document("ipm/nuexo", "EMP_1946.pdf")
    assert _is_target_document("ipm/nuexo", "14652_IPM.pdf")
    assert not _is_target_document("ipm/nuexo", "CNDS PRONTOCARDIO.pdf")
    assert not _is_target_document(
        "ipm/nuexo", "CONSULTA REGULARIDADE DO EMPREGADOR.pdf"
    )


def test_tramitando_report_filter_accepts_only_exact_numeric_name() -> None:
    assert _is_tramitando_report_document("RELATORIO_19218_27877736.pdf")
    assert _is_tramitando_report_document("RELATÓRIO_19218_27877736.PDF")
    assert not _is_tramitando_report_document("RELATORIO.pdf")
    assert not _is_tramitando_report_document("RELATORIO_19218.pdf")
    assert not _is_tramitando_report_document("RELATORIO_19218_X.pdf")


def test_process_list_reports_expired_session_to_orchestrator() -> None:
    class LoginPage:
        url = ""

        def goto(self, *_args: object, **_kwargs: object) -> None:
            self.url = "https://spuvirtual.sepog.fortaleza.ce.gov.br/auth/login"

    settings = SimpleNamespace(
        portal_origin="https://spuvirtual.sepog.fortaleza.ce.gov.br",
        page_timeout_seconds=90,
    )
    client = SpuPortalClient(  # type: ignore[arg-type]
        settings,
        downloads_dir=Path("."),
    )

    with pytest.raises(SpuSessionExpiredError, match="renovacao humana"):
        client._open_process_list(LoginPage())  # type: ignore[arg-type]
    assert not hasattr(client, "_login")


def test_profile_lock_rejects_simultaneous_browser(tmp_path: Path) -> None:
    profile_dir = tmp_path / "browser_profile"

    with spu_profile_lock(profile_dir):
        with pytest.raises(SpuProfileInUseError, match="ja esta em uso"):
            with spu_profile_lock(profile_dir):
                pass

    with spu_profile_lock(profile_dir):
        pass

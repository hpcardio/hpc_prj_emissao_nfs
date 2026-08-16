from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import dlt

from nfs_fortaleza.spu_report_storage import PROCESS_REPORT_TABLE_NAME


PROCESS_TABLE_NAME = "processos_ipm"
HISTORY_TABLE_NAME = "processos_historico_ipm"
COGESTAO_TABLE_NAME = "processos_ipm_saude_cogestao"
EMPENHO_TABLE_NAME = "processos_empenho_ipm"
NOTA_FISCAL_TABLE_NAME = "processos_nota_fiscal_ipm"


def spu_resources(
    processes: Iterable[dict[str, Any]],
    statuses: Iterable[dict[str, Any]],
    cogestao_rows: Iterable[dict[str, Any]],
    empenho_rows: Iterable[dict[str, Any]],
    nota_fiscal_rows: Iterable[dict[str, Any]],
) -> list[Any]:
    process_resource = dlt.resource(
        list(processes),
        name=PROCESS_TABLE_NAME,
        primary_key="numero_processo",
        write_disposition="merge",
        columns={
            "numero_processo": {"data_type": "text", "nullable": False},
            "status_processo": {"data_type": "text", "nullable": False},
            "tipo_processo_assunto": {"data_type": "text", "nullable": True},
            "data_abertura": {"data_type": "date", "nullable": True},
            "motivo_finalizacao": {"data_type": "text", "nullable": True},
            "url_visualizacao": {"data_type": "text", "nullable": True},
            "detalhes_finalizados_extraidos": {
                "data_type": "bool",
                "nullable": False,
            },
            "extraido_em": {"data_type": "timestamp", "nullable": False},
        },
    )
    status_resource = dlt.resource(
        list(statuses),
        name=HISTORY_TABLE_NAME,
        primary_key="id_status",
        write_disposition="merge",
        columns={
            "id_status": {"data_type": "text", "nullable": False},
            "numero_processo": {"data_type": "text", "nullable": False},
            "status_processo": {"data_type": "text", "nullable": False},
            "observado_em": {"data_type": "timestamp", "nullable": False},
        },
    )
    cogestao_resource = dlt.resource(
        list(cogestao_rows),
        name=COGESTAO_TABLE_NAME,
        primary_key="id_registro",
        write_disposition="merge",
        columns=_cogestao_columns(),
    )
    empenho_resource = dlt.resource(
        list(empenho_rows),
        name=EMPENHO_TABLE_NAME,
        primary_key="id_registro",
        write_disposition="merge",
        columns={
            "id_registro": {"data_type": "text", "nullable": False},
            "numero_processo": {"data_type": "text", "nullable": False},
            "documento_id": {"data_type": "text", "nullable": False},
            "documento_nome": {"data_type": "text", "nullable": False},
            "banco": {"data_type": "text", "nullable": True},
            "codigo_conta": {"data_type": "text", "nullable": True},
            "codigo_agencia": {"data_type": "text", "nullable": True},
            "conta": {"data_type": "text", "nullable": True},
        },
    )
    nota_fiscal_resource = dlt.resource(
        list(nota_fiscal_rows),
        name=NOTA_FISCAL_TABLE_NAME,
        primary_key="id_registro",
        write_disposition="merge",
        columns={
            "id_registro": {"data_type": "text", "nullable": False},
            "numero_processo": {"data_type": "text", "nullable": False},
            "documento_id": {"data_type": "text", "nullable": False},
            "documento_nome": {"data_type": "text", "nullable": False},
            "numero_nfse": {"data_type": "text", "nullable": True},
            "chave_acesso_nfse": {"data_type": "text", "nullable": True},
            "cnpj_cpf_nif_prestador": {
                "data_type": "text",
                "nullable": True,
            },
        },
    )
    return [
        process_resource,
        status_resource,
        cogestao_resource,
        empenho_resource,
        nota_fiscal_resource,
    ]


def _cogestao_columns() -> dict[str, dict[str, Any]]:
    columns: dict[str, dict[str, Any]] = {
        name: {"data_type": "text", "nullable": True}
        for name in (
            "id_registro",
            "cnpj",
            "nome_prestador",
            "numero_processo",
            "competencia_tms",
            "competencia_producao",
            "nr",
            "nr_origem",
            "processo_recurso",
            "processo_origem",
            "documento_id",
            "documento_nome",
        )
    }
    for name in ("id_registro", "processo_origem", "documento_id"):
        columns[name]["nullable"] = False
    columns["data_fechamento"] = {"data_type": "date", "nullable": True}
    columns["pagina_pdf"] = {"data_type": "bigint", "nullable": False}
    for name in (
        "valor_informado",
        "valor_aprovado_producao",
        "valor_glosado_producao",
        "valor_protocolo",
        "valor_aprovado_protocolo",
        "valor_glosado_protocolo",
    ):
        columns[name] = {
            "data_type": "decimal",
            "precision": 18,
            "scale": 2,
            "nullable": True,
        }
    return columns


def process_report_resource(
    rows: Iterable[dict[str, Any]],
) -> Any:
    return dlt.resource(
        list(rows),
        name=PROCESS_REPORT_TABLE_NAME,
        primary_key="id_registro",
        write_disposition="merge",
        columns={
            "id_registro": {"data_type": "text", "nullable": False},
            "numero_processo": {"data_type": "text", "nullable": False},
            "status_processo": {"data_type": "text", "nullable": False},
            "documento_id": {"data_type": "text", "nullable": False},
            "documento_nome": {"data_type": "text", "nullable": False},
            "pagina_pdf": {"data_type": "bigint", "nullable": False},
            "cd_remessa": {"data_type": "bigint", "nullable": False},
            "nome_paciente": {"data_type": "text", "nullable": False},
            "numero_guia": {"data_type": "text", "nullable": True},
            "numero_conta": {"data_type": "text", "nullable": True},
            "cd_atendimento": {"data_type": "bigint", "nullable": True},
            "competencia": {"data_type": "date", "nullable": False},
            "valor": {
                "data_type": "decimal",
                "precision": 18,
                "scale": 2,
                "nullable": False,
            },
            "extraido_em": {"data_type": "timestamp", "nullable": False},
        },
    )

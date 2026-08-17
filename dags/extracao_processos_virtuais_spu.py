from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.operators.python import get_current_context
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.spu_auth import (
    SpuInteractiveAuthError,
    renew_spu_session,
)
from nfs_fortaleza.spu_config import SpuSettings, load_spu_settings
from nfs_fortaleza.spu_extraction import (
    SpuExtractionPayload,
    extract_and_load_spu_pdfs,
    load_spu_process_batches,
)
from nfs_fortaleza.spu_portal import SpuSessionExpiredError


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


POSTGRES_CONN_ID = os.getenv(
    "SPU_POSTGRES_CONN_ID",
    os.getenv("NFSE_POSTGRES_CONN_ID", "postgres_prontocardio"),
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
DOWNLOADS_DIR = Path(
    os.getenv("SPU_DOWNLOADS_DIR", "/usr/local/airflow/data/spu")
)
NOVNC_PORT = os.getenv("SPU_NOVNC_PORT", "6080")


def _execute_with_session_renewal(
    settings: SpuSettings,
    operation: Callable[[], T],
) -> T:
    try:
        return operation()
    except SpuSessionExpiredError as expired:
        if not settings.auto_renew_session:
            raise AirflowFailException(str(expired)) from expired

        LOGGER.warning(
            "Sessao SPU expirada. Abrindo a renovacao interativa no "
            "navegador visivel do scheduler. Acesse o desktop protegido "
            "pelo tunel SSH em "
            f"http://localhost:{NOVNC_PORT}/vnc.html?autoconnect=true"
            "&resize=scale."
        )
        try:
            renew_spu_session(settings)
        except SpuInteractiveAuthError as auth_error:
            raise AirflowFailException(str(auth_error)) from auth_error

        LOGGER.info(
            "Sessao SPU renovada. Retomando a operacao que foi interrompida."
        )
        try:
            return operation()
        except SpuSessionExpiredError as repeated:
            raise AirflowFailException(
                "A sessao SPU continuou expirada depois da renovacao humana."
            ) from repeated


@dag(
    dag_id="extracao_processos_virtuais_spu",
    description=(
        "Extrai novos processos virtuais do SPU, materializa PDFs do IPM e "
        "carrega os dados com dlt."
    ),
    schedule=os.getenv("SPU_EXTRACTION_SCHEDULE", "0 5 * * *"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=15),
        "execution_timeout": timedelta(hours=8),
    },
    tags=["spu", "processos-virtuais", "pdf", "ipm", "dlt"],
)
def extracao_processos_virtuais_spu():
    @task(task_id="carregar_processos", pool="nfse_portal")
    def carregar_processos() -> dict[str, object]:
        context = get_current_context()
        payload = SpuExtractionPayload.from_mapping(context["dag_run"].conf)

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ["DATABASE_URL"] = hook.get_uri()
        os.environ["POSTGRES_SCHEMA"] = POSTGRES_SCHEMA
        settings = load_spu_settings()
        summary = _execute_with_session_renewal(
            settings,
            lambda: load_spu_process_batches(
                settings,
                payload,
                downloads_dir=DOWNLOADS_DIR,
            ),
        )
        return summary.as_dict()

    @task(task_id="processar_pdfs", pool="nfse_portal")
    def processar_pdfs(process_summary: dict[str, object]) -> dict[str, object]:
        raw_numbers = process_summary.get("finalizados_para_pdf", [])
        process_numbers = tuple(str(number) for number in raw_numbers)

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ["DATABASE_URL"] = hook.get_uri()
        os.environ["POSTGRES_SCHEMA"] = POSTGRES_SCHEMA
        settings = load_spu_settings()
        summary = _execute_with_session_renewal(
            settings,
            lambda: extract_and_load_spu_pdfs(
                settings,
                process_numbers,
                downloads_dir=DOWNLOADS_DIR,
            ),
        )
        return summary.as_dict()

    processamento = processar_pdfs(carregar_processos())
    relatorios_tramitando = TriggerDagRunOperator(
        task_id="acionar_relatorios_tramitando_spu",
        trigger_dag_id="extracao_relatorios_tramitando_spu",
        wait_for_completion=True,
        poke_interval=30,
    )
    materializar = TriggerDagRunOperator(
        task_id="acionar_materializacao_glosas_ipm",
        trigger_dag_id="materializacao_glosas_ipm",
        wait_for_completion=False,
    )
    processamento >> relatorios_tramitando >> materializar


dag = extracao_processos_virtuais_spu()

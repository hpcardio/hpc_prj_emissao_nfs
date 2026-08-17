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
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.spu_auth import SpuInteractiveAuthError, renew_spu_session
from nfs_fortaleza.spu_config import SpuSettings, load_spu_settings
from nfs_fortaleza.spu_extraction import (
    SpuExtractionPayload,
    extract_and_load_process_reports,
)
from nfs_fortaleza.spu_portal import SpuSessionExpiredError


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
POSTGRES_CONN_ID = os.getenv(
    "SPU_POSTGRES_CONN_ID",
    os.getenv("NFSE_POSTGRES_CONN_ID", "postgres_prontocardio"),
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
DOWNLOADS_DIR = Path(os.getenv("SPU_DOWNLOADS_DIR", "/usr/local/airflow/data/spu"))
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
            "Sessão SPU expirada. Renove-a em "
            f"http://localhost:{NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale."
        )
        try:
            renew_spu_session(settings)
        except SpuInteractiveAuthError as auth_error:
            raise AirflowFailException(str(auth_error)) from auth_error
        return operation()


@dag(
    dag_id="extracao_relatorios_tramitando_spu",
    description=(
        "Extrai RELATORIO_<remessa>_<id>.pdf de processos SPU finalizados "
        "ou em tramitação desde 2024."
    ),
    schedule=None,
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
    tags=[
        "spu",
        "processos",
        "finalizado",
        "tramitando",
        "pdf",
        "ipm",
        "dlt",
    ],
)
def extracao_relatorios_tramitando_spu():
    @task(task_id="extrair_relatorios", pool="nfse_portal")
    def extrair_relatorios() -> dict[str, object]:
        context = get_current_context()
        payload = SpuExtractionPayload.from_mapping(context["dag_run"].conf)
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ["DATABASE_URL"] = hook.get_uri()
        os.environ["POSTGRES_SCHEMA"] = POSTGRES_SCHEMA
        settings = load_spu_settings()
        summary = _execute_with_session_renewal(
            settings,
            lambda: extract_and_load_process_reports(
                settings,
                payload.numero_processos,
                downloads_dir=DOWNLOADS_DIR / "relatorios_processos",
            ),
        )
        return summary.as_dict()

    extrair_relatorios()


dag = extracao_relatorios_tramitando_spu()

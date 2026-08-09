from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.decorators import dag, task
from airflow.operators.python import get_current_context
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.config import load_settings
from nfs_fortaleza.extraction import (
    ExtractionPayload,
    extract_and_load_nfse,
)
from nfs_fortaleza.portal import PortalOptions


POSTGRES_CONN_ID = os.getenv(
    "NFSE_POSTGRES_CONN_ID",
    "postgres_prontocardio",
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
DOWNLOADS_DIR = Path(
    os.getenv("NFSE_DOWNLOADS_DIR", "/usr/local/airflow/data/nfse")
)
ARTIFACTS_DIR = Path(
    os.getenv("NFSE_ARTIFACTS_DIR", "/usr/local/airflow/data/artifacts")
)


@dag(
    dag_id="extracao_nfse",
    description="Extrai NFS-e do ISS Fortaleza e carrega a tabela nfse_xml.",
    schedule=os.getenv("NFSE_EXTRACTION_SCHEDULE", "*/15 * * * *"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),
    },
    tags=["nfse", "extracao", "dlt"],
)
def extracao_nfse():
    @task(task_id="extrair_e_carregar", pool="nfse_portal")
    def extrair_e_carregar() -> dict[str, object]:
        context = get_current_context()
        dag_run = context["dag_run"]
        reference_date = context["data_interval_end"].in_timezone(
            "America/Fortaleza"
        ).date()
        payload = ExtractionPayload.from_mapping(
            dag_run.conf,
            default_date=reference_date,
        )

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ.setdefault("DATABASE_URL", hook.get_uri())
        os.environ.setdefault("POSTGRES_SCHEMA", POSTGRES_SCHEMA)
        settings = load_settings()
        summary = extract_and_load_nfse(
            settings,
            payload,
            options=PortalOptions(
                downloads_dir=DOWNLOADS_DIR,
                artifacts_dir=ARTIFACTS_DIR,
                query_date=reference_date,
            ),
        )
        return summary.as_dict()

    extrair_e_carregar()


dag = extracao_nfse()

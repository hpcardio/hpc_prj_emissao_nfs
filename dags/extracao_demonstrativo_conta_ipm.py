from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.decorators import dag, task
from airflow.operators.python import get_current_context
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.ipm_config import load_ipm_settings
from nfs_fortaleza.ipm_extraction import (
    IpmExtractionPayload,
    extract_and_load_ipm_demonstratives,
)


POSTGRES_CONN_ID = os.getenv(
    "IPM_POSTGRES_CONN_ID",
    os.getenv("NFSE_POSTGRES_CONN_ID", "postgres_prontocardio"),
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
DOWNLOADS_DIR = Path(
    os.getenv("IPM_DOWNLOADS_DIR", "/usr/local/airflow/data/ipm")
)
TIMEOUT_SECONDS = float(os.getenv("IPM_TIMEOUT_SECONDS", "60"))


@dag(
    dag_id="extracao_demonstrativo_conta_ipm",
    description=(
        "Extrai somente novas referencias de contas medicas do IPM Saude e "
        "carrega demonstrativo_conta_ipm."
    ),
    schedule=os.getenv("IPM_EXTRACTION_SCHEDULE", "0 4 * * *"),
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
    tags=["ipm", "demonstrativo", "conta-medica", "dlt"],
)
def extracao_demonstrativo_conta_ipm():
    @task(task_id="extrair_e_carregar")
    def extrair_e_carregar() -> dict[str, object]:
        context = get_current_context()
        payload = IpmExtractionPayload.from_mapping(context["dag_run"].conf)

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ["DATABASE_URL"] = hook.get_uri()
        os.environ["POSTGRES_SCHEMA"] = POSTGRES_SCHEMA
        settings = load_ipm_settings()
        summary = extract_and_load_ipm_demonstratives(
            settings,
            payload,
            downloads_dir=DOWNLOADS_DIR,
            timeout_seconds=TIMEOUT_SECONDS,
        )
        return summary.as_dict()

    extracao = extrair_e_carregar()
    materializar = TriggerDagRunOperator(
        task_id="acionar_materializacao_glosas_ipm",
        trigger_dag_id="materializacao_glosas_ipm",
        wait_for_completion=False,
    )
    extracao >> materializar


dag = extracao_demonstrativo_conta_ipm()

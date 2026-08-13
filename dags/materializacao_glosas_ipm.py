from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.glosas_ipm_stage import carregar_hpc_intermediaria


POSTGRES_CONN_ID = os.getenv(
    "IPM_POSTGRES_CONN_ID",
    os.getenv("NFSE_POSTGRES_CONN_ID", "postgres_prontocardio"),
)
ORACLE_CONN_ID = os.getenv("IPM_ORACLE_CONN_ID", "oracle_prontocardio")
ORACLE_CLIENT_LIB_DIR = os.getenv(
    "ORACLE_CLIENT_LIB_DIR", "/opt/oracle/instantclient_19_23"
)
DBT_PROJECT_DIR = Path(
    os.getenv("GLOSAS_IPM_DBT_PROJECT_DIR", "/usr/local/airflow/dbt_glosas_ipm")
)


@dag(
    dag_id="materializacao_glosas_ipm",
    description=(
        "Carrega tabelas intermediárias Oracle e executa o projeto dbt das "
        "sete regras de glosas IPM."
    ),
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    tags=["ipm", "glosas", "dbt"],
)
def materializacao_glosas_ipm():
    @task(task_id="carregar_hpc_intermediaria", pool="oracle")
    def carregar_hpc() -> dict[str, int]:
        try:
            from airflow.providers.oracle.hooks.oracle import OracleHook
        except ModuleNotFoundError as exc:
            raise AirflowFailException(
                "O provider Oracle não está instalado na imagem do Airflow. "
                "Reconstrua o ambiente após atualizar requirements.txt."
            ) from exc
        oracle = OracleHook(
            oracle_conn_id=ORACLE_CONN_ID,
            thick_mode=True,
            thick_mode_lib_dir=ORACLE_CLIENT_LIB_DIR,
        ).get_conn()
        postgres = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID).get_conn()
        try:
            return carregar_hpc_intermediaria(oracle, postgres)
        finally:
            oracle.close()
            postgres.close()

    @task(task_id="executar_dbt")
    def executar_dbt(_: dict[str, int]) -> str:
        conexao = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID).get_connection(
            POSTGRES_CONN_ID
        )
        ambiente = {
            **os.environ,
            "DBT_POSTGRES_HOST": conexao.host,
            "DBT_POSTGRES_PORT": str(conexao.port or 5432),
            "DBT_POSTGRES_USER": conexao.login,
            "DBT_POSTGRES_PASSWORD": conexao.password or "",
            "DBT_POSTGRES_DBNAME": conexao.schema,
        }
        resultado = subprocess.run(
            [
                "dbt", "build", "--project-dir", str(DBT_PROJECT_DIR),
                "--profiles-dir", str(DBT_PROJECT_DIR),
            ],
            env=ambiente,
            capture_output=True,
            text=True,
            check=False,
        )
        if resultado.returncode:
            raise AirflowFailException(
                (resultado.stderr or resultado.stdout)[-8000:]
            )
        return resultado.stdout[-8000:]

    executar_dbt(carregar_hpc())


dag = materializacao_glosas_ipm()

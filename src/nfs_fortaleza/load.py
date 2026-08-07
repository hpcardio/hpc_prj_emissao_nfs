from __future__ import annotations

import os
from pathlib import Path

import dlt
import psycopg2
from psycopg2 import sql

from nfs_fortaleza.config import Settings
from nfs_fortaleza.nfse_xml import (
    NfseIdentity,
    nfse_identity,
    nfse_xml_resource,
)
from nfs_fortaleza.periods import DateRangePeriod, MonthPeriod


QueryPeriod = MonthPeriod | DateRangePeriod


def _destination_table_exists(
    settings: Settings,
    table_name: str,
) -> bool:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = %s
                )
                """,
                (settings.postgres_schema, table_name),
            )
            return bool(cursor.fetchone()[0])


def _existing_nfse_keys(
    settings: Settings,
    table_name: str,
) -> set[NfseIdentity]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT codigo_verificacao_nfse, "
                    "prestador_cnpj, numero_nfse "
                    "FROM {}.{} "
                    "WHERE NULLIF(BTRIM(codigo_verificacao_nfse), '') "
                    "IS NOT NULL "
                    "OR (prestador_cnpj IS NOT NULL "
                    "AND numero_nfse IS NOT NULL)"
                ).format(
                    sql.Identifier(settings.postgres_schema),
                    sql.Identifier(table_name),
                )
            )
            identities = {
                identity
                for (
                    codigo_verificacao_nfse,
                    prestador_cnpj,
                    numero_nfse,
                ) in cursor.fetchall()
                if (
                    identity := nfse_identity(
                        codigo_verificacao_nfse,
                        prestador_cnpj=prestador_cnpj,
                        numero_nfse=numero_nfse,
                    )
                )
                is not None
            }
    return identities


def load_nfse_xml(
    settings: Settings,
    file_path: Path,
    competencia: QueryPeriod,
    *,
    table_name: str = "nfse_xml",
):
    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url

    table_exists = _destination_table_exists(settings, table_name)
    existing_nfse_keys = (
        _existing_nfse_keys(settings, table_name)
        if table_exists
        else set()
    )

    resource = nfse_xml_resource(
        file_path,
        competencia,
        table_name=table_name,
        existing_nfse_keys=tuple(sorted(existing_nfse_keys)),
    )

    pipeline = dlt.pipeline(
        pipeline_name="iss_fortaleza",
        destination="postgres",
        dataset_name=settings.postgres_schema,
    )
    if table_exists:
        return pipeline.run(resource)

    # A table managed by dlt may have been removed directly from PostgreSQL
    # while its schema history remains in _dlt_version. Reset only this
    # resource so dlt emits the CREATE TABLE before running the merge job.
    return pipeline.run(resource, refresh="drop_resources")

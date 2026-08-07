from __future__ import annotations

import os
from pathlib import Path

import dlt
import psycopg2

from nfs_fortaleza.config import Settings
from nfs_fortaleza.nfse_xml import nfse_xml_resource
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


def load_nfse_xml(
    settings: Settings,
    file_path: Path,
    competencia: QueryPeriod,
    *,
    table_name: str = "nfse_xml",
):
    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url

    table_exists = _destination_table_exists(settings, table_name)

    resource = nfse_xml_resource(
        file_path,
        competencia,
        table_name=table_name,
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

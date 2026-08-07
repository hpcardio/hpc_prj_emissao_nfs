from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nfs_fortaleza.config import Settings
from nfs_fortaleza.load import (
    _destination_table_exists,
    _existing_nfse_keys,
    load_nfse_xml,
)
from nfs_fortaleza.periods import MonthPeriod


def _settings() -> Settings:
    return Settings(
        portal_url="https://example.test/grpfor/home.seam",
        cpf_login="00000000000",
        senha="secret",
        database_url="postgresql://database.test/nfse",
        postgres_schema="api_prontocardio",
    )


class DestinationTableTests(unittest.TestCase):
    @patch("nfs_fortaleza.load.psycopg2.connect")
    def test_checks_the_configured_schema_and_table(self, connect) -> None:
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (True,)

        exists = _destination_table_exists(_settings(), "nfse_xml")

        self.assertTrue(exists)
        connect.assert_called_once_with("postgresql://database.test/nfse")
        self.assertEqual(
            cursor.execute.call_args.args[1],
            ("api_prontocardio", "nfse_xml"),
        )

    @patch("nfs_fortaleza.load.psycopg2.connect")
    def test_reads_existing_nfse_identities(self, connect) -> None:
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            (" chave-abc ", "11.111.111/0001-11", "000123"),
            (None, "22.222.222/0001-22", "000456"),
            (None, None, "789"),
        ]

        identities = _existing_nfse_keys(_settings(), "nfse_xml")

        self.assertEqual(
            identities,
            {
                ("codigo_verificacao_nfse", "CHAVE-ABC"),
                ("prestador_numero_nfse", "22222222000122", "456"),
            },
        )
        connect.assert_called_once_with("postgresql://database.test/nfse")

    @patch("nfs_fortaleza.load.nfse_xml_resource")
    @patch("nfs_fortaleza.load.dlt.pipeline")
    @patch(
        "nfs_fortaleza.load._existing_nfse_keys",
        return_value={("codigo_verificacao_nfse", "CHAVE-ABC")},
    )
    @patch("nfs_fortaleza.load._destination_table_exists", return_value=True)
    def test_uses_regular_merge_when_table_exists(
        self,
        table_exists,
        existing_keys,
        pipeline_factory,
        resource_factory,
    ) -> None:
        resource = MagicMock()
        resource_factory.return_value = resource
        pipeline = pipeline_factory.return_value

        load_nfse_xml(
            _settings(),
            Path("nota.xml"),
            MonthPeriod(2026, 7),
        )

        table_exists.assert_called_once_with(_settings(), "nfse_xml")
        existing_keys.assert_called_once_with(_settings(), "nfse_xml")
        resource_factory.assert_called_once_with(
            Path("nota.xml"),
            MonthPeriod(2026, 7),
            table_name="nfse_xml",
            existing_nfse_keys=(("codigo_verificacao_nfse", "CHAVE-ABC"),),
        )
        pipeline.run.assert_called_once_with(resource)

    @patch("nfs_fortaleza.load.nfse_xml_resource")
    @patch("nfs_fortaleza.load.dlt.pipeline")
    @patch("nfs_fortaleza.load._existing_nfse_keys")
    @patch("nfs_fortaleza.load._destination_table_exists", return_value=False)
    def test_recreates_resource_when_table_was_removed_outside_dlt(
        self,
        table_exists,
        existing_keys,
        pipeline_factory,
        resource_factory,
    ) -> None:
        resource = MagicMock()
        resource_factory.return_value = resource
        pipeline = pipeline_factory.return_value

        load_nfse_xml(
            _settings(),
            Path("nota.xml"),
            MonthPeriod(2026, 7),
        )

        table_exists.assert_called_once_with(_settings(), "nfse_xml")
        existing_keys.assert_not_called()
        resource_factory.assert_called_once_with(
            Path("nota.xml"),
            MonthPeriod(2026, 7),
            table_name="nfse_xml",
            existing_nfse_keys=(),
        )
        pipeline.run.assert_called_once_with(
            resource,
            refresh="drop_resources",
        )


if __name__ == "__main__":
    unittest.main()

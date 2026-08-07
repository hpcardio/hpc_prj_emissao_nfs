from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nfs_fortaleza.nfse_xml import (
    _filter_new_nfse_records,
    nfse_identity,
    nfse_xml_resource,
    parse_nfse_xml,
)
from nfs_fortaleza.periods import MonthPeriod


class ParseNfseXmlTests(unittest.TestCase):
    def test_reads_competencia_directly_from_inf_nfse(self) -> None:
        xml = b"""
        <CompNfse>
          <Nfse>
            <InfNfse>
              <Numero>123</Numero>
              <Competencia>2026-01-01</Competencia>
            </InfNfse>
          </Nfse>
        </CompNfse>
        """

        [record] = parse_nfse_xml(xml, "202601", "nota.xml")

        self.assertEqual(record["competencia_nfse"], "2026-01-01")


class NfseIdentityTests(unittest.TestCase):
    def test_normalizes_cnpj_and_invoice_number(self) -> None:
        self.assertEqual(
            nfse_identity("11.111.111/0001-11", "000123"),
            ("11111111000111", "123"),
        )

    def test_filters_database_records_and_duplicates_from_same_file(self) -> None:
        existing = {("11111111000111", "1")}
        records = [
            {
                "prestador_cnpj": "11.111.111/0001-11",
                "numero_nfse": "0001",
            },
            {
                "prestador_cnpj": "22.222.222/0001-22",
                "numero_nfse": "2",
            },
            {
                "prestador_cnpj": "22.222.222/0001-22",
                "numero_nfse": "0002",
            },
        ]

        result = list(_filter_new_nfse_records(records, existing))

        self.assertEqual(result, [records[1]])
        self.assertEqual(
            existing,
            {("11111111000111", "1"), ("22222222000122", "2")},
        )

    def test_dlt_resource_does_not_yield_an_existing_invoice(self) -> None:
        xml = b"""
        <CompNfse>
          <Nfse>
            <InfNfse>
              <Numero>000123</Numero>
              <PrestadorServico>
                <IdentificacaoPrestador>
                  <Cnpj>11111111000111</Cnpj>
                </IdentificacaoPrestador>
              </PrestadorServico>
            </InfNfse>
          </Nfse>
        </CompNfse>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nota.xml"
            path.write_bytes(xml)
            resource = nfse_xml_resource(
                path,
                MonthPeriod(2026, 1),
                existing_nfse_keys={("11111111000111", "123")},
            )

            records = list(resource)

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()

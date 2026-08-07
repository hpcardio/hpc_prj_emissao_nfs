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
    def test_uses_normalized_verification_code_as_invoice_key(self) -> None:
        self.assertEqual(
            nfse_identity(
                " chave-abc ",
                prestador_cnpj="11.111.111/0001-11",
                numero_nfse="000123",
            ),
            ("codigo_verificacao_nfse", "CHAVE-ABC"),
        )

    def test_falls_back_to_normalized_provider_and_number_without_key(self) -> None:
        self.assertEqual(
            nfse_identity(
                None,
                prestador_cnpj="11.111.111/0001-11",
                numero_nfse="000123",
            ),
            ("prestador_numero_nfse", "11111111000111", "123"),
        )

    def test_filters_existing_key_and_keeps_same_number_with_new_key(self) -> None:
        existing = {("codigo_verificacao_nfse", "CHAVE-1")}
        records = [
            {
                "codigo_verificacao_nfse": " chave-1 ",
                "prestador_cnpj": "11.111.111/0001-11",
                "numero_nfse": "0001",
            },
            {
                "codigo_verificacao_nfse": "CHAVE-2",
                "prestador_cnpj": "11.111.111/0001-11",
                "numero_nfse": "0001",
            },
            {
                "codigo_verificacao_nfse": "chave-2",
                "prestador_cnpj": "33.333.333/0001-33",
                "numero_nfse": "9999",
            },
        ]

        result = list(_filter_new_nfse_records(records, existing))

        self.assertEqual(result, [records[1]])
        self.assertEqual(
            existing,
            {
                ("codigo_verificacao_nfse", "CHAVE-1"),
                ("codigo_verificacao_nfse", "CHAVE-2"),
            },
        )

    def test_dlt_resource_does_not_yield_an_existing_invoice(self) -> None:
        xml = b"""
        <CompNfse>
          <Nfse>
            <InfNfse>
              <Numero>000123</Numero>
              <CodigoVerificacao>Chave-ABC</CodigoVerificacao>
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
                existing_nfse_keys={
                    ("codigo_verificacao_nfse", "CHAVE-ABC")
                },
            )

            records = list(resource)

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()

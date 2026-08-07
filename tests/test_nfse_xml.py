from __future__ import annotations

import unittest

from nfs_fortaleza.nfse_xml import parse_nfse_xml


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


if __name__ == "__main__":
    unittest.main()

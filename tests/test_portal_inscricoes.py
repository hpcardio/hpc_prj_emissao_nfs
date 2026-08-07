from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from nfs_fortaleza.config import Settings
from nfs_fortaleza.periods import MonthPeriod
from nfs_fortaleza.portal import (
    InscricaoRow,
    PortalClient,
    PortalOptions,
    _validate_exported_inscricao,
)


class ExportCompetenciaInscricoesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            portal_url="https://example.test/grpfor/home.seam",
            cpf_login="00000000000",
            senha="secret",
            database_url="postgresql://example.test/db",
            postgres_schema="test",
        )

    def test_uses_a_fresh_session_for_each_additional_inscricao(self) -> None:
        first = InscricaoRow("0", "11.111.111/0001-11", "100", "Empresa A")
        second = InscricaoRow("1", "22.222.222/0001-22", "200", "Empresa B")
        first_session = Mock(name="first_session")
        second_session = Mock(name="second_session")
        home = Mock(url="https://example.test/grpfor/home.seam")

        with tempfile.TemporaryDirectory() as directory:
            client = PortalClient(
                self.settings,
                PortalOptions(
                    downloads_dir=Path(directory),
                    artifacts_dir=Path(directory),
                ),
            )
            first_export = Path(directory) / "first.xml"
            second_export = Path(directory) / "second.xml"

            with (
                patch.object(
                    client,
                    "_login_dlt_session",
                    side_effect=(first_session, second_session),
                ) as login,
                patch.object(
                    client,
                    "_available_inscricoes",
                    side_effect=([first, second], [first, second]),
                ) as available,
                patch.object(client, "_request_get", return_value=home),
                patch.object(client, "_select_inscricao") as select,
                patch.object(
                    client,
                    "_export_competencia_with_requests",
                    side_effect=(first_export, second_export),
                ) as export,
                patch(
                    "nfs_fortaleza.portal._validate_exported_inscricao"
                ) as validate,
            ):
                result = client.export_competencia(
                    MonthPeriod(year=2026, month=1)
                )

        self.assertEqual(result, [first_export, second_export])
        self.assertEqual(login.call_count, 2)
        self.assertEqual(available.call_count, 2)
        select.assert_has_calls(
            [call(first_session, home, first), call(second_session, home, second)]
        )
        export.assert_has_calls(
            [
                call(first_session, MonthPeriod(year=2026, month=1), first),
                call(second_session, MonthPeriod(year=2026, month=1), second),
            ]
        )
        validate.assert_has_calls(
            [call(first_export, first), call(second_export, second)]
        )


class ValidateExportedInscricaoTests(unittest.TestCase):
    def test_accepts_xml_from_selected_inscricao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nota.xml"
            path.write_text(_xml("11111111000111"), encoding="utf-8")

            _validate_exported_inscricao(
                path,
                InscricaoRow("0", "11.111.111/0001-11", "100", "Empresa A"),
            )

    def test_rejects_xml_from_another_inscricao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nota.xml"
            path.write_text(_xml("22222222000122"), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "nao pertence a inscricao selecionada",
            ):
                _validate_exported_inscricao(
                    path,
                    InscricaoRow(
                        "0",
                        "11.111.111/0001-11",
                        "100",
                        "Empresa A",
                    ),
                )


def _xml(cnpj: str) -> str:
    return f"""
    <CompNfse>
      <Nfse>
        <InfNfse>
          <PrestadorServico>
            <IdentificacaoPrestador><Cnpj>{cnpj}</Cnpj></IdentificacaoPrestador>
          </PrestadorServico>
        </InfNfse>
      </Nfse>
    </CompNfse>
    """


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from nfs_fortaleza.issuance import (
    IssuanceLedger,
    IssuanceResult,
    _extract_direct_form_state,
    _extract_invoice_number,
    _extract_pdf_url,
    _find_suggestion_selection_action,
    _merge_preserving_values,
    _script_action_ids,
    _select_option_value,
    _suggestion_selection_value,
    _taker_form_values,
    _taker_values_from_state,
    filter_unissued_rows,
)
from nfs_fortaleza.spreadsheet import (
    InvoiceSpreadsheetRow,
    _cell_multiline_text,
    is_valid_cpf,
    load_invoice_rows,
    select_rows,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def example_row() -> InvoiceSpreadsheetRow:
    return InvoiceSpreadsheetRow(
        row_number=2,
        paciente="ANTONIO ÍRIS DE FREITAS",
        local="CLINICA 2",
        tipo_atendimento="AMBULATORIO",
        atendimento="317189",
        cpf="155.061.423-15",
        valor=Decimal("60.75"),
        rua="rua Alameda rosa Maria",
        numero_casa="219",
        bairro="cidade 2000",
        cidade="FORTALEZA",
        uf="CE",
        tipo_exame="ANÁLISE CLÍNICA",
        data="2026-05-13 00:00:00",
        email="",
    )


class SpreadsheetTests(unittest.TestCase):
    def test_loads_reference_first_row(self) -> None:
        rows = load_invoice_rows(PROJECT_ROOT / "NOTAS FISCAIS.xlsx")

        self.assertGreater(len(rows), 300)
        self.assertEqual(rows[0], example_row())
        self.assertEqual(rows[0].valor_br, "60,75")

    def test_validates_cpf_check_digits(self) -> None:
        self.assertTrue(is_valid_cpf("155.061.423-15"))
        self.assertFalse(is_valid_cpf("155.061.423-16"))
        self.assertFalse(is_valid_cpf("111.111.111-11"))

    def test_preserves_procedure_lines_from_spreadsheet(self) -> None:
        self.assertEqual(
            _cell_multiline_text(
                "40901106 - ECODOPPLERCARDIOGRAMA\n"
                "  20102038   -   MONITORIZACAO ARTERIAL  "
            ),
            "40901106 - ECODOPPLERCARDIOGRAMA\r\n"
            "20102038 - MONITORIZACAO ARTERIAL",
        )

    def test_requires_explicit_row_or_all(self) -> None:
        with self.assertRaises(ValueError):
            select_rows([example_row()], row_number=None, all_rows=False, limit=None)

    def test_builds_required_pdf_name(self) -> None:
        self.assertEqual(
            example_row().pdf_filename("8"),
            "8 - CLINICA 2, AMBULATORIO e ANTONIO ÍRIS DE FREITAS.pdf",
        )


class JsfParserTests(unittest.TestCase):
    def test_extracts_only_direct_controls_from_main_form(self) -> None:
        html = """
        <form id="emitirnfseForm">
          <input type="hidden" name="emitirnfseForm" value="emitirnfseForm" />
          <input name="emitirnfseForm:idNome" value="Maria" />
          <input name="emitirnfseForm:idFormularioPesquisaCnae:idCnaePesquisa" value="8610101" />
          <select name="emitirnfseForm:comboEscolherNbs">
            <option value="">...</option>
            <option value="476" selected="selected">123011900 - SERVIÇOS HOSPITALARES</option>
          </select>
          <input type="hidden" name="javax.faces.ViewState" value="state-1" />
        </form>
        """

        state = _extract_direct_form_state(html, "emitirnfseForm")

        self.assertEqual(state["emitirnfseForm"], "emitirnfseForm")
        self.assertEqual(state["emitirnfseForm:idNome"], "Maria")
        self.assertEqual(state["emitirnfseForm:comboEscolherNbs"], "476")
        self.assertNotIn("emitirnfseForm:idFormularioPesquisaCnae:idCnaePesquisa", state)

    def test_finds_option_by_portal_label_prefix(self) -> None:
        html = """
        <select name="emitirnfseForm:comboEscolherNbs">
          <option value="475">123011500 - OUTROS</option>
          <option value="476">123011900 - SERVIÇOS HOSPITALARES</option>
        </select>
        """

        self.assertEqual(
            _select_option_value(
                html,
                "emitirnfseForm:comboEscolherNbs",
                "123011900",
                prefix=True,
            ),
            "476",
        )

    def test_fills_all_available_taker_fields(self) -> None:
        html = """
        <select name="emitirnfseForm:comboEscolherPais">
          <option value="1">BRASIL</option>
        </select>
        <select name="emitirnfseForm:comboEscolherEstado">
          <option value="6">CE</option>
        </select>
        <select name="emitirnfseForm:comboEscolherCidade">
          <option value="1389">FORTALEZA</option>
        </select>
        """

        row = replace(
            example_row(),
            cep="60181110",
            complemento="APTO 101",
            telefone="(85) 99999-9999",
        )
        values = _taker_form_values(
            html,
            row,
            "emitirnfseForm",
        )

        self.assertEqual(values["emitirnfseForm:idCEP"], "60181110")
        self.assertEqual(
            values["emitirnfseForm:idEndereco"],
            "rua Alameda rosa Maria",
        )
        self.assertEqual(values["emitirnfseForm:idNumero"], "219")
        self.assertEqual(
            values["emitirnfseForm:idComplemento"],
            "APTO 101",
        )
        self.assertEqual(
            values["emitirnfseForm:idBairro"],
            "cidade 2000",
        )
        self.assertEqual(
            values["emitirnfseForm:idTelefone"],
            "(85) 99999-9999",
        )
        self.assertEqual(
            values["emitirnfseForm:comboEscolherCidade"],
            "1389",
        )

    def test_preserves_existing_optional_taker_data_when_source_is_empty(
        self,
    ) -> None:
        html = """
        <select name="emitirnfseForm:comboEscolherPais">
          <option value="1">BRASIL</option>
        </select>
        <select name="emitirnfseForm:comboEscolherEstado">
          <option value="6">CE</option>
        </select>
        <select name="emitirnfseForm:comboEscolherCidade">
          <option value="1389">FORTALEZA</option>
        </select>
        """
        row = replace(
            example_row(),
            cep="",
            complemento="",
            telefone="",
        )

        values = _taker_form_values(html, row, "emitirnfseForm")

        self.assertNotIn("emitirnfseForm:idCEP", values)
        self.assertNotIn("emitirnfseForm:idComplemento", values)
        self.assertNotIn("emitirnfseForm:idTelefone", values)

    def test_preserves_taker_data_when_ajax_returns_stale_registration(
        self,
    ) -> None:
        state = {
            "emitirnfseForm": "emitirnfseForm",
            "emitirnfseForm:idCEP": "60181110",
            "emitirnfseForm:idComplemento": "APTO 101",
            "emitirnfseForm:idTelefone": "(85) 99999-9999",
            "emitirnfseForm:idValorServicoPrestado": "60,75",
            "javax.faces.ViewState": "state-1",
        }
        stale_ajax = """
        <form id="emitirnfseForm">
          <input name="emitirnfseForm:idCEP" value="" />
          <input name="emitirnfseForm:idComplemento" value="" />
          <input name="emitirnfseForm:idTelefone" value="" />
          <input name="emitirnfseForm:idValorServicoPrestado" value="60,75" />
          <input
            type="hidden"
            name="javax.faces.ViewState"
            value="state-2"
          />
        </form>
        """
        taker_values = _taker_values_from_state(
            state,
            "emitirnfseForm",
        )

        merged = _merge_preserving_values(
            state,
            stale_ajax,
            "emitirnfseForm",
            taker_values,
        )

        self.assertEqual(merged["emitirnfseForm:idCEP"], "60181110")
        self.assertEqual(
            merged["emitirnfseForm:idComplemento"],
            "APTO 101",
        )
        self.assertEqual(
            merged["emitirnfseForm:idTelefone"],
            "(85) 99999-9999",
        )
        self.assertEqual(merged["javax.faces.ViewState"], "state-2")

    def test_extracts_jsf_actions_in_execution_order(self) -> None:
        script = """
        A4J.AJAX.Submit('emitirnfseForm',event,
          {'parameters':{'emitirnfseForm:j_id861':'emitirnfseForm:j_id861'}});
        A4J.AJAX.Submit('emitirnfseForm',event,
          {'parameters':{'emitirnfseForm:btnCalcular':'emitirnfseForm:btnCalcular'}});
        """

        self.assertEqual(
            _script_action_ids(script),
            ["emitirnfseForm:j_id861", "emitirnfseForm:btnCalcular"],
        )

    def test_extracts_success_number_and_pdf_endpoint(self) -> None:
        html = """
        <label>Número da Nota:</label><input value="8" />
        <object data="/grpfor/a4j/s/3_3_3.FinalResource.pdf"></object>
        """

        self.assertEqual(_extract_invoice_number(html), "8")
        self.assertEqual(
            _extract_pdf_url(html, "https://iss.example/grpfor/pages/sucesso.seam?cid=1"),
            "https://iss.example/grpfor/a4j/s/3_3_3.FinalResource.pdf",
        )

    def test_uses_richfaces_row_index_and_exact_onselect_action(self) -> None:
        html = """
        <script>
        new RichFaces.Suggestion(
          'emitirnfseForm','emitirnfseForm:cpfPesquisaTomador','emitirnfseForm:j_id216',
          {'onselect':function(suggestion,event){
            A4J.AJAX.Submit('emitirnfseForm',event,{
              'parameters':{
                'emitirnfseForm:j_id216:j_id223':'emitirnfseForm:j_id216:j_id223'
              }
            })
          }}
        );
        </script>
        <table>
          <tr class="richfaces_suggestionEntry">
            <td style="display:none">15506142315</td>
            <td>ANTONIO ÍRIS DE FREITAS</td>
          </tr>
          <tr><td>
            <input onclick="'emitirnfseForm:j_id265':'emitirnfseForm:j_id265'" />
          </td></tr>
        </table>
        """

        self.assertEqual(_suggestion_selection_value(html, example_row()), "0")
        self.assertEqual(
            _find_suggestion_selection_action(html, "emitirnfseForm:j_id216"),
            "emitirnfseForm:j_id216:j_id223",
        )


class LedgerTests(unittest.TestCase):
    def test_prevents_reissuing_a_successful_row(self) -> None:
        row = example_row()
        with tempfile.TemporaryDirectory() as directory:
            ledger = IssuanceLedger(Path(directory) / "ledger.jsonl")
            result = IssuanceResult(2, "8", Path(directory) / row.pdf_filename("8"), row.paciente, row.cpf, True)
            ledger.record_success(row, result)

            pending, skipped = filter_unissued_rows([row], ledger)

        self.assertEqual(pending, [])
        self.assertEqual(skipped, [row])


if __name__ == "__main__":
    unittest.main()

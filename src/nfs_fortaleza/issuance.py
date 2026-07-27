from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from dlt.sources.helpers.requests import Response, Session

from nfs_fortaleza.config import Settings
from nfs_fortaleza.portal import (
    PortalClient,
    PortalOptions,
    _extract_view_state,
    _find_inscricao_by_cnpj,
)
from nfs_fortaleza.spreadsheet import InvoiceSpreadsheetRow


ISSUER_CNPJ = "59932105000121"
EMISSION_MENU_LABEL = "Emitir NFS-e"
EMISSION_FORM = "emitirnfseForm"
REGISTRATION_FORM = "cadastrarCliente"
HOSPITAL_CNAE_SEARCH = "8610101"
HOSPITAL_CNAE_DESCRIPTION = (
    "ATIVIDADES DE ATENDIMENTO HOSPITALAR, EXCETO PRONTO-SOCORRO "
    "E UNIDADES PARA ATENDIMENTO A URGENCIAS"
)


class IssuancePortalError(RuntimeError):
    """Raised when the NFS-e portal rejects or cannot complete an issuance step."""


@dataclass(frozen=True)
class IssuanceResult:
    row_number: int
    numero_nfse: str
    pdf_path: Path
    paciente: str
    cpf: str
    client_registered: bool


class IssuanceLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def successful_hashes(self) -> set[str]:
        if not self.path.is_file():
            return set()
        successful: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == "success" and record.get("row_hash"):
                successful.add(str(record["row_hash"]))
        return successful

    def record_success(self, row: InvoiceSpreadsheetRow, result: IssuanceResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "status": "success",
            "row_hash": row.row_hash,
            "source_row": row.row_number,
            "numero_nfse": result.numero_nfse,
            "pdf_path": str(result.pdf_path),
            "paciente": row.paciente,
            "cpf": row.cpf_digits,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class NfseIssuanceClient(PortalClient):
    """Issues Fortaleza NFS-e using only the portal's HTTP/JSF endpoints."""

    def __init__(self, settings: Settings, options: PortalOptions | None = None) -> None:
        super().__init__(settings, options)
        self.options.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.options.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def issue(self, row: InvoiceSpreadsheetRow, *, cnpj: str = ISSUER_CNPJ) -> IssuanceResult:
        session = self._login_dlt_session()
        home = self._request_get(session, self.settings.portal_page("home.seam"))
        inscriptions = self._available_inscricoes(session)
        selected = _find_inscricao_by_cnpj(inscriptions, cnpj)
        self._select_inscricao(session, home, selected)

        emit_page = self._open_emission_page(session)
        emit_page, client_registered = self._load_or_register_taker(session, emit_page, row)
        configured_page, state = self._configure_service(session, emit_page, row)
        validated_page, state = self._validate_invoice(session, configured_page, state)
        success_page = self._confirm_invoice(session, emit_page, validated_page, state)
        numero_nfse = _extract_invoice_number(success_page.text)
        pdf_path = self._download_pdf(session, success_page, row, numero_nfse)

        return IssuanceResult(
            row_number=row.row_number,
            numero_nfse=numero_nfse,
            pdf_path=pdf_path,
            paciente=row.paciente,
            cpf=row.cpf_digits,
            client_registered=client_registered,
        )

    def download_existing_pdf(
        self,
        row: InvoiceSpreadsheetRow,
        numero_nfse: str,
        *,
        cnpj: str = ISSUER_CNPJ,
    ) -> Path:
        """Recover the PDF of an already issued NFS-e without issuing anything."""
        session = self._login_dlt_session()
        home = self._request_get(session, self.settings.portal_page("home.seam"))
        selected = _find_inscricao_by_cnpj(self._available_inscricoes(session), cnpj)
        self._select_inscricao(session, home, selected)

        query = self._open_nfse_query(session)
        query_url = query.url
        view_state = _extract_view_state(query.text)
        common = {
            "AJAXREQUEST": "_viewRoot",
            "consultarnfseForm": "consultarnfseForm",
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:numNfse": "",
            "AJAX:EVENTS_COUNT": "1",
        }
        for event_fields in (
            {"consultarnfseForm:j_id170": "consultarnfseForm:j_id170"},
            {
                "ajaxSingle": "consultarnfseForm:numero_doc_tab_id",
                "consultarnfseForm:j_id184": "consultarnfseForm:j_id184",
            },
            {"consultarnfseForm:numero_doc_tab_id": "consultarnfseForm:numero_doc_tab_id"},
        ):
            payload = dict(common)
            payload.update(event_fields)
            payload["javax.faces.ViewState"] = view_state
            view_state = self._ajax_post(session, query_url, payload, view_state)

        result = self._request_post(
            session,
            query_url,
            self._number_query_payload(numero_nfse, view_state),
            ajax=True,
        )
        result_view_state = _extract_view_state(result.text, default=view_state)
        visualize = _find_element_by_title(result.text, "Visualizar")
        actions = _script_action_ids(visualize.attrs.get("onclick", ""))
        if not actions:
            raise IssuancePortalError(f"Acao Visualizar da NFS-e {numero_nfse} nao encontrada.")
        payload = self._number_form_payload(numero_nfse, result_view_state)
        payload[actions[0]] = actions[0]
        visualization = self._request_post(session, query_url, payload, ajax=False)
        if numero_nfse != _extract_invoice_number(visualization.text):
            raise IssuancePortalError("A visualizacao retornada nao corresponde ao numero solicitado.")
        return self._download_pdf(session, visualization, row, numero_nfse)

    def _open_emission_page(self, session: Session) -> Response:
        home = self._request_get(session, self.settings.portal_page("home.seam"))
        try:
            element = _find_action_element(home.text, EMISSION_MENU_LABEL, form_prefix="formMenuTopo")
        except IssuancePortalError as exc:
            debug = self._save_http_artifact("menu_emitir_nfse_nao_encontrado", home.text)
            raise IssuancePortalError(f"{exc} Debug: {debug}") from exc
        action = _primary_action_id(element)
        state = _extract_direct_form_state(home.text, "formMenuTopo")
        response = self._request_post(
            session,
            home.url,
            _ajax_payload(state, action),
            ajax=True,
        )
        redirect = _extract_redirect_url(response.text, response.url)
        if not redirect:
            debug = self._save_http_artifact("emissao_menu_sem_redirect", response.text)
            raise IssuancePortalError(f"Menu Emitir NFS-e nao retornou o redirecionamento esperado. Debug: {debug}")
        page = self._request_get(session, redirect)
        if "Emitir NFS-e" not in _visible_text(page.text):
            debug = self._save_http_artifact("emissao_nao_abriu", page.text)
            raise IssuancePortalError(f"Tela Emitir NFS-e nao abriu. Debug: {debug}")
        return page

    def _load_or_register_taker(
        self,
        session: Session,
        emit_page: Response,
        row: InvoiceSpreadsheetRow,
    ) -> tuple[Response, bool]:
        searched, found = self._search_taker(session, emit_page, row)
        if found:
            return searched, False

        registration = self._open_client_registration(session, searched)
        saved = self._register_client(session, registration, row)
        if _taker_loaded(saved.text, row.cpf_digits):
            return saved, True

        reopened = self._open_emission_page(session)
        searched_again, found_again = self._search_taker(session, reopened, row)
        if not found_again:
            debug = self._save_http_artifact("cliente_cadastrado_nao_recuperado", searched_again.text)
            raise IssuancePortalError(
                f"Cliente da linha {row.row_number} foi cadastrado, mas nao foi recuperado pelo CPF. Debug: {debug}"
            )
        return searched_again, True

    def _search_taker(
        self,
        session: Session,
        emit_page: Response,
        row: InvoiceSpreadsheetRow,
    ) -> tuple[Response, bool]:
        state = _extract_direct_form_state(emit_page.text, EMISSION_FORM)
        cpf_field = f"{EMISSION_FORM}:cpfPesquisaTomador"
        radio_field = f"{EMISSION_FORM}:tipoPesquisaTomadorRb"
        suggestion_field = _find_suggestion_field(emit_page.text, EMISSION_FORM)
        state[radio_field] = "CPF"
        state[cpf_field] = row.cpf_formatted
        state[f"{suggestion_field}_selection"] = ""

        payload = dict(state)
        payload.update(
            {
                "AJAXREQUEST": "_viewRoot",
                suggestion_field: suggestion_field,
                "ajaxSingle": suggestion_field,
                "inputvalue": row.cpf_formatted,
                "AJAX:EVENTS_COUNT": "1",
            }
        )
        response = self._request_post(session, emit_page.url, payload, ajax=True)
        if not _has_suggestion_entry(response.text, row.cpf_digits):
            self._save_http_artifact("tomador_pesquisa_sem_resultado", response.text)
            return emit_page, False

        selected = self._select_taker_suggestion(
            session,
            emit_page,
            response,
            state,
            suggestion_field,
            row,
        )
        if not _taker_loaded(selected.text, row.cpf_digits):
            debug = self._save_http_artifact("tomador_nao_carregado", selected.text)
            raise IssuancePortalError(
                f"CPF {row.cpf_formatted} apareceu na pesquisa, mas os Dados do Tomador nao foram carregados. "
                f"Debug: {debug}"
            )
        return selected, True

    def _select_taker_suggestion(
        self,
        session: Session,
        emit_page: Response,
        suggestion_response: Response,
        state: dict[str, str],
        suggestion_field: str,
        row: InvoiceSpreadsheetRow,
    ) -> Response:
        state = _merge_direct_form_state(state, suggestion_response.text, EMISSION_FORM)
        selection_value = _suggestion_selection_value(suggestion_response.text, row)
        state[f"{suggestion_field}_selection"] = selection_value
        state[f"{EMISSION_FORM}:cpfPesquisaTomador"] = row.cpf_digits

        action = _find_suggestion_selection_action(emit_page.text, suggestion_field)
        selected = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(state, action),
            ajax=True,
        )
        state = _merge_direct_form_state(state, selected.text, EMISSION_FORM)

        followup_action = _find_named_function_action(emit_page.text, "chamarCarregarAbaExportacao")
        loaded = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(state, followup_action),
            ajax=True,
        )
        return _materialize_ajax_response(_materialize_ajax_response(emit_page, selected), loaded)

    def _open_client_registration(self, session: Session, emit_page: Response) -> Response:
        action = _find_modal_confirmation_action(
            emit_page.text,
            panel_marker="cadastro_cliente_modal_panel",
            label="Sim",
        )
        state = _extract_direct_form_state(emit_page.text, EMISSION_FORM)
        payload = dict(state)
        payload[action] = action
        response = self._request_post(session, emit_page.url, payload, ajax=False)
        if "Cadastrar Cliente/Fornecedor" not in _visible_text(response.text):
            debug = self._save_http_artifact("cadastro_cliente_nao_abriu", response.text)
            raise IssuancePortalError(f"Tela Cadastrar Cliente/Fornecedor nao abriu. Debug: {debug}")
        return response

    def _register_client(
        self,
        session: Session,
        registration: Response,
        row: InvoiceSpreadsheetRow,
    ) -> Response:
        type_field = f"{REGISTRATION_FORM}:comboEscolherTipoNaturezaJuridica"
        state = _extract_direct_form_state(registration.text, REGISTRATION_FORM)
        state[type_field] = _select_option_value(registration.text, type_field, "Pessoa Física")
        type_action = _select_event_action(registration.text, type_field)
        selected_type = self._request_post(
            session,
            registration.url,
            _ajax_payload(state, type_action),
            ajax=True,
        )
        state = _merge_direct_form_state(state, selected_type.text, REGISTRATION_FORM)

        registration_markup = registration.text + selected_type.text
        field_values = {
            f"{REGISTRATION_FORM}:idInscricaoMunicipalFortaleza": "",
        }
        field_values.update(
            _taker_form_values(
                registration_markup,
                row,
                REGISTRATION_FORM,
            )
        )
        state.update(field_values)
        save_element = _find_action_element(registration_markup, "Salvar")
        save_action = _primary_action_id(save_element)
        payload = dict(state)
        payload[save_action] = save_element.attrs.get("value", "Salvar") or "Salvar"
        response = self._request_post(session, registration.url, payload, ajax=False)
        messages = _extract_error_messages(response.text)
        if messages:
            debug = self._save_http_artifact("cadastro_cliente_rejeitado", response.text)
            raise IssuancePortalError(f"Cadastro do cliente rejeitado: {'; '.join(messages)}. Debug: {debug}")
        return response

    def _configure_service(
        self,
        session: Session,
        emit_page: Response,
        row: InvoiceSpreadsheetRow,
    ) -> tuple[Response, dict[str, str]]:
        state = _extract_direct_form_state(emit_page.text, EMISSION_FORM)
        state.update(_taker_form_values(emit_page.text, row, EMISSION_FORM))
        nested_form = f"{EMISSION_FORM}:idFormularioPesquisaCnae"
        nested_state = _extract_direct_form_state(emit_page.text, nested_form)
        nested_state[f"{nested_form}:idCnaePesquisa"] = HOSPITAL_CNAE_SEARCH
        nested_state[f"{nested_form}:idAtividadeCpbsDescricaoPesquisa"] = ""
        search_element = _find_action_element(emit_page.text, "Pesquisar", form_prefix=nested_form)
        search_action = _primary_action_id(search_element)
        container = _ajax_container(search_element.attrs.get("onclick", "")) or "_viewRoot"
        searched = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(nested_state, search_action, container=container),
            ajax=True,
        )

        row_index = _find_cnae_row_index(searched.text, HOSPITAL_CNAE_DESCRIPTION)
        select_action = _find_cnae_row_action(searched.text, nested_form, row_index)
        nested_state = _merge_direct_form_state(nested_state, searched.text, nested_form)
        selected = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(nested_state, select_action, container=container),
            ajax=True,
        )
        state = _merge_direct_form_state(state, selected.text, EMISSION_FORM)
        current_markup = emit_page.text + selected.text

        activity_field = f"{EMISSION_FORM}:comboEscolherAtividadeCpbs"
        state[activity_field] = _selected_option_value(current_markup, activity_field)
        state[f"{EMISSION_FORM}:idAliquota"] = "3,00"

        nbs_field = f"{EMISSION_FORM}:comboEscolherNbs"
        try:
            state[nbs_field] = _select_option_value(current_markup, nbs_field, "123011900", prefix=True)
        except IssuancePortalError as exc:
            debug = self._save_http_artifact("cnae_selecionado_sem_nbs", selected.text)
            search_debug = self._save_http_artifact("cnae_resultado_pesquisa", searched.text)
            raise IssuancePortalError(f"{exc} Debug: {debug}; pesquisa: {search_debug}") from exc
        nbs_response = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(state, _select_event_action(current_markup, nbs_field)),
            ajax=True,
        )
        state = _merge_direct_form_state(state, nbs_response.text, EMISSION_FORM)
        current_markup += nbs_response.text

        indicator_field = f"{EMISSION_FORM}:comboEscolherIndOperacao"
        state[indicator_field] = _select_option_value(current_markup, indicator_field, "030104", prefix=True)
        indicator_response = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(state, _select_event_action(current_markup, indicator_field)),
            ajax=True,
        )
        state = _merge_direct_form_state(state, indicator_response.text, EMISSION_FORM)
        current_markup += indicator_response.text

        cst_field = f"{EMISSION_FORM}:comboEscolherCst"
        state[cst_field] = _select_option_value(current_markup, cst_field, "200", prefix=True)
        cst_response = self._request_post(
            session,
            emit_page.url,
            _ajax_payload(state, _select_event_action(current_markup, cst_field)),
            ajax=True,
        )
        state = _merge_direct_form_state(state, cst_response.text, EMISSION_FORM)
        current_markup += cst_response.text

        classification_field = f"{EMISSION_FORM}:comboEscolherClassTrib"
        state[classification_field] = _select_option_value(
            current_markup,
            classification_field,
            "200029",
            prefix=True,
        )
        nature_field = f"{EMISSION_FORM}:comboEscolherLocalPrestacao"
        state[nature_field] = _select_option_value(
            current_markup,
            nature_field,
            "Tributação no Município",
        )
        state.update(
            {
                f"{EMISSION_FORM}:tabPanel": "abaServico",
                f"{EMISSION_FORM}:comboIntermediario": "false",
                f"{EMISSION_FORM}:comboOperacaoConsumo": "false",
                f"{EMISSION_FORM}:comboDestinatario": "TOMADOR",
                f"{EMISSION_FORM}:idDescricaoServico": row.tipo_exame,
                f"{EMISSION_FORM}:idValorServicoPrestado": row.valor_br,
                f"{EMISSION_FORM}:idAliquota": "3,00",
            }
        )
        return _materialize_ajax_response(emit_page, cst_response), state

    def _validate_invoice(
        self,
        session: Session,
        configured_page: Response,
        state: dict[str, str],
    ) -> tuple[Response, dict[str, str]]:
        validation_element = _find_action_element(
            configured_page.text,
            "Validar Campos",
            form_prefix=EMISSION_FORM,
        )
        action_ids = _script_action_ids(validation_element.attrs.get("onclick", ""))
        if validation_element.element_id and validation_element.element_id not in action_ids:
            action_ids.append(validation_element.element_id)
        if not action_ids:
            raise IssuancePortalError("Acoes do botao Validar Campos da NFS-e nao foram encontradas.")

        latest = configured_page
        for action in action_ids:
            response = self._request_post(
                session,
                configured_page.url,
                _ajax_payload(state, action),
                ajax=True,
            )
            state = _merge_direct_form_state(state, response.text, EMISSION_FORM)
            latest = _materialize_ajax_response(configured_page, response)

        messages = _extract_error_messages(latest.text)
        if messages:
            debug = self._save_http_artifact("validacao_nfse_rejeitada", latest.text)
            raise IssuancePortalError(f"Validacao da NFS-e rejeitada: {'; '.join(messages)}. Debug: {debug}")
        try:
            _find_action_element(
                latest.text,
                "Confirmar Emissão de NFS-e",
                form_prefix=EMISSION_FORM,
            )
        except IssuancePortalError as exc:
            debug = self._save_http_artifact("validacao_nfse_sem_confirmacao", latest.text)
            raise IssuancePortalError(
                "O portal nao habilitou a confirmacao apos validar os campos da NFS-e. "
                f"Debug: {debug}"
            ) from exc
        return latest, state

    def _confirm_invoice(
        self,
        session: Session,
        original_page: Response,
        validated_page: Response,
        state: dict[str, str],
    ) -> Response:
        del original_page
        element = _find_action_element(
            validated_page.text,
            "Confirmar Emissão de NFS-e",
            form_prefix=EMISSION_FORM,
        )
        actual_action = _primary_action_id(element)
        container = _ajax_container(element.attrs.get("onclick", "")) or "_viewRoot"

        response = self._request_post(
            session,
            validated_page.url,
            _ajax_payload(state, actual_action, container=container),
            ajax=True,
        )
        redirect = _extract_redirect_url(response.text, response.url)
        success = self._request_get(session, redirect) if redirect else _materialize_ajax_response(validated_page, response)
        if not re.search(r"Nota Fiscal emitida com sucesso", _visible_text(success.text), flags=re.I):
            debug = self._save_http_artifact("confirmacao_nfse_resposta_ambigua", success.text)
            raise IssuancePortalError(
                "O portal nao apresentou a confirmacao de emissao. Antes de repetir, consulte as notas emitidas "
                f"para evitar duplicidade. Debug: {debug}"
            )
        return success

    def _download_pdf(
        self,
        session: Session,
        success_page: Response,
        row: InvoiceSpreadsheetRow,
        numero_nfse: str,
    ) -> Path:
        pdf_url = _extract_pdf_url(success_page.text, success_page.url)
        response = self._request_get(session, pdf_url)
        if not response.content.startswith(b"%PDF"):
            target = self.options.artifacts_dir / (
                f"pdf_nfse_{numero_nfse}_invalido_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"
            )
            target.write_bytes(response.content)
            raise IssuancePortalError(
                f"O download da NFS-e {numero_nfse} nao retornou um PDF valido. Resposta salva em {target}"
            )
        target = self.options.downloads_dir / row.pdf_filename(numero_nfse)
        target.write_bytes(response.content)
        return target


@dataclass(frozen=True)
class _Element:
    tag: str
    element_id: str
    attrs: dict[str, str]
    text: str


@dataclass(frozen=True)
class _Select:
    name: str
    attrs: dict[str, str]
    options: tuple[tuple[str, str, bool], ...]


class _MarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: dict[str, str] = {}
        self.selects: list[_Select] = []
        self.elements: list[_Element] = []
        self._select: tuple[str, dict[str, str], list[tuple[str, str, bool]]] | None = None
        self._option: tuple[str, bool, list[str]] | None = None
        self._textarea: tuple[str, list[str]] | None = None
        self._element_stack: list[tuple[str, str, dict[str, str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        lowered = tag.lower()
        if lowered in {"a", "button"}:
            self._element_stack.append((lowered, attrs_dict.get("id") or attrs_dict.get("name", ""), attrs_dict, []))
        elif lowered == "input":
            self.elements.append(
                _Element(
                    tag=lowered,
                    element_id=attrs_dict.get("id") or attrs_dict.get("name", ""),
                    attrs=attrs_dict,
                    text=attrs_dict.get("value", ""),
                )
            )
            self._add_input(attrs_dict)
        elif lowered == "select":
            self._select = (attrs_dict.get("name") or attrs_dict.get("id", ""), attrs_dict, [])
        elif lowered == "option" and self._select is not None:
            self._option = (attrs_dict.get("value", ""), "selected" in attrs_dict, [])
        elif lowered == "textarea":
            self._textarea = (attrs_dict.get("name") or attrs_dict.get("id", ""), [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option[2].append(data)
        if self._textarea is not None:
            self._textarea[1].append(data)
        for _tag, _element_id, _attrs, text in self._element_stack:
            text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "option" and self._select is not None and self._option is not None:
            value, selected, text = self._option
            self._select[2].append((value, " ".join(text).strip(), selected))
            self._option = None
        elif lowered == "select" and self._select is not None:
            name, attrs, options = self._select
            self.selects.append(_Select(name, attrs, tuple(options)))
            selected = next((value for value, _label, is_selected in options if is_selected), None)
            if selected is None and options:
                selected = options[0][0]
            if name and selected is not None and "disabled" not in attrs:
                self.controls[name] = selected
            self._select = None
        elif lowered == "textarea" and self._textarea is not None:
            name, text = self._textarea
            if name:
                self.controls[name] = "".join(text)
            self._textarea = None
        elif lowered in {"a", "button"} and self._element_stack:
            element_tag, element_id, attrs, text = self._element_stack.pop()
            self.elements.append(_Element(element_tag, element_id, attrs, " ".join(text).strip()))

    def _add_input(self, attrs: dict[str, str]) -> None:
        name = attrs.get("name", "")
        input_type = attrs.get("type", "text").lower()
        if (
            not name
            or "disabled" in attrs
            or input_type in {"submit", "button", "image", "reset", "file"}
            or (input_type in {"radio", "checkbox"} and "checked" not in attrs)
        ):
            return
        self.controls[name] = attrs.get("value", "on" if input_type in {"radio", "checkbox"} else "")


def _parse_markup(text: str) -> _MarkupParser:
    parser = _MarkupParser()
    parser.feed(_html_fragment(text))
    return parser


def _html_fragment(text: str) -> str:
    if re.match(r"\s*<(?:partial-response|ajax-response)", text, flags=re.I):
        cdata = re.findall(r"<!\[CDATA\[(.*?)\]\]>", text, flags=re.S)
        if cdata:
            return "\n".join(cdata)
    return text


def _extract_direct_form_state(text: str, form_id: str) -> dict[str, str]:
    controls = _parse_markup(text).controls
    state: dict[str, str] = {form_id: form_id}
    direct_prefix = f"{form_id}:"
    expected_colons = form_id.count(":") + 1
    for name, value in controls.items():
        if name == "javax.faces.ViewState":
            state[name] = value
        elif name.startswith(direct_prefix) and name.count(":") == expected_colons:
            if name != value:
                state[name] = value
    state["javax.faces.ViewState"] = _extract_view_state(text, default=state.get("javax.faces.ViewState", ""))
    if not state["javax.faces.ViewState"]:
        raise IssuancePortalError(f"ViewState do formulario {form_id} nao encontrado.")
    return state


def _merge_direct_form_state(state: dict[str, str], text: str, form_id: str) -> dict[str, str]:
    merged = dict(state)
    controls = _parse_markup(text).controls
    direct_prefix = f"{form_id}:"
    expected_colons = form_id.count(":") + 1
    for name, value in controls.items():
        if name.startswith(direct_prefix) and name.count(":") == expected_colons and name != value:
            merged[name] = value
    merged["javax.faces.ViewState"] = _extract_view_state(
        text,
        default=merged.get("javax.faces.ViewState", ""),
    )
    return merged


def _ajax_payload(
    state: dict[str, str],
    action: str,
    *,
    container: str = "_viewRoot",
    ajax_single: str | None = None,
) -> dict[str, str]:
    payload = dict(state)
    payload["AJAXREQUEST"] = container
    payload[action] = action
    if ajax_single:
        payload["ajaxSingle"] = ajax_single
    payload["AJAX:EVENTS_COUNT"] = "1"
    return payload


def _find_action_element(text: str, label: str, form_prefix: str | None = None) -> _Element:
    expected = _normalize(label)
    candidates = []
    for element in _parse_markup(text).elements:
        visible = _normalize(element.text or element.attrs.get("value", ""))
        if expected in visible and (not form_prefix or element.element_id.startswith(form_prefix)):
            candidates.append(element)
    if not candidates:
        raise IssuancePortalError(f"Acao {label!r} nao encontrada no portal.")
    return candidates[-1]


def _find_element_by_title(text: str, title: str) -> _Element:
    expected = _normalize(title)
    candidates = [
        element
        for element in _parse_markup(text).elements
        if _normalize(element.attrs.get("title", "")) == expected
    ]
    if not candidates:
        raise IssuancePortalError(f"Elemento com titulo {title!r} nao encontrado no portal.")
    return candidates[-1]


def _primary_action_id(element: _Element) -> str:
    actions = _script_action_ids(element.attrs.get("onclick", ""))
    if element.element_id in actions:
        return element.element_id
    if actions:
        return actions[-1]
    if element.element_id:
        return element.element_id
    raise IssuancePortalError(f"Elemento {element.text!r} nao possui identificador de acao.")


def _script_action_ids(script: str) -> list[str]:
    ids: list[str] = []
    patterns = (
        r"""['"]([^'"]+)['"]\s*:\s*['"]\1['"]""",
        r"""similarityGroupingId\s*:\s*['"]([^'"]+)['"]""",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, script):
            value = match.group(1)
            if ":" in value and value not in ids:
                ids.append(value)
    return ids


def _ajax_container(script: str) -> str | None:
    match = re.search(r"""containerId\s*['"]?\s*:\s*['"]([^'"]+)['"]""", script)
    return match.group(1) if match else None


def _find_suggestion_field(text: str, form_id: str) -> str:
    parser = _parse_markup(text)
    suffix = "_selection"
    fields = [
        name[: -len(suffix)]
        for name in parser.controls
        if name.startswith(f"{form_id}:") and name.endswith(suffix)
    ]
    if not fields:
        match = re.search(rf"""id=["']({re.escape(form_id)}:[^"']+)["'][^>]*class=["'][^"']*rich-sb""", text)
        if match:
            return match.group(1)
        raise IssuancePortalError("Componente de pesquisa do tomador por CPF nao encontrado.")
    return fields[0]


def _find_suggestion_selection_action(text: str, suggestion_field: str) -> str:
    component_position = next(
        (
            match.start()
            for match in re.finditer(r"new\s+RichFaces\.Suggestion", text)
            if suggestion_field in text[match.start() : match.start() + 2_000]
        ),
        -1,
    )
    onselect_position = text.find("onselect", component_position)
    if component_position < 0 or onselect_position < 0:
        raise IssuancePortalError("Script onselect da pesquisa do tomador nao encontrado.")
    snippet = text[onselect_position : onselect_position + 2_000]
    actions = [
        action
        for action in _script_action_ids(snippet)
        if action not in {suggestion_field, f"{suggestion_field}_selection"}
    ]
    if not actions:
        raise IssuancePortalError("Acao para carregar o tomador selecionado nao encontrada.")
    return actions[0]


def _find_named_function_action(text: str, function_name: str) -> str:
    match = re.search(
        rf"{re.escape(function_name)}\s*=\s*function\s*\(\)\s*\{{(.*?)\}};",
        text,
        flags=re.S,
    )
    if not match:
        raise IssuancePortalError(f"Funcao AJAX {function_name!r} nao encontrada.")
    actions = _script_action_ids(match.group(1))
    if not actions:
        raise IssuancePortalError(f"Acao da funcao AJAX {function_name!r} nao encontrada.")
    return actions[-1]


def _suggestion_selection_value(text: str, row: InvoiceSpreadsheetRow) -> str:
    entries = list(re.finditer(
        r"""<tr[^>]+class=["'][^"']*suggestionEntry[^"']*["'][^>]*>(.*?)</tr>""",
        text,
        flags=re.I | re.S,
    ))
    for index, entry in enumerate(entries):
        if row.cpf_digits in re.sub(r"\D", "", _visible_text(entry.group(1))):
            # RichFaces 3 Suggestion.selectEntry submits the zero-based row index
            # through the generated *_selection hidden input.
            return str(index)
    parser = _parse_markup(text)
    candidates = [
        value
        for name, value in parser.controls.items()
        if name.endswith("_selection") and value
    ]
    if candidates:
        return candidates[0]
    match = re.search(rf"""value=["']([^"']*{re.escape(row.cpf_digits)}[^"']*)["']""", text)
    return unescape(match.group(1)) if match else row.cpf_formatted


def _has_suggestion_entry(text: str, cpf_digits: str) -> bool:
    for match in re.finditer(
        r"""<tr[^>]+class=["'][^"']*suggestionEntry[^"']*["'][^>]*>(.*?)</tr>""",
        text,
        flags=re.I | re.S,
    ):
        if cpf_digits in re.sub(r"\D", "", _visible_text(match.group(1))):
            return True
    return False


def _find_modal_confirmation_action(text: str, *, panel_marker: str, label: str) -> str:
    return _primary_action_id(_find_modal_confirmation_element(text, panel_marker=panel_marker, label=label))


def _find_modal_confirmation_element(text: str, *, panel_marker: str, label: str) -> _Element:
    position = text.find(panel_marker)
    if position < 0:
        raise IssuancePortalError(f"Painel de confirmacao {panel_marker!r} nao encontrado.")
    start = max(0, position - 1_000)
    snippet = text[start : position + 40_000]
    return _find_action_element(snippet, label)


def _select_event_action(text: str, field_name: str) -> str:
    selects = [select for select in _parse_markup(text).selects if select.name == field_name]
    if not selects:
        raise IssuancePortalError(f"Campo de selecao {field_name!r} nao encontrado.")
    script = selects[-1].attrs.get("onchange") or selects[-1].attrs.get("onclick", "")
    actions = [action for action in _script_action_ids(script) if action != field_name]
    if not actions:
        raise IssuancePortalError(f"Evento AJAX do campo {field_name!r} nao encontrado.")
    return actions[-1]


def _select_option_value(text: str, field_name: str, label: str, *, prefix: bool = False) -> str:
    selects = [select for select in _parse_markup(text).selects if select.name == field_name]
    expected = _normalize(label)
    for select in reversed(selects):
        for value, option_label, _selected in select.options:
            normalized = _normalize(option_label)
            if (prefix and normalized.startswith(expected)) or (not prefix and normalized == expected):
                return value
    raise IssuancePortalError(f"Opcao {label!r} nao encontrada no campo {field_name!r}.")


def _selected_option_value(text: str, field_name: str) -> str:
    selects = [select for select in _parse_markup(text).selects if select.name == field_name]
    for select in reversed(selects):
        selected = next((value for value, _label, is_selected in select.options if is_selected), None)
        if selected is not None:
            return selected
    raise IssuancePortalError(f"Nenhuma opcao selecionada no campo {field_name!r}.")


def _taker_form_values(
    text: str,
    row: InvoiceSpreadsheetRow,
    form_id: str,
) -> dict[str, str]:
    values = {
        f"{form_id}:idCPFCNPJ": row.cpf_formatted,
        f"{form_id}:idNome": row.paciente,
        f"{form_id}:comboEscolherPais": _select_option_value(
            text,
            f"{form_id}:comboEscolherPais",
            "BRASIL",
        ),
        f"{form_id}:comboEscolherEstado": _select_option_value(
            text,
            f"{form_id}:comboEscolherEstado",
            row.uf,
        ),
        f"{form_id}:comboEscolherCidade": _select_option_value(
            text,
            f"{form_id}:comboEscolherCidade",
            row.cidade,
        ),
    }
    optional_values = {
        f"{form_id}:idCEP": row.cep_digits,
        f"{form_id}:idEndereco": row.rua,
        f"{form_id}:idNumero": row.numero_casa,
        f"{form_id}:idComplemento": row.complemento,
        f"{form_id}:idBairro": row.bairro,
        f"{form_id}:idTelefone": row.telefone[:20],
        f"{form_id}:inputEmail3": row.email,
    }
    values.update(
        {
            field: value
            for field, value in optional_values.items()
            if value
        }
    )
    return values


def _find_cnae_row_index(text: str, description: str) -> str:
    decoded = _html_fragment(text)
    expected = _normalize(description)
    for table_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", decoded, flags=re.I | re.S):
        if expected not in _normalize(_visible_text(table_row)):
            continue
        match = re.search(r"idDatatableListaCnae:(\d+):", table_row)
        if match:
            return match.group(1)
    raise IssuancePortalError(f"CNAE hospitalar {description!r} nao encontrado no resultado da pesquisa.")


def _find_cnae_row_action(text: str, nested_form: str, row_index: str) -> str:
    prefix = f"{nested_form}:idDatatableListaCnae:{row_index}:"
    for element in _parse_markup(text).elements:
        if element.element_id.startswith(prefix) and _normalize(element.attrs.get("title", "")) == "selecionar":
            return element.element_id
    raise IssuancePortalError(f"Acao da linha {row_index} do CNAE nao encontrada.")


def _extract_redirect_url(text: str, current_url: str) -> str | None:
    decoded = unescape(text)
    patterns = (
        r"""<redirect[^>]+url=["']([^"']+)["']""",
        r"""<meta[^>]+name=["']Location["'][^>]+content=["']([^"']+)["']""",
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']Location["']""",
        r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        r"""url=([^"'\s>]+\.seam[^"'\s>]*)""",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.I)
        if match:
            return urljoin(current_url, unescape(match.group(1)))
    return None


def _extract_invoice_number(text: str) -> str:
    decoded = _html_fragment(text)
    controls = _parse_markup(decoded).controls
    for name, value in controls.items():
        if re.search(r"(?:num|numero).*nfse|numero.*nota", name, flags=re.I) and value.isdigit():
            return value
    patterns = (
        r"""N[uú]mero da Nota:\s*</[^>]+>.{0,1000}?value=["'](\d+)["']""",
        r"""N[uú]mero da Nota:.{0,1000}?value=["'](\d+)["']""",
        r"""N[uú]mero da NFS-e.{0,500}?(\d+)""",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.I | re.S)
        if match:
            return match.group(1)
    raise IssuancePortalError("Numero da Nota nao encontrado na confirmacao da emissao.")


def _extract_pdf_url(text: str, current_url: str) -> str:
    decoded = _html_fragment(text)
    patterns = (
        r"""<object[^>]+data=["']([^"']+)["']""",
        r"""<(?:embed|iframe)[^>]+src=["']([^"']+)["']""",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.I)
        if match:
            return urljoin(current_url, unescape(match.group(1)))
    raise IssuancePortalError("Link do PDF nao encontrado na confirmacao da emissao.")


def _extract_error_messages(text: str) -> list[str]:
    decoded = _html_fragment(text)
    candidates = re.findall(
        r"""<(?:span|li|td)[^>]+(?:rich-messages-label|rich-message-label|error|erro)[^>]*>(.*?)</(?:span|li|td)>""",
        decoded,
        flags=re.I | re.S,
    )
    messages: list[str] = []
    for candidate in candidates:
        message = _visible_text(candidate)
        if re.search(r"\b(?:sucesso|sucessfully|successfully)\b", message, flags=re.I):
            continue
        if message and message not in messages:
            messages.append(message)
    return messages


def _taker_loaded(text: str, cpf_digits: str) -> bool:
    state = _extract_direct_form_state(text, EMISSION_FORM)
    name = state.get(f"{EMISSION_FORM}:idNome", "").strip()
    cpf = re.sub(r"\D", "", state.get(f"{EMISSION_FORM}:idCPFCNPJ", ""))
    if not cpf:
        cpf = re.sub(r"\D", "", _visible_text(text))
    return bool(name and cpf_digits in cpf)


def _materialize_ajax_response(base: Response, ajax: Response) -> Response:
    """Expose AJAX fragments through a Response-shaped object without another HTTP request."""
    materialized = Response()
    materialized.status_code = ajax.status_code
    materialized.url = base.url
    materialized.headers = ajax.headers
    materialized.encoding = ajax.encoding or "utf-8"
    materialized._content = (base.text + "\n" + _html_fragment(ajax.text)).encode(materialized.encoding)
    return materialized


def _visible_text(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", unescape(text))
    return re.sub(r"\s+", " ", without_tags).strip()


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _visible_text(value))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def filter_unissued_rows(
    rows: Iterable[InvoiceSpreadsheetRow],
    ledger: IssuanceLedger,
) -> tuple[list[InvoiceSpreadsheetRow], list[InvoiceSpreadsheetRow]]:
    successful = ledger.successful_hashes()
    pending: list[InvoiceSpreadsheetRow] = []
    skipped: list[InvoiceSpreadsheetRow] = []
    for row in rows:
        (skipped if row.row_hash in successful else pending).append(row)
    return pending, skipped


def result_as_dict(result: IssuanceResult) -> dict[str, object]:
    values = asdict(result)
    values["pdf_path"] = str(result.pdf_path)
    return values

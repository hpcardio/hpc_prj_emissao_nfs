from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Collection, Iterable, Iterator
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import dlt
from dlt.sources.filesystem import FileItemDict, filesystem

from nfs_fortaleza.periods import DateRangePeriod, MonthPeriod


QueryPeriod = MonthPeriod | DateRangePeriod
NfseIdentity = tuple[str, str]


def infer_nfse_period(path: Path) -> MonthPeriod:
    """Infer the monthly period from the competence or issue date in an XML/ZIP."""
    content = path.read_bytes()
    periods: set[MonthPeriod] = set()

    for _xml_name, xml_content in _iter_xml_documents(path.name, content):
        root = ET.fromstring(xml_content)
        for node in _iter_nfse_nodes(root):
            record = _mapped_record(node)
            for value in (record.get("competencia_nfse"), record.get("data_hora")):
                period = _month_period_from_xml_value(value)
                if period:
                    periods.add(period)
                    break

    if not periods:
        raise ValueError(f"Nao foi possivel identificar a competencia no XML/ZIP {path}.")
    if len(periods) > 1:
        labels = ", ".join(sorted(period.label for period in periods))
        raise ValueError(f"O XML/ZIP {path} contem NFS-e de competencias diferentes: {labels}.")
    return next(iter(periods))


def nfse_xml_resource(
    path: Path,
    competencia: QueryPeriod,
    *,
    table_name: str = "nfse_xml",
    existing_nfse_keys: Collection[NfseIdentity] = (),
):
    """Build a dlt filesystem resource that reads exported NFS-e XML files."""
    source = filesystem(bucket_url=str(path.resolve().parent), file_glob=path.name)
    resource = source | read_nfse_xml(
        competencia.yyyymm,
        tuple(existing_nfse_keys),
    )
    resource = resource.with_name(table_name)
    resource.apply_hints(
        primary_key="row_hash",
        write_disposition="merge",
        columns=_column_hints(),
    )
    return resource


@dlt.transformer
def read_nfse_xml(
    items: Iterator[FileItemDict],
    competencia: str,
    existing_nfse_keys: tuple[NfseIdentity, ...] = (),
) -> Iterator[dict[str, Any]]:
    """Read XML/ZIP files from dlt filesystem FileItemDict objects."""
    known_nfse_keys = set(existing_nfse_keys)
    for item in items:
        with item.open() as file:
            content = file.read()
        file_name = Path(item["file_name"]).name
        for xml_name, xml_content in _iter_xml_documents(file_name, content):
            yield from _filter_new_nfse_records(
                parse_nfse_xml(xml_content, competencia, xml_name),
                known_nfse_keys,
            )


def _filter_new_nfse_records(
    records: Iterable[dict[str, Any]],
    known_nfse_keys: set[NfseIdentity],
) -> Iterator[dict[str, Any]]:
    for record in records:
        identity = nfse_identity(
            record.get("prestador_cnpj"),
            record.get("numero_nfse"),
        )
        if identity is not None:
            if identity in known_nfse_keys:
                continue
            known_nfse_keys.add(identity)
        yield record


def nfse_identity(
    prestador_cnpj: Any,
    numero_nfse: Any,
) -> NfseIdentity | None:
    cnpj = re.sub(r"\D", "", str(prestador_cnpj or ""))
    number = str(numero_nfse or "").strip()
    if not cnpj or not number:
        return None
    if number.isdigit():
        number = number.lstrip("0") or "0"
    return cnpj, number


def parse_nfse_xml(xml_content: bytes, competencia: str, arquivo_origem: str) -> Iterator[dict[str, Any]]:
    root = ET.fromstring(xml_content)
    downloaded_at = datetime.now(timezone.utc).isoformat()

    for index, node in enumerate(_iter_nfse_nodes(root), start=1):
        record = _mapped_record(node)
        record["competencia_consulta"] = competencia
        record["arquivo_origem"] = arquivo_origem
        record["indice_documento_xml"] = index
        record["baixado_em"] = downloaded_at
        record["xml_campos"] = json.dumps(_flatten_xml(node), ensure_ascii=False, sort_keys=True)
        record["xml_documento"] = ET.tostring(node, encoding="unicode")
        record["row_hash"] = _row_hash(record)
        yield record


def _iter_xml_documents(file_name: str, content: bytes) -> Iterator[tuple[str, bytes]]:
    if zipfile.is_zipfile(BytesIO(content)):
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for member in archive.namelist():
                if member.lower().endswith(".xml"):
                    yield Path(member).name, archive.read(member)
        return
    yield file_name, content


def _month_period_from_xml_value(value: Any) -> MonthPeriod | None:
    if value is None:
        return None
    raw = str(value).strip()

    match = re.search(r"\b(\d{4})-(\d{1,2})(?:-\d{1,2})?\b", raw)
    if match:
        return MonthPeriod(year=int(match.group(1)), month=int(match.group(2)))

    match = re.search(r"\b(\d{1,2})/(\d{4})\b", raw)
    if match:
        return MonthPeriod(year=int(match.group(2)), month=int(match.group(1)))

    match = re.fullmatch(r"(\d{4})(\d{2})(?:\d{2})?", raw)
    if match:
        return MonthPeriod(year=int(match.group(1)), month=int(match.group(2)))
    return None


def _iter_nfse_nodes(root: ET.Element) -> list[ET.Element]:
    comp_nfse = [element for element in root.iter() if _local_name(element.tag) == "CompNfse"]
    if comp_nfse:
        return comp_nfse

    nfse = [element for element in root.iter() if _local_name(element.tag) == "Nfse"]
    if nfse:
        return nfse

    inf_nfse = [element for element in root.iter() if _local_name(element.tag) == "InfNfse"]
    return inf_nfse or [root]


def _mapped_record(node: ET.Element) -> dict[str, Any]:
    return {
        "codigo_status_nfse": _text(node, ("Nfse", "InfNfse", "Status"), ("InfNfse", "Status")),
        "identificacao_sim_nao": _first_by_name(node, "IdentificacaoSimNao", "SimNao"),
        "valor_booleano": _first_by_name(node, "true", "false"),
        "data_hora": _text(
            node,
            ("Nfse", "InfNfse", "DataEmissao"),
            ("InfNfse", "DataEmissao"),
            ("DataHora",),
            ("DataHoraRecebimento",),
        ),
        "quantidade_rps_lote": _text(node, ("LoteRps", "QuantidadeRps"), ("QuantidadeRps",)),
        "numero_rps": _text(node, _rps_path("IdentificacaoRps", "Numero"), ("IdentificacaoRps", "Numero")),
        "serie_rps": _text(node, _rps_path("IdentificacaoRps", "Serie"), ("IdentificacaoRps", "Serie")),
        "informacoes_adicionais": _text(node, ("Nfse", "InfNfse", "OutrasInformacoes"), ("OutrasInformacoes",)),
        "codigo_item_lista_servico": _text(node, _inf_servico_path("ItemListaServico"), _servico_path("ItemListaServico")),
        "codigo_cnae": _text(node, _inf_servico_path("CodigoCnae"), _servico_path("CodigoCnae")),
        "codigo_tributacao": _text(
            node,
            _inf_servico_path("CodigoTributacaoMunicipio"),
            _servico_path("CodigoTributacaoMunicipio"),
        ),
        "aliquota": _normalize_aliquota(_text(node, _inf_servico_valores_path("Aliquota"), _servico_valores_path("Aliquota"))),
        "aliquota_xml": _text(node, _inf_servico_valores_path("Aliquota"), _servico_valores_path("Aliquota")),
        "discriminacao": _text(node, _inf_servico_path("Discriminacao"), _servico_path("Discriminacao")),
        "codigo_municipio_servico": _text(
            node,
            _inf_servico_path("CodigoMunicipio"),
            _inf_servico_path("MunicipioIncidencia"),
            _servico_path("CodigoMunicipio"),
            _servico_path("MunicipioIncidencia"),
        ),
        "prestador_inscricao_municipal": _text(
            node,
            _inf_prestador_path("IdentificacaoPrestador", "InscricaoMunicipal"),
            _prestador_path("IdentificacaoPrestador", "InscricaoMunicipal"),
            _declaracao_path("Prestador", "InscricaoMunicipal"),
        ),
        "prestador_razao_social": _text(node, _inf_prestador_path("RazaoSocial"), _prestador_path("RazaoSocial")),
        "prestador_nome_fantasia": _text(node, _inf_prestador_path("NomeFantasia"), _prestador_path("NomeFantasia")),
        "prestador_cnpj": _text(
            node,
            _inf_prestador_path("IdentificacaoPrestador", "Cnpj"),
            _inf_prestador_path("IdentificacaoPrestador", "CpfCnpj", "Cnpj"),
            _prestador_path("IdentificacaoPrestador", "CpfCnpj", "Cnpj"),
            _declaracao_path("Prestador", "CpfCnpj", "Cnpj"),
        ),
        "prestador_cpf": _text(
            node,
            _inf_prestador_path("IdentificacaoPrestador", "Cpf"),
            _inf_prestador_path("IdentificacaoPrestador", "CpfCnpj", "Cpf"),
            _prestador_path("IdentificacaoPrestador", "CpfCnpj", "Cpf"),
            _declaracao_path("Prestador", "CpfCnpj", "Cpf"),
        ),
        "prestador_endereco": _text(node, _inf_prestador_endereco_path("Endereco"), _prestador_endereco_path("Endereco")),
        "prestador_numero_endereco": _text(node, _inf_prestador_endereco_path("Numero"), _prestador_endereco_path("Numero")),
        "prestador_complemento_endereco": _text(node, _inf_prestador_endereco_path("Complemento"), _prestador_endereco_path("Complemento")),
        "prestador_bairro": _text(node, _inf_prestador_endereco_path("Bairro"), _prestador_endereco_path("Bairro")),
        "prestador_codigo_municipio": _text(node, _inf_prestador_endereco_path("CodigoMunicipio"), _prestador_endereco_path("CodigoMunicipio")),
        "prestador_uf": _text(node, _inf_prestador_endereco_path("Uf"), _prestador_endereco_path("Uf")),
        "prestador_cep": _text(node, _inf_prestador_endereco_path("Cep"), _prestador_endereco_path("Cep")),
        "prestador_email": _text(node, _inf_prestador_path("Contato", "Email"), _prestador_path("Contato", "Email")),
        "prestador_telefone": _text(node, _inf_prestador_path("Contato", "Telefone"), _prestador_path("Contato", "Telefone")),
        "tomador_cpf": _text(node, _inf_tomador_path("IdentificacaoTomador", "CpfCnpj", "Cpf"), _tomador_path("IdentificacaoTomador", "CpfCnpj", "Cpf")),
        "tomador_cnpj": _text(node, _inf_tomador_path("IdentificacaoTomador", "CpfCnpj", "Cnpj"), _tomador_path("IdentificacaoTomador", "CpfCnpj", "Cnpj")),
        "tomador_indicador_cpf_cnpj": _text(node, _inf_tomador_path("IdentificacaoTomador", "CpfCnpj", "IndicadorCpfCnpj"), _tomador_path("IdentificacaoTomador", "CpfCnpj", "IndicadorCpfCnpj")),
        "tomador_razao_social": _text(node, _inf_tomador_path("RazaoSocial"), _tomador_path("RazaoSocial")),
        "tomador_endereco": _text(node, _inf_tomador_path("Endereco", "Endereco"), _tomador_path("Endereco", "Endereco")),
        "tomador_numero_endereco": _text(node, _inf_tomador_path("Endereco", "Numero"), _tomador_path("Endereco", "Numero")),
        "tomador_complemento_endereco": _text(node, _inf_tomador_path("Endereco", "Complemento"), _tomador_path("Endereco", "Complemento")),
        "tomador_bairro": _text(node, _inf_tomador_path("Endereco", "Bairro"), _tomador_path("Endereco", "Bairro")),
        "tomador_codigo_municipio": _text(node, _inf_tomador_path("Endereco", "CodigoMunicipio"), _tomador_path("Endereco", "CodigoMunicipio")),
        "tomador_uf": _text(node, _inf_tomador_path("Endereco", "Uf"), _tomador_path("Endereco", "Uf")),
        "tomador_cep": _text(node, _inf_tomador_path("Endereco", "Cep"), _tomador_path("Endereco", "Cep")),
        "tomador_email": _text(node, _inf_tomador_path("Contato", "Email"), _tomador_path("Contato", "Email")),
        "tomador_telefone": _text(node, _inf_tomador_path("Contato", "Telefone"), _tomador_path("Contato", "Telefone")),
        "codigo_obra": _text(node, _declaracao_path("ConstrucaoCivil", "CodigoObra"), ("ConstrucaoCivil", "CodigoObra")),
        "codigo_art": _text(node, _declaracao_path("ConstrucaoCivil", "Art"), ("ConstrucaoCivil", "Art")),
        "numero_lote_rps": _text(node, ("LoteRps", "NumeroLote"), ("NumeroLote",)),
        "protocolo_recebimento_rps": _text(node, ("Protocolo",), ("NumeroProtocolo",)),
        "codigo_situacao_lote_rps": _text(node, ("Situacao",), ("CodigoSituacaoLoteRps",)),
        "codigo_mensagem_retorno": _text(node, ("MensagemRetorno", "Codigo"), ("Codigo",)),
        "motivo_cancelamento_nfse": _text(node, ("Cancelamento", "Motivo"), ("MotivoCancelamento",)),
        "cancelamento_codigo": _text(node, ("Cancelamento", "Confirmacao", "Pedido", "InfPedidoCancelamento", "CodigoCancelamento"), ("CodigoCancelamento",)),
        "cancelamento_data_hora": _text(node, ("Cancelamento", "Confirmacao", "DataHora"), ("DataHoraCancelamento",)),
        "numero_nfse": _text(node, ("Nfse", "InfNfse", "Numero"), ("InfNfse", "Numero")),
        "competencia_nfse": _text(
            node,
            ("Nfse", "InfNfse", "Competencia"),
            ("InfNfse", "Competencia"),
            _declaracao_path("Competencia"),
            ("Competencia",),
        ),
        "codigo_verificacao_nfse": _text(
            node,
            ("Nfse", "InfNfse", "CodigoVerificacao"),
            ("InfNfse", "CodigoVerificacao"),
        ),
        "disponibilidade_nfse": _first_by_name(node, "Disponibilidade", "DisponibilidadeNfse"),
        "codigo_status_rps": _text(node, _rps_path("Status"), ("Rps", "Status")),
        "codigo_natureza_operacao": _text(node, ("Nfse", "InfNfse", "NaturezaOperacao"), _servico_path("NaturezaOperacao"), ("NaturezaOperacao",)),
        "codigo_regime_especial_tributacao": _text(node, ("Nfse", "InfNfse", "RegimeEspecialTributacao"), _declaracao_path("RegimeEspecialTributacao")),
        "descricao_tributo": _first_by_name(node, "DescricaoTributo", "DescricaoDoTributo"),
        "codigo_tipo_rps": _text(node, _rps_path("IdentificacaoRps", "Tipo"), ("IdentificacaoRps", "Tipo")),
        "intermediario_cpf": _text(node, _intermediario_path("CpfCnpj", "Cpf"), _intermediario_path("IdentificacaoIntermediario", "CpfCnpj", "Cpf")),
        "intermediario_cnpj": _text(node, _intermediario_path("CpfCnpj", "Cnpj"), _intermediario_path("IdentificacaoIntermediario", "CpfCnpj", "Cnpj")),
        "intermediario_inscricao_municipal": _text(node, _intermediario_path("InscricaoMunicipal"), _intermediario_path("IdentificacaoIntermediario", "InscricaoMunicipal")),
        "intermediario_razao_social": _text(node, _intermediario_path("RazaoSocial")),
        "orgao_gerador_codigo_municipio": _text(node, ("Nfse", "InfNfse", "OrgaoGerador", "CodigoMunicipio"), ("OrgaoGerador", "CodigoMunicipio")),
        "orgao_gerador_uf": _text(node, ("Nfse", "InfNfse", "OrgaoGerador", "Uf"), ("OrgaoGerador", "Uf")),
        "valor_servicos": _text(node, _inf_servico_valores_path("ValorServicos"), _servico_valores_path("ValorServicos")),
        "valor_deducoes": _text(node, _inf_servico_valores_path("ValorDeducoes"), _servico_valores_path("ValorDeducoes")),
        "valor_pis": _text(node, _inf_servico_valores_path("ValorPis"), _servico_valores_path("ValorPis")),
        "valor_cofins": _text(node, _inf_servico_valores_path("ValorCofins"), _servico_valores_path("ValorCofins")),
        "valor_inss": _text(node, _inf_servico_valores_path("ValorInss"), _servico_valores_path("ValorInss")),
        "valor_ir": _text(node, _inf_servico_valores_path("ValorIr"), _servico_valores_path("ValorIr")),
        "valor_csll": _text(node, _inf_servico_valores_path("ValorCsll"), _servico_valores_path("ValorCsll")),
        "outras_retencoes": _text(node, _inf_servico_valores_path("OutrasRetencoes"), _servico_valores_path("OutrasRetencoes")),
        "iss_retido": _text(node, _inf_servico_valores_path("IssRetido"), _servico_valores_path("IssRetido")),
        "valor_iss": _text(node, _inf_servico_valores_path("ValorIss"), _servico_valores_path("ValorIss")),
        "valor_iss_retido": _text(node, _inf_servico_valores_path("ValorIssRetido"), _servico_valores_path("ValorIssRetido")),
        "desconto_incondicionado": _text(
            node,
            _inf_servico_valores_path("DescontoIncondicionado"),
            _servico_valores_path("DescontoIncondicionado"),
        ),
        "desconto_condicionado": _text(node, _inf_servico_valores_path("DescontoCondicionado"), _servico_valores_path("DescontoCondicionado")),
        "base_calculo": _text(node, _inf_servico_valores_path("BaseCalculo"), ("Nfse", "InfNfse", "ValoresNfse", "BaseCalculo"), ("ValoresNfse", "BaseCalculo")),
        "valor_liquido_nfse": _text(
            node,
            _inf_servico_valores_path("ValorLiquidoNfse"),
            ("Nfse", "InfNfse", "ValoresNfse", "ValorLiquidoNfse"),
            ("ValoresNfse", "ValorLiquidoNfse"),
        ),
    }


def _declaracao_path(*parts: str) -> tuple[str, ...]:
    return ("Nfse", "InfNfse", "DeclaracaoPrestacaoServico", "InfDeclaracaoPrestacaoServico", *parts)


def _inf_path(*parts: str) -> tuple[str, ...]:
    return ("Nfse", "InfNfse", *parts)


def _rps_path(*parts: str) -> tuple[str, ...]:
    return _declaracao_path("Rps", *parts)


def _inf_servico_path(*parts: str) -> tuple[str, ...]:
    return _inf_path("Servico", *parts)


def _servico_path(*parts: str) -> tuple[str, ...]:
    return _declaracao_path("Servico", *parts)


def _inf_servico_valores_path(*parts: str) -> tuple[str, ...]:
    return _inf_servico_path("Valores", *parts)


def _servico_valores_path(*parts: str) -> tuple[str, ...]:
    return _servico_path("Valores", *parts)


def _inf_prestador_path(*parts: str) -> tuple[str, ...]:
    return _inf_path("PrestadorServico", *parts)


def _prestador_path(*parts: str) -> tuple[str, ...]:
    return ("Nfse", "InfNfse", "PrestadorServico", *parts)


def _inf_prestador_endereco_path(*parts: str) -> tuple[str, ...]:
    return _inf_prestador_path("Endereco", *parts)


def _prestador_endereco_path(*parts: str) -> tuple[str, ...]:
    return _prestador_path("Endereco", *parts)


def _inf_tomador_path(*parts: str) -> tuple[str, ...]:
    return _inf_path("TomadorServico", *parts)


def _tomador_path(*parts: str) -> tuple[str, ...]:
    return _declaracao_path("Tomador", *parts)


def _intermediario_path(*parts: str) -> tuple[str, ...]:
    return _declaracao_path("Intermediario", *parts)


def _text(node: ET.Element, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _find_path_text(node, path)
        if value is not None:
            return value
    return None


def _find_path_text(node: ET.Element, path: tuple[str, ...]) -> str | None:
    if not path:
        return _clean_text(node.text)

    candidates = [node]
    if _local_name(node.tag) == path[0]:
        path = path[1:]

    for part in path:
        next_candidates: list[ET.Element] = []
        for candidate in candidates:
            next_candidates.extend(child for child in list(candidate) if _local_name(child.tag) == part)
        candidates = next_candidates
        if not candidates:
            return None

    for candidate in candidates:
        text = _clean_text(candidate.text)
        if text is not None:
            return text
    return None


def _first_by_name(node: ET.Element, *names: str) -> str | None:
    expected = set(names)
    for element in node.iter():
        if _local_name(element.tag) in expected:
            text = _clean_text(element.text)
            if text is not None:
                return text
    return None


def _flatten_xml(node: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}

    def walk(element: ET.Element, path: list[str]) -> None:
        children = list(element)
        text = _clean_text(element.text)
        if text is not None:
            key = _normalize_key("_".join(path))
            if key in values:
                suffix = 2
                while f"{key}_{suffix}" in values:
                    suffix += 1
                key = f"{key}_{suffix}"
            values[key] = text
        for child in children:
            walk(child, [*path, _local_name(child.tag)])

    walk(node, [_local_name(node.tag)])
    return values


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def _normalize_key(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").lower()
    return value or "campo"


def _normalize_aliquota(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return value
    if number > 1:
        number = number / 100
    return f"{number:.4f}"


def _column_hints() -> dict[str, dict[str, str]]:
    text_columns = (
        "codigo_status_nfse",
        "identificacao_sim_nao",
        "valor_booleano",
        "quantidade_rps_lote",
        "numero_rps",
        "serie_rps",
        "informacoes_adicionais",
        "codigo_item_lista_servico",
        "codigo_cnae",
        "codigo_tributacao",
        "aliquota",
        "aliquota_xml",
        "discriminacao",
        "codigo_municipio_servico",
        "prestador_inscricao_municipal",
        "prestador_razao_social",
        "prestador_nome_fantasia",
        "prestador_cnpj",
        "prestador_cpf",
        "prestador_endereco",
        "prestador_numero_endereco",
        "prestador_complemento_endereco",
        "prestador_bairro",
        "prestador_codigo_municipio",
        "prestador_uf",
        "prestador_cep",
        "prestador_email",
        "prestador_telefone",
        "tomador_cpf",
        "tomador_cnpj",
        "tomador_indicador_cpf_cnpj",
        "tomador_razao_social",
        "tomador_endereco",
        "tomador_numero_endereco",
        "tomador_complemento_endereco",
        "tomador_bairro",
        "tomador_codigo_municipio",
        "tomador_uf",
        "tomador_cep",
        "tomador_email",
        "tomador_telefone",
        "codigo_obra",
        "codigo_art",
        "numero_lote_rps",
        "protocolo_recebimento_rps",
        "codigo_situacao_lote_rps",
        "codigo_mensagem_retorno",
        "motivo_cancelamento_nfse",
        "cancelamento_codigo",
        "cancelamento_data_hora",
        "numero_nfse",
        "competencia_nfse",
        "codigo_verificacao_nfse",
        "disponibilidade_nfse",
        "codigo_status_rps",
        "codigo_natureza_operacao",
        "codigo_regime_especial_tributacao",
        "descricao_tributo",
        "codigo_tipo_rps",
        "intermediario_cpf",
        "intermediario_cnpj",
        "intermediario_inscricao_municipal",
        "intermediario_razao_social",
        "orgao_gerador_codigo_municipio",
        "orgao_gerador_uf",
        "valor_servicos",
        "valor_deducoes",
        "valor_pis",
        "valor_cofins",
        "valor_inss",
        "valor_ir",
        "valor_csll",
        "outras_retencoes",
        "iss_retido",
        "valor_iss",
        "valor_iss_retido",
        "desconto_incondicionado",
        "desconto_condicionado",
        "base_calculo",
        "valor_liquido_nfse",
        "competencia_consulta",
        "arquivo_origem",
        "xml_campos",
        "xml_documento",
        "row_hash",
    )
    hints = {column: {"data_type": "text", "nullable": True} for column in text_columns}
    hints["row_hash"]["nullable"] = False
    hints["indice_documento_xml"] = {"data_type": "bigint", "nullable": True}
    hints["data_hora"] = {"data_type": "timestamp", "nullable": True}
    hints["baixado_em"] = {"data_type": "timestamp", "nullable": True}
    return hints


def _row_hash(record: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"baixado_em", "row_hash"}
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence

from nfs_fortaleza.issuance import IssuanceResult
from nfs_fortaleza.spreadsheet import InvoiceSpreadsheetRow, select_rows


LOCAL_TIME_SQL = "timezone('America/Sao_Paulo', now())"
VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PDF_MIME_TYPE = "application/pdf"


class BatchConfigurationError(ValueError):
    """Raised when a manually triggered DAG has an invalid payload."""


class BatchDatabaseError(RuntimeError):
    """Raised when the operational batch cannot be found or updated."""


class BatchArtifactError(RuntimeError):
    """Raised when an issuance artifact cannot be safely persisted."""


@dataclass(frozen=True)
class BatchPayload:
    lote_id: int
    solicitacao_ids: tuple[int, ...]
    cnpj_por_solicitacao: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> BatchPayload:
        payload = value or {}
        lote_id = payload.get("lote_id")
        solicitacao_ids = payload.get("solicitacao_ids")
        cnpj_por_solicitacao = payload.get("cnpj_por_solicitacao") or {}

        if isinstance(lote_id, bool) or not isinstance(lote_id, int) or lote_id <= 0:
            raise BatchConfigurationError("dag_run.conf.lote_id deve ser um inteiro positivo.")
        if not isinstance(solicitacao_ids, list) or not solicitacao_ids:
            raise BatchConfigurationError(
                "dag_run.conf.solicitacao_ids deve ser uma lista nao vazia."
            )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in solicitacao_ids
        ):
            raise BatchConfigurationError(
                "dag_run.conf.solicitacao_ids aceita somente inteiros positivos."
            )
        if len(solicitacao_ids) != len(set(solicitacao_ids)):
            raise BatchConfigurationError(
                "dag_run.conf.solicitacao_ids nao pode conter IDs repetidos."
            )
        if not isinstance(cnpj_por_solicitacao, Mapping):
            raise BatchConfigurationError(
                "dag_run.conf.cnpj_por_solicitacao deve ser um objeto."
            )
        cnpjs_normalizados: dict[int, str] = {}
        for raw_id, raw_cnpj in cnpj_por_solicitacao.items():
            try:
                solicitacao_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise BatchConfigurationError(
                    "cnpj_por_solicitacao possui um ID invalido."
                ) from exc
            if solicitacao_id not in solicitacao_ids:
                raise BatchConfigurationError(
                    "cnpj_por_solicitacao possui um ID fora do lote."
                )
            cnpjs_normalizados[solicitacao_id] = _issuer_cnpj(raw_cnpj)
        if cnpjs_normalizados and set(cnpjs_normalizados) != set(
            solicitacao_ids
        ):
            raise BatchConfigurationError(
                "cnpj_por_solicitacao deve informar o CNPJ de todos os itens."
            )
        return cls(
            lote_id=lote_id,
            solicitacao_ids=tuple(solicitacao_ids),
            cnpj_por_solicitacao=cnpjs_normalizados,
        )


@dataclass(frozen=True)
class PendingIssuance:
    emissao_id: int
    lote_id: int
    solicitacao_id: int
    usuario_id: int
    cnpj_emissor: str
    paciente: str
    local: str
    tipo_atendimento: str
    atendimento: str
    cpf: str
    valor: Any
    rua: str
    numero_casa: str
    bairro: str
    cep: str
    complemento: str
    telefone: str
    cidade: str
    uf: str
    tipo_exame: str
    data: Any
    email: str

    def invoice_row(
        self,
        *,
        default_city: str = "FORTALEZA",
        default_uf: str = "CE",
    ) -> InvoiceSpreadsheetRow:
        row = InvoiceSpreadsheetRow(
            row_number=self.solicitacao_id,
            paciente=_text(self.paciente),
            local=_text(self.local),
            tipo_atendimento=_text(self.tipo_atendimento),
            atendimento=_text(self.atendimento),
            cpf=_text(self.cpf),
            valor=_money(self.valor, self.solicitacao_id),
            rua=_text(self.rua),
            numero_casa=_text(self.numero_casa),
            bairro=_text(self.bairro),
            cep=_text(self.cep),
            complemento=_text(self.complemento),
            telefone=_text(self.telefone),
            cidade=_text(self.cidade) or default_city,
            uf=(_text(self.uf) or default_uf).upper(),
            tipo_exame=_multiline_text(self.tipo_exame),
            data=_date_text(self.data),
            email=_text(self.email),
        )
        select_rows([row], row_number=row.row_number, all_rows=False, limit=None)
        return row


@dataclass(frozen=True)
class BatchRunSummary:
    lote_id: int
    dag_run_id: str
    selected: int
    claimed: int
    emitted: int
    failed: int
    pending: int
    processing: int
    batch_status: str

    @property
    def incomplete(self) -> bool:
        return (
            self.failed > 0
            or self.pending > 0
            or self.processing > 0
            or self.batch_status == "ERRO"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "lote_id": self.lote_id,
            "dag_run_id": self.dag_run_id,
            "selecionadas": self.selected,
            "processadas": self.claimed,
            "emitidas": self.emitted,
            "erros": self.failed,
            "pendentes": self.pending,
            "processando": self.processing,
            "status_lote": self.batch_status,
        }


class Issuer(Protocol):
    def issue(
        self,
        row: InvoiceSpreadsheetRow,
        *,
        cnpj: str,
    ) -> IssuanceResult: ...


class IssuanceRepository(Protocol):
    def start_run(self, lote_id: int, dag_run_id: str) -> str: ...

    def list_pending(
        self,
        lote_id: int,
        solicitacao_ids: Sequence[int],
    ) -> list[PendingIssuance]: ...

    def claim(self, emissao_id: int) -> bool: ...

    def mark_success(
        self,
        item: PendingIssuance,
        result: IssuanceResult,
        dag_run_id: str,
    ) -> None: ...

    def mark_failure(
        self,
        item: PendingIssuance,
        error: str,
        dag_run_id: str,
    ) -> None: ...

    def fail_run(self, lote_id: int, dag_run_id: str, error: str) -> None: ...

    def finish_run(
        self,
        lote_id: int,
        dag_run_id: str,
        *,
        selected: int,
        claimed: int,
        emitted: int,
        failed: int,
    ) -> BatchRunSummary: ...


class BatchIssuanceService:
    def __init__(
        self,
        repository: IssuanceRepository,
        issuer: Issuer,
        *,
        cnpj: str | None = None,
        default_city: str = "FORTALEZA",
        default_uf: str = "CE",
    ) -> None:
        self.repository = repository
        self.issuer = issuer
        self.cnpj = _issuer_cnpj(cnpj) if _text(cnpj) else ""
        self.default_city = default_city
        self.default_uf = default_uf

    def run(self, payload: BatchPayload, *, dag_run_id: str) -> BatchRunSummary:
        batch_status = self.repository.start_run(payload.lote_id, dag_run_id)
        if batch_status == "EMITIDA":
            return self.repository.finish_run(
                payload.lote_id,
                dag_run_id,
                selected=len(payload.solicitacao_ids),
                claimed=0,
                emitted=0,
                failed=0,
            )

        claimed = 0
        emitted = 0
        failed = 0
        try:
            items = self.repository.list_pending(
                payload.lote_id,
                payload.solicitacao_ids,
            )
            for item in items:
                if not self.repository.claim(item.emissao_id):
                    continue
                claimed += 1
                try:
                    row = item.invoice_row(
                        default_city=self.default_city,
                        default_uf=self.default_uf,
                    )
                    result = self.issuer.issue(
                        row,
                        cnpj=self._resolve_cnpj(item, payload),
                    )
                except Exception as exc:
                    failed += 1
                    self.repository.mark_failure(
                        item,
                        _error_text(exc),
                        dag_run_id,
                    )
                    continue

                emitted += 1
                self.repository.mark_success(item, result, dag_run_id)

            return self.repository.finish_run(
                payload.lote_id,
                dag_run_id,
                selected=len(payload.solicitacao_ids),
                claimed=claimed,
                emitted=emitted,
                failed=failed,
            )
        except Exception as exc:
            self.repository.fail_run(
                payload.lote_id,
                dag_run_id,
                _error_text(exc),
            )
            raise

    def _resolve_cnpj(
        self,
        item: PendingIssuance,
        payload: BatchPayload,
    ) -> str:
        cnpj_banco = (
            _issuer_cnpj(item.cnpj_emissor)
            if _text(item.cnpj_emissor)
            else ""
        )
        cnpj_payload = payload.cnpj_por_solicitacao.get(
            item.solicitacao_id,
            "",
        )
        if cnpj_banco and cnpj_payload and cnpj_banco != cnpj_payload:
            raise BatchConfigurationError(
                "O CNPJ emissor recebido diverge do registro da emissao."
            )
        cnpj = cnpj_banco or cnpj_payload or self.cnpj
        if not cnpj:
            raise BatchConfigurationError(
                f"CNPJ emissor nao informado para a solicitacao "
                f"#{item.solicitacao_id}."
            )
        return cnpj


class PostgresIssuanceRepository:
    """Persists the Airflow issuance workflow using a DB-API connection."""

    def __init__(self, connection: Any, *, schema: str) -> None:
        if not VALID_IDENTIFIER.fullmatch(schema):
            raise BatchConfigurationError(f"Schema PostgreSQL invalido: {schema!r}.")
        self.connection = connection
        self.schema = schema

    def start_run(self, lote_id: int, dag_run_id: str) -> str:
        sql = f"""
            UPDATE {self._table("lote_emissao_nfse")}
               SET dag_run_id = %s,
                   airflow_disparado_em = COALESCE(
                       airflow_disparado_em,
                       {LOCAL_TIME_SQL}
                   ),
                   erro_disparo = NULL,
                   status = CASE
                       WHEN status IN ('PENDENTE', 'ERRO') THEN 'PROCESSANDO'
                       ELSE status
                   END
             WHERE id = %s
         RETURNING status
        """
        row = self._write_returning_one(sql, (dag_run_id, lote_id))
        if row is None:
            raise BatchDatabaseError(f"Lote de emissao #{lote_id} nao encontrado.")
        return str(row[0])

    def list_pending(
        self,
        lote_id: int,
        solicitacao_ids: Sequence[int],
    ) -> list[PendingIssuance]:
        sql = f"""
            SELECT
                e.id AS emissao_id,
                e.lote_id,
                e.solicitacao_nota_id AS solicitacao_id,
                e.usuario_id,
                COALESCE(
                    NULLIF(e.cnpj_emissor, ''),
                    NULLIF(s.cnpj_emissor, '')
                ) AS cnpj_emissor,
                s.nm_paciente AS paciente,
                s.local,
                s.tipo_atendimento,
                s.codigo_atendimento AS atendimento,
                s.nr_cpf AS cpf,
                s.valor_nota AS valor,
                s.ds_endereco AS rua,
                s.nr_endereco AS numero_casa,
                s.nm_bairro AS bairro,
                s.nr_cep AS cep,
                s.ds_complemento AS complemento,
                s.nr_fone AS telefone,
                COALESCE(
                    to_jsonb(s) ->> 'cidade',
                    to_jsonb(s) ->> 'nm_cidade'
                ) AS cidade,
                COALESCE(
                    to_jsonb(s) ->> 'uf',
                    to_jsonb(s) ->> 'sg_uf'
                ) AS uf,
                s.procedimento AS tipo_exame,
                s.data_criacao AS data,
                s.email
              FROM {self._table("emissao_nfse")} e
              JOIN {self._table("solicitacao_nota")} s
                ON s.id = e.solicitacao_nota_id
              JOIN {self._table("solicitacao_nota_workflow")} w
                ON w.solicitacao_nota_id = s.id
             WHERE e.lote_id = %s
               AND e.solicitacao_nota_id = ANY(%s)
               AND e.status = 'PENDENTE'
               AND w.validacao = 'VALIDADA'
               AND w.status = 'EMISSAO_SOLICITADA'
             ORDER BY e.id
        """
        rows = self._read_dicts(sql, (lote_id, list(solicitacao_ids)))
        return [PendingIssuance(**row) for row in rows]

    def claim(self, emissao_id: int) -> bool:
        sql = f"""
            UPDATE {self._table("emissao_nfse")}
               SET status = 'PROCESSANDO',
                   erro = NULL,
                   data_atualizacao = {LOCAL_TIME_SQL}
             WHERE id = %s
               AND status = 'PENDENTE'
         RETURNING id
        """
        return self._write_returning_one(sql, (emissao_id,)) is not None

    def mark_success(
        self,
        item: PendingIssuance,
        result: IssuanceResult,
        dag_run_id: str,
    ) -> None:
        public_pdf_name, pdf_content, pdf_sha256 = _read_pdf_artifact(result)
        protocol = getattr(result, "protocolo", None)
        observation = _limit(
            f"NFS-e {result.numero_nfse} emitida. "
            f"dag_run_id={dag_run_id}; pdf={public_pdf_name}",
            500,
        )
        statements = [
            (
                f"""
                INSERT INTO {self._table("emissao_nfse_arquivo")} (
                    emissao_nfse_id,
                    nome_arquivo,
                    tipo_mime,
                    conteudo,
                    tamanho_bytes,
                    sha256
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (emissao_nfse_id) DO UPDATE
                   SET nome_arquivo = EXCLUDED.nome_arquivo,
                       tipo_mime = EXCLUDED.tipo_mime,
                       conteudo = EXCLUDED.conteudo,
                       tamanho_bytes = EXCLUDED.tamanho_bytes,
                       sha256 = EXCLUDED.sha256,
                       data_atualizacao = {LOCAL_TIME_SQL}
                """,
                (
                    item.emissao_id,
                    public_pdf_name,
                    PDF_MIME_TYPE,
                    pdf_content,
                    len(pdf_content),
                    pdf_sha256,
                ),
            ),
            (
                f"""
                UPDATE {self._table("emissao_nfse")}
                   SET status = 'EMITIDA',
                       numero_nfse = %s,
                       protocolo = %s,
                       erro = NULL,
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE id = %s
                   AND status = 'PROCESSANDO'
                """,
                (result.numero_nfse, protocol, item.emissao_id),
            ),
            (
                f"""
                UPDATE {self._table("solicitacao_nota_workflow")}
                   SET status = 'EMITIDA',
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE solicitacao_nota_id = %s
                   AND status = 'EMISSAO_SOLICITADA'
                """,
                (item.solicitacao_id,),
            ),
            (
                f"""
                INSERT INTO {self._table("solicitacao_nota_evento")} (
                    solicitacao_nota_id,
                    usuario_id,
                    tipo_acao,
                    observacao
                ) VALUES (%s, %s, 'NFSE_EMITIDA', %s)
                """,
                (item.solicitacao_id, item.usuario_id, observation),
            ),
        ]
        self._transaction(statements)

    def mark_failure(
        self,
        item: PendingIssuance,
        error: str,
        dag_run_id: str,
    ) -> None:
        database_error = _limit(error, 1000)
        observation = _limit(
            f"Falha na emissao. dag_run_id={dag_run_id}; erro={error}",
            500,
        )
        statements = [
            (
                f"""
                UPDATE {self._table("emissao_nfse")}
                   SET status = 'ERRO',
                       erro = %s,
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE id = %s
                   AND status = 'PROCESSANDO'
                """,
                (database_error, item.emissao_id),
            ),
            (
                f"""
                UPDATE {self._table("solicitacao_nota_workflow")}
                   SET status = 'ERRO_EMISSAO',
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE solicitacao_nota_id = %s
                   AND status = 'EMISSAO_SOLICITADA'
                """,
                (item.solicitacao_id,),
            ),
            (
                f"""
                INSERT INTO {self._table("solicitacao_nota_evento")} (
                    solicitacao_nota_id,
                    usuario_id,
                    tipo_acao,
                    observacao
                ) VALUES (%s, %s, 'ERRO_EMISSAO', %s)
                """,
                (item.solicitacao_id, item.usuario_id, observation),
            ),
        ]
        self._transaction(statements)

    def fail_run(self, lote_id: int, dag_run_id: str, error: str) -> None:
        message = _limit(f"dag_run_id={dag_run_id}; erro={error}", 1000)
        statements = [
            (
                f"""
                UPDATE {self._table("emissao_nfse")}
                   SET status = 'ERRO',
                       erro = %s,
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE lote_id = %s
                   AND status = 'PROCESSANDO'
                """,
                (message, lote_id),
            ),
            (
                f"""
                UPDATE {self._table("solicitacao_nota_workflow")} w
                   SET status = 'ERRO_EMISSAO',
                       data_atualizacao = {LOCAL_TIME_SQL}
                 WHERE status = 'EMISSAO_SOLICITADA'
                   AND EXISTS (
                       SELECT 1
                         FROM {self._table("emissao_nfse")} e
                        WHERE e.lote_id = %s
                          AND e.solicitacao_nota_id = w.solicitacao_nota_id
                          AND e.status = 'ERRO'
                          AND e.erro = %s
                   )
                """,
                (lote_id, message),
            ),
            (
                f"""
                UPDATE {self._table("lote_emissao_nfse")}
                   SET status = 'ERRO',
                       dag_run_id = %s,
                       erro_disparo = %s
                 WHERE id = %s
                   AND status <> 'EMITIDA'
                """,
                (dag_run_id, message, lote_id),
            ),
        ]
        self._transaction(statements)

    def finish_run(
        self,
        lote_id: int,
        dag_run_id: str,
        *,
        selected: int,
        claimed: int,
        emitted: int,
        failed: int,
    ) -> BatchRunSummary:
        sql = f"""
            SELECT status, COUNT(*) AS quantidade
              FROM {self._table("emissao_nfse")}
             WHERE lote_id = %s
             GROUP BY status
        """
        counts = {
            str(row["status"]): int(row["quantidade"])
            for row in self._read_dicts(sql, (lote_id,))
        }
        total = sum(counts.values())
        if counts.get("ERRO", 0):
            batch_status = "ERRO"
            error = self._batch_error(lote_id, dag_run_id)
        elif total > 0 and counts.get("EMITIDA", 0) == total:
            batch_status = "EMITIDA"
            error = None
        else:
            batch_status = "ERRO"
            error = _limit(
                f"dag_run_id={dag_run_id}; lote terminou com "
                f"{counts.get('PENDENTE', 0)} item(ns) pendente(s) e "
                f"{counts.get('PROCESSANDO', 0)} item(ns) em processamento.",
                1000,
            )

        self._write(
            f"""
            UPDATE {self._table("lote_emissao_nfse")}
               SET status = %s,
                   dag_run_id = %s,
                   erro_disparo = %s
             WHERE id = %s
            """,
            (batch_status, dag_run_id, error, lote_id),
        )
        return BatchRunSummary(
            lote_id=lote_id,
            dag_run_id=dag_run_id,
            selected=selected,
            claimed=claimed,
            emitted=emitted,
            failed=failed,
            pending=counts.get("PENDENTE", 0),
            processing=counts.get("PROCESSANDO", 0),
            batch_status=batch_status,
        )

    def _batch_error(self, lote_id: int, dag_run_id: str) -> str:
        rows = self._read_dicts(
            f"""
            SELECT solicitacao_nota_id, erro
              FROM {self._table("emissao_nfse")}
             WHERE lote_id = %s
               AND status = 'ERRO'
             ORDER BY id
             LIMIT 5
            """,
            (lote_id,),
        )
        details = "; ".join(
            f"solicitacao={row['solicitacao_nota_id']}: {row['erro']}"
            for row in rows
        )
        return _limit(f"dag_run_id={dag_run_id}; {details}", 1000)

    def _table(self, table_name: str) -> str:
        return f'"{self.schema}"."{table_name}"'

    def _read_dicts(
        self,
        sql: str,
        parameters: Sequence[Any],
    ) -> list[dict[str, Any]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, parameters)
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _write_returning_one(
        self,
        sql: str,
        parameters: Sequence[Any],
    ) -> Sequence[Any] | None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, parameters)
            row = cursor.fetchone()
            self.connection.commit()
            return row
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def _write(self, sql: str, parameters: Sequence[Any]) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, parameters)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def _transaction(
        self,
        statements: Sequence[tuple[str, Sequence[Any]]],
    ) -> None:
        cursor = self.connection.cursor()
        try:
            for sql, parameters in statements:
                cursor.execute(sql, parameters)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _multiline_text(value: Any) -> str:
    lines = (
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in str(value or "").splitlines()
    )
    return "\r\n".join(line for line in lines if line)


def _issuer_cnpj(value: Any) -> str:
    cnpj = re.sub(r"\D", "", _text(value))
    if len(cnpj) != 14:
        raise BatchConfigurationError(
            "O CNPJ emissor deve conter 14 digitos."
        )
    return cnpj


def _money(value: Any, solicitacao_id: int) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        result = Decimal(str(value))
    else:
        raw = _text(value)
        if not raw:
            raise ValueError(
                f"Solicitacao {solicitacao_id}: valor do servico nao informado."
            )
        normalized = raw.replace("R$", "").replace(" ", "")
        if "," in normalized:
            normalized = normalized.replace(".", "").replace(",", ".")
        try:
            result = Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError(
                f"Solicitacao {solicitacao_id}: valor invalido: {raw!r}."
            ) from exc
    return result.quantize(Decimal("0.01"))


def _date_text(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return _text(value)


def _read_pdf_artifact(result: IssuanceResult) -> tuple[str, bytes, str]:
    numero_nfse = _text(result.numero_nfse)
    if not numero_nfse or not numero_nfse.isdigit():
        raise BatchArtifactError(
            "Numero da NFS-e invalido para persistencia do PDF."
        )

    try:
        content = result.pdf_path.read_bytes()
    except OSError as exc:
        raise BatchArtifactError(
            f"Nao foi possivel ler o PDF da NFS-e {numero_nfse}."
        ) from exc

    if not content.startswith(b"%PDF"):
        raise BatchArtifactError(
            f"O arquivo da NFS-e {numero_nfse} nao e um PDF valido."
        )

    public_name = f"nfse-{numero_nfse}.pdf"
    return public_name, content, hashlib.sha256(content).hexdigest()


def _error_text(error: Exception) -> str:
    message = re.sub(r"\s+", " ", str(error)).strip()
    return message or error.__class__.__name__


def _limit(value: str, size: int) -> str:
    return value[:size]

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from nfs_fortaleza.batch import (
    BatchArtifactError,
    BatchConfigurationError,
    BatchIssuanceService,
    BatchPayload,
    BatchRunSummary,
    PendingIssuance,
    PostgresIssuanceRepository,
)
from nfs_fortaleza.issuance import IssuanceResult


def pending_item(
    *,
    emissao_id: int = 1,
    solicitacao_id: int = 10,
    valor: object = "60,75",
    cnpj_emissor: str = "05613278000158",
) -> PendingIssuance:
    return PendingIssuance(
        emissao_id=emissao_id,
        lote_id=5,
        solicitacao_id=solicitacao_id,
        usuario_id=9,
        cnpj_emissor=cnpj_emissor,
        paciente="PACIENTE TESTE",
        local="CLINICA 2",
        tipo_atendimento="AMBULATORIO",
        atendimento="317189",
        cpf="15506142315",
        valor=valor,
        rua="RUA TESTE",
        numero_casa="100",
        bairro="CENTRO",
        cep="60000000",
        complemento="APTO 101",
        telefone="85999999999",
        cidade="",
        uf="",
        tipo_exame="ANALISE CLINICA",
        data=datetime(2026, 7, 23, 10, 0),
        email="paciente@example.com",
    )


class BatchPayloadTests(unittest.TestCase):
    def test_accepts_api_contract(self) -> None:
        payload = BatchPayload.from_mapping(
            {
                "lote_id": 42,
                "solicitacao_ids": [101, 102],
                "cnpj_por_solicitacao": {
                    "101": "05613278000158",
                    "102": "08711085000128",
                },
            }
        )

        self.assertEqual(payload.lote_id, 42)
        self.assertEqual(payload.solicitacao_ids, (101, 102))
        self.assertEqual(
            payload.cnpj_por_solicitacao,
            {
                101: "05613278000158",
                102: "08711085000128",
            },
        )

    def test_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(BatchConfigurationError):
            BatchPayload.from_mapping(
                {"lote_id": 42, "solicitacao_ids": [101, 101]}
            )

    def test_database_row_uses_safe_city_defaults_and_validates_value(self) -> None:
        row = pending_item().invoice_row(
            default_city="FORTALEZA",
            default_uf="CE",
        )

        self.assertEqual(row.valor, Decimal("60.75"))
        self.assertEqual(row.cidade, "FORTALEZA")
        self.assertEqual(row.uf, "CE")
        self.assertEqual(row.cep, "60000000")
        self.assertEqual(row.complemento, "APTO 101")
        self.assertEqual(row.telefone, "85999999999")

        with self.assertRaisesRegex(ValueError, "valor do servico"):
            pending_item(valor=None).invoice_row()

    def test_preserves_each_procedure_on_its_own_line(self) -> None:
        item = replace(
            pending_item(),
            tipo_exame=(
                "40901106 - ECODOPPLERCARDIOGRAMA\n"
                "  20102038   -   MONITORIZACAO ARTERIAL  \n\n"
                "40302580 - UREIA, DOSAGEM"
            ),
        )

        row = item.invoice_row()

        self.assertEqual(
            row.tipo_exame,
            "40901106 - ECODOPPLERCARDIOGRAMA\r\n"
            "20102038 - MONITORIZACAO ARTERIAL\r\n"
            "40302580 - UREIA, DOSAGEM",
        )


class FakeRepository:
    def __init__(self, items: list[PendingIssuance], *, status: str = "PROCESSANDO"):
        self.items = items
        self.status = status
        self.claimed: list[int] = []
        self.successes: list[int] = []
        self.failures: list[int] = []
        self.run_failures: list[str] = []

    def start_run(self, lote_id: int, dag_run_id: str) -> str:
        self.lote_id = lote_id
        self.dag_run_id = dag_run_id
        return self.status

    def list_pending(self, lote_id: int, solicitacao_ids):
        selected = set(solicitacao_ids)
        return [
            item
            for item in self.items
            if item.lote_id == lote_id and item.solicitacao_id in selected
        ]

    def claim(self, emissao_id: int) -> bool:
        self.claimed.append(emissao_id)
        return True

    def mark_success(self, item, result, dag_run_id):
        self.successes.append(item.emissao_id)

    def mark_failure(self, item, error, dag_run_id):
        self.failures.append(item.emissao_id)

    def fail_run(self, lote_id, dag_run_id, error):
        self.run_failures.append(error)

    def finish_run(
        self,
        lote_id,
        dag_run_id,
        *,
        selected,
        claimed,
        emitted,
        failed,
    ):
        return BatchRunSummary(
            lote_id=lote_id,
            dag_run_id=dag_run_id,
            selected=selected,
            claimed=claimed,
            emitted=emitted,
            failed=failed,
            pending=0,
            processing=0,
            batch_status="ERRO" if failed else "EMITIDA",
        )


class FakeIssuer:
    def __init__(self, *, fail_request_id: int | None = None):
        self.fail_request_id = fail_request_id
        self.rows: list[int] = []
        self.cnpjs: list[str] = []

    def issue(self, row, *, cnpj):
        self.rows.append(row.row_number)
        self.cnpjs.append(cnpj)
        if row.row_number == self.fail_request_id:
            raise RuntimeError("portal indisponivel")
        return IssuanceResult(
            row_number=row.row_number,
            numero_nfse=str(1000 + row.row_number),
            pdf_path=Path(f"/tmp/{row.row_number}.pdf"),
            paciente=row.paciente,
            cpf=row.cpf,
            client_registered=False,
        )


class BatchServiceTests(unittest.TestCase):
    def test_processes_each_database_item_and_persists_individual_result(self) -> None:
        repository = FakeRepository(
            [
                pending_item(emissao_id=1, solicitacao_id=10),
                pending_item(
                    emissao_id=2,
                    solicitacao_id=20,
                    cnpj_emissor="08711085000128",
                ),
            ]
        )
        issuer = FakeIssuer(fail_request_id=20)
        service = BatchIssuanceService(
            repository,
            issuer,
            cnpj="59932105000121",
        )

        summary = service.run(
            BatchPayload(5, (10, 20)),
            dag_run_id="api_prontocardio_nfse_lote_5",
        )

        self.assertEqual(repository.claimed, [1, 2])
        self.assertEqual(repository.successes, [1])
        self.assertEqual(repository.failures, [2])
        self.assertEqual(
            issuer.cnpjs,
            ["05613278000158", "08711085000128"],
        )
        self.assertEqual(summary.emitted, 1)
        self.assertEqual(summary.failed, 1)
        self.assertTrue(summary.incomplete)

    def test_does_not_reissue_a_finished_batch(self) -> None:
        repository = FakeRepository(
            [pending_item()],
            status="EMITIDA",
        )
        issuer = FakeIssuer()
        service = BatchIssuanceService(
            repository,
            issuer,
            cnpj="59932105000121",
        )

        summary = service.run(
            BatchPayload(5, (10,)),
            dag_run_id="manual__retry",
        )

        self.assertEqual(issuer.rows, [])
        self.assertEqual(summary.batch_status, "EMITIDA")


class CapturingCursor:
    description = [
        ("emissao_id",),
        ("lote_id",),
        ("solicitacao_id",),
        ("usuario_id",),
        ("cnpj_emissor",),
        ("paciente",),
        ("local",),
        ("tipo_atendimento",),
        ("atendimento",),
        ("cpf",),
        ("valor",),
        ("rua",),
        ("numero_casa",),
        ("bairro",),
        ("cep",),
        ("complemento",),
        ("telefone",),
        ("cidade",),
        ("uf",),
        ("tipo_exame",),
        ("data",),
        ("email",),
    ]

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters):
        self.connection.sql = sql
        self.connection.parameters = parameters

    @staticmethod
    def fetchall():
        return []

    @staticmethod
    def close():
        return None


class CapturingConnection:
    def cursor(self):
        return CapturingCursor(self)


class TransactionCapturingCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters):
        self.connection.statements.append((sql, parameters))

    @staticmethod
    def close():
        return None


class TransactionCapturingConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return TransactionCapturingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class RepositoryQueryTests(unittest.TestCase):
    def test_database_is_authoritative_for_approval_and_pending_flags(self) -> None:
        connection = CapturingConnection()
        repository = PostgresIssuanceRepository(
            connection,
            schema="api_prontocardio",
        )

        repository.list_pending(42, [101, 102])

        compact_sql = " ".join(connection.sql.split())
        self.assertIn("e.status = 'PENDENTE'", compact_sql)
        self.assertIn("w.validacao = 'VALIDADA'", compact_sql)
        self.assertIn("w.status = 'EMISSAO_SOLICITADA'", compact_sql)
        self.assertIn("s.valor_nota AS valor", compact_sql)
        self.assertIn("s.nr_cep AS cep", compact_sql)
        self.assertIn("s.ds_complemento AS complemento", compact_sql)
        self.assertIn("s.nr_fone AS telefone", compact_sql)
        self.assertIn("e.cnpj_emissor", compact_sql)
        self.assertEqual(connection.parameters, (42, [101, 102]))

    def test_success_upserts_valid_pdf_before_marking_issuance_emitted(self) -> None:
        pdf_content = b"%PDF-1.7\nconteudo de teste\n%%EOF"
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "123 - PACIENTE TESTE.pdf"
            pdf_path.write_bytes(pdf_content)
            result = IssuanceResult(
                row_number=10,
                numero_nfse="123",
                pdf_path=pdf_path,
                paciente="PACIENTE TESTE",
                cpf="15506142315",
                client_registered=False,
            )
            connection = TransactionCapturingConnection()
            repository = PostgresIssuanceRepository(
                connection,
                schema="api_prontocardio",
            )

            repository.mark_success(
                pending_item(),
                result,
                "api_prontocardio_nfse_lote_5",
            )

        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(len(connection.statements), 4)

        artifact_sql, artifact_parameters = connection.statements[0]
        compact_artifact_sql = " ".join(artifact_sql.split())
        self.assertIn(
            'INSERT INTO "api_prontocardio"."emissao_nfse_arquivo"',
            compact_artifact_sql,
        )
        self.assertIn(
            "ON CONFLICT (emissao_nfse_id) DO UPDATE",
            compact_artifact_sql,
        )
        self.assertIn(
            "data_atualizacao = timezone('America/Sao_Paulo', now())",
            compact_artifact_sql,
        )
        self.assertEqual(
            artifact_parameters,
            (
                1,
                "nfse-123.pdf",
                "application/pdf",
                pdf_content,
                len(pdf_content),
                hashlib.sha256(pdf_content).hexdigest(),
            ),
        )

        issuance_sql, _issuance_parameters = connection.statements[1]
        self.assertIn("SET status = 'EMITIDA'", issuance_sql)
        _event_sql, event_parameters = connection.statements[3]
        observation = event_parameters[2]
        self.assertIn("pdf=nfse-123.pdf", observation)
        self.assertNotIn("PACIENTE TESTE", observation)
        self.assertNotIn(str(pdf_path), observation)

    def test_success_rejects_invalid_pdf_before_opening_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "resposta.html"
            pdf_path.write_bytes(b"<html>erro do portal</html>")
            result = IssuanceResult(
                row_number=10,
                numero_nfse="123",
                pdf_path=pdf_path,
                paciente="PACIENTE TESTE",
                cpf="15506142315",
                client_registered=False,
            )
            connection = TransactionCapturingConnection()
            repository = PostgresIssuanceRepository(
                connection,
                schema="api_prontocardio",
            )

            with self.assertRaisesRegex(BatchArtifactError, "PDF valido"):
                repository.mark_success(
                    pending_item(),
                    result,
                    "api_prontocardio_nfse_lote_5",
                )

        self.assertEqual(connection.statements, [])
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()

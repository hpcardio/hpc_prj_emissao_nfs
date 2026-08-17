from pathlib import Path

import pytest

from nfs_fortaleza.glosas_ipm_materialization import (
    MATERIALIZAR_RASTREIO_SQL,
    MATERIALIZAR_REGISTROS_SQL,
    materializar_registros_glosa,
)


class CursorFake:
    def __init__(self, rowcounts):
        self.rowcounts = iter(rowcounts)
        self.rowcount = -1
        self.comandos = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, comando):
        self.comandos.append(comando)
        self.rowcount = next(self.rowcounts)


class PostgresFake:
    def __init__(self, rowcounts=(3, 5)):
        self.cursor_fake = CursorFake(rowcounts)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_fake

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_materializa_registros_e_rastreios_em_uma_transacao():
    postgres = PostgresFake()

    resultado = materializar_registros_glosa(postgres)

    assert postgres.cursor_fake.comandos == [
        MATERIALIZAR_REGISTROS_SQL,
        MATERIALIZAR_RASTREIO_SQL,
    ]
    assert resultado == {"registros_glosa": 3, "rastreios": 5}
    assert postgres.commits == 1
    assert postgres.rollbacks == 0


def test_desfaz_transacao_quando_materializacao_falha():
    postgres = PostgresFake(rowcounts=())

    with pytest.raises(StopIteration):
        materializar_registros_glosa(postgres)

    assert postgres.commits == 0
    assert postgres.rollbacks == 1


def test_materializacao_preserva_tratativas_e_e_idempotente():
    registros = " ".join(MATERIALIZAR_REGISTROS_SQL.upper().split())
    rastreios = " ".join(MATERIALIZAR_RASTREIO_SQL.upper().split())

    assert "WHERE NOT EXISTS" in registros
    assert "EXISTENTE.SN_ATIVO = 'TRUE'" in registros
    assert "ON CONFLICT (ID_REGISTRO) DO UPDATE" in rastreios
    assert "ORDER BY (ITEM.DT_RECURSO IS NULL) DESC" in rastreios


def test_materializacao_usa_mesmo_destino_para_ambos_os_status():
    registros = " ".join(MATERIALIZAR_REGISTROS_SQL.upper().split())
    rastreios = " ".join(MATERIALIZAR_RASTREIO_SQL.upper().split())

    assert "LEFT JOIN VINCULOS" in registros
    assert "THEN 'TRIAGEM' ELSE 'CONCILIACAO'" in registros
    assert "LEFT JOIN VINCULOS" in rastreios
    assert (
        "ITEM.CONCILIACAO_REMESSA_ID IS NOT DISTINCT FROM "
        "VINCULOS.CONCILIACAO_REMESSA_ID"
    ) in rastreios


def test_mart_nao_anexa_glosa_ao_primeiro_lancamento_da_conta():
    raiz = Path(__file__).parents[1] / "dbt_glosas_ipm" / "models"
    resolucao = (
        raiz / "intermediate" / "int_ipm_glosas_resolvidas.sql"
    ).read_text()
    mart = (
        raiz / "marts" / "processos_relatorios_itens_ipm.sql"
    ).read_text()

    assert "case when quantidade_itens = 1 then cd_lancamento" not in resolucao
    assert "glosa.cd_lancamento = item.cd_lancamento" in mart
    assert "item.ordem_item_conta = 1" not in mart
    assert "nullif(glosa.codigo_servico, '')" in mart
    assert "nullif(glosa.descricao_servico, '')" in mart

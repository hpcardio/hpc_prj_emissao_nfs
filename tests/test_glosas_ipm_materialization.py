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

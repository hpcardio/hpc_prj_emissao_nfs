from datetime import date

from nfs_fortaleza.glosas_ipm_stage import (
    REMESSAS_SQL,
    carregar_hpc_intermediaria,
)


class CursorPostgres:
    def __init__(self):
        self.comandos = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, comando):
        self.comandos.append(comando)

    def fetchone(self):
        return date(2026, 1, 1), date(2026, 1, 31)


class ConexaoPostgres:
    def __init__(self):
        self.destino = CursorPostgres()
        self.commits = 0

    def cursor(self):
        return self.destino

    def commit(self):
        self.commits += 1


class CursorOracle:
    description = (("CD_REMESSA",),)

    def execute(self, *_):
        return None

    def fetchmany(self, _):
        return []

    def close(self):
        return None


class ConexaoOracle:
    def cursor(self):
        return CursorOracle()


def test_cria_tabela_de_itens_antes_de_alterar_colunas():
    postgres = ConexaoPostgres()

    resultado = carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [" ".join(item.split()) for item in postgres.destino.comandos]
    indice_create = next(
        indice
        for indice, comando in enumerate(comandos)
        if "CREATE TABLE IF NOT EXISTS" in comando
        and "ipm_itens_oracle" in comando
    )
    indice_alter = next(
        indice
        for indice, comando in enumerate(comandos)
        if "ALTER TABLE api_prontocardio_staging.ipm_itens_oracle" in comando
    )

    assert indice_create < indice_alter
    assert resultado == {"remessas": 0, "itens": 0}
    assert postgres.commits == 1


def test_total_da_remessa_soma_registros_consolidados_por_conta():
    consulta = " ".join(REMESSAS_SQL.upper().split())

    assert "MAX(VL_TOTAL_CONTA)" not in consulta
    assert "MAX(VL_TOTAL_REGISTRO) AS VL_TOTAL_REGISTRO" in consulta
    assert "SUM(VL_TOTAL_REGISTRO) AS VALOR_TOTAL" in consulta
    assert "GROUP BY CD_REMESSA, CD_REG" in consulta

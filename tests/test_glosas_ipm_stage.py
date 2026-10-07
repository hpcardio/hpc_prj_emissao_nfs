from datetime import date

from nfs_fortaleza.glosas_ipm_stage import (
    ITENS_SQL,
    REMESSAS_SQL,
    _consulta_com_escopo,
    carregar_hpc_intermediaria,
)


class CursorPostgres:
    def __init__(
        self,
        periodo=(date(2026, 1, 1), date(2026, 1, 31)),
        guias=(),
    ):
        self.comandos = []
        self.periodo = periodo
        self.proxima_linha = None
        self.guias = guias

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, comando, _parametros=None):
        self.comandos.append(comando)
        texto = str(comando)
        if "SELECT EXISTS" in texto:
            self.proxima_linha = (False,)
        elif "SELECT MIN(data_referencia)" in texto:
            self.proxima_linha = self.periodo
        elif "array_agg(DISTINCT BTRIM(numero_guia_senha::text))" in texto:
            self.proxima_linha = (self.guias,)

    def fetchone(self):
        return self.proxima_linha


def _comando_normalizado(comando) -> str:
    return " ".join(str(comando).split())


class ConexaoPostgres:
    def __init__(
        self,
        periodo=(date(2026, 1, 1), date(2026, 1, 31)),
        guias=(),
    ):
        self.destino = CursorPostgres(periodo, guias)
        self.commits = 0

    def cursor(self):
        return self.destino

    def commit(self):
        self.commits += 1


class CursorOracle:
    description = (("CD_REMESSA",),)

    def __init__(self):
        self.consulta = ""
        self.parametros = {}

    def execute(self, consulta, parametros=None):
        self.consulta = str(consulta)
        self.parametros = parametros or {}
        return None

    def fetchmany(self, _):
        return []

    def close(self):
        return None


class ConexaoOracle:
    def __init__(self):
        self.cursores = []

    def cursor(self):
        cursor = CursorOracle()
        self.cursores.append(cursor)
        return cursor


def test_cria_tabela_de_itens_antes_de_alterar_colunas():
    postgres = ConexaoPostgres()

    resultado = carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
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


def test_garante_tabela_de_relatorios_antes_da_execucao_dbt():
    postgres = ConexaoPostgres()

    carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
    assert any(
        "CREATE TABLE IF NOT EXISTS" in comando
        and "processos_relatorios_ipm" in comando
        and "_dlt_load_id" in comando
        and "_dlt_id" in comando
        for comando in comandos
    )


def test_periodo_hpc_considera_demonstrativo_e_relatorios_spu():
    postgres = ConexaoPostgres()

    carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
    consulta_periodo = next(
        comando
        for comando in comandos
        if "SELECT MIN(data_referencia), MAX(data_referencia)" in comando
    )
    assert "demonstrativo_processos_ipm" in consulta_periodo
    assert "demonstrativo_conta_ipm" in consulta_periodo
    assert "origem_maida IS NOT NULL" in consulta_periodo
    assert "processos_relatorios_ipm" in consulta_periodo
    assert consulta_periodo.count("DATE '2024-01-01'") == 3
    assert consulta_periodo.count("INTERVAL '2 months'") == 3
    assert "status_processo IN ('FINALIZADO', 'TRAMITANDO')" in consulta_periodo


def test_confirma_tabela_de_relatorios_sem_periodo_do_demonstrativo():
    postgres = ConexaoPostgres(periodo=(None, None))

    resultado = carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    assert resultado == {"remessas": 0, "itens": 0}
    assert postgres.commits == 1


def test_total_da_remessa_soma_registros_consolidados_por_conta():
    consulta = " ".join(REMESSAS_SQL.upper().split())

    assert "MAX(VL_TOTAL_CONTA)" not in consulta
    assert "MAX(VL_TOTAL_REGISTRO) AS VL_TOTAL_REGISTRO" in consulta
    assert "SUM(VL_TOTAL_REGISTRO) AS VALOR_TOTAL" in consulta
    assert "GROUP BY CD_REMESSA, CD_REG" in consulta


def test_consulta_de_itens_usa_guias_maida_em_qualquer_convenio():
    consulta, parametros = _consulta_com_escopo(
        ITENS_SQL, ("412690", "413462")
    )

    consulta = _comando_normalizado(consulta)
    assert "h.cd_convenio IN (:convenio_base_0)" in consulta
    assert "h.nr_guia IN (:guia_0, :guia_1)" in consulta
    assert parametros == {
        "convenio_base_0": 10,
        "guia_0": "412690",
        "guia_1": "413462",
    }


def test_guias_sao_divididas_em_lotes_compativeis_com_oracle():
    guias = tuple(str(numero) for numero in range(501))

    consulta, _ = _consulta_com_escopo(ITENS_SQL, guias)

    assert consulta.count("h.nr_guia IN (") == 2
    assert ":guia_0" in consulta
    assert ":guia_500" in consulta


def test_carga_limita_outros_convenios_as_guias_maida():
    postgres = ConexaoPostgres(guias=("412690",))
    oracle = ConexaoOracle()

    carregar_hpc_intermediaria(oracle, postgres)

    consultas_carga = [
        cursor
        for cursor in oracle.cursores
        if "data_inicial" in cursor.parametros
    ]
    assert len(consultas_carga) == 2
    assert all(
        cursor.parametros["convenio_base_0"] == 10
        for cursor in consultas_carga
    )
    consulta_itens = next(
        cursor for cursor in consultas_carga if "guia_0" in cursor.parametros
    )
    assert consulta_itens.parametros["guia_0"] == "412690"
    assert "h.nr_guia IN (:guia_0)" in consulta_itens.consulta
    assert all("__ESCOPO" not in cursor.consulta for cursor in consultas_carga)

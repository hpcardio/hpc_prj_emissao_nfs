from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from psycopg2.extras import execute_values


REMESSAS_SQL = """
WITH contas AS (
    SELECT cd_remessa, cd_reg, nm_convenio, cnpj_convenio, dt_competencia,
           MAX(vl_total_registro) AS vl_total_registro
      FROM dbamv.hpc_v_conta_atendimento
     WHERE sn_pertence_pacote = 'N'
       AND cd_convenio = 10
       AND cd_remessa IS NOT NULL
       AND dt_competencia >= :data_inicial
       AND dt_competencia < :data_final
     GROUP BY cd_remessa, cd_reg, nm_convenio, cnpj_convenio,
              dt_competencia
)
SELECT cd_remessa, nm_convenio, cnpj_convenio,
       TO_CHAR(dt_competencia, 'MM/YYYY') AS competencia,
       SUM(vl_total_registro) AS valor_total
  FROM contas
 GROUP BY cd_remessa, nm_convenio, cnpj_convenio,
          TO_CHAR(dt_competencia, 'MM/YYYY')
"""

ITENS_SQL = """
SELECT h.cd_remessa, h.cd_reg, h.cd_lancamento, h.cd_atendimento,
       h.cd_paciente, h.nm_paciente, h.cd_prestador, h.nm_prestador,
       h.cd_convenio, h.cnpj_convenio, h.nm_convenio, h.tp_atendimento,
       h.nr_guia, h.nr_carteira, h.cd_pro_fat, h.cd_tuss, h.descricao,
       h.dt_atendimento, h.dt_alta,
       h.dt_competencia, h.dt_lancamento, h.qt_lancamento,
       h.vl_total_conta, h.cd_gru_fat, h.ds_gru_fat,
       g.cd_gru_pro, g.ds_gru_pro
  FROM dbamv.hpc_v_conta_atendimento h
  LEFT JOIN dbamv.pro_fat p ON p.cd_pro_fat = h.cd_pro_fat
  LEFT JOIN dbamv.gru_pro g ON g.cd_gru_pro = p.cd_gru_pro
 WHERE h.cd_remessa IS NOT NULL
   AND h.cd_convenio = 10
   AND (
       (h.dt_competencia >= :data_inicial AND h.dt_competencia < :data_final)
       OR (h.dt_lancamento >= :data_inicial AND h.dt_lancamento < :data_final)
       OR (h.dt_atendimento >= :data_inicial AND h.dt_atendimento < :data_final)
   )
"""


def _linhas(cursor, tamanho: int = 5000) -> Iterable[list[tuple[Any, ...]]]:
    while lote := cursor.fetchmany(tamanho):
        yield lote


def _mes_seguinte(valor: date) -> date:
    if valor.month == 12:
        return date(valor.year + 1, 1, 1)
    return date(valor.year, valor.month + 1, 1)


def _periodo_demonstrativo(destino) -> tuple[date, date] | None:
    destino.execute("""
        SELECT MIN(data_realizacao), MAX(data_realizacao)
          FROM api_prontocardio.demonstrativo_processos_ipm
         WHERE COALESCE(valor_glosa, 0) > 0
    """)
    data_inicial, data_final = destino.fetchone()
    if data_inicial is None or data_final is None:
        return None
    if isinstance(data_inicial, datetime):
        data_inicial = data_inicial.date()
    if isinstance(data_final, datetime):
        data_final = data_final.date()
    return date(data_inicial.year, data_inicial.month, 1), _mes_seguinte(
        data_final
    )


def carregar_hpc_intermediaria(oracle, postgres) -> dict[str, int]:
    with postgres.cursor() as destino:
        periodo = _periodo_demonstrativo(destino)
        if periodo is None:
            return {"remessas": 0, "itens": 0}
        data_inicial, data_final = periodo
        destino.execute("CREATE SCHEMA IF NOT EXISTS api_prontocardio_staging")
        destino.execute("""
            CREATE TABLE IF NOT EXISTS
                api_prontocardio_staging.ipm_remessas_oracle (
                cd_remessa BIGINT, nm_convenio TEXT, cnpj_convenio TEXT,
                competencia TEXT, valor_total NUMERIC(18,2)
            )
        """)
        destino.execute("""
            CREATE TABLE IF NOT EXISTS
                api_prontocardio_staging.ipm_itens_oracle (
                cd_remessa BIGINT, cd_reg BIGINT, cd_lancamento BIGINT,
                cd_atendimento BIGINT, cd_paciente BIGINT, nm_paciente TEXT,
                cd_prestador BIGINT, nm_prestador TEXT, cd_convenio BIGINT,
                cnpj_convenio TEXT, nm_convenio TEXT, tp_atendimento TEXT,
                nr_guia TEXT, nr_carteira TEXT, cd_pro_fat TEXT, cd_tuss TEXT,
                descricao TEXT, dt_atendimento TIMESTAMP, dt_alta TIMESTAMP,
                dt_competencia DATE, dt_lancamento TIMESTAMP,
                qt_lancamento NUMERIC, vl_total_conta NUMERIC(18,2),
                cd_gru_fat BIGINT, ds_gru_fat TEXT,
                cd_gru_pro BIGINT, ds_gru_pro TEXT
            )
        """)
        destino.execute("""
            ALTER TABLE api_prontocardio_staging.ipm_itens_oracle
                ADD COLUMN IF NOT EXISTS cd_gru_pro BIGINT,
                ADD COLUMN IF NOT EXISTS ds_gru_pro TEXT
        """)
        destino.execute(
            "TRUNCATE api_prontocardio_staging.ipm_remessas_oracle, "
            "api_prontocardio_staging.ipm_itens_oracle"
        )

        totais = {}
        for nome, consulta, tabela in (
            ("remessas", REMESSAS_SQL, "ipm_remessas_oracle"),
            ("itens", ITENS_SQL, "ipm_itens_oracle"),
        ):
            origem = oracle.cursor()
            origem.execute(
                consulta,
                {"data_inicial": data_inicial, "data_final": data_final},
            )
            colunas = [item[0].lower() for item in origem.description]
            quantidade = 0
            for lote in _linhas(origem):
                execute_values(
                    destino,
                    f"INSERT INTO api_prontocardio_staging.{tabela} "
                    f"({', '.join(colunas)}) VALUES %s",
                    lote,
                )
                quantidade += len(lote)
            origem.close()
            totais[nome] = quantidade
        destino.execute("""
            CREATE INDEX IF NOT EXISTS ix_ipm_remessas_oracle_chave
                ON api_prontocardio_staging.ipm_remessas_oracle
                (competencia, valor_total)
        """)
        destino.execute("""
            CREATE INDEX IF NOT EXISTS ix_ipm_itens_oracle_remessa
                ON api_prontocardio_staging.ipm_itens_oracle (cd_remessa)
        """)
        postgres.commit()
        return totais

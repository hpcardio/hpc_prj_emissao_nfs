with processos as (
    select distinct
        numero_processo,
        competencia_producao,
        round(valor_protocolo_cogestao::numeric, 2) as valor_protocolo
    from {{ ref('stg_demonstrativo_processos_ipm') }}
    where status_associacao like 'ASSOCIADO%'
), candidatos as (
    select
        p.numero_processo,
        p.competencia_producao,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa,
        count(*) over (
            partition by p.numero_processo, p.competencia_producao,
                         p.valor_protocolo
        ) as quantidade_candidatos
    from processos p
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.competencia = p.competencia_producao
     and round(r.valor_total::numeric, 2) = p.valor_protocolo
)
select *
from candidatos
where quantidade_candidatos = 1

with processos as (
    select distinct
        numero_processo,
        competencia_producao,
        upper(btrim(numero_protocolo)) as numero_protocolo,
        round(valor_protocolo_cogestao::numeric, 2) as valor_protocolo
    from {{ ref('stg_demonstrativo_processos_ipm') }}
    where status_associacao like 'ASSOCIADO%'
), manuais as (
    select
        upper(btrim(numero_processo)) as numero_processo_normalizado,
        btrim(competencia_producao) as competencia_producao,
        upper(btrim(nr)) as numero_protocolo,
        cd_remessa
    from {{ source('prontocardio', 'associacoes_remessas_ipm_manuais') }}
), candidatos_automaticos as (
    select
        p.numero_processo,
        p.competencia_producao,
        p.numero_protocolo,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa,
        count(*) over (
            partition by p.numero_processo, p.competencia_producao,
                         p.numero_protocolo, p.valor_protocolo
        ) as quantidade_candidatos
    from processos p
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.competencia = p.competencia_producao
     and round(r.valor_total::numeric, 2) = p.valor_protocolo
), automaticos as (
    select candidato.*, 'automatica'::text as origem_associacao
    from candidatos_automaticos candidato
    where candidato.quantidade_candidatos = 1
      and not exists (
          select 1
          from manuais manual
          where manual.numero_processo_normalizado
                = upper(btrim(candidato.numero_processo))
            and manual.competencia_producao
                = candidato.competencia_producao
            and manual.numero_protocolo = candidato.numero_protocolo
      )
), resolvidos_manuais as (
    select distinct
        p.numero_processo,
        p.competencia_producao,
        p.numero_protocolo,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa,
        1::bigint as quantidade_candidatos,
        'manual'::text as origem_associacao
    from manuais manual
    join (
        select distinct numero_processo, competencia_producao,
                        numero_protocolo, valor_protocolo
        from processos
    ) p
      on upper(btrim(p.numero_processo))
         = manual.numero_processo_normalizado
     and p.competencia_producao = manual.competencia_producao
     and p.numero_protocolo = manual.numero_protocolo
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.cd_remessa = manual.cd_remessa
     and r.competencia = manual.competencia_producao
)
select * from automaticos
union all
select * from resolvidos_manuais

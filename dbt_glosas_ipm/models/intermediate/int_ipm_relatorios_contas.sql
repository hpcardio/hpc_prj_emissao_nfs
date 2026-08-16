with candidatos as (
    select 1 as prioridade,
           'remessa_conta_atendimento'::text as criterio,
           r.id_registro,
           r.numero_processo,
           r.numero_processo_normalizado,
           r.status_processo,
           r.cd_remessa,
           coalesce(r.competencia,
                    date_trunc('month', i.dt_competencia)::date) as competencia,
           r.numero_guia_normalizada,
           i.conta
      from {{ ref('stg_processos_relatorios_ipm') }} r
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = r.cd_remessa
       and i.conta = r.conta_informada
       and i.cd_atendimento = r.cd_atendimento_informado
     where r.conta_informada is not null
       and r.cd_atendimento_informado is not null

    union all
    select 2, 'remessa_conta_guia', r.id_registro,
           r.numero_processo, r.numero_processo_normalizado,
           r.status_processo,
           r.cd_remessa,
           coalesce(r.competencia,
                    date_trunc('month', i.dt_competencia)::date) as competencia,
           r.numero_guia_normalizada, i.conta
      from {{ ref('stg_processos_relatorios_ipm') }} r
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = r.cd_remessa
       and i.conta = r.conta_informada
       and i.nr_guia_normalizada = r.numero_guia_normalizada
     where r.conta_informada is not null
       and r.numero_guia_normalizada <> ''

    union all
    select 3, 'remessa_atendimento_guia', r.id_registro,
           r.numero_processo, r.numero_processo_normalizado,
           r.status_processo,
           r.cd_remessa,
           coalesce(r.competencia,
                    date_trunc('month', i.dt_competencia)::date) as competencia,
           r.numero_guia_normalizada, i.conta
      from {{ ref('stg_processos_relatorios_ipm') }} r
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = r.cd_remessa
       and i.cd_atendimento = r.cd_atendimento_informado
       and i.nr_guia_normalizada = r.numero_guia_normalizada
     where r.cd_atendimento_informado is not null
       and r.numero_guia_normalizada <> ''

    union all
    select 4, 'remessa_conta', r.id_registro,
           r.numero_processo, r.numero_processo_normalizado,
           r.status_processo,
           r.cd_remessa,
           coalesce(r.competencia,
                    date_trunc('month', i.dt_competencia)::date) as competencia,
           r.numero_guia_normalizada, i.conta
      from {{ ref('stg_processos_relatorios_ipm') }} r
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = r.cd_remessa
       and i.conta = r.conta_informada
     where r.conta_informada is not null
), resumo as (
    select id_registro, prioridade, criterio,
           count(distinct conta) as quantidade_contas
      from candidatos
     group by id_registro, prioridade, criterio
), regras_seguras as (
    select *,
           row_number() over (
               partition by id_registro order by prioridade
           ) as ordem_segura
      from resumo
     where quantidade_contas = 1
), regra_escolhida as (
    select * from regras_seguras where ordem_segura = 1
)
select distinct
       c.id_registro,
       c.numero_processo,
       c.numero_processo_normalizado,
       c.status_processo,
       c.cd_remessa,
       c.competencia,
       c.numero_guia_normalizada,
       c.conta,
       c.criterio
  from candidatos c
  join regra_escolhida e
    on e.id_registro = c.id_registro
   and e.prioridade = c.prioridade

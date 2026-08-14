with demonstrativos as (
    select
        d.*,
        r.cd_remessa as cd_remessa_esperada,
        to_char(d.data_realizacao, 'YYYY-MM') as mes_realizacao,
        upper(btrim(coalesce(d.numero_guia_senha, ''))) as guia_normalizada,
        upper(btrim(coalesce(d.codigo_servico, ''))) as servico_normalizado,
        ltrim(
            regexp_replace(coalesce(d.codigo_beneficiario, ''), '[^0-9]', '', 'g'),
            '0'
        ) as carteira_normalizada,
        round(coalesce(d.valor_processado, 0)::numeric, 2) as valor_normalizado
    from {{ ref('stg_demonstrativo_processos_ipm') }} d
    join {{ ref('int_ipm_processos_remessas') }} r
      on r.numero_processo = d.numero_processo
     and r.competencia_producao = d.competencia_producao
     and r.valor_protocolo
         = round(d.valor_protocolo_cogestao::numeric, 2)
     and r.numero_protocolo = upper(btrim(d.numero_protocolo))
    where d.status_associacao like 'ASSOCIADO%'
), candidatos as (
    select 1 as prioridade, 'competencia_guia_servico_carteira'::text as criterio,
           d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select 2, 'competencia_servico_carteira', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select 3, 'competencia_tuss_carteira', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_tuss_normalizado <> ''
       and i.cd_tuss_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado

    union all
    select 4, 'lancamento_coalesce_servico_carteira', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and coalesce(nullif(i.cd_pro_fat_normalizado, ''), i.cd_tuss_normalizado)
           = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select 5, 'competencia_coalesce_servico_valor', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and coalesce(nullif(i.cd_tuss_normalizado, ''), i.cd_pro_fat_normalizado)
           = d.servico_normalizado
       and i.valor_item = d.valor_normalizado

    union all
    select 6, 'atendimento_guia_coalesce_servico_valor', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_atendimento, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and coalesce(nullif(i.cd_tuss_normalizado, ''), i.cd_pro_fat_normalizado)
           = d.servico_normalizado
       and i.valor_item = d.valor_normalizado

    union all
    select 7, 'lancamento_pro_fat_carteira_valor', d.id_registro, i.*
      from demonstrativos d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado
)
select * from candidatos

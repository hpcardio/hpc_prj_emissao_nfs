with associados_remessa as (
    select
        d.*,
        r.cd_remessa
    from {{ ref('stg_demonstrativo_processos_ipm') }} d
    left join {{ ref('int_ipm_processos_remessas') }} r
      on r.numero_processo = d.numero_processo
     and r.competencia_producao = d.competencia_producao
     and r.valor_protocolo
         = round(d.valor_protocolo_cogestao::numeric, 2)
     and r.numero_protocolo = upper(btrim(d.numero_protocolo))
), primeira_regra_insegura as (
    select id_registro, criterio,
           row_number() over (partition by id_registro order by prioridade) as ordem
    from (
        select id_registro, prioridade, criterio
        from {{ ref('int_ipm_candidatos_sete_regras') }}
        group by id_registro, prioridade, criterio
        having count(
            distinct (numero_processo_resolvido, cd_remessa, conta)
        ) > 1
    ) regras
), pendentes as (
    select
        d.*,
        i.criterio,
        case
            when d.status_associacao = 'SEM_PROCESSO' then 'sem_processo'
            when d.status_associacao = 'AMBIGUO' then 'processo_ambiguo'
            when d.cd_remessa is null then 'remessa_nao_encontrada_ou_ambigua'
            when i.criterio is not null then 'ambiguo'
            else 'nao_encontrado'
        end as motivo
    from associados_remessa d
    left join {{ ref('int_ipm_glosas_resolvidas') }} ok using (id_registro)
    left join primeira_regra_insegura i
      on i.id_registro = d.id_registro and i.ordem = 1
    where ok.id_registro is null
)
select
    id_registro,
    numero_processo,
    cd_remessa,
    motivo,
    valor_glosa,
    criterio as criterio_correspondencia,
    '[]'::jsonb as remessas_candidatas,
    numero_protocolo,
    data_realizacao,
    numero_guia_senha,
    codigo_servico,
    codigo_beneficiario,
    codigo_glosa,
    valor_processado,
    timezone('America/Sao_Paulo', now()) as data_primeira_ocorrencia,
    timezone('America/Sao_Paulo', now()) as data_ultima_tentativa
from pendentes

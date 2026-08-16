with fonte as (
    select *,
           nullif(
               regexp_replace(
                   coalesce(numero_conta, ''), '[^0-9]', '', 'g'
               ),
               ''
           ) as numero_conta_numerica
      from {{ source('prontocardio', 'processos_relatorios_ipm') }}
), competencias_contadas as (
    select
        upper(btrim(numero_processo)) as numero_processo_normalizado,
        date_trunc('month', competencia)::date as competencia,
        count(*) as quantidade
    from fonte
    where competencia is not null
    group by 1, 2
), competencias_ordenadas as (
    select *,
           row_number() over (
               partition by numero_processo_normalizado
               order by quantidade desc, competencia desc
           ) as ordem
    from competencias_contadas
), competencias_processos as (
    select numero_processo_normalizado, competencia
    from competencias_ordenadas
    where ordem = 1
)
select
    id_registro::text as id_registro,
    btrim(numero_processo) as numero_processo,
    upper(btrim(numero_processo)) as numero_processo_normalizado,
    upper(btrim(status_processo)) as status_processo,
    documento_id::text as documento_id,
    documento_nome,
    pagina_pdf::bigint as pagina_pdf,
    cd_remessa::bigint as cd_remessa,
    nome_paciente,
    upper(btrim(coalesce(numero_guia, ''))) as numero_guia_normalizada,
    case
        -- Contas reais são curtas. Sequências maiores são texto de PDF
        -- concatenado e não podem ser convertidas com segurança para bigint.
        when numero_conta_numerica ~ '^[0-9]{1,18}$'
        then numero_conta_numerica::bigint
    end as conta_informada,
    cd_atendimento::bigint as cd_atendimento_informado,
    coalesce(
        competencia_processo.competencia,
        date_trunc('month', fonte.competencia)::date
    ) as competencia,
    round(coalesce(valor, 0)::numeric, 2) as valor,
    extraido_em
from fonte
left join competencias_processos competencia_processo
  on competencia_processo.numero_processo_normalizado
     = upper(btrim(fonte.numero_processo))
where upper(btrim(status_processo)) in ('FINALIZADO', 'TRAMITANDO')
  and split_part(numero_processo, '/', 2) ~ '^[0-9]{4}$'
  and split_part(numero_processo, '/', 2)::integer >= 2024

with contas_relatorio as (
    select
        conta.*,
        rel.documento_id,
        rel.documento_nome,
        rel.pagina_pdf,
        rel.nome_paciente as nome_paciente_relatorio,
        rel.valor as valor_conta_relatorio,
        rel.extraido_em,
        row_number() over (
            partition by conta.numero_processo_normalizado,
                         conta.cd_remessa, conta.conta
            order by rel.extraido_em desc, conta.id_registro desc
        ) as ordem_conta
    from {{ ref('int_ipm_relatorios_contas') }} conta
    join {{ ref('stg_processos_relatorios_ipm') }} rel
      using (id_registro)
), contas_unicas as (
    select * from contas_relatorio where ordem_conta = 1
), itens_hpc as (
    -- A view Oracle pode repetir exatamente o mesmo lançamento por causa
    -- dos relacionamentos de cadastro. Preservamos variações reais (TUSS,
    -- serviço etc.) e removemos somente linhas integralmente idênticas.
    select distinct * from {{ ref('stg_hpc_itens_ipm') }}
)
select
    md5(
        concat_ws(
            '|',
            conta.id_registro,
            md5(to_jsonb(item)::text)
        )
    ) as id_item_relatorio,
    conta.id_registro as id_registro_relatorio,
    conta.numero_processo,
    conta.numero_processo_normalizado,
    conta.status_processo,
    conta.documento_id,
    conta.documento_nome,
    conta.pagina_pdf,
    conta.competencia,
    conta.nome_paciente_relatorio,
    conta.valor_conta_relatorio,
    conta.criterio as criterio_conta,
    item.*
from contas_unicas conta
join itens_hpc item
  on item.cd_remessa = conta.cd_remessa
 and item.conta = conta.conta

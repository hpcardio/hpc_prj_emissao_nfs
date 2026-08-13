with resumo_regra as (
    select
        id_registro,
        prioridade,
        criterio,
        count(distinct conta) as quantidade_contas,
        count(distinct (conta, cd_lancamento)) as quantidade_itens
    from {{ ref('int_ipm_candidatos_sete_regras') }}
    group by id_registro, prioridade, criterio
), regras_seguras as (
    select
        *,
        row_number() over (partition by id_registro order by prioridade) as ordem_segura
    from resumo_regra
    where quantidade_contas = 1
), regra_escolhida as (
    select * from regras_seguras where ordem_segura = 1
), candidatos_escolhidos as (
    select c.*,
           e.quantidade_itens,
           row_number() over (
               partition by c.id_registro
               order by c.conta, c.cd_lancamento
           ) as ordem_item
    from {{ ref('int_ipm_candidatos_sete_regras') }} c
    join regra_escolhida e
      on e.id_registro = c.id_registro
     and e.prioridade = c.prioridade
)
select
    id_registro,
    criterio,
    case when quantidade_itens = 1 then 'item_unico' else 'conta_unica' end
        as status_correspondencia,
    cd_remessa,
    conta,
    case when quantidade_itens = 1 then cd_lancamento end as cd_lancamento,
    cd_atendimento,
    cd_paciente,
    nm_paciente,
    cd_prestador,
    nm_prestador,
    cd_convenio,
    cnpj_convenio,
    nm_convenio,
    tp_atendimento,
    nr_guia_normalizada as nr_guia,
    cd_pro_fat_normalizado as cd_pro_fat,
    cd_tuss_normalizado as cd_tuss,
    descricao,
    dt_atendimento,
    dt_alta,
    dt_competencia,
    dt_lancamento,
    qt_lancamento,
    valor_item,
    cd_gru_fat,
    ds_gru_fat,
    cd_gru_pro,
    ds_gru_pro
from candidatos_escolhidos
where ordem_item = 1

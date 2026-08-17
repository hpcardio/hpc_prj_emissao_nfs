select
    md5(
        concat_ws(
            '|',
            item.id_item_relatorio,
            coalesce(glosa.id_registro, '')
        )
    ) as id_registro,
    item.id_item_relatorio,
    item.id_registro_relatorio,
    glosa.id_registro as demonstrativo_id_registro,
    item.numero_processo,
    item.status_processo,
    item.documento_id,
    item.documento_nome,
    item.pagina_pdf,
    item.competencia,
    item.nome_paciente_relatorio,
    item.valor_conta_relatorio,
    item.criterio_conta,
    item.cd_remessa,
    item.conta,
    item.cd_lancamento,
    item.cd_atendimento,
    item.cd_paciente,
    item.nm_paciente,
    item.cd_prestador,
    item.nm_prestador,
    item.cd_convenio,
    item.cnpj_convenio,
    item.nm_convenio,
    item.tp_atendimento,
    item.nr_guia_normalizada as nr_guia,
    coalesce(
        nullif(glosa.codigo_servico, ''),
        item.cd_pro_fat_normalizado
    ) as cd_pro_fat,
    item.cd_tuss_normalizado as cd_tuss,
    coalesce(
        nullif(glosa.descricao_servico, ''),
        item.descricao
    ) as descricao,
    item.dt_atendimento,
    item.dt_alta,
    item.dt_competencia,
    item.dt_lancamento,
    item.qt_lancamento,
    item.valor_item,
    item.cd_gru_fat,
    item.ds_gru_fat,
    item.cd_gru_pro,
    item.ds_gru_pro,
    glosa.numero_protocolo,
    glosa.codigo_servico,
    glosa.codigo_glosa,
    glosa.codigo_beneficiario,
    glosa.referencia,
    glosa.valor_protocolo,
    glosa.valor_glosa_protocolo,
    glosa.valor_processado,
    glosa.valor_liberado,
    glosa.valor_glosa,
    glosa.data_realizacao,
    glosa.criterio_correspondencia as criterio_demonstrativo
from {{ ref('int_ipm_relatorios_itens') }} item
left join {{ ref('glosas_ipm_vinculadas') }} glosa
  on upper(btrim(glosa.numero_processo))
     = item.numero_processo_normalizado
 and glosa.cd_remessa = item.cd_remessa
 and glosa.conta = item.conta
 and glosa.cd_lancamento = item.cd_lancamento

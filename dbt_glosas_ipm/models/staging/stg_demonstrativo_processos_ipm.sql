with legado as (
    select
        id_registro::text as id_registro, referencia, numero_lote,
        numero_protocolo, valor_protocolo, valor_glosa_protocolo,
        numero_guia_senha, data_realizacao, descricao_servico,
        codigo_tabela, codigo_servico, quantidade_executada,
        valor_processado, valor_liberado, valor_glosa, codigo_glosa,
        nome_beneficiario, codigo_beneficiario, cogestao_id_registro,
        numero_processo, competencia_producao, valor_protocolo_cogestao,
        status_associacao, candidatos_associacao,
        null::text as origem_maida
    from {{ source('prontocardio', 'demonstrativo_processos_ipm') }}
    where coalesce(valor_glosa, 0) > 0
), maida as (
    select
        id_registro::text as id_registro, referencia, numero_lote,
        numero_protocolo, valor_protocolo, valor_glosa_protocolo,
        numero_guia_senha, data_realizacao, descricao_servico,
        codigo_tabela, codigo_servico, quantidade_executada,
        valor_processado, valor_liberado, valor_glosa, codigo_glosa,
        nome_beneficiario, codigo_beneficiario,
        null::text as cogestao_id_registro,
        coalesce(nullif(btrim(numero_lote), ''), 'MAIDA') as numero_processo,
        to_char(referencia, 'MM/YYYY') as competencia_producao,
        null::numeric as valor_protocolo_cogestao,
        'maida'::text as status_associacao,
        null::jsonb as candidatos_associacao,
        origem_maida
    from {{ source('prontocardio', 'demonstrativo_conta_ipm') }}
    where origem_maida is not null
      and coalesce(valor_glosa, 0) > 0
)
select * from legado
union all
select * from maida

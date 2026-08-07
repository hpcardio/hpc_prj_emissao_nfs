# NFS-e Fortaleza com Astro, Airflow e dlt

Automação para emitir NFS-e, consultar notas emitidas, baixar PDF/XML e
carregar dados fiscais no PostgreSQL.

O projeto não usa automação visual de navegador. A comunicação com o portal
ISS Fortaleza é feita pelos endpoints HTTP JSF/Seam, preservando cookies,
conversação e `javax.faces.ViewState`.

## Sumário

- [Visão geral](#visão-geral)
- [Configuração compartilhada](#configuração-compartilhada)
- [Emissão de NFS-e — `emissao_nfse`](#emissão-de-nfs-e--emissao_nfse)
- [Extração de NFS-e — `extracao_nfse`](#extração-de-nfs-e--extracao_nfse)
- [Testes](#testes)

## Visão geral

Emissão e extração são funcionalidades independentes:

| DAG | Responsabilidade | Disparo |
| --- | --- | --- |
| `emissao_nfse` | Emitir solicitações aprovadas, baixar o PDF e registrar o resultado | Manual ou API REST, sem agendamento |
| `extracao_nfse` | Consultar notas existentes, baixar XML e carregar `nfse_xml` | Diariamente às 03:00 ou manualmente |

```mermaid
flowchart LR
    API[api_prontocardio] -->|lote_id + IDs| AIRFLOW[Airflow/Astro]
    AIRFLOW --> EMISSAO[DAG emissao_nfse]
    AIRFLOW --> EXTRACAO[DAG extracao_nfse]
    EMISSAO -->|Lê e atualiza solicitações| PG[(PostgreSQL)]
    EMISSAO -->|Emite e baixa PDF| ISS[ISS Fortaleza]
    EXTRACAO -->|Consulta e baixa XML| ISS
    EXTRACAO -->|dlt merge| NFSE[(nfse_xml)]
```

As DAGs compartilham somente a infraestrutura, as credenciais do portal e a
conexão PostgreSQL. Payloads, regras, horários, resultados e documentação
operacional ficam separados nas seções de cada funcionalidade.

## Configuração compartilhada

### Pré-requisitos

- Python 3.12 ou superior;
- Docker Engine;
- Astro CLI;
- PostgreSQL acessível pelos containers;

### Estrutura do projeto

```text
.
├── .astro/
├── dags/
│   ├── emissao_nfse.py
│   └── extracao_nfse.py
├── include/
├── plugins/
├── src/
│   └── nfs_fortaleza/
│       ├── batch.py
│       ├── cli.py
│       ├── config.py
│       ├── extraction.py
│       ├── issuance.py
│       ├── load.py
│       ├── nfse_xml.py
│       ├── periods.py
│       ├── portal.py
│       └── spreadsheet.py
├── tests/
├── Dockerfile
├── docker-compose.override.yml
├── pyproject.toml
└── requirements.txt
```

### Variáveis de ambiente

Crie o arquivo local:

```bash
cp .env.example .env
```

Preencha as credenciais e a conexão:

```dotenv
PORTAL_PREFEITURA_FORTALEZA=https://iss.fortaleza.ce.gov.br/grpfor/home.seam
CPF_LOGIN=CPF_USADO_NO_LOGIN
SENHA='SENHA_DO_PORTAL'
# Fallback apenas para disparos legados; a DAG usa o CNPJ de cada emissão.
NFSE_ISSUER_CNPJ=59932105000121

DATABASE_URL=postgresql+psycopg://usuario:senha@host:5432/banco
POSTGRES_SCHEMA=api_prontocardio
NFSE_POSTGRES_CONN_ID=postgres_prontocardio
AIRFLOW_CONN_POSTGRES_PRONTOCARDIO=postgresql://usuario:senha@host:5432/banco
```

Nos disparos feitos pela API, `dag_run.conf.cnpj_por_solicitacao`
transporta o CNPJ emissor de cada item. A DAG confere esse valor com o
snapshot gravado em `emissao_nfse.cnpj_emissor` e seleciona a inscrição
correspondente no portal, inclusive quando um lote contém CNPJs diferentes.

Se uma senha contiver `$`, mantenha o valor entre aspas simples para impedir
a interpolação pelo Docker Compose:

```dotenv
SENHA='senha-com-$-literal'
```

Variáveis opcionais para priorizar uma inscrição:

| Variável | Uso |
| --- | --- |
| `INSCRICAO_CNPJ` | Selecionar pelo CNPJ |
| `INSCRICAO_MUNICIPAL` | Selecionar pela inscrição municipal |
| `INSCRICAO_NOME` | Selecionar por parte da razão social |

### Executar com Astro

Valide a imagem e as DAGs:

```bash
astro dev parse
```

Inicie o ambiente:

```bash
astro dev start
```

Serviços locais:

| Serviço | Endereço |
| --- | --- |
| Airflow | `http://localhost:8082` |
| PostgreSQL de metadados do Airflow | `localhost:5433` |

Essas portas são definidas em `.astro/config.yaml` para não disputar a porta
`5432` do banco da aplicação nem a porta `8080` de outros serviços.

Para encerrar:

```bash
astro dev stop
```

Use `astro dev kill` somente quando também quiser remover containers e
metadados locais do Airflow.

PDFs, XMLs e diagnósticos persistem no volume Docker nomeado `nfse_data`,
montado em `/usr/local/airflow/data`. O volume preserva a propriedade do
usuário `astro` e evita erros de permissão causados por UIDs diferentes entre
host e container.

### Conexão PostgreSQL e pool

As DAGs usam a conexão `postgres_prontocardio`. No desenvolvimento local,
`airflow_settings.yaml` contém os mesmos dados de `DATABASE_URL`.

O arquivo possui senha em texto simples e, por isso, fica fora do Git e do
contexto de build. Depois de iniciar ou reiniciar o Astro, importe os objetos:

```bash
astro dev object import
```

Também é possível definir a conexão pela variável
`AIRFLOW_CONN_POSTGRES_PRONTOCARDIO`. Variáveis de ambiente têm precedência
sobre conexões importadas no banco de metadados do Airflow.

Em ambiente local, prefira preencher essa variável no `.env`: assim a conexão
já estará disponível quando os containers iniciarem. A DAG de emissão também
possui a etapa `aguardar_postgres`, que tolera o pequeno intervalo entre a
subida do Airflow e a importação de `airflow_settings.yaml`. Somente essa
verificação pode ser repetida; a etapa `processar_lote` permanece sem retry
automático para não correr o risco de emitir a mesma nota novamente após uma
falha ocorrida no portal.

O pool `nfse_portal` possui um slot e serializa o acesso ao portal. Em um
deployment Astro, armazene conexão e credenciais no painel ou em um secret
backend.

## Emissão de NFS-e — `emissao_nfse`

### Objetivo e arquivos

A DAG recebe um lote criado por `api_prontocardio`, consulta novamente o banco
e emite somente os itens que continuam aprovados e pendentes.

| Arquivo | Responsabilidade |
| --- | --- |
| `dags/emissao_nfse.py` | Definição da DAG e leitura do `dag_run.conf` |
| `src/nfs_fortaleza/batch.py` | Elegibilidade, reserva dos itens e atualização dos estados |
| `src/nfs_fortaleza/issuance.py` | Emissão no portal por requisições HTTP |
| `src/nfs_fortaleza/spreadsheet.py` | Validação dos dados e nome do PDF |

### Fluxo para o usuário

<div align="center">
  <p>
    O usuário seleciona as solicitações aprovadas e acompanha o resultado pela
    aplicação, sem acessar manualmente o portal da Prefeitura.
  </p>
  <table width="82%">
    <tr>
      <td align="center" bgcolor="#E8F1FF">
        <br>
        <strong>📋 Solicitações aprovadas</strong><br><br>
        A aplicação apresenta os itens disponíveis para emissão.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#F1ECFF">
        <br>
        <strong>☑️ Seleção das notas</strong><br><br>
        O usuário marca uma ou mais solicitações.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#EAF7FF">
        <br>
        <strong>🚀 Solicitação da emissão</strong><br><br>
        A aplicação envia os itens selecionados para processamento.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#FFF5D6">
        <br>
        <strong>⏳ Emissão em andamento</strong><br><br>
        Cliente, serviço e valor são conferidos automaticamente.
        <br><br>
      </td>
    </tr>
  </table>

  <table width="92%">
    <tr>
      <td align="center" width="46%"><strong>↙</strong></td>
      <td align="center" width="8%"></td>
      <td align="center" width="46%"><strong>↘</strong></td>
    </tr>
    <tr>
      <td align="center" bgcolor="#E6F7EC">
        <br>
        <strong>✅ Nota emitida</strong><br><br>
        Número e PDF disponíveis para consulta.
        <br><br>
      </td>
      <td align="center"></td>
      <td align="center" bgcolor="#FDECEC">
        <br>
        <strong>⚠️ Emissão não concluída</strong><br><br>
        O motivo fica disponível para correção.
        <br><br>
      </td>
    </tr>
  </table>
</div>

### Regras de negócio

```mermaid
flowchart TD
    INICIO["Usuário seleciona<br/>as solicitações aprovadas"] --> GRUPO["Sistema reúne as notas<br/>que serão emitidas"]
    GRUPO --> AUTOMACAO["A emissão automática é iniciada"]

    subgraph SELECAO["1. Conferência das solicitações"]
        AUTOMACAO --> BUSCA["Busca somente as notas selecionadas"]
        BUSCA --> PRONTA{"A nota está pronta<br/>para emissão?"}
        REGRAS["A solicitação foi aprovada<br/>Foi marcada para emissão<br/>Ainda não foi emitida"] -.-> PRONTA
        PRONTA -- "Não" --> FORA["Não envia a nota<br/>e informa a pendência"]
        PRONTA -- "Sim" --> ANDAMENTO["Marca a nota como em andamento<br/>para evitar emissão duplicada"]
        ANDAMENTO --> DADOS{"Os dados estão completos?<br/>CPF, paciente, serviço,<br/>local, atendimento e valor"}
        DADOS -- "Não" --> ERRO
    end

    subgraph PREFEITURA["2. Emissão no site da Prefeitura"]
        DADOS -- "Sim" --> ACESSO["Acessa o site da Prefeitura"]
        ACESSO --> EMPRESA["Seleciona a empresa emissora correta"]
        EMPRESA --> EMITIR["Abre a opção Emitir NFS-e"]
        EMITIR --> CLIENTE["Pesquisa o cliente pelo CPF"]
        CLIENTE --> TOMADOR{"O cliente já está<br/>cadastrado?"}
        TOMADOR -- "Não" --> CADASTRO["Cadastra nome, CPF,<br/>endereço, cidade e e-mail"]
        TOMADOR -- "Sim" --> SERVICO
        CADASTRO --> SERVICO["Preenche o serviço realizado<br/>com o tipo de exame"]
        SERVICO --> VALOR["Informa o valor aprovado"]
        VALOR --> VALIDAR["Confere os dados antes de emitir"]
        VALIDAR --> CAMPOS{"A Prefeitura<br/>aceitou os dados?"}
        CAMPOS -- "Não" --> ERRO["Interrompe a emissão<br/>e guarda o motivo do erro"]
        CAMPOS -- "Sim" --> CONFIRMAR["Confirma a emissão"]
        CONFIRMAR --> EMITIDA{"A nota foi<br/>emitida com sucesso?"}
        EMITIDA -- "Não" --> ERRO
        EMITIDA -- "Sim" --> PDF["Obtém o número e baixa o PDF"]
    end

    subgraph RESULTADO["3. Resultado na aplicação"]
        PDF --> SUCESSO["Nota concluída<br/>Número e PDF disponíveis"]
        ERRO --> FALHA["Nota requer atenção<br/>Motivo disponível para correção"]
        SUCESSO --> LOTE{"Todas as notas selecionadas<br/>foram emitidas?"}
        FALHA --> LOTE
        FORA --> LOTE
        LOTE -- "Sim" --> LOTE_OK["Processo concluído"]
        LOTE -- "Não" --> LOTE_ERRO["Processo requer atenção"]
    end

    classDef origem fill:#172033,stroke:#d9e2f2,color:#ffffff,stroke-width:1.5px;
    classDef processo fill:#1d2738,stroke:#cbd5e1,color:#ffffff,stroke-width:1.5px;
    classDef decisao fill:#4a3418,stroke:#f4c56a,color:#ffffff,stroke-width:1.5px;
    classDef sucesso fill:#123b2a,stroke:#70d6a0,color:#ffffff,stroke-width:1.5px;
    classDef falha fill:#471f25,stroke:#f08a96,color:#ffffff,stroke-width:1.5px;
    classDef regra fill:#2a2d33,stroke:#9ca3af,color:#ffffff,stroke-dasharray: 4 3;

    class INICIO,GRUPO,AUTOMACAO origem;
    class BUSCA,ANDAMENTO,ACESSO,EMPRESA,EMITIR,CLIENTE,CADASTRO,SERVICO,VALOR,VALIDAR,CONFIRMAR,PDF processo;
    class PRONTA,DADOS,TOMADOR,CAMPOS,EMITIDA,LOTE decisao;
    class SUCESSO,LOTE_OK sucesso;
    class ERRO,FALHA,FORA,LOTE_ERRO falha;
    class REGRAS regra;
```

Os IDs recebidos definem somente o escopo. Antes de acessar a Prefeitura, a
DAG exige simultaneamente:

- item pertencente ao `lote_id` recebido;
- solicitação presente em `solicitacao_ids`;
- emissão com estado `PENDENTE`;
- validação `VALIDADA`;
- workflow `EMISSAO_SOLICITADA`;
- CPF válido, paciente, procedimento, local e tipo de atendimento;
- `valor_nota` maior que zero.

O item é alterado de `PENDENTE` para `PROCESSANDO` de forma atômica. Assim,
duas execuções não conseguem emitir a mesma nota.

### Emissão no ISS Fortaleza

Para cada item elegível, a automação:

1. autentica no portal e seleciona o CNPJ `59.932.105/0001-21`;
2. abre `NFS-e > Emitir NFS-e`;
3. pesquisa o tomador pelo CPF;
4. cadastra Pessoa Física quando o cliente ainda não existe;
5. preenche serviço, descrição e valor aprovado;
6. solicita a validação dos campos;
7. confirma somente quando o portal habilita a ação final;
8. recupera o número da nota;
9. baixa um PDF válido;
10. atualiza emissão, workflow, evento e lote.

O cadastro do tomador utiliza paciente, CPF, rua, número, bairro, cidade e
e-mail. A descrição do serviço recebe o tipo de exame.

Os parâmetros fiscais usados pelo fluxo são:

| Campo | Valor |
| --- | --- |
| CNAE | `861010101` |
| NBS | `123011900` |
| Indicador da operação | `030104` |
| CST | `200` |
| Classificação tributária | `200029` |
| Alíquota | `3,00` |

O PDF segue o padrão:

```text
NÚMERO DA NFS-E - LOCAL, TIPO ATENDIMENTO e PACIENTE.pdf
```

### Estados e auditoria

| Momento | Workflow | Emissão | Lote |
| --- | --- | --- | --- |
| Após validação | `VALIDADA` | — | — |
| Selecionada para emissão | `EMISSAO_SOLICITADA` | `PENDENTE` | `PENDENTE` |
| Durante a execução | `EMISSAO_SOLICITADA` | `PROCESSANDO` | `PROCESSANDO` |
| Concluída | `EMITIDA` | `EMITIDA` | `EMITIDA` se todos concluírem |
| Falha | `ERRO_EMISSAO` | `ERRO` | `ERRO` |

Número, protocolo, `dag_run_id`, horários e eventual erro ficam registrados.
Os eventos são gravados como `NFSE_EMITIDA` ou `ERRO_EMISSAO`.

### Integração com `api_prontocardio`

O projeto `/home/rafaelamorim/repo/api_prontocardio` fornece:

- endpoint `POST /app_glosas/requisicoes/emissoes-nfse`;
- cliente REST em `app_prontocardio/services/airflow_nfse.py`;
- tabelas operacionais e migrations `20260723_028` e `20260723_029`.

Configure a API:

```dotenv
AIRFLOW_NFSE_BASE_URL=http://host.docker.internal:8082
AIRFLOW_NFSE_DAG_ID=emissao_nfse
AIRFLOW_NFSE_DAG_RUNS_PATH=/api/v1/dags/{dag_id}/dagRuns
AIRFLOW_NFSE_TOKEN=
AIRFLOW_NFSE_USERNAME=admin
AIRFLOW_NFSE_PASSWORD=admin
AIRFLOW_NFSE_TIMEOUT_SECONDS=15
AIRFLOW_NFSE_VERIFY_SSL=false
```

Em produção, use HTTPS e um usuário de serviço com permissão mínima. Quando a
API estiver em Docker, `localhost:8082` apontará para o próprio container;
use `host.docker.internal`, um DNS compartilhado ou o endereço publicado do
Airflow.

Emissão individual:

```bash
curl -X POST "http://localhost:8000/app_glosas/requisicoes/emissoes-nfse" \
  -H "Authorization: Bearer TOKEN_DA_API" \
  -H "Content-Type: application/json" \
  -d '{"solicitacao_ids":[101]}'
```

Emissão em lote:

```bash
curl -X POST "http://localhost:8000/app_glosas/requisicoes/emissoes-nfse" \
  -H "Authorization: Bearer TOKEN_DA_API" \
  -H "Content-Type: application/json" \
  -d '{"solicitacao_ids":[101,102,103]}'
```

A API envia ao Airflow:

```json
{
  "dag_run_id": "api_prontocardio_nfse_lote_42",
  "conf": {
    "origem": "API_PRONTOCARDIO",
    "lote_id": 42,
    "solicitacao_ids": [101, 102, 103]
  }
}
```

#### Contrato PostgreSQL

| Tabela | Responsabilidade |
| --- | --- |
| `solicitacao_nota` | Dados fiscais e cadastrais aprovados |
| `solicitacao_nota_workflow` | Aprovação e estado funcional |
| `lote_emissao_nfse` | Estado, disparo, `dag_run_id` e erro do lote |
| `emissao_nfse` | Estado e resultado de cada nota |
| `solicitacao_nota_evento` | Auditoria do processamento |

A seleção da DAG equivale a:

```sql
SELECT e.id
FROM api_prontocardio.emissao_nfse e
JOIN api_prontocardio.solicitacao_nota s
  ON s.id = e.solicitacao_nota_id
JOIN api_prontocardio.solicitacao_nota_workflow w
  ON w.solicitacao_nota_id = s.id
WHERE e.lote_id = :lote_id
  AND e.solicitacao_nota_id = ANY(:solicitacao_ids)
  AND e.status = 'PENDENTE'
  AND w.validacao = 'VALIDADA'
  AND w.status = 'EMISSAO_SOLICITADA';
```

O valor aprovado vem de `solicitacao_nota.valor_nota`. Na ausência de cidade
ou UF, são aplicados `FORTALEZA` e `CE`.

#### Disparo direto para diagnóstico

```bash
curl -u "admin:admin" \
  -X POST "http://localhost:8082/api/v1/dags/emissao_nfse/dagRuns" \
  -H "Content-Type: application/json" \
  -d '{
    "dag_run_id":"teste_nfse_lote_42",
    "conf":{"lote_id":42,"solicitacao_ids":[101,102]}
  }'
```

Esse disparo não cria lote nem aprova solicitações. Se os registros não
estiverem nos estados exigidos, nenhuma nota será emitida.

### Emissão manual por planilha

O comando manual permanece disponível para operação assistida e diagnóstico.
A integração oficial em lote deve usar `emissao_nfse` e o PostgreSQL.

#### Instalação local

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

#### Colunas da planilha

Por padrão, o comando lê `NOTAS FISCAIS.xlsx`:

| Coluna | Uso |
| --- | --- |
| `PACIENTE` | Nome do tomador e nome do PDF |
| `CPF` | Pesquisa ou cadastro do tomador |
| `RUA` | Logradouro |
| `NUMERO CASA` | Número do endereço |
| `BAIRRO` | Bairro |
| `CIDADE` | Cidade |
| `UF` | Unidade federativa |
| `EMAIL` | E-mail |
| `TIPO DE EXAME` | Descrição do serviço |
| `VALOR` | Valor do serviço |
| `LOCAL` | Nome do PDF |
| `TIPO ATENDIMENTO` | Nome do PDF |

`ATENDIMENTO` e `DATA` são opcionais. Cabeçalhos são normalizados quanto a
maiúsculas, minúsculas e acentuação. A primeira linha de dados é a linha `2`.

#### Conferir sem emitir

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --linha 2
```

Sem `--confirmar-emissao`, o comando valida e exibe a linha, mas não autentica
nem altera o portal.

Prévia de até dez linhas:

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --todas \
  --limite 10
```

#### Emitir uma nota

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --linha 2 \
  --cnpj 59932105000121 \
  --confirmar-emissao
```

O CNPJ pode ser omitido quando for a inscrição padrão.

#### Emitir várias linhas

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --todas \
  --limite 10 \
  --confirmar-emissao
```

É obrigatório informar `--linha` ou `--todas`. No modo em lote, uma falha
interrompe as próximas linhas; emissões anteriores permanecem registradas.

#### Proteção contra duplicidade

Após obter um PDF válido, o comando grava o sucesso em
`downloads/emissoes_nfse.jsonl`. Uma linha com o mesmo conteúdo será ignorada
nas execuções seguintes.

Erros de navegação ou respostas inesperadas geram HTML em `.artifacts/`.

| Parâmetro | Descrição |
| --- | --- |
| `PLANILHA` | Caminho do XLSX |
| `--linha N` | Processar uma linha |
| `--todas` | Processar todas as linhas pendentes |
| `--limite N` | Limitar o lote |
| `--cnpj CNPJ` | Selecionar a inscrição emissora |
| `--downloads-dir DIR` | Alterar o diretório de PDFs |
| `--confirmar-emissao` | Autorizar a emissão fiscal real |

## Extração de NFS-e — `extracao_nfse`

### Objetivo e arquivos

A DAG consulta NFS-e já existentes, exporta os XMLs e carrega os campos
normalizados em `POSTGRES_SCHEMA.nfse_xml`.

| Arquivo | Responsabilidade |
| --- | --- |
| `dags/extracao_nfse.py` | Agenda e leitura dos filtros do `dag_run.conf` |
| `src/nfs_fortaleza/extraction.py` | Resolução dos filtros e execução |
| `src/nfs_fortaleza/portal.py` | Consulta, paginação e exportação dos XMLs |
| `src/nfs_fortaleza/nfse_xml.py` | Parse e normalização do XML |
| `src/nfs_fortaleza/load.py` | Pipeline `dlt` e carga PostgreSQL |
| `src/nfs_fortaleza/periods.py` | Competências, datas e janelas permitidas |

### Agenda e filtros da DAG

Por padrão, `extracao_nfse` executa diariamente às 03:00. A agenda pode ser
alterada por `NFSE_EXTRACTION_SCHEDULE`.

Uma execução agendada consulta o dia representado pelo início do intervalo de
dados do Airflow. Execuções manuais aceitam somente um dos formatos abaixo.

Competência:

```json
{"competencia": "07/2026"}
```

Intervalo:

```json
{"inicio": "01/07/2026", "fim": "23/07/2026"}
```

Nota específica:

```json
{"cnpj": "59932105000121", "numero_nfse": "8"}
```

Disparo REST:

```bash
curl -u "admin:admin" \
  -X POST "http://localhost:8082/api/v1/dags/extracao_nfse/dagRuns" \
  -H "Content-Type: application/json" \
  -d '{
    "dag_run_id":"extracao_manual_202607",
    "conf":{"competencia":"07/2026"}
  }'
```

### Regras de consulta

Para cada competência ou janela, a automação:

1. abre `NFS-e > Consultar NFS-e`;
2. seleciona `Serviços Prestados`;
3. usa o filtro `Período Emissão/Tomador`;
4. preenche as datas permitidas;
5. consulta as notas;
6. exporta o XML de cada linha;
7. percorre todas as páginas;
8. entrega XML ou ZIP ao pipeline `dlt`;
9. normaliza e carrega os campos no PostgreSQL.

O portal não aceita data final futura. Na competência atual, a data final é
limitada ao dia de hoje.

Consultas sem CPF/CNPJ do tomador são limitadas a 31 dias. Intervalos maiores
são divididos automaticamente por mês e pelo limite do portal.

Se nenhuma nota for encontrada, a execução registra `Ignorado` e continua
para a próxima janela.

```mermaid
flowchart TD
    A[Receber competência, intervalo ou número] --> B[Acessar ISS Fortaleza]
    B --> C[Autenticar]
    C --> D[Abrir Consultar NFS-e]
    D --> E[Selecionar Serviços Prestados]
    E --> F[Dividir o período em janelas permitidas]
    F --> G[Consultar uma janela]
    G --> H{Existem notas?}
    H -- Não --> I[Registrar janela sem notas]
    H -- Sim --> J[Exportar XML de cada linha]
    J --> K{Existe próxima página?}
    K -- Sim --> J
    K -- Não --> L[Processar XML ou ZIP]
    L --> M[Normalizar campos fiscais]
    M --> N[Carregar nfse_xml]
```

### Execução manual em desenvolvimento e produção

Cada execução precisa de um `run-id` exclusivo. O padrão abaixo inclui a
competência e um timestamp para evitar `DagRunAlreadyExists`. Ao repetir uma
competência, o pipeline consulta `nfse_xml` e envia ao `dlt` somente notas cuja
combinação de CNPJ do prestador e número da NFS-e ainda não exista na tabela.

No desenvolvimento local, com o projeto iniciado por `astro dev start`, use o
comando nativo do Astro:

```bash
astro dev run dags trigger extracao_nfse \
  --run-id "extracao_manual_202608_$(date +%Y%m%d_%H%M%S)" \
  --conf '{"competencia":"08/2026"}'
```

Em produção, execute o Airflow dentro do container do scheduler:

```bash
docker exec -it prj-web-nfs_1f87bb-scheduler-1 \
  airflow dags trigger extracao_nfse \
  --run-id "extracao_manual_202608_$(date +%Y%m%d_%H%M%S)" \
  --conf '{"competencia":"08/2026"}'
```

Se o nome do container mudar após um novo deployment, localize-o antes com:

```bash
docker ps --filter name=prj-web-nfs --filter name=scheduler \
  --format '{{.Names}}'
```

Para outra competência, altere tanto `202608` no `run-id` quanto `08/2026` no
`--conf`.

### Uso pela linha de comando

Consultar e carregar uma nota específica:

```bash
.venv/bin/nfs-fortaleza run \
  --cnpj 59932105000121 \
  --numero-nfse 12345
```

`--cnpj` e `--numero-nfse` devem ser informados juntos e não podem ser
combinados com filtros de período.

Consultar e carregar uma competência:

```bash
.venv/bin/nfs-fortaleza run --competencia 06/2026
```

Baixar sem carregar:

```bash
.venv/bin/nfs-fortaleza download --competencia 06/2026
```

Carregar XML ou ZIP já baixado:

```bash
.venv/bin/nfs-fortaleza load-file \
  downloads/nota_5.xml \
  --competencia 06/2026
```

Intervalo de competências:

```bash
.venv/bin/nfs-fortaleza run --inicio 01/2026 --fim 06/2026
```

Intervalo de datas:

```bash
.venv/bin/nfs-fortaleza run \
  --inicio 01/06/2026 \
  --fim 09/07/2026
```

Somente download de um intervalo:

```bash
.venv/bin/nfs-fortaleza download \
  --inicio 01/06/2026 \
  --fim 09/07/2026
```

### Carga e deduplicação

O resultado é entregue a
`dlt.sources.filesystem.filesystem`. Um `@dlt.transformer` interpreta XML/ZIP,
preserva o documento original e gera `row_hash`.

A carga usa `merge` com `row_hash`, impedindo duplicação em `nfse_xml`.

Principais grupos de campos:

- situação, cancelamento e mensagens;
- número, código de verificação, competência e datas;
- RPS e lote de RPS;
- serviço, CNAE, tributação, alíquota e discriminação;
- prestador e tomador;
- endereço e contato;
- construção civil, obra e ART;
- valores, deduções, retenções, ISS e descontos;
- `xml_campos`, com o XML normalizado;
- `xml_documento`, com o documento original;
- `row_hash`, chave de merge.

`aliquota` é normalizada com quatro casas decimais. O conteúdo original
permanece em `aliquota_xml`.

### Observações do portal

O botão geral `Exportar XML das Notas Selecionadas` pode retornar XML vazio
quando a seleção visual não está sincronizada no servidor. A automação usa o
link `Exportar XML` de cada linha e envia o formulário com o
`javax.faces.ViewState` atual.

Falhas de login, navegação, sessão ou mudança do portal geram HTML de
diagnóstico em `.artifacts/`.

## Testes

Execute a suíte sem emitir notas:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Valide também a imagem e a integridade das DAGs:

```bash
astro dev parse
```

Os testes cobrem payloads das DAGs, regras de elegibilidade, proteção contra
reemissão, leitura da planilha, ações JSF, consulta por número e filtros da
extração.

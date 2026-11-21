# Pipeline de cotações de moedas (API → pandas → PostgreSQL)

Projeto de estudo em Engenharia de Dados, nível iniciante: um pipeline em Python que busca a cotação de 10 moedas em reais numa API pública, trata os dados com pandas e os grava no PostgreSQL em duas camadas (bruta e tratada). Pode ser executado várias vezes sem duplicar registros.

> **Sobre o escopo:** é um projeto de aprendizado, executado localmente. Não é um sistema de produção. As limitações conhecidas estão listadas [no fim deste documento](#limitações-conhecidas-e-próximos-passos).

## Problema de negócio

Uma loja online importa produtos dos EUA e da Europa. O time financeiro precisa acompanhar diariamente a cotação de moedas estrangeiras em relação ao real para decidir a precificação. Antes, os valores eram copiados à mão de um site para uma planilha, o que gerava erros e não deixava um histórico confiável.

**Objetivo:** automatizar a coleta das cotações dos últimos 30 dias, tratá-las e disponibilizá-las em uma tabela no PostgreSQL pronta para consulta por analistas.

## Como funciona

```
 API Frankfurter ──► extrair ──► raw_cotacoes (acumula tudo que veio da API)
   (1 chamada          │
    por moeda)         ▼
                    limpar ──► validar ──► cotacoes_moedas (1 linha por data + moeda)
                  (tipos, duplicatas,   (vazio? cotação ≤ 0?      via upsert
                   data_ingestao)        moeda inesperada?)
                                                                      │
                                                                      ▼
                                                              validar carga (SQL)
```

Ordem de execução do `main()`:

1. Cria a conexão com o banco e **testa antes de qualquer coisa** (falha cedo se o banco estiver fora do ar).
2. Extrai as cotações de cada moeda para BRL (últimos 30 dias, de hoje).
3. Grava os dados extraídos em `raw_cotacoes` (acrescenta, nunca sobrescreve).
4. Limpa e tipa os dados com pandas.
5. Valida a qualidade; se falhar, o pipeline para com erro claro.
6. Cria a tabela tratada, se ainda não existir, e carrega com upsert.
7. Confere a carga consultando o banco (contagem, período, linhas por moeda).

## Stack

- Python 3, `requests`, `pandas`
- SQLAlchemy + `psycopg2` (conexão com o banco)
- PostgreSQL
- `python-dotenv` (credenciais fora do código)
- Testado com Python 3.14 e PostgreSQL 18 no WSL (Ubuntu)

## Estrutura do projeto

```
projeto_cotacoes/
├── main.py              # o pipeline inteiro (funções + orquestração)
├── requirements.txt     # dependências
├── .env.example         # modelo das variáveis de ambiente (sem valores reais)
├── .gitignore           # mantém .env e .venv fora do Git
├── README.md
└── sql/
    └── validacao.sql    # consultas para conferir a carga
```

## Como executar

### Pré-requisitos

Python 3 e PostgreSQL instalados. Os comandos abaixo foram escritos para Linux/WSL.

### 1. Criar o banco

```bash
sudo service postgresql start
sudo -u postgres psql
```

Dentro do `psql`, troque a senha por uma sua:

```sql
CREATE USER cotacoes_user WITH PASSWORD 'troque_esta_senha';
CREATE DATABASE cotacoes_db OWNER cotacoes_user;
\q
```

### 2. Ambiente Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Variáveis de ambiente

```bash
cp .env.example .env
```

Edite o `.env` com a senha criada no passo 1. O arquivo `.env` não é versionado.

### 4. Executar

```bash
python3 main.py
```

As tabelas `raw_cotacoes` e `cotacoes_moedas` são criadas automaticamente na primeira execução.

### 5. Conferir o resultado

As consultas de checagem estão em [`sql/validacao.sql`](sql/validacao.sql):

```bash
psql -U cotacoes_user -h localhost -d cotacoes_db -f sql/validacao.sql
```

## Modelo de dados

**`cotacoes_moedas`** (camada tratada)

| Coluna | Tipo | Descrição |
|---|---|---|
| `data` | `DATE` | Data da cotação |
| `moeda` | `VARCHAR(3)` | Código da moeda de origem (ex.: `USD`) |
| `cotacao_brl` | `FLOAT` | Quanto vale 1 unidade da moeda, em reais |
| `data_ingestao` | `TIMESTAMP` | Quando este valor foi gravado ou alterado pela última vez |

Chave primária: `(data, moeda)`.

**`raw_cotacoes`** (camada bruta)

Criada automaticamente pelo pandas (`to_sql`), com os tipos inferidos. Tem as mesmas colunas, e `data_ingestao` indica **em qual execução** a linha entrou. Não tem chave primária: o objetivo é acumular o histórico de tudo que a API devolveu.

## Decisões de projeto

**Duas camadas com papéis diferentes.** A `raw_cotacoes` acumula a cada execução; a `cotacoes_moedas` mantém uma linha por data e moeda, sempre com o valor mais recente. A raw é gravada *antes* da limpeza e da validação: se a validação interromper o pipeline, o que a API devolveu naquela execução continua registrado.

**Idempotência por upsert.** Rodar o pipeline várias vezes não duplica linhas na tabela tratada. Isso é garantido por três peças:

1. a chave primária `(data, moeda)` impede duas linhas para o mesmo dia e moeda;
2. os dados são carregados primeiro numa tabela temporária (`stg_cotacoes`), que é apagada no fim;
3. um `INSERT ... ON CONFLICT (data, moeda) DO UPDATE` insere as datas novas e, para as que já existem, atualiza **apenas se a cotação mudou** (`WHERE ... IS DISTINCT FROM ...`).

**Significado de `data_ingestao`.** Na tabela tratada, linhas cuja cotação não mudou mantêm o timestamp antigo; só é atualizado quando o valor muda. Assim a coluna responde "desde quando este valor está aqui". O trade-off: ao ser revisado, o valor antigo é sobrescrito na tabela tratada (ele continua na `raw_cotacoes`).

**Revisões da API.** Durante o desenvolvimento, as cotações dos dias mais recentes mudaram entre duas execuções feitas em dias diferentes. Foi uma das razões para guardar a raw e para atualizar valores na tratada em vez de ignorar conflitos.

**Uma chamada por moeda.** A API aceita uma moeda base por requisição e já devolve a cotação direto em BRL, então o código faz um laço sobre a lista `MOEDAS`. Para adicionar uma moeda, basta incluí-la na lista.

**Janela de datas.** `DIAS = 30` busca de 30 dias atrás até hoje, com os dois extremos incluídos: são **31 datas** por moeda. A API devolve cotações também para sábados e domingos, e o pipeline não filtra esses dias.

**Falhar cedo e de forma visível.** O banco é testado antes da extração; a chamada à API tem `timeout` e `raise_for_status`; qualquer exceção é registrada com o traceback completo e o programa termina com código de saída 1 (útil para agendadores).

**Segredos fora do código.** Usuário, senha e host vêm do `.env`. A URL de conexão é montada com `URL.create`, que trata caracteres especiais na senha.

## Validações de qualidade

Antes de carregar a tabela tratada, a função `validar_cotacoes` interrompe o pipeline se:

- o DataFrame estiver vazio;
- existir alguma cotação menor ou igual a zero;
- existir alguma moeda fora da lista `MOEDAS`.

Depois da carga, `validar_carga` compara a contagem do banco com a do DataFrame e mostra o período e as linhas por moeda.

## Testando a idempotência

1. Rode `python3 main.py` e anote o total de `cotacoes_moedas`.
2. Rode de novo. O total **não muda** (10 moedas × 31 datas = 310 linhas).
3. A `raw_cotacoes` cresce 310 linhas por execução, o que é o comportamento esperado.

```bash
psql -U cotacoes_user -h localhost -d cotacoes_db -c \
"SELECT (SELECT COUNT(*) FROM raw_cotacoes) AS raw, (SELECT COUNT(*) FROM cotacoes_moedas) AS tratada;"
```

## Limitações conhecidas e próximos passos

- **Nulos:** não há checagem explícita de `cotacao_brl` nula na validação. Hoje um nulo só seria barrado pela restrição `NOT NULL` do banco, na hora da carga (erro tardio). Próximo passo: validar nulos antes de carregar.
- **A "raw" não é totalmente crua:** ela guarda o DataFrame após renomear colunas e converter datas, e não o JSON original da API. Uma versão mais fiel guardaria o JSON em uma coluna `jsonb`.
- **`FLOAT` para cotações:** é um tipo de ponto flutuante. Para valores monetários, `NUMERIC` é a escolha mais segura por ter precisão exata; aqui serve porque são taxas de referência, mas é uma decisão a revisar.
- **Fuso horário:** `data_ingestao` usa a hora local da máquina, sem fuso.
- **Contagem no log:** a mensagem "registros processados" do upsert conta as linhas enviadas, e não as que foram de fato inseridas ou alteradas.
- **Sem testes automatizados, sem retentativas na API e sem agendamento.** Próximos passos naturais: testes com `pytest`, retry com espera, execução diária (cron ou um orquestrador) e Docker.
- **Fins de semana:** o tratamento de dias sem mercado aberto é uma decisão de negócio ainda não definida.
- **Sem tabela de moedas:** o código da moeda é repetido em cada linha; uma dimensão de moedas seria o passo seguinte de modelagem.

## Fonte dos dados

[Frankfurter](https://frankfurter.dev): API gratuita e sem chave de acesso. São **taxas de referência** publicadas por bancos centrais e outras fontes oficiais, e não cotações de mercado em tempo real.

## Autor

Felipe Honorio, estudante de Ciência da Computação.
[GitHub](https://github.com/felipehnr00) · [LinkedIn](https://www.linkedin.com/in/felipehonorio00/)

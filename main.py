import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# Garante o carregamento do .env localizado na mesma pasta do main.py
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)
print(env_path, env_path.exists())  # o arquivo existe nesse caminho?
print(load_dotenv(dotenv_path=env_path))  # True = carregou, False = não achou o arquivo
print(os.getenv("DB_USER"))  # usuário (não é segredo)
print(os.getenv("DB_PASSWORD") is None)  # True = a senha NÃO chegou

API_URL = "https://api.frankfurter.dev/v2/rates"
MOEDAS = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "CNY", "SEK", "NZD"]
MOEDA_ALVO = "BRL"
DIAS = 30


# Passo 1 - Extração
def extrair_cotacoes(moeda, data_inicial, data_final):
    params = {
        "base": moeda,
        "quotes": MOEDA_ALVO,
        "from": data_inicial.isoformat(),
        "to": data_final.isoformat(),
    }
    resposta = requests.get(API_URL, params=params, timeout=10)
    resposta.raise_for_status()
    return resposta.json()


# Passo 2 - Transformação
def transformar_cotacoes(dados):
    df = pd.DataFrame(dados)
    df = df.rename(
        columns={
            "date": "data",
            "base": "moeda",
            "rate": "cotacao_brl",
        }
    )
    df["data"] = pd.to_datetime(df["data"])
    return df[["data", "moeda", "cotacao_brl"]]


def extrair_todas(data_inicial, data_final):
    frames = []
    for moeda in MOEDAS:
        dados = extrair_cotacoes(moeda, data_inicial, data_final)
        frames.append(transformar_cotacoes(dados))
    return pd.concat(frames, ignore_index=True)


# Passo 3 - Limpeza e Tipagem
def limpar_cotacoes(df):
    df = df.copy()
    df["cotacao_brl"] = df["cotacao_brl"].astype(float)
    df["moeda"] = df["moeda"].str.upper().str.strip()
    df = df.drop_duplicates(subset=["data", "moeda"])
    print("Contagem de valores nulos por coluna (0 significa nenhum valor ausente):")
    print(df.isna().sum())
    df["data_ingestao"] = datetime.now()
    return df.sort_values(["data", "moeda"]).reset_index(drop=True)


# Passo 4 - Validações de Qualidade antes de carregar
def validar_cotacoes(df):
    if df.empty:
        raise ValueError("Dataframe vazio. A API não retornou dados.")
    if (df["cotacao_brl"] <= 0).any():
        raise ValueError("Existem cotações menores ou iguais a zero.")
    if not df["moeda"].isin(MOEDAS).all():
        raise ValueError("Existem moedas inesperadas.")
    print("Validações OK")


# Passo 5 - Conexão e Carga (Load com Upsert)
def criar_engine():
    porta = os.getenv("DB_PORT")
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(porta) if porta else 5432,
        database=os.getenv("DB_NAME"),
    )
    return create_engine(url)


def testar_conexao(engine):
    with engine.connect() as conexao:
        resultado = conexao.execute(text("SELECT version()"))
        print(f"Conectado ao banco: {resultado.scalar()}")


def criar_tabela(engine):
    query = text("""
        CREATE TABLE IF NOT EXISTS cotacoes_moedas (
            data DATE NOT NULL,
            moeda VARCHAR(3) NOT NULL,
            cotacao_brl FLOAT NOT NULL,
            data_ingestao TIMESTAMP NOT NULL,
            PRIMARY KEY (data, moeda)
        );
    """)
    with engine.begin() as conexao:
        conexao.execute(query)
    print("Tabela 'cotacoes_moedas' verificada/criada com sucesso.")


def upsert_cotacoes(df, engine):
    df_carga = df.copy()
    df_carga["data"] = df_carga["data"].dt.date

    df_carga.to_sql("stg_cotacoes", engine, if_exists="replace", index=False)

    query_upsert = text("""
        INSERT INTO cotacoes_moedas (data, moeda, cotacao_brl, data_ingestao)
        SELECT CAST(data AS DATE), moeda, cotacao_brl, data_ingestao
        FROM stg_cotacoes
        ON CONFLICT (data, moeda)
        DO UPDATE SET
            cotacao_brl = EXCLUDED.cotacao_brl,
            data_ingestao = EXCLUDED.data_ingestao
        WHERE cotacoes_moedas.cotacao_brl IS DISTINCT FROM EXCLUDED.cotacao_brl;
    """)

    with engine.begin() as conexao:
        conexao.execute(query_upsert)
        conexao.execute(text("DROP TABLE IF EXISTS stg_cotacoes;"))

    print(
        f"Upsert realizado com sucesso na tabela! {len(df_carga)} registros processados."
    )


def validar_carga(engine, df):
    with engine.connect() as conexao:
        total = conexao.execute(text("SELECT COUNT(*) FROM cotacoes_moedas")).scalar()
        periodo = conexao.execute(
            text("SELECT MIN(data), MAX(data) FROM cotacoes_moedas")
        ).one()  # .one() devolve a única linha do resultado
    print(f"Linhas no banco: {total} | Linhas no DataFrame: {len(df)}")
    print(f"Período no banco: {periodo[0]} a {periodo[1]}")
    por_moeda = pd.read_sql(  # resultado da consulta direto em DataFrame
        text(
            "SELECT moeda, COUNT(*) AS linhas FROM cotacoes_moedas GROUP BY moeda ORDER BY moeda"
        ),
        engine,
    )
    print(por_moeda)


def carregar_raw(df_bruto, engine):
    df_raw = df_bruto.copy()  # não mexe no DataFrame original
    df_raw["data_ingestao"] = (
        datetime.now()
    )  # marca em qual execução estas linhas entraram
    df_raw.to_sql(
        "raw_cotacoes",  # tabela criada automaticamente se não existir
        engine,
        if_exists="append",  # acrescenta sem apagar o que já existe
        index=False,  # não grava o índice do pandas como coluna
    )
    print(f"Raw: {len(df_raw)} linhas acrescentadas em 'raw_cotacoes'.")


if __name__ == "__main__":
    hoje = date.today()
    engine = criar_engine()
    testar_conexao(engine)

    df_bruto = extrair_todas(hoje - timedelta(days=DIAS), hoje)
    carregar_raw(df_bruto, engine)

    df = limpar_cotacoes(df_bruto)
    validar_cotacoes(df)
    criar_tabela(engine)
    upsert_cotacoes(df, engine)
    validar_carga(engine, df)

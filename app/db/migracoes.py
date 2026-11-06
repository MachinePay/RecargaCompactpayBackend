"""Sem Alembic (mesmo esquema da CompactPay): o create_all cria tabelas
novas, mas nao adiciona coluna em tabela que ja existe. Aqui entra o
ALTER TABLE ADD COLUMN para as colunas novas dos models (sempre nullable)."""
import logging

from sqlalchemy import inspect, text


def adicionar_colunas_faltando(engine, base) -> None:
    inspetor = inspect(engine)
    tabelas = set(inspetor.get_table_names())
    with engine.begin() as conexao:
        for tabela in base.metadata.sorted_tables:
            if tabela.name not in tabelas:
                continue
            existentes = {c["name"] for c in inspetor.get_columns(tabela.name)}
            for coluna in tabela.columns:
                if coluna.name in existentes:
                    continue
                tipo = coluna.type.compile(dialect=engine.dialect)
                logging.info("Migracao: %s.%s %s", tabela.name, coluna.name, tipo)
                conexao.execute(text(f'ALTER TABLE {tabela.name} ADD COLUMN "{coluna.name}" {tipo}'))


def criar_indices_faltando(engine, base) -> None:
    """create_all nao cria indice novo em tabela que ja existe: cria aqui."""
    inspetor = inspect(engine)
    tabelas = set(inspetor.get_table_names())
    for tabela in base.metadata.sorted_tables:
        if tabela.name not in tabelas:
            continue
        existentes = {i["name"] for i in inspetor.get_indexes(tabela.name)}
        for indice in tabela.indexes:
            if indice.name not in existentes:
                logging.info("Migracao: indice %s", indice.name)
                indice.create(bind=engine, checkfirst=True)

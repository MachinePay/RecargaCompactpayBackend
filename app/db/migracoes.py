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


def numerar_carregadores(engine) -> None:
    """Carregador sem numero (antigos): da o proximo numero, na ordem de cadastro."""
    from sqlalchemy import func
    from sqlalchemy.orm import Session

    from app.models.carregador import Carregador

    with Session(engine) as db:
        sem_numero = db.query(Carregador).filter(Carregador.numero.is_(None)).order_by(Carregador.created_at, Carregador.id_hardware).all()
        if not sem_numero:
            return
        proximo = (db.query(func.max(Carregador.numero)).scalar() or 0) + 1
        for carregador in sem_numero:
            carregador.numero = proximo
            proximo += 1
        db.commit()
        logging.info("Migracao: %s carregador(es) numerado(s)", len(sem_numero))


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

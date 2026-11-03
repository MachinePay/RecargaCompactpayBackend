from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

if settings.DATABASE_URL.startswith("sqlite"):
    # O servidor OCPP e os workers usam o banco de threads diferentes.
    engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=15,
        max_overflow=15,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def eh_postgres() -> bool:
    return engine.dialect.name == "postgresql"

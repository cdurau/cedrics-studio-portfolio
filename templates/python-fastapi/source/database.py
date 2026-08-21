import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from pydantic_settings import BaseSettings

logger = logging.getLogger("app")

class Settings(BaseSettings):
    database_url: str = "sqlite:///./app.db"
    port: int = 3000
    host: str = "0.0.0.0"

settings = Settings()

# Support both SQLite and PostgreSQL without code rewrite
# hide_parameters=True for safety
engine = create_engine(
    settings.database_url,
    hide_parameters=True,
    # Check same thread only needed for sqlite
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

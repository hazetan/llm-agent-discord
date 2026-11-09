from os import makedirs
from os.path import abspath, dirname, join

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = dirname(dirname(abspath(__file__)))

DB_DIR = join(BASE_DIR, '.database')
makedirs(DB_DIR, exist_ok=True)
DB_PATH = join(BASE_DIR, '/database', 'data.db')
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"


engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)

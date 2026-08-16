import os
import sys

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STERLING_ADAPTER_API_KEYS", "test-key-123")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session as SASession
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest.fixture()
def db():
    # StaticPool so :memory: is shared across connections within one test,
    # matching the fix this repo's own test suite already documented needing
    # (Working/backend PROJECT_STATUS.md §6: "each session was getting its
    # own private empty database").
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=SASession)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

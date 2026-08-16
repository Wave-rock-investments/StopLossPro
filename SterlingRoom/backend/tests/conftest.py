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


@pytest.fixture(autouse=True)
def _reset_rate_limit_backend_between_tests():
    """The rate limiter (app/rate_limit.py) keeps its backend — and every
    counter in it — in a process-wide singleton, exactly like it must in
    production (that's the whole point of the shared-state design for
    multi-worker deployments). But that means, without this fixture,
    unrelated test files sharing one pytest process would silently share
    rate-limit counters too: e.g. every test in test_admin.py calling
    POST /admin/bootstrap would burn down the SAME 5-per-hour
    admin_bootstrap quota, and the 6th test across the whole file (not
    within any single test) would start failing with 429s that have
    nothing to do with what that test is actually checking. Reset before
    (and after, for safety) every test so each test's rate-limit behavior
    is judged only against requests IT made.
    """
    from app.rate_limit import reset_backend_for_tests

    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


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

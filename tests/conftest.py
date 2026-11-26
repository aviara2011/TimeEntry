import pytest
from fastapi.testclient import TestClient

from timeentry.web.app import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db(app):
    session = app.state.sessionmaker()
    yield session
    session.close()

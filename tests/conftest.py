import os
import pytest

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("CLAUDE_API_KEY", "test-claude-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BASE_URL", "http://localhost:5000")


@pytest.fixture
def app():
    from app import app
    app.config["TESTING"] = True
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db():
    from db import _db
    d = _db()
    yield d
    d.close()

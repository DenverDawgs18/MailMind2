"""
pytest configuration.

We set the environment BEFORE importing the app so its module-level config
picks up dev-safe defaults (no real Redis, no real Stripe, in-memory SQLite).
"""
import os
import tempfile

import pytest
from cryptography.fernet import Fernet

os.environ["MAILMIND_TEST"] = "1"
os.environ.setdefault("MAILMIND_PRODUCTION", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("STRIPE_API_KEY", "sk_test_dummy")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("MICROSOFT_CLIENT_ID", "test-ms-id")
os.environ.setdefault("MICROSOFT_CLIENT_SECRET", "test-ms-secret")
os.environ.setdefault("DOMAIN", "http://localhost:5000")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("TEST_WEBHOOK", "whsec_test_dummy")


class _FakeRedis:
    """Minimal Redis substitute for tests — no external service required."""

    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    # flask-session's RedisSessionInterface uses a small subset of the API.
    def setex(self, key, ttl, value):
        self._store[key] = value

    def keys(self, pattern=None):
        return list(self._store.keys())


@pytest.fixture
def app():
    import app as app_module

    fake_redis = _FakeRedis()
    app_module._redis_client = fake_redis
    app_module.app.config["SESSION_REDIS"] = fake_redis

    fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    app_module.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    with app_module.app.app_context():
        app_module.db.create_all()
        yield app_module.app
        app_module.db.session.remove()
        app_module.db.drop_all()

    os.unlink(db_path)


@pytest.fixture
def client(app):
    return app.test_client()

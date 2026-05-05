"""Session fixture for security integration tests (Issue #589).

Loads real REDIS_PASSWORD / REDIS_BACKEND_PASSWORD from the project's .env
(if present) and skips the suite when neither env nor file provides them.
"""

import os
from pathlib import Path

import pytest

try:
    from dotenv import dotenv_values  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore[assignment]


@pytest.fixture(scope="session", autouse=True)
def _load_redis_env():
    if dotenv_values is not None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for key, value in dotenv_values(env_path).items():
                if key in {"REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"} and value:
                    os.environ.setdefault(key, value)

    for required in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        if required not in os.environ or not os.environ[required]:
            pytest.skip(
                f"{required} not set; integration test requires .env or env vars"
            )

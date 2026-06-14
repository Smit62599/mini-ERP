"""
Application configuration.

We centralise settings here so the Flask factory can pick the right
config object per environment without scattering os.getenv calls.
"""

import os
from dotenv import load_dotenv

# Load `.env` early so DATABASE_URL is available when SQLAlchemy initialises.
load_dotenv()


class Config:
    """Base configuration shared by all environments."""

    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/shiv_furniture",
    )
    # Disable modification tracking – we write explicit audit rows instead.
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Ensure each HTTP request gets a clean session (rollback on error).
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/shiv_furniture_test",
    )


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}

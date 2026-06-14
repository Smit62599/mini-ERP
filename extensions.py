"""
Shared Flask extensions.

Initialised here (without an app) and bound later inside create_app().
This avoids circular imports between models, routes, and the factory.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Single SQLAlchemy instance for the whole application.
db = SQLAlchemy()

# Flask-Login manages session-based authentication and `current_user`.
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

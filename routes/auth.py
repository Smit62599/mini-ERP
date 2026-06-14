"""
Authentication routes – login, logout, and user loader registration.

Flask-Login uses a "user loader" callback to rehydrate the User object
from the session cookie on every request.
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from extensions import db, login_manager
from models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@login_manager.user_loader
def load_user(user_id: str):
    """Flask-Login callback – fetch user by primary key from session."""
    return db.session.get(User, int(user_id))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        try:
            user = db.session.scalar(select(User).where(User.email == email))
        except OperationalError:
            db.session.rollback()
            flash(
                "Database connection failed. Ensure PostgreSQL is running, "
                "then restart the app.",
                "danger",
            )
            return render_template("auth/login.html")

        if user and user.check_password(password):
            remember = request.form.get("remember") == "on"
            login_user(user, remember=remember)
            next_page = request.args.get("next")
            if next_page:
                return redirect(next_page)
            return redirect(url_for("sales.list_orders"))

        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile")
def profile_redirect():
    """Legacy URL – wireframe uses /profile."""
    return redirect(url_for("user_pages.profile"))

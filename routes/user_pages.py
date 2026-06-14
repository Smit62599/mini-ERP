"""
Top-level user-facing pages: signup, profile, and profile edit.

Routes live at /signup and /profile (no /auth prefix) per wireframe URLs.
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from extensions import db
from models import User, UserRole

user_pages_bp = Blueprint("user_pages", __name__)

SIGNUP_POSITIONS = ("Admin", "Sales Manager", "Salesperson")

POSITION_ROLE_MAP: dict[str, UserRole] = {
    "Admin": UserRole.ADMIN,
    "Sales Manager": UserRole.SALES,
    "Salesperson": UserRole.SALES,
}


@user_pages_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        if current_user.is_authenticated:
            flash("Log out first before registering a new account.", "warning")
            return redirect(url_for("user_pages.signup"))

        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        mobile = request.form.get("mobile_number", "").strip()
        email = request.form.get("email", "").strip().lower()
        position = request.form.get("position", "").strip()
        password = request.form.get("password", "")

        if not all([name, address, mobile, email, position, password]):
            flash("All fields are required.", "danger")
            return render_template(
                "signup.html", positions=SIGNUP_POSITIONS, form=request.form
            )

        if position not in POSITION_ROLE_MAP:
            flash("Please select a valid position.", "danger")
            return render_template(
                "signup.html", positions=SIGNUP_POSITIONS, form=request.form
            )

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template(
                "signup.html", positions=SIGNUP_POSITIONS, form=request.form
            )

        existing = db.session.scalar(select(User).where(User.email == email))
        if existing:
            flash("An account with this email already exists.", "danger")
            return render_template(
                "signup.html", positions=SIGNUP_POSITIONS, form=request.form
            )

        user = User(
            name=name,
            email=email,
            address=address,
            mobile_number=mobile,
            position=position,
            role=POSITION_ROLE_MAP[position],
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Account created successfully. Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("signup.html", positions=SIGNUP_POSITIONS, form=None)


@user_pages_bp.route("/profile")
@login_required
def profile():
    return render_template("profile.html")


@user_pages_bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    if request.method == "POST":
        address = request.form.get("address", "").strip()
        mobile = request.form.get("mobile_number", "").strip()

        if not address or not mobile:
            flash("Address and mobile number are required.", "danger")
            return render_template("profile_edit.html")

        current_user.address = address
        current_user.mobile_number = mobile
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("user_pages.profile"))

    return render_template("profile_edit.html")

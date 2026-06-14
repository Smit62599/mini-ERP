"""
Admin user management – list, detail, permissions (System Administrator Dashboard).
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select

from extensions import db
from models import User, UserRole
from routes.decorators import role_required
from services.user_permission_service import (
    ADMIN_POSITIONS,
    MODULE_LABELS,
    PERMISSION_SCHEMA,
    build_module_rows,
    create_user_with_defaults,
    parse_permissions_from_form,
    resolve_admin_position,
    save_user_admin_settings,
)

admin_users_bp = Blueprint("admin_users", __name__, url_prefix="/admin/users")


@admin_users_bp.route("/")
@login_required
@role_required(UserRole.ADMIN)
def list_users():
    search = request.args.get("q", "").strip()
    query = select(User).order_by(User.name)
    if search:
        like = f"%{search}%"
        query = query.where(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.position.ilike(like),
            )
        )
    users = db.session.scalars(query).all()
    return render_template(
        "admin/users/list.html",
        users=users,
        search=search,
    )


@admin_users_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def new_user():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        mobile = request.form.get("mobile_number", "").strip()
        email = request.form.get("email", "").strip().lower()
        position = request.form.get("position", "").strip()
        password = request.form.get("password", "")

        if not all([name, address, mobile, email, position, password]):
            flash("All fields are required.", "danger")
            return render_template(
                "admin/users/new.html",
                positions=ADMIN_POSITIONS,
                form=request.form,
            )

        if position not in ADMIN_POSITIONS:
            flash("Please select a valid position.", "danger")
            return render_template(
                "admin/users/new.html",
                positions=ADMIN_POSITIONS,
                form=request.form,
            )

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template(
                "admin/users/new.html",
                positions=ADMIN_POSITIONS,
                form=request.form,
            )

        try:
            user = create_user_with_defaults(
                name=name,
                email=email,
                address=address,
                mobile_number=mobile,
                position=position,
                password=password,
            )
            flash(f"User {user.name} created. Set module permissions below.", "success")
            return redirect(url_for("admin_users.manage_user", user_id=user.id))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "admin/users/new.html",
                positions=ADMIN_POSITIONS,
                form=request.form,
            )

    return render_template("admin/users/new.html", positions=ADMIN_POSITIONS, form=None)


@admin_users_bp.route("/<int:user_id>", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def manage_user(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin_users.list_users"))

    active_tab = request.form.get("active_tab") or request.args.get("tab", "sales")
    if active_tab not in PERMISSION_SCHEMA:
        active_tab = "sales"

    if request.method == "POST":
        position = request.form.get("position", "").strip()
        if position not in ADMIN_POSITIONS:
            flash("Please select a valid position.", "danger")
        else:
            try:
                permissions = parse_permissions_from_form(request.form)
                save_user_admin_settings(
                    user=user,
                    position=position,
                    permissions=permissions,
                )
                flash(f"Updated settings for {user.name}.", "success")
                return redirect(
                    url_for("admin_users.manage_user", user_id=user.id, tab=active_tab)
                )
            except ValueError as exc:
                flash(str(exc), "danger")

    module_rows = {
        module: build_module_rows(module, user)
        for module in PERMISSION_SCHEMA
    }
    return render_template(
        "admin/users/form.html",
        user=user,
        positions=ADMIN_POSITIONS,
        selected_position=resolve_admin_position(user),
        module_labels=MODULE_LABELS,
        modules=list(PERMISSION_SCHEMA.keys()),
        module_rows=module_rows,
        active_tab=active_tab,
    )


@admin_users_bp.route("/delete", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN)
def delete_users():
    raw_ids = request.form.getlist("user_ids")
    if not raw_ids:
        flash("Select at least one user to delete.", "warning")
        return redirect(url_for("admin_users.list_users"))

    try:
        user_ids = [int(uid) for uid in raw_ids]
    except ValueError:
        flash("Invalid user selection.", "danger")
        return redirect(url_for("admin_users.list_users"))

    if current_user.id in user_ids:
        flash("You cannot delete your own account.", "danger")
        user_ids = [uid for uid in user_ids if uid != current_user.id]

    if not user_ids:
        return redirect(url_for("admin_users.list_users"))

    admin_count = db.session.scalar(
        select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
    )
    deleting_admins = db.session.scalars(
        select(User).where(User.id.in_(user_ids), User.role == UserRole.ADMIN)
    ).all()
    if admin_count - len(deleting_admins) < 1:
        flash("Cannot delete the last system administrator.", "danger")
        return redirect(url_for("admin_users.list_users"))

    for user in db.session.scalars(select(User).where(User.id.in_(user_ids))).all():
        db.session.delete(user)
    db.session.commit()
    flash(f"Deleted {len(user_ids)} user(s).", "success")
    return redirect(url_for("admin_users.list_users"))

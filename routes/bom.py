"""Bill of Materials routes."""

from decimal import InvalidOperation

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from extensions import db
from models import BOMComponent, BOMOperation, BillOfMaterials, Product, UserRole, WorkCenter
from routes.decorators import role_required
from services.bom_service import BOMError, bom_to_dict, create_bom, sync_bom_components, sync_bom_operations

bom_bp = Blueprint("bom", __name__, url_prefix="/bom")


def _parse_component_rows() -> list[dict]:
    rows = []
    for pid, qty in zip(
        request.form.getlist("component_product_id"),
        request.form.getlist("quantity_required"),
    ):
        if pid and qty:
            rows.append({"component_product_id": pid, "quantity_required": qty})
    return rows


def _parse_operation_rows() -> list[dict]:
    rows = []
    for name, wc, dur in zip(
        request.form.getlist("operation_name"),
        request.form.getlist("work_center_id"),
        request.form.getlist("duration_minutes"),
    ):
        if name and wc:
            rows.append(
                {
                    "operation_name": name,
                    "work_center_id": wc,
                    "duration_minutes": dur,
                }
            )
    return rows


@bom_bp.route("/")
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def list_boms():
    boms = (
        BillOfMaterials.query.options(joinedload(BillOfMaterials.finished_product))
        .order_by(BillOfMaterials.bom_number)
        .all()
    )
    return render_template("bom/list.html", boms=boms)


@bom_bp.route("/new", methods=["GET", "POST"])
@bom_bp.route("/<int:bom_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def bom_form(bom_id: int | None = None):
    bom = (
        BillOfMaterials.query.options(
            joinedload(BillOfMaterials.components).joinedload(BOMComponent.component_product),
            joinedload(BillOfMaterials.operations).joinedload(BOMOperation.work_center),
        )
        .filter_by(id=bom_id)
        .first()
        if bom_id
        else None
    )
    products = Product.query.order_by(Product.name).all()
    work_centers = WorkCenter.query.order_by(WorkCenter.name).all()

    if request.method == "POST":
        try:
            finished_product_id = int(request.form["finished_product_id"])
            quantity = int(request.form.get("quantity") or 1)

            with db.session.begin_nested():
                if bom:
                    bom.finished_product_id = finished_product_id
                    bom.quantity = quantity
                else:
                    bom = create_bom(
                        finished_product_id=finished_product_id,
                        quantity=quantity,
                        user_id=current_user.id,
                    )
                sync_bom_components(
                    bom=bom,
                    component_rows=_parse_component_rows(),
                    user_id=current_user.id,
                )
                sync_bom_operations(
                    bom=bom,
                    operation_rows=_parse_operation_rows(),
                    user_id=current_user.id,
                )
            db.session.commit()
            flash(f"BoM {bom.bom_number} saved.", "success")
            return redirect(url_for("bom.list_boms"))
        except (BOMError, InvalidOperation, ValueError, KeyError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "bom/form.html",
        bom=bom,
        products=products,
        work_centers=work_centers,
        all_products=products,
    )


@bom_bp.route("/<int:bom_id>/api")
@login_required
def bom_api(bom_id: int):
    """JSON payload for MO form BoM ingredient injection."""
    bom = (
        BillOfMaterials.query.options(
            joinedload(BillOfMaterials.components).joinedload(BOMComponent.component_product),
            joinedload(BillOfMaterials.operations).joinedload(BOMOperation.work_center),
        )
        .filter_by(id=bom_id)
        .first()
    )
    if not bom:
        return jsonify({"error": "BoM not found"}), 404
    return jsonify(bom_to_dict(bom))

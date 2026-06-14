"""
Manufacturing module routes – MO list, form, and state machine actions.
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from extensions import db
from models import (
    BillOfMaterials,
    ManufacturingOrder,
    ManufacturingOrderStatus,
    MOComponent,
    Product,
    User,
    UserRole,
    WorkCenter,
    WorkOrder,
)
from routes.decorators import role_required
from services.manufacturing_service import (
    InvalidMOTransition,
    ManufacturingOrderError,
    cancel_manufacturing_order,
    confirm_manufacturing_order,
    create_manufacturing_order,
    produce_manufacturing_order,
    start_manufacturing_order,
    update_draft_mo,
)

manufacturing_bp = Blueprint("manufacturing", __name__, url_prefix="/manufacturing")

LIST_PER_PAGE = 20


def _parse_int(value: str | None, *, field: str = "value") -> int:
    """Parse form integers that may arrive as decimal strings (e.g. '45.000')."""
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field} is required.")
    try:
        return int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def _parse_optional_int(value: str | None, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        return default


def _assignable_operators() -> list[User]:
    return (
        User.query.filter(User.role == UserRole.MANUFACTURING)
        .order_by(User.name)
        .all()
    )


def _resolve_assignee_id(form_value: str | None) -> int:
    if current_user.role == UserRole.MANUFACTURING:
        return current_user.id
    allowed_ids = {u.id for u in _assignable_operators()}
    if form_value:
        assignee_id = int(form_value)
        if assignee_id not in allowed_ids:
            raise ManufacturingOrderError(
                "Invalid assignee – only manufacturing operators can be assigned."
            )
        return assignee_id
    operators = _assignable_operators()
    if not operators:
        raise ManufacturingOrderError("No manufacturing operators exist.")
    return operators[0].id


def _parse_mo_component_rows() -> list[dict]:
    rows = []
    for pid, req, con in zip(
        request.form.getlist("component_product_id"),
        request.form.getlist("required_qty"),
        request.form.getlist("consumed_qty"),
    ):
        if pid:
            rows.append(
                {
                    "component_product_id": pid,
                    "required_qty": req or "0",
                    "consumed_qty": con or "0",
                }
            )
    return rows


def _parse_mo_operation_rows() -> list[dict]:
    rows = []
    for name, wc, exp, real in zip(
        request.form.getlist("operation_name"),
        request.form.getlist("work_center_id"),
        request.form.getlist("expected_duration"),
        request.form.getlist("real_duration"),
    ):
        if name and wc:
            rows.append(
                {
                    "operation_name": name,
                    "work_center_id": wc,
                    "expected_duration": exp or "0",
                    "real_duration": real or "0",
                }
            )
    return rows


def _products_availability_json() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "on_hand_qty": float(p.on_hand_qty),
            "reserved_qty": float(p.reserved_qty),
            "free_to_use_qty": float(p.free_to_use_qty),
        }
        for p in Product.query.order_by(Product.name).all()
    ]


@manufacturing_bp.route("/orders")
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def list_orders():
    status_filter = request.args.get("status")

    query = (
        db.session.query(ManufacturingOrder)
        .options(
            joinedload(ManufacturingOrder.product),
            joinedload(ManufacturingOrder.assignee),
        )
        .order_by(ManufacturingOrder.created_at.desc())
    )

    if status_filter:
        try:
            status_enum = ManufacturingOrderStatus(status_filter)
            query = query.filter(ManufacturingOrder.status == status_enum)
        except ValueError:
            flash(f"Unknown status filter: {status_filter}", "warning")

    page = request.args.get("page", 1, type=int)
    pagination = query.paginate(page=page, per_page=LIST_PER_PAGE, error_out=False)

    status_counts: dict[str, int] = {s.value: 0 for s in ManufacturingOrderStatus}
    for row in (
        db.session.query(ManufacturingOrder.status, func.count(ManufacturingOrder.id))
        .group_by(ManufacturingOrder.status)
        .all()
    ):
        status_counts[row[0].value] = row[1]

    pagination_args = {}
    if status_filter:
        pagination_args["status"] = status_filter

    return render_template(
        "manufacturing/mo_list.html",
        pagination=pagination,
        statuses=list(ManufacturingOrderStatus),
        status_counts=status_counts,
        current_status=status_filter,
        pagination_args=pagination_args,
    )


@manufacturing_bp.route("/orders/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def new_order():
    if request.method == "POST":
        try:
            with db.session.begin_nested():
                order = create_manufacturing_order(
                    finished_product_id=int(request.form["finished_product_id"]),
                    quantity=_parse_int(request.form.get("quantity"), field="quantity"),
                    bom_id=int(request.form["bom_id"]) if request.form.get("bom_id") else None,
                    assignee_id=_resolve_assignee_id(request.form.get("assignee_id")),
                    user_id=current_user.id,
                    component_rows=_parse_mo_component_rows(),
                    operation_rows=_parse_mo_operation_rows(),
                )
            db.session.commit()
            flash(f"Manufacturing Order {order.mo_number} created.", "success")
            return redirect(url_for("manufacturing.view_order", order_id=order.id))
        except (ManufacturingOrderError, InvalidOperation, KeyError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return _render_mo_form(None)


@manufacturing_bp.route("/orders/<int:order_id>", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def view_order(order_id: int):
    order = (
        ManufacturingOrder.query.options(
            joinedload(ManufacturingOrder.product),
            joinedload(ManufacturingOrder.bom),
            joinedload(ManufacturingOrder.assignee),
            joinedload(ManufacturingOrder.components).joinedload(MOComponent.component_product),
            joinedload(ManufacturingOrder.work_orders).joinedload(WorkOrder.work_center),
        )
        .filter_by(id=order_id)
        .first()
    )
    if not order:
        flash("Manufacturing order not found.", "danger")
        return redirect(url_for("manufacturing.list_orders"))

    if request.method == "POST" and order.status == ManufacturingOrderStatus.DRAFT:
        try:
            with db.session.begin_nested():
                update_draft_mo(
                    order=order,
                    finished_product_id=int(request.form["finished_product_id"]),
                    quantity=_parse_int(request.form.get("quantity"), field="quantity"),
                    bom_id=int(request.form["bom_id"]) if request.form.get("bom_id") else None,
                    assignee_id=_resolve_assignee_id(request.form.get("assignee_id")),
                    component_rows=_parse_mo_component_rows(),
                    operation_rows=_parse_mo_operation_rows(),
                    user_id=current_user.id,
                )
            db.session.commit()
            flash("MO saved.", "success")
            return redirect(url_for("manufacturing.view_order", order_id=order.id))
        except (ManufacturingOrderError, InvalidOperation, KeyError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return _render_mo_form(order)


def _render_mo_form(order: ManufacturingOrder | None):
    finished_products = Product.query.order_by(Product.name).all()
    boms = (
        BillOfMaterials.query.options(joinedload(BillOfMaterials.finished_product))
        .order_by(BillOfMaterials.bom_number)
        .all()
    )
    return render_template(
        "manufacturing/mo_form.html",
        order=order,
        finished_products=finished_products,
        boms=boms,
        operators=_assignable_operators(),
        work_centers=WorkCenter.query.order_by(WorkCenter.name).all(),
        component_products=Product.query.order_by(Product.name).all(),
        products_json=_products_availability_json(),
        boms_json=[
            {
                "id": b.id,
                "bom_number": b.bom_number,
                "finished_product_id": b.finished_product_id,
            }
            for b in boms
        ],
    )


@manufacturing_bp.route("/orders/<int:order_id>/confirm", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def confirm_order(order_id: int):
    order = db.session.get(ManufacturingOrder, order_id)
    try:
        confirm_manufacturing_order(order=order, user_id=current_user.id)
        db.session.commit()
        flash(f"MO {order.mo_number} confirmed – components reserved.", "success")
    except (ManufacturingOrderError, InvalidMOTransition) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manufacturing.view_order", order_id=order_id))


@manufacturing_bp.route("/orders/<int:order_id>/start", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def start_order(order_id: int):
    order = db.session.get(ManufacturingOrder, order_id)
    try:
        start_manufacturing_order(order=order, user_id=current_user.id)
        db.session.commit()
        flash(f"Production started for {order.mo_number}.", "success")
    except (ManufacturingOrderError, InvalidMOTransition) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manufacturing.view_order", order_id=order_id))


@manufacturing_bp.route("/orders/<int:order_id>/produce", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def produce_order(order_id: int):
    order = db.session.get(ManufacturingOrder, order_id)
    try:
        consumed = {}
        for comp in order.components:
            key = f"consumed_qty_{comp.id}"
            if key in request.form:
                consumed[comp.id] = Decimal(request.form[key])

        real_durations = {}
        for wo in order.work_orders:
            key = f"real_duration_{wo.id}"
            if key in request.form and request.form[key]:
                real_durations[wo.id] = _parse_int(request.form[key], field="real duration")

        produce_manufacturing_order(
            order=order,
            consumed_quantities=consumed,
            real_durations=real_durations,
            user_id=current_user.id,
        )
        db.session.commit()
        flash(f"MO {order.mo_number} produced – inventory updated.", "success")
    except (ManufacturingOrderError, InvalidMOTransition, InvalidOperation) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manufacturing.view_order", order_id=order_id))


@manufacturing_bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.MANUFACTURING)
def cancel_order(order_id: int):
    order = db.session.get(ManufacturingOrder, order_id)
    try:
        cancel_manufacturing_order(order=order, user_id=current_user.id)
        db.session.commit()
        flash(f"MO {order.mo_number} cancelled.", "info")
    except (ManufacturingOrderError, InvalidMOTransition) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manufacturing.view_order", order_id=order_id))

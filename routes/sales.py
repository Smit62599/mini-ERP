"""
Sales module routes – list, create, view, confirm, deliver, cancel.

These are thin controllers: they parse HTTP input, call the service layer,
and render templates.  All inventory/audit business rules live in services/.
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from extensions import db
from models import AuditLog, Customer, Product, SalesOrder, SalesOrderStatus, User, UserRole
from routes.decorators import role_required
from routes.sales_access import apply_sales_order_list_filter, assert_sales_order_access
from services.inventory_service import InsufficientStockError
from services.sales_service import (
    InvalidStatusTransition,
    SalesOrderError,
    add_line_to_order,
    cancel_sales_order,
    confirm_sales_order,
    create_sales_order,
    deliver_sales_order,
)

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")

LIST_PER_PAGE = 20


def _list_pagination_args() -> dict:
    """Preserve filter/search query params across pagination links."""
    args = {}
    if request.args.get("status"):
        args["status"] = request.args.get("status")
    if request.args.get("q"):
        args["q"] = request.args.get("q")
    if request.args.get("mine") == "1":
        args["mine"] = "1"
    if request.args.get("view"):
        args["view"] = request.args.get("view")
    return args


def _assignable_salespeople() -> list[User]:
    """
    Users eligible to be assigned as Sales Person on an order.

    Only users with the SALES role appear here – System Administrator and
    other elevated roles are never valid salesperson assignments.
    """
    return (
        User.query.filter(User.role == UserRole.SALES)
        .order_by(User.name)
        .all()
    )


def _resolve_sales_person_id(form_value: str | None) -> int:
    """
    Determine sales_person_id from form input with RBAC enforcement.

    Sales users are always assigned to themselves.  Admins may pick any
    member of the sales team but never an admin/owner account.
    """
    if current_user.role == UserRole.SALES:
        return current_user.id

    allowed_ids = {u.id for u in _assignable_salespeople()}
    if form_value:
        sales_person_id = int(form_value)
        if sales_person_id not in allowed_ids:
            raise SalesOrderError(
                "Invalid sales person – only sales team members can be assigned."
            )
        return sales_person_id
    # Default admin to first sales user if available, else self
    sales_team = _assignable_salespeople()
    return sales_team[0].id if sales_team else current_user.id


# ---------------------------------------------------------------------------
# List view – supports status filter and text search (wireframe requirement)
# ---------------------------------------------------------------------------


@sales_bp.route("/")
@login_required
def list_orders():
    query = apply_sales_order_list_filter(SalesOrder.query)

    # Status filter from dashboard buttons (e.g. ?status=confirmed)
    status_filter = request.args.get("status")
    if status_filter:
        try:
            status_enum = SalesOrderStatus(status_filter)
            query = query.filter(SalesOrder.status == status_enum)
        except ValueError:
            flash(f"Unknown status filter: {status_filter}", "warning")

    # Search by SO reference or customer name
    search = request.args.get("q", "").strip()
    if search:
        query = query.join(Customer).filter(
            or_(
                SalesOrder.so_number.ilike(f"%{search}%"),
                Customer.name.ilike(f"%{search}%"),
            )
        )

    # "My orders" filter – orders assigned to the logged-in salesperson
    if request.args.get("mine") == "1":
        query = query.filter(SalesOrder.sales_person_id == current_user.id)

    page = request.args.get("page", 1, type=int)
    view_mode = request.args.get("view", "list")  # list | kanban

    pagination = (
        query.options(
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.sales_person),
        )
        .order_by(SalesOrder.created_at.desc())
        .paginate(page=page, per_page=LIST_PER_PAGE, error_out=False)
    )

    # Status counts scoped to the same visibility as the list query.
    counts_query = apply_sales_order_list_filter(db.session.query(SalesOrder))
    status_counts = {
        status.value: count
        for status, count in counts_query.with_entities(
            SalesOrder.status, func.count(SalesOrder.id)
        ).group_by(SalesOrder.status).all()
    }

    orders_by_status = {s: [] for s in SalesOrderStatus}
    for order in pagination.items:
        orders_by_status[order.status].append(order)

    return render_template(
        "sales/list.html",
        pagination=pagination,
        orders_by_status=orders_by_status,
        status_counts=status_counts,
        statuses=list(SalesOrderStatus),
        current_status=status_filter,
        search=search,
        view_mode=view_mode,
        pagination_args=_list_pagination_args(),
    )


# ---------------------------------------------------------------------------
# Create new Draft order
# ---------------------------------------------------------------------------


@sales_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def new_order():
    customers = Customer.query.order_by(Customer.name).all()
    salespeople = _assignable_salespeople()
    products = Product.query.order_by(Product.name).all()

    if request.method == "POST":
        try:
            customer_id = int(request.form["customer_id"])
            customer_address = request.form.get("customer_address", "").strip()
            sales_person_id = _resolve_sales_person_id(
                request.form.get("sales_person_id")
            )

            with db.session.begin_nested():
                order = create_sales_order(
                    customer_id=customer_id,
                    customer_address=customer_address or None,
                    sales_person_id=sales_person_id,
                    user_id=current_user.id,
                )

                # Process line items submitted as parallel arrays from the form.
                product_ids = request.form.getlist("product_id")
                quantities = request.form.getlist("ordered_qty")
                lines_added = 0
                for pid, qty_str in zip(product_ids, quantities):
                    if pid and qty_str:
                        add_line_to_order(
                            sales_order=order,
                            product_id=int(pid),
                            ordered_qty=Decimal(qty_str),
                            user_id=current_user.id,
                        )
                        lines_added += 1

                if lines_added == 0:
                    raise SalesOrderError("Please add at least one product with a quantity.")

            db.session.commit()
            flash(f"Sales Order {order.so_number} created.", "success")
            return redirect(url_for("sales.view_order", order_id=order.id))

        except (SalesOrderError, InvalidOperation, KeyError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "sales/form.html",
        order=None,
        customers=customers,
        salespeople=salespeople,
        products=products,
        readonly=False,
    )


# ---------------------------------------------------------------------------
# View / edit a single order
# ---------------------------------------------------------------------------


@sales_bp.route("/<int:order_id>", methods=["GET", "POST"])
@login_required
def view_order(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if not order:
        flash("Sales order not found.", "danger")
        return redirect(url_for("sales.list_orders"))
    assert_sales_order_access(order)

    customers = Customer.query.order_by(Customer.name).all()
    salespeople = _assignable_salespeople()
    products = Product.query.order_by(Product.name).all()

    # Read-only once confirmed (except delivered_qty on partial delivery)
    readonly = order.status != SalesOrderStatus.DRAFT

    if request.method == "POST" and order.status == SalesOrderStatus.DRAFT:
        if current_user.role not in (UserRole.ADMIN, UserRole.SALES):
            flash("You do not have permission to edit orders.", "danger")
            return redirect(url_for("sales.view_order", order_id=order_id))

        try:
            with db.session.begin_nested():
                order.customer_id = int(request.form["customer_id"])
                order.customer_address = request.form.get("customer_address", "").strip()
                order.sales_person_id = _resolve_sales_person_id(
                    request.form.get("sales_person_id")
                )
                order.recalculate_total()
            db.session.commit()
            flash("Order updated.", "success")
        except (KeyError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

        return redirect(url_for("sales.view_order", order_id=order_id))

    return render_template(
        "sales/form.html",
        order=order,
        customers=customers,
        salespeople=salespeople,
        products=products,
        readonly=readonly,
    )


# ---------------------------------------------------------------------------
# Confirm – triggers inventory reservation (POST only, idempotent-ish)
# ---------------------------------------------------------------------------


@sales_bp.route("/<int:order_id>/confirm", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def confirm_order(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if not order:
        flash("Sales order not found.", "danger")
        return redirect(url_for("sales.list_orders"))
    assert_sales_order_access(order)

    try:
        flags = confirm_sales_order(sales_order=order, user_id=current_user.id)
        flash(f"Order {order.so_number} confirmed.", "success")
        for flag in flags:
            flash(
                f"Procurement required for '{flag.product_name}': "
                f"shortfall of {flag.shortfall} ({flag.procurement_type.upper()} needed).",
                "warning",
            )
        for msg in getattr(order, "_auto_procurement_messages", []):
            flash(msg, "info")
    except InsufficientStockError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except (InvalidStatusTransition, SalesOrderError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("sales.view_order", order_id=order_id))


# ---------------------------------------------------------------------------
# Deliver – post delivered quantities from the form
# ---------------------------------------------------------------------------


@sales_bp.route("/<int:order_id>/deliver", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def deliver_order(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if not order:
        flash("Sales order not found.", "danger")
        return redirect(url_for("sales.list_orders"))
    assert_sales_order_access(order)

    try:
        # Collect target delivered qty for every line (form input or existing DB value).
        line_deliveries = {}
        for line in order.lines:
            field_name = f"delivered_qty_{line.id}"
            if field_name in request.form:
                line_deliveries[line.id] = Decimal(request.form[field_name])
            else:
                line_deliveries[line.id] = line.delivered_qty

        deliver_sales_order(
            sales_order=order,
            line_deliveries=line_deliveries,
            user_id=current_user.id,
        )
        flash(
            f"Delivery recorded. Order status: {order.status_label}.",
            "success",
        )
        if order.status == SalesOrderStatus.PARTIALLY_DELIVERED:
            pending = sum(
                1 for ln in order.lines if ln.delivered_qty < ln.ordered_qty
            )
            flash(
                f"{pending} line(s) still pending – set delivered qty equal to "
                f"ordered qty on all lines for Fully Delivered.",
                "info",
            )
    except InsufficientStockError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except (InvalidStatusTransition, SalesOrderError, InvalidOperation) as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("sales.view_order", order_id=order_id))


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@sales_bp.route("/<int:order_id>/cancel", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def cancel_order(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if not order:
        flash("Sales order not found.", "danger")
        return redirect(url_for("sales.list_orders"))
    assert_sales_order_access(order)

    try:
        cancel_sales_order(sales_order=order, user_id=current_user.id)
        flash(f"Order {order.so_number} cancelled.", "info")
    except InvalidStatusTransition as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("sales.view_order", order_id=order_id))


# ---------------------------------------------------------------------------
# Audit logs for a specific sales order (wireframe "Logs" button)
# ---------------------------------------------------------------------------


@sales_bp.route("/<int:order_id>/logs")
@login_required
def order_logs(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if not order:
        flash("Sales order not found.", "danger")
        return redirect(url_for("sales.list_orders"))
    assert_sales_order_access(order)

    logs = (
        AuditLog.query.filter(
            AuditLog.module == "sales",
            AuditLog.record_type == "SalesOrder",
            AuditLog.record_id == order_id,
        )
        .order_by(AuditLog.created_at.desc())
        .all()
    )

    # Also include line-level logs for this order's line IDs.
    line_ids = [line.id for line in order.lines]
    if line_ids:
        line_logs = (
            AuditLog.query.filter(
                AuditLog.module == "sales",
                AuditLog.record_type == "SalesOrderLine",
                AuditLog.record_id.in_(line_ids),
            )
            .order_by(AuditLog.created_at.desc())
            .all()
        )
        logs = sorted(logs + line_logs, key=lambda l: l.created_at, reverse=True)

    return render_template("sales/logs.html", order=order, logs=logs)

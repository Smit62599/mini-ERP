"""Purchase order routes – list, manual create, edit, and stock receiving."""

from decimal import InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from extensions import db
from models import Product, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus, UserRole, Vendor
from routes.decorators import role_required
from services.purchase_service import (
    PurchaseOrderError,
    complete_purchase_order,
    create_purchase_order,
    update_purchase_order_lines,
)

purchase_bp = Blueprint("purchase", __name__, url_prefix="/purchase")


@purchase_bp.route("/orders")
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def list_orders():
    orders = (
        PurchaseOrder.query.options(
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.product),
        )
        .order_by(PurchaseOrder.created_at.desc())
        .all()
    )
    return render_template("purchase/list.html", orders=orders)


@purchase_bp.route("/orders/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def new_order():
    vendors = Vendor.query.order_by(Vendor.name).all()
    products = Product.query.order_by(Product.name).all()

    if request.method == "POST":
        try:
            with db.session.begin_nested():
                po = create_purchase_order(
                    vendor_id=int(request.form["vendor_id"]),
                    product_ids=request.form.getlist("product_id"),
                    quantities=request.form.getlist("ordered_qty"),
                    user_id=current_user.id,
                )
            db.session.commit()
            flash(f"Purchase Order {po.po_number} created.", "success")
            return redirect(url_for("purchase.edit_order", order_id=po.id))
        except (PurchaseOrderError, InvalidOperation, ValueError, KeyError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "purchase/form.html",
        order=None,
        vendors=vendors,
        products=products,
        readonly=False,
    )


@purchase_bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES)
def edit_order(order_id: int):
    order = (
        PurchaseOrder.query.options(
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.product),
        )
        .filter_by(id=order_id)
        .first()
    )
    if not order:
        flash("Purchase order not found.", "danger")
        return redirect(url_for("purchase.list_orders"))

    vendors = Vendor.query.order_by(Vendor.name).all()
    products = Product.query.order_by(Product.name).all()
    readonly = order.is_locked
    can_receive = order.status not in (
        PurchaseOrderStatus.DONE,
        PurchaseOrderStatus.CANCELLED,
    )

    if request.method == "POST":
        action = request.form.get("action", "save")
        try:
            if action == "complete":
                if order.is_locked:
                    raise PurchaseOrderError("Purchase order is already closed.")
                with db.session.begin_nested():
                    update_purchase_order_lines(
                        po=order,
                        product_ids=request.form.getlist("product_id"),
                        ordered_quantities=request.form.getlist("ordered_qty"),
                        received_quantities=request.form.getlist("received_qty"),
                        user_id=current_user.id,
                    )
                    complete_purchase_order(po=order, user_id=current_user.id)
                db.session.commit()
                flash(f"PO {order.po_number} completed – stock received.", "success")
            else:
                if order.is_locked:
                    raise PurchaseOrderError("Cannot modify a Done or Cancelled PO.")
                with db.session.begin_nested():
                    order.vendor_id = int(request.form["vendor_id"])
                    update_purchase_order_lines(
                        po=order,
                        product_ids=request.form.getlist("product_id"),
                        ordered_quantities=request.form.getlist("ordered_qty"),
                        received_quantities=request.form.getlist("received_qty"),
                        user_id=current_user.id,
                    )
                db.session.commit()
                flash("Purchase order saved.", "success")
            return redirect(url_for("purchase.edit_order", order_id=order.id))
        except PurchaseOrderError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "purchase/form.html",
        order=order,
        vendors=vendors,
        products=products,
        readonly=readonly,
        can_receive=can_receive,
    )

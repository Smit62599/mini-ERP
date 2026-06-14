"""Product master data routes."""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import BillOfMaterials, ProcurementType, Product, UserRole, Vendor
from routes.decorators import role_required
from services.product_service import ProductError, create_product, delete_product, update_product

products_bp = Blueprint("products", __name__, url_prefix="/products")

LIST_PER_PAGE = 20


@products_bp.route("/")
@login_required
@role_required(UserRole.ADMIN, UserRole.SALES, UserRole.MANUFACTURING)
def list_products():
    page = request.args.get("page", 1, type=int)
    pagination = (
        Product.query.order_by(Product.name)
        .paginate(page=page, per_page=LIST_PER_PAGE, error_out=False)
    )
    return render_template(
        "products/list.html",
        pagination=pagination,
        pagination_args={},
    )


@products_bp.route("/new", methods=["GET", "POST"])
@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def product_form(product_id: int | None = None):
    product = db.session.get(Product, product_id) if product_id else None
    vendors = Vendor.query.order_by(Vendor.name).all()
    boms = BillOfMaterials.query.order_by(BillOfMaterials.bom_number).all()

    if request.method == "POST":
        try:
            name = request.form["name"]
            sales_price = Decimal(request.form["sales_price"])
            cost_price = Decimal(request.form["cost_price"])
            on_hand = Decimal(request.form.get("on_hand_qty") or "0")
            procure = request.form.get("procure_on_demand") == "on"
            proc_type_raw = request.form.get("procurement_type") or None
            proc_type = ProcurementType(proc_type_raw) if proc_type_raw else None
            vendor_id = int(request.form["vendor_id"]) if request.form.get("vendor_id") else None
            bom_id = int(request.form["bom_id"]) if request.form.get("bom_id") else None

            with db.session.begin_nested():
                if product:
                    update_product(
                        product=product,
                        name=name,
                        sales_price=sales_price,
                        cost_price=cost_price,
                        procure_on_demand=procure,
                        procurement_type=proc_type,
                        vendor_id=vendor_id,
                        bom_id=bom_id,
                        user_id=current_user.id,
                    )
                else:
                    product = create_product(
                        name=name,
                        sales_price=sales_price,
                        cost_price=cost_price,
                        on_hand_qty=on_hand,
                        procure_on_demand=procure,
                        procurement_type=proc_type,
                        vendor_id=vendor_id,
                        bom_id=bom_id,
                        user_id=current_user.id,
                    )
            db.session.commit()
            flash(f"Product '{product.name}' saved.", "success")
            return redirect(url_for("products.list_products"))
        except (ProductError, InvalidOperation, ValueError, KeyError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "products/form.html",
        product=product,
        vendors=vendors,
        boms=boms,
        procurement_types=list(ProcurementType),
    )


@products_bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN)
def delete_product_route(product_id: int):
    product = db.session.get(Product, product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products.list_products"))
    try:
        name = product.name
        with db.session.begin_nested():
            delete_product(product=product, user_id=current_user.id)
        db.session.commit()
        flash(f"Product '{name}' deleted. Historical order lines preserved.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Cannot delete product: {exc}", "danger")
    return redirect(url_for("products.list_products"))

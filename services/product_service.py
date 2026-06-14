"""
Product master data business logic.
"""

from decimal import Decimal

from extensions import db
from models import ProcurementType, Product, Vendor
from services.audit_service import log_audit, log_field_change


class ProductError(Exception):
    pass


def create_product(
    *,
    name: str,
    sales_price: Decimal,
    cost_price: Decimal,
    on_hand_qty: Decimal,
    procure_on_demand: bool,
    procurement_type: ProcurementType | None,
    vendor_id: int | None,
    bom_id: int | None,
    user_id: int,
) -> Product:
    product = Product(
        name=name.strip(),
        sales_price=sales_price,
        cost_price=cost_price,
        on_hand_qty=on_hand_qty,
        reserved_qty=Decimal("0"),
        procure_on_demand=procure_on_demand,
        procurement_type=procurement_type if procure_on_demand else None,
        vendor_id=vendor_id if procurement_type == ProcurementType.PURCHASE else None,
        bom_id=bom_id if procurement_type == ProcurementType.MANUFACTURE else None,
    )
    db.session.add(product)
    db.session.flush()

    log_audit(
        user_id=user_id,
        module="products",
        record_type="Product",
        record_id=product.id,
        action="created",
        field_changed="name",
        old_value=None,
        new_value=product.name,
    )
    return product


def update_product(
    *,
    product: Product,
    name: str,
    sales_price: Decimal,
    cost_price: Decimal,
    procure_on_demand: bool,
    procurement_type: ProcurementType | None,
    vendor_id: int | None,
    bom_id: int | None,
    user_id: int,
) -> Product:
    changes = {
        "name": (product.name, name.strip()),
        "sales_price": (product.sales_price, sales_price),
        "cost_price": (product.cost_price, cost_price),
        "procure_on_demand": (product.procure_on_demand, procure_on_demand),
    }
    product.name = name.strip()
    product.sales_price = sales_price
    product.cost_price = cost_price
    product.procure_on_demand = procure_on_demand
    product.procurement_type = procurement_type if procure_on_demand else None
    product.vendor_id = vendor_id if product.procurement_type == ProcurementType.PURCHASE else None
    product.bom_id = bom_id if product.procurement_type == ProcurementType.MANUFACTURE else None

    for field, (old, new) in changes.items():
        if str(old) != str(new):
            log_field_change(
                user_id=user_id,
                module="products",
                record_type="Product",
                record_id=product.id,
                field_name=field,
                old_value=old,
                new_value=new,
            )
    return product


def delete_product(*, product: Product, user_id: int) -> None:
    """
    Remove product from master data.

    Historical line FKs (SO/PO/BoM/MO) are SET NULL at the database layer
    so audit history remains intact without template crashes.
    """
    name = product.name
    product_id = product.id
    db.session.delete(product)
    db.session.flush()
    log_audit(
        user_id=user_id,
        module="products",
        record_type="Product",
        record_id=product_id,
        action="deleted",
        field_changed="name",
        old_value=name,
        new_value=None,
    )

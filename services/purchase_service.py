"""
Purchase order business logic – manual creation, editing, and stock receiving.
"""

from decimal import Decimal

from extensions import db
from models import (
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Vendor,
)
from services.audit_service import log_audit, log_field_change, log_status_change
from services.inventory_service import lock_products_for_update, receive_purchase_stock


class PurchaseOrderError(Exception):
    pass


def generate_po_number() -> str:
    last = db.session.query(PurchaseOrder.po_number).order_by(PurchaseOrder.id.desc()).first()
    if last and last[0]:
        num = int(last[0].split("-")[1]) + 1
    else:
        num = 1
    return f"PO-{num:06d}"


def _parse_lines(product_ids: list[str], quantities: list[str]) -> list[tuple[int, Decimal]]:
    lines: list[tuple[int, Decimal]] = []
    for pid, qty in zip(product_ids, quantities):
        if pid and qty:
            qty_dec = Decimal(str(qty))
            if qty_dec <= 0:
                raise PurchaseOrderError("Ordered quantity must be greater than zero.")
            lines.append((int(pid), qty_dec))
    if not lines:
        raise PurchaseOrderError("Add at least one product line.")
    return lines


def create_purchase_order(
    *,
    vendor_id: int,
    product_ids: list[str],
    quantities: list[str],
    user_id: int,
) -> PurchaseOrder:
    vendor = db.session.get(Vendor, vendor_id)
    if not vendor:
        raise PurchaseOrderError("Vendor not found.")

    po = PurchaseOrder(
        po_number=generate_po_number(),
        vendor_id=vendor.id,
        status=PurchaseOrderStatus.DRAFT,
    )
    db.session.add(po)
    db.session.flush()

    for product_id, qty in _parse_lines(product_ids, quantities):
        if not db.session.get(Product, product_id):
            raise PurchaseOrderError(f"Product {product_id} not found.")
        db.session.add(
            PurchaseOrderLine(
                purchase_order_id=po.id,
                product_id=product_id,
                quantity=qty,
                received_qty=Decimal("0"),
            )
        )

    log_status_change(
        user_id=user_id,
        module="purchase",
        record_type="PurchaseOrder",
        record_id=po.id,
        old_status="",
        new_status=PurchaseOrderStatus.DRAFT.value,
    )
    return po


def create_purchase_order_from_shortfall(
    *,
    product: Product,
    quantity: Decimal,
    user_id: int,
    sales_order_id: int,
) -> PurchaseOrder:
    """Create a draft PO for a procure-on-demand purchase product."""
    vendor = db.session.get(Vendor, product.vendor_id)
    if not vendor:
        raise PurchaseOrderError(f"Product '{product.name}' has no vendor.")

    po = PurchaseOrder(
        po_number=generate_po_number(),
        vendor_id=vendor.id,
        status=PurchaseOrderStatus.DRAFT,
    )
    db.session.add(po)
    db.session.flush()

    db.session.add(
        PurchaseOrderLine(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity=quantity,
            received_qty=Decimal("0"),
        )
    )

    log_status_change(
        user_id=user_id,
        module="purchase",
        record_type="PurchaseOrder",
        record_id=po.id,
        old_status="",
        new_status=PurchaseOrderStatus.DRAFT.value,
    )
    log_audit(
        user_id=user_id,
        module="purchase",
        record_type="PurchaseOrder",
        record_id=po.id,
        action="auto_generated",
        field_changed="sales_order_id",
        old_value=None,
        new_value=str(sales_order_id),
    )
    return po


def update_purchase_order_lines(
    *,
    po: PurchaseOrder,
    product_ids: list[str],
    ordered_quantities: list[str],
    received_quantities: list[str],
    user_id: int,
) -> None:
    if po.is_locked:
        raise PurchaseOrderError("Cannot edit a Done or Cancelled purchase order.")

    po.lines.clear()
    db.session.flush()
    line_count = 0
    for pid, oq, rq in zip(product_ids, ordered_quantities, received_quantities):
        if not pid or not oq:
            continue
        ordered = Decimal(str(oq))
        received = Decimal(str(rq or "0"))
        if ordered <= 0:
            raise PurchaseOrderError("Ordered quantity must be greater than zero.")
        if received < 0:
            raise PurchaseOrderError("Received quantity cannot be negative.")
        if received > ordered:
            raise PurchaseOrderError("Received quantity cannot exceed ordered quantity.")
        po.lines.append(
            PurchaseOrderLine(
                product_id=int(pid),
                quantity=ordered,
                received_qty=received,
            )
        )
        line_count += 1

    if line_count == 0:
        raise PurchaseOrderError("Add at least one product line.")

    log_field_change(
        user_id=user_id,
        module="purchase",
        record_type="PurchaseOrder",
        record_id=po.id,
        field_name="lines",
        old_value=None,
        new_value=f"{line_count} line(s)",
    )


def complete_purchase_order(*, po: PurchaseOrder, user_id: int) -> None:
    """
    Mark PO Done and post stock receipts (ACID).

    Adds each line's received_qty to PRODUCTS.on_hand_qty and writes
    STOCK_LEDGER rows with transaction_type purchase_receipt.
    """
    if po.is_locked:
        raise PurchaseOrderError("Purchase order is already closed.")

    lines = PurchaseOrderLine.query.filter_by(purchase_order_id=po.id).all()
    if not lines:
        raise PurchaseOrderError("Cannot complete a PO with no lines.")

    with db.session.begin_nested():
        product_ids = [ln.product_id for ln in lines if ln.product_id]
        locked = lock_products_for_update(product_ids)

        for line in lines:
            if not line.product_id:
                raise PurchaseOrderError(
                    "Cannot receive stock for a line with a deleted product."
                )
            received = Decimal(str(line.received_qty))
            if received <= 0:
                raise PurchaseOrderError(
                    "Enter received quantity greater than zero on all lines before completing."
                )
            if received > line.quantity:
                raise PurchaseOrderError("Received quantity exceeds ordered quantity.")

            product = locked[line.product_id]
            receive_purchase_stock(
                product=product,
                quantity=received,
                purchase_order_id=po.id,
                user_id=user_id,
            )

        old = po.status.value
        po.status = PurchaseOrderStatus.DONE
        log_status_change(
            user_id=user_id,
            module="purchase",
            record_type="PurchaseOrder",
            record_id=po.id,
            old_status=old,
            new_status=PurchaseOrderStatus.DONE.value,
        )

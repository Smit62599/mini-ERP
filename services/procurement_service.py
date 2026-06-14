"""
Procure-on-demand auto-trigger – creates PO or MO when SO confirm causes shortfall.
"""

from decimal import Decimal

from extensions import db
from models import ProcurementType, Product
from services.audit_service import log_audit
from services.manufacturing_service import create_manufacturing_order_from_shortfall
from services.purchase_service import create_purchase_order_from_shortfall


def trigger_auto_procurement(
    *,
    product: Product,
    shortage_qty: Decimal,
    user_id: int,
    sales_order_id: int,
) -> str | None:
    """
    Auto-generate a draft PO or MO when procure_on_demand is True and stock
    is insufficient after SO confirmation.

    Returns a human-readable summary string for flash messages, or None.
    """
    if shortage_qty <= 0:
        return None

    if not product.procure_on_demand or not product.procurement_type:
        return None

    if product.procurement_type == ProcurementType.PURCHASE:
        if not product.vendor_id:
            log_audit(
                user_id=user_id,
                module="inventory",
                record_type="Product",
                record_id=product.id,
                action="auto_procurement_failed",
                field_changed="vendor_id",
                old_value=None,
                new_value=f"No vendor configured for SO#{sales_order_id}",
            )
            return f"Auto-PO skipped for '{product.name}': no vendor configured."

        po = create_purchase_order_from_shortfall(
            product=product,
            quantity=shortage_qty,
            user_id=user_id,
            sales_order_id=sales_order_id,
        )
        return f"Auto-generated Purchase Order {po.po_number} for {shortage_qty} × {product.name}."

    if product.procurement_type == ProcurementType.MANUFACTURE:
        if not product.bom_id:
            log_audit(
                user_id=user_id,
                module="inventory",
                record_type="Product",
                record_id=product.id,
                action="auto_procurement_failed",
                field_changed="bom_id",
                old_value=None,
                new_value=f"No BoM configured for SO#{sales_order_id}",
            )
            return f"Auto-MO skipped for '{product.name}': no BoM configured."

        mo = create_manufacturing_order_from_shortfall(
            product=product,
            quantity=int(shortage_qty.to_integral_value()),
            user_id=user_id,
            sales_order_id=sales_order_id,
        )
        return f"Auto-generated Manufacturing Order {mo.mo_number} for {shortage_qty} × {product.name}."

    return None

"""
Inventory service – stock checks, reservations, ledger writes.

All on_hand_qty mutations MUST go through these helpers so every physical
movement appends an immutable StockLedger row (ACID audit trail).
"""

from decimal import Decimal

from sqlalchemy import select

from extensions import db
from models import (
    Product,
    StockLedger,
    StockReferenceType,
    StockTransactionType,
)
from services.audit_service import log_audit


class InsufficientStockError(Exception):
    """Raised when free_to_use_qty < required_qty and procure_on_demand is False."""

    def __init__(self, product_name: str, required: Decimal, available: Decimal):
        self.product_name = product_name
        self.required = required
        self.available = available
        super().__init__(
            f"Insufficient stock for '{product_name}': "
            f"need {required}, available {available}"
        )


class ProcurementRequiredFlag:
    """Returned when SO confirm hits a shortfall with procure_on_demand enabled."""

    def __init__(self, product: Product, shortfall: Decimal):
        self.product_id = product.id
        self.product_name = product.name
        self.shortfall = shortfall
        self.procurement_type = (
            product.procurement_type.value if product.procurement_type else "unknown"
        )


def lock_products_for_update(product_ids: list[int]) -> dict[int, Product]:
    """PostgreSQL SELECT … FOR UPDATE in ascending ID order (deadlock-safe)."""
    if not product_ids:
        return {}

    sorted_ids = sorted(set(product_ids))
    stmt = (
        select(Product)
        .where(Product.id.in_(sorted_ids))
        .order_by(Product.id)
        .with_for_update()
    )
    products = db.session.scalars(stmt).all()
    return {p.id: p for p in products}


def check_availability(product: Product, required_qty: Decimal) -> bool:
    """Confirm-time check: unreserved stock must cover the order line."""
    return product.free_to_use_qty >= required_qty


def check_delivery_stock(product: Product, quantity: Decimal) -> bool:
    """
    Delivery-time check: physical warehouse stock must cover the shipment.

    At delivery, qty is already reserved, so free_to_use_qty is often zero even
    though on_hand_qty holds the goods. Never use free_to_use for this check.
    """
    return product.on_hand_qty >= quantity


def _append_ledger(
    *,
    product: Product,
    transaction_type: StockTransactionType,
    reference_type: StockReferenceType,
    reference_id: int,
    quantity_change: Decimal,
    user_id: int,
) -> StockLedger:
    """Single choke-point for all StockLedger inserts."""
    ledger = StockLedger(
        product_id=product.id,
        transaction_type=transaction_type,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=quantity_change,
        resulting_on_hand_qty=product.on_hand_qty,
        created_by=user_id,
    )
    db.session.add(ledger)
    return ledger


def adjust_on_hand_qty(
    *,
    product: Product,
    quantity_change: Decimal,
    transaction_type: StockTransactionType,
    reference_type: StockReferenceType,
    reference_id: int,
    user_id: int,
) -> StockLedger:
    """
    Mutate on_hand_qty and write ledger – never call product.on_hand_qty = … directly.
    """
    product.on_hand_qty = product.on_hand_qty + quantity_change
    return _append_ledger(
        product=product,
        transaction_type=transaction_type,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=quantity_change,
        user_id=user_id,
    )


def reserve_stock(
    *,
    product: Product,
    quantity: Decimal,
    reference_type: StockReferenceType,
    reference_id: int,
    user_id: int,
) -> StockLedger:
    product.reserved_qty = product.reserved_qty + quantity
    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.RESERVE,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=quantity,
        user_id=user_id,
    )


def deliver_stock(
    *,
    product: Product,
    quantity: Decimal,
    reference_type: StockReferenceType,
    reference_id: int,
    user_id: int,
) -> StockLedger:
    """
    Process a delivery: decrease on_hand and release reservation.

    Validates against physical on_hand_qty (not free_to_use_qty) because
    reserved stock is already earmarked and no longer counts as 'free'.
    """
    if product.on_hand_qty < quantity:
        raise InsufficientStockError(
            product.name, quantity, product.on_hand_qty
        )

    product.on_hand_qty = product.on_hand_qty - quantity
    product.reserved_qty = product.reserved_qty - quantity

    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.DELIVERY,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=-quantity,
        user_id=user_id,
    )


def unreserve_stock(
    *,
    product: Product,
    quantity: Decimal,
    reference_type: StockReferenceType,
    reference_id: int,
    user_id: int,
) -> StockLedger | None:
    quantity = min(Decimal(str(quantity)), Decimal(str(product.reserved_qty)))
    if quantity <= 0:
        return None

    product.reserved_qty = product.reserved_qty - quantity
    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.UNRESERVE,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=-quantity,
        user_id=user_id,
    )


def consume_production_stock(
    *,
    product: Product,
    quantity: Decimal,
    manufacturing_order_id: int,
    user_id: int,
) -> StockLedger:
    """Deduct component on_hand and reserved during MO produce."""
    product.on_hand_qty = product.on_hand_qty - quantity
    product.reserved_qty = max(product.reserved_qty - quantity, Decimal("0"))
    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.PRODUCTION_CONSUMPTION,
        reference_type=StockReferenceType.MANUFACTURING_ORDER,
        reference_id=manufacturing_order_id,
        quantity_change=-quantity,
        user_id=user_id,
    )


def receive_production_output(
    *,
    product: Product,
    quantity: Decimal,
    manufacturing_order_id: int,
    user_id: int,
) -> StockLedger:
    """Add finished goods on_hand during MO produce."""
    product.on_hand_qty = product.on_hand_qty + quantity
    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.PRODUCTION_RECEIPT,
        reference_type=StockReferenceType.MANUFACTURING_ORDER,
        reference_id=manufacturing_order_id,
        quantity_change=quantity,
        user_id=user_id,
    )


def receive_purchase_stock(
    *,
    product: Product,
    quantity: Decimal,
    purchase_order_id: int,
    user_id: int,
) -> StockLedger:
    """Add purchased stock to on_hand with immutable ledger row."""
    product.on_hand_qty = product.on_hand_qty + quantity
    return _append_ledger(
        product=product,
        transaction_type=StockTransactionType.PURCHASE_RECEIPT,
        reference_type=StockReferenceType.PURCHASE_ORDER,
        reference_id=purchase_order_id,
        quantity_change=quantity,
        user_id=user_id,
    )


def log_procurement_required(
    *,
    product: Product,
    shortfall: Decimal,
    sales_order_id: int,
    user_id: int,
) -> ProcurementRequiredFlag:
    flag = ProcurementRequiredFlag(product, shortfall)
    log_audit(
        user_id=user_id,
        module="inventory",
        record_type="Product",
        record_id=product.id,
        action="procurement_required",
        field_changed="free_to_use_qty",
        old_value=str(product.free_to_use_qty),
        new_value=(
            f"SHORTFALL {shortfall} – "
            f"{'PO' if product.procurement_type and product.procurement_type.value == 'purchase' else 'MO'} "
            f"required for SO#{sales_order_id}"
        ),
    )
    return flag

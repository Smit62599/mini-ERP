"""
Sales order business logic.

All state transitions (confirm, deliver, cancel) run inside explicit
PostgreSQL transactions with nested savepoints so a partial failure
rolls back the entire operation atomically.
"""

from decimal import Decimal

from extensions import db
from models import (
    Customer,
    Product,
    SalesOrder,
    SalesOrderLine,
    SalesOrderStatus,
    StockReferenceType,
)
from services.audit_service import log_field_change, log_status_change
from services.inventory_service import (
    InsufficientStockError,
    ProcurementRequiredFlag,
    check_availability,
    deliver_stock,
    lock_products_for_update,
    log_procurement_required,
    check_delivery_stock,
    reserve_stock,
    unreserve_stock,
)
from services.procurement_service import trigger_auto_procurement


class SalesOrderError(Exception):
    """Base exception for invalid sales order operations."""


class InvalidStatusTransition(SalesOrderError):
    pass


# ---------------------------------------------------------------------------
# SO number generation
# ---------------------------------------------------------------------------


def generate_so_number() -> str:
    """
    Produce the next sequential reference (SO-000001, SO-000002, …).

    Uses MAX(so_number) inside the current transaction.  For production
    workloads you'd switch to a PostgreSQL SEQUENCE, but this is sufficient
    for the hackathon slice.
    """
    last = db.session.query(SalesOrder.so_number).order_by(SalesOrder.id.desc()).first()
    if last and last[0]:
        num = int(last[0].split("-")[1]) + 1
    else:
        num = 1
    return f"SO-{num:06d}"


# ---------------------------------------------------------------------------
# Create / update helpers
# ---------------------------------------------------------------------------


def create_sales_order(
    *,
    customer_id: int,
    customer_address: str | None,
    sales_person_id: int,
    user_id: int,
) -> SalesOrder:
    """Create a new Draft sales order and log the creation."""
    order = SalesOrder(
        so_number=generate_so_number(),
        customer_id=customer_id,
        customer_address=customer_address,
        sales_person_id=sales_person_id,
        status=SalesOrderStatus.DRAFT,
        total_amount=0,
    )
    db.session.add(order)
    db.session.flush()  # Assign PK before audit log

    log_status_change(
        user_id=user_id,
        module="sales",
        record_type="SalesOrder",
        record_id=order.id,
        old_status="",
        new_status=SalesOrderStatus.DRAFT.value,
    )
    return order


def add_line_to_order(
    *,
    sales_order: SalesOrder,
    product_id: int,
    ordered_qty: Decimal,
    user_id: int,
) -> SalesOrderLine:
    """
    Add a product line with a price snapshot.

    CRITICAL: sales_price is copied from Product.sales_price at this
    moment.  Future price changes on the product master do NOT affect
    this line – that is intentional ERP price-locking behaviour.
    """
    if sales_order.status != SalesOrderStatus.DRAFT:
        raise SalesOrderError("Lines can only be added to Draft orders.")

    product = db.session.get(Product, product_id)
    if not product:
        raise SalesOrderError(f"Product {product_id} not found.")

    line = SalesOrderLine(
        sales_order_id=sales_order.id,
        product_id=product_id,
        ordered_qty=ordered_qty,
        delivered_qty=Decimal("0"),
        sales_price=product.sales_price,  # <-- PRICE SNAPSHOT
    )
    db.session.add(line)
    db.session.flush()

    log_field_change(
        user_id=user_id,
        module="sales",
        record_type="SalesOrderLine",
        record_id=line.id,
        field_name="product_id",
        old_value=None,
        new_value=f"{product.name} x {ordered_qty} @ {product.sales_price}",
    )

    sales_order.recalculate_total()
    return line


# ---------------------------------------------------------------------------
# Confirm – inventory reservation with concurrency guard
# ---------------------------------------------------------------------------


def confirm_sales_order(*, sales_order: SalesOrder, user_id: int) -> list[ProcurementRequiredFlag]:
    """
    Transition Draft → Confirmed with strict inventory checks.

    Transaction flow:
      1. Validate status is Draft.
      2. Lock all products referenced by order lines (FOR UPDATE).
      3. For each line:
         a. If free_to_use >= ordered_qty → reserve stock.
         b. If insufficient AND procure_on_demand → flag PO/MO, still confirm.
         c. If insufficient AND NOT procure_on_demand → abort entire transaction.
      4. Update status, write audit log, commit.
    """
    if sales_order.status != SalesOrderStatus.DRAFT:
        raise InvalidStatusTransition(
            f"Cannot confirm order in '{sales_order.status.value}' status."
        )

    if not sales_order.lines:
        raise SalesOrderError("Cannot confirm an order with no line items.")

    procurement_flags: list[ProcurementRequiredFlag] = []
    auto_procurement_messages: list[str] = []

    # Nested savepoint – if anything fails, the whole confirm rolls back.
    with db.session.begin_nested():
        product_ids = [line.product_id for line in sales_order.lines if line.product_id]
        locked_products = lock_products_for_update(product_ids)

        for line in sales_order.lines:
            if not line.product_id:
                raise SalesOrderError(
                    "Cannot confirm: order contains a line with a deleted product."
                )
            product = locked_products[line.product_id]
            required = Decimal(str(line.ordered_qty))

            if check_availability(product, required):
                reserve_stock(
                    product=product,
                    quantity=required,
                    reference_type=StockReferenceType.SALES_ORDER,
                    reference_id=sales_order.id,
                    user_id=user_id,
                )
                line.reserved_qty = required
            elif product.procure_on_demand:
                shortfall = required - max(product.free_to_use_qty, Decimal("0"))
                flag = log_procurement_required(
                    product=product,
                    shortfall=shortfall,
                    sales_order_id=sales_order.id,
                    user_id=user_id,
                )
                procurement_flags.append(flag)

                available = max(product.free_to_use_qty, Decimal("0"))
                if available > 0:
                    reserve_stock(
                        product=product,
                        quantity=available,
                        reference_type=StockReferenceType.SALES_ORDER,
                        reference_id=sales_order.id,
                        user_id=user_id,
                    )
                line.reserved_qty = available

                # Auto-generate draft PO or MO for the exact shortage.
                if shortfall > 0:
                    msg = trigger_auto_procurement(
                        product=product,
                        shortage_qty=shortfall,
                        user_id=user_id,
                        sales_order_id=sales_order.id,
                    )
                    if msg:
                        auto_procurement_messages.append(msg)
            else:
                raise InsufficientStockError(
                    product.name, required, product.free_to_use_qty
                )

        old_status = sales_order.status.value
        sales_order.status = SalesOrderStatus.CONFIRMED
        sales_order.recalculate_total()

        log_status_change(
            user_id=user_id,
            module="sales",
            record_type="SalesOrder",
            record_id=sales_order.id,
            old_status=old_status,
            new_status=SalesOrderStatus.CONFIRMED.value,
        )

    db.session.commit()
    sales_order._auto_procurement_messages = auto_procurement_messages  # type: ignore[attr-defined]
    return procurement_flags


# ---------------------------------------------------------------------------
# Deliver – post delivered quantities and move physical stock
# ---------------------------------------------------------------------------


def deliver_sales_order(
    *,
    sales_order: SalesOrder,
    line_deliveries: dict[int, Decimal],
    user_id: int,
) -> SalesOrderStatus:
    """
    Process delivery quantities and update order status.

    Args:
        line_deliveries: mapping of SalesOrderLine.id → target delivered_qty
            (absolute value from the form, NOT an increment).

    Status rules (wireframe):
      • Fully Delivered – every line has delivered_qty == ordered_qty
      • Partially Delivered – at least one line delivered but not all complete
    """
    allowed_statuses = (
        SalesOrderStatus.CONFIRMED,
        SalesOrderStatus.PARTIALLY_DELIVERED,
    )
    if sales_order.status not in allowed_statuses:
        raise InvalidStatusTransition(
            f"Cannot deliver order in '{sales_order.status.value}' status."
        )

    with db.session.begin_nested():
        product_ids = [line.product_id for line in sales_order.lines if line.product_id]
        locked_products = lock_products_for_update(product_ids)
        old_total = sales_order.total_amount
        any_increment = False

        for line in sales_order.lines:
            if line.id not in line_deliveries:
                continue

            # Form submits the desired absolute delivered quantity.
            target_delivered = Decimal(str(line_deliveries[line.id]))

            if target_delivered < line.delivered_qty:
                raise SalesOrderError(
                    f"Delivered qty cannot be reduced below {line.delivered_qty} "
                    f"for line {line.id}."
                )
            if target_delivered > line.ordered_qty:
                raise SalesOrderError(
                    f"Delivered qty ({target_delivered}) exceeds ordered qty "
                    f"({line.ordered_qty}) for line {line.id}."
                )

            increment = target_delivered - line.delivered_qty
            if increment <= 0:
                continue

            any_increment = True

            if not line.product_id:
                raise SalesOrderError(
                    f"Cannot deliver line {line.id}: product was deleted from master data."
                )
            product = locked_products[line.product_id]

            # Delivery validates physical stock – reserved qty is already committed.
            if not check_delivery_stock(product, increment):
                raise InsufficientStockError(
                    product.name, increment, product.on_hand_qty
                )

            deliver_stock(
                product=product,
                quantity=increment,
                reference_type=StockReferenceType.SALES_ORDER,
                reference_id=sales_order.id,
                user_id=user_id,
            )

            old_delivered = line.delivered_qty
            line.delivered_qty = target_delivered
            # Reduce line-level reservation by the amount physically delivered.
            line.reserved_qty = max(line.reserved_qty - increment, Decimal("0"))

            log_field_change(
                user_id=user_id,
                module="sales",
                record_type="SalesOrderLine",
                record_id=line.id,
                field_name="delivered_qty",
                old_value=old_delivered,
                new_value=target_delivered,
            )

        if not any_increment:
            raise SalesOrderError(
                "No delivery recorded. Enter a delivered quantity greater than "
                "the current value, then click Deliver."
            )

        # Status: Fully Delivered when ALL lines match ordered qty.
        all_delivered = all(
            line.delivered_qty >= line.ordered_qty for line in sales_order.lines
        )
        any_delivered = any(line.delivered_qty > 0 for line in sales_order.lines)

        old_status = sales_order.status.value
        if all_delivered:
            new_status = SalesOrderStatus.DELIVERED
        elif any_delivered:
            new_status = SalesOrderStatus.PARTIALLY_DELIVERED
        else:
            new_status = sales_order.status

        sales_order.status = new_status
        sales_order.recalculate_total()

        if old_total != sales_order.total_amount:
            log_field_change(
                user_id=user_id,
                module="sales",
                record_type="SalesOrder",
                record_id=sales_order.id,
                field_name="total_amount",
                old_value=old_total,
                new_value=sales_order.total_amount,
            )

        if old_status != new_status.value:
            log_status_change(
                user_id=user_id,
                module="sales",
                record_type="SalesOrder",
                record_id=sales_order.id,
                old_status=old_status,
                new_status=new_status.value,
            )

    db.session.commit()
    return sales_order.status


# ---------------------------------------------------------------------------
# Cancel – release reservations and lock the order
# ---------------------------------------------------------------------------


def cancel_sales_order(*, sales_order: SalesOrder, user_id: int) -> None:
    """Cancel an order and unreserve any stock that was reserved on confirm."""
    if sales_order.status in (
        SalesOrderStatus.DELIVERED,
        SalesOrderStatus.CANCELLED,
    ):
        raise InvalidStatusTransition(
            f"Cannot cancel order in '{sales_order.status.value}' status."
        )

    with db.session.begin_nested():
        if sales_order.status in (
            SalesOrderStatus.CONFIRMED,
            SalesOrderStatus.PARTIALLY_DELIVERED,
        ):
            product_ids = [line.product_id for line in sales_order.lines if line.product_id]
            locked_products = lock_products_for_update(product_ids)

            for line in sales_order.lines:
                if line.reserved_qty <= 0:
                    continue
                if not line.product_id:
                    continue
                product = locked_products[line.product_id]
                unreserve_stock(
                    product=product,
                    quantity=line.reserved_qty,
                    reference_type=StockReferenceType.SALES_ORDER,
                    reference_id=sales_order.id,
                    user_id=user_id,
                )
                line.reserved_qty = Decimal("0")

        old_status = sales_order.status.value
        sales_order.status = SalesOrderStatus.CANCELLED

        log_status_change(
            user_id=user_id,
            module="sales",
            record_type="SalesOrder",
            record_id=sales_order.id,
            old_status=old_status,
            new_status=SalesOrderStatus.CANCELLED.value,
        )

    db.session.commit()

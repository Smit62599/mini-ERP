"""
Manufacturing order business logic – creation and state machine transitions.
"""

from datetime import datetime, timezone
from decimal import Decimal

from extensions import db
from models import (
    BillOfMaterials,
    ManufacturingOrder,
    ManufacturingOrderStatus,
    MOComponent,
    Product,
    User,
    UserRole,
    WorkOrder,
    WorkOrderStatus,
)
from services.audit_service import log_field_change, log_status_change
from services.inventory_service import (
    consume_production_stock,
    lock_products_for_update,
    receive_production_output,
    reserve_stock,
    unreserve_stock,
)
from models import StockReferenceType


class ManufacturingOrderError(Exception):
    pass


class InvalidMOTransition(ManufacturingOrderError):
    pass


def generate_mo_number() -> str:
    last = db.session.query(ManufacturingOrder.mo_number).order_by(ManufacturingOrder.id.desc()).first()
    if last and last[0]:
        try:
            num = int(last[0].split("-")[1]) + 1
        except (IndexError, ValueError):
            num = ManufacturingOrder.query.count() + 1
    else:
        num = 1
    return f"MO-{num:06d}"


def _scale_qty(bom_qty: Decimal, bom_base: int, mo_qty: int) -> Decimal:
    if bom_base <= 0:
        bom_base = 1
    return (bom_qty / Decimal(bom_base)) * Decimal(mo_qty)


def explode_bom_into_mo(*, mo: ManufacturingOrder, bom: BillOfMaterials) -> None:
    """Copy BoM template lines into MO components and work orders."""
    mo.components.clear()
    mo.work_orders.clear()

    for comp in bom.components:
        required = _scale_qty(comp.quantity_required, bom.quantity, mo.quantity)
        db.session.add(
            MOComponent(
                manufacturing_order_id=mo.id,
                component_product_id=comp.component_product_id,
                required_qty=required,
                consumed_qty=Decimal("0"),
            )
        )

    for op in bom.operations:
        expected = int(
            (Decimal(op.duration_minutes) / Decimal(bom.quantity)) * Decimal(mo.quantity)
        )
        db.session.add(
            WorkOrder(
                manufacturing_order_id=mo.id,
                operation_name=op.operation_name,
                work_center_id=op.work_center_id,
                expected_duration=max(expected, 1),
                real_duration=0,
                status=WorkOrderStatus.PENDING,
            )
        )


def create_manufacturing_order(
    *,
    finished_product_id: int,
    quantity: int,
    bom_id: int | None,
    assignee_id: int,
    user_id: int,
    component_rows: list[dict] | None = None,
    operation_rows: list[dict] | None = None,
) -> ManufacturingOrder:
    product = db.session.get(Product, finished_product_id)
    if not product:
        raise ManufacturingOrderError("Finished product does not exist.")
    if quantity <= 0:
        raise ManufacturingOrderError("Quantity must be greater than zero.")

    assignee = db.session.get(User, assignee_id)
    if not assignee:
        raise ManufacturingOrderError("Assignee does not exist.")

    bom = db.session.get(BillOfMaterials, bom_id) if bom_id else None
    if bom_id and not bom:
        raise ManufacturingOrderError("Bill of Materials not found.")

    order = ManufacturingOrder(
        mo_number=generate_mo_number(),
        finished_product_id=finished_product_id,
        quantity=quantity,
        bom_id=bom_id,
        assignee_id=assignee_id,
        status=ManufacturingOrderStatus.DRAFT,
    )
    db.session.add(order)
    db.session.flush()

    if component_rows or operation_rows:
        _apply_mo_lines_from_form(order, component_rows or [], operation_rows or [])
    elif bom:
        explode_bom_into_mo(mo=order, bom=bom)

    log_status_change(
        user_id=user_id,
        module="manufacturing",
        record_type="ManufacturingOrder",
        record_id=order.id,
        old_status="",
        new_status=ManufacturingOrderStatus.DRAFT.value,
    )
    return order


def create_manufacturing_order_from_shortfall(
    *,
    product: Product,
    quantity: int,
    user_id: int,
    sales_order_id: int,
) -> ManufacturingOrder:
    """Auto-generate draft MO from SO procure-on-demand hook."""
    operator = (
        User.query.filter(User.role == UserRole.MANUFACTURING)
        .order_by(User.id)
        .first()
    )
    if not operator:
        raise ManufacturingOrderError("No manufacturing operator exists for auto-MO.")

    order = create_manufacturing_order(
        finished_product_id=product.id,
        quantity=max(quantity, 1),
        bom_id=product.bom_id,
        assignee_id=operator.id,
        user_id=user_id,
    )
    log_field_change(
        user_id=user_id,
        module="manufacturing",
        record_type="ManufacturingOrder",
        record_id=order.id,
        field_name="auto_generated_from_so",
        old_value=None,
        new_value=str(sales_order_id),
    )
    return order


def _parse_duration(value) -> int:
    """Duration fields may be submitted as '30.000' from numeric inputs."""
    if value is None or str(value).strip() == "":
        return 0
    return int(Decimal(str(value).strip()))


def update_draft_mo(
    *,
    order: ManufacturingOrder,
    finished_product_id: int,
    quantity: int,
    bom_id: int | None,
    assignee_id: int,
    component_rows: list[dict],
    operation_rows: list[dict],
    user_id: int,
) -> ManufacturingOrder:
    if order.status != ManufacturingOrderStatus.DRAFT:
        raise ManufacturingOrderError("Only draft MOs can be edited.")

    order.finished_product_id = finished_product_id
    order.quantity = quantity
    order.bom_id = bom_id
    order.assignee_id = assignee_id

    bom = db.session.get(BillOfMaterials, bom_id) if bom_id else None
    if component_rows or operation_rows:
        _apply_mo_lines_from_form(order, component_rows, operation_rows)
    elif bom:
        explode_bom_into_mo(mo=order, bom=bom)
    else:
        order.components.clear()
        order.work_orders.clear()

    log_field_change(
        user_id=user_id,
        module="manufacturing",
        record_type="ManufacturingOrder",
        record_id=order.id,
        field_name="header",
        old_value=None,
        new_value=f"qty={quantity}",
    )
    return order


def _apply_mo_lines_from_form(
    order: ManufacturingOrder,
    component_rows: list[dict],
    operation_rows: list[dict],
) -> None:
    order.components.clear()
    order.work_orders.clear()

    for row in component_rows:
        if not row.get("component_product_id"):
            continue
        required = Decimal(str(row.get("required_qty") or 0))
        if required <= 0:
            continue
        db.session.add(
            MOComponent(
                manufacturing_order_id=order.id,
                component_product_id=int(row["component_product_id"]),
                required_qty=required,
                consumed_qty=Decimal(str(row.get("consumed_qty") or 0)),
            )
        )

    for row in operation_rows:
        if not row.get("operation_name"):
            continue
        db.session.add(
            WorkOrder(
                manufacturing_order_id=order.id,
                operation_name=row["operation_name"].strip(),
                work_center_id=int(row["work_center_id"]),
                expected_duration=max(_parse_duration(row.get("expected_duration")), 0),
                real_duration=max(_parse_duration(row.get("real_duration")), 0),
                status=WorkOrderStatus.PENDING,
            )
        )


def confirm_manufacturing_order(*, order: ManufacturingOrder, user_id: int) -> None:
    if order.status != ManufacturingOrderStatus.DRAFT:
        raise InvalidMOTransition("Only draft MOs can be confirmed.")

    # Draft MO may have BoM selected but lines not saved – explode before validate.
    if not order.components and order.bom_id:
        bom = db.session.get(BillOfMaterials, order.bom_id)
        if bom:
            explode_bom_into_mo(mo=order, bom=bom)
            db.session.flush()

    if not order.components:
        raise ManufacturingOrderError("MO must have at least one component.")

    with db.session.begin_nested():
        product_ids = [c.component_product_id for c in order.components if c.component_product_id]
        locked = lock_products_for_update(product_ids)

        for comp in order.components:
            if not comp.component_product_id:
                raise ManufacturingOrderError(
                    "Cannot confirm: a component line references a deleted product."
                )
            product = locked[comp.component_product_id]
            qty = Decimal(str(comp.required_qty))
            reserve_stock(
                product=product,
                quantity=qty,
                reference_type=StockReferenceType.MANUFACTURING_ORDER,
                reference_id=order.id,
                user_id=user_id,
            )

        old = order.status.value
        order.status = ManufacturingOrderStatus.CONFIRMED
        log_status_change(
            user_id=user_id,
            module="manufacturing",
            record_type="ManufacturingOrder",
            record_id=order.id,
            old_status=old,
            new_status=ManufacturingOrderStatus.CONFIRMED.value,
        )


def start_manufacturing_order(*, order: ManufacturingOrder, user_id: int) -> None:
    if order.status != ManufacturingOrderStatus.CONFIRMED:
        raise InvalidMOTransition("Only confirmed MOs can be started.")

    with db.session.begin_nested():
        old = order.status.value
        order.status = ManufacturingOrderStatus.IN_PROGRESS
        order.started_at = datetime.now(timezone.utc)
        for wo in order.work_orders:
            wo.status = WorkOrderStatus.IN_PROGRESS

        log_status_change(
            user_id=user_id,
            module="manufacturing",
            record_type="ManufacturingOrder",
            record_id=order.id,
            old_status=old,
            new_status=ManufacturingOrderStatus.IN_PROGRESS.value,
        )


def produce_manufacturing_order(
    *,
    order: ManufacturingOrder,
    consumed_quantities: dict[int, Decimal],
    real_durations: dict[int, int],
    user_id: int,
) -> None:
    """
    ACID produce transaction: consume components, receive finished goods, close MO.
    """
    if order.status != ManufacturingOrderStatus.IN_PROGRESS:
        raise InvalidMOTransition("Only in-progress MOs can be produced.")

    with db.session.begin_nested():
        component_ids = [c.component_product_id for c in order.components]
        finished_id = order.finished_product_id
        locked = lock_products_for_update(component_ids + [finished_id])

        for comp in order.components:
            consumed = Decimal(str(consumed_quantities.get(comp.id, comp.required_qty)))
            if consumed < 0:
                raise ManufacturingOrderError("Consumed quantity cannot be negative.")
            comp.consumed_qty = consumed
            if consumed > 0:
                consume_production_stock(
                    product=locked[comp.component_product_id],
                    quantity=consumed,
                    manufacturing_order_id=order.id,
                    user_id=user_id,
                )

        finished = locked[finished_id]
        receive_production_output(
            product=finished,
            quantity=Decimal(order.quantity),
            manufacturing_order_id=order.id,
            user_id=user_id,
        )

        for wo in order.work_orders:
            if wo.id in real_durations:
                wo.real_duration = max(int(real_durations[wo.id]), 0)
            wo.status = WorkOrderStatus.DONE

        old = order.status.value
        order.status = ManufacturingOrderStatus.DONE
        order.completed_at = datetime.now(timezone.utc)

        log_status_change(
            user_id=user_id,
            module="manufacturing",
            record_type="ManufacturingOrder",
            record_id=order.id,
            old_status=old,
            new_status=ManufacturingOrderStatus.DONE.value,
        )


def cancel_manufacturing_order(*, order: ManufacturingOrder, user_id: int) -> None:
    if order.status in (ManufacturingOrderStatus.DONE, ManufacturingOrderStatus.CANCELLED):
        raise InvalidMOTransition(f"Cannot cancel MO in '{order.status.value}' status.")

    with db.session.begin_nested():
        if order.status in (
            ManufacturingOrderStatus.CONFIRMED,
            ManufacturingOrderStatus.IN_PROGRESS,
        ):
            product_ids = [c.component_product_id for c in order.components]
            locked = lock_products_for_update(product_ids)
            for comp in order.components:
                product = locked[comp.component_product_id]
                unreserve_stock(
                    product=product,
                    quantity=comp.required_qty,
                    reference_type=StockReferenceType.MANUFACTURING_ORDER,
                    reference_id=order.id,
                    user_id=user_id,
                )

        old = order.status.value
        order.status = ManufacturingOrderStatus.CANCELLED
        for wo in order.work_orders:
            wo.status = WorkOrderStatus.CANCELLED

        log_status_change(
            user_id=user_id,
            module="manufacturing",
            record_type="ManufacturingOrder",
            record_id=order.id,
            old_status=old,
            new_status=ManufacturingOrderStatus.CANCELLED.value,
        )

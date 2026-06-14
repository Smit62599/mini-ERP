"""
Bill of Materials business logic.
"""

from decimal import Decimal

from extensions import db
from models import BOMComponent, BOMOperation, BillOfMaterials, Product, WorkCenter
from services.audit_service import log_audit, log_field_change


class BOMError(Exception):
    pass


def generate_bom_number() -> str:
    last = db.session.query(BillOfMaterials.bom_number).order_by(BillOfMaterials.id.desc()).first()
    if last and last[0]:
        num = int(last[0].split("-")[1]) + 1
    else:
        num = 1
    return f"BOM-{num:06d}"


def create_bom(
    *,
    finished_product_id: int,
    quantity: int,
    user_id: int,
) -> BillOfMaterials:
    product = db.session.get(Product, finished_product_id)
    if not product:
        raise BOMError("Finished product not found.")

    bom = BillOfMaterials(
        bom_number=generate_bom_number(),
        finished_product_id=finished_product_id,
        quantity=quantity or 1,
    )
    db.session.add(bom)
    db.session.flush()

    log_audit(
        user_id=user_id,
        module="bom",
        record_type="BillOfMaterials",
        record_id=bom.id,
        action="created",
        field_changed="finished_product_id",
        old_value=None,
        new_value=product.name,
    )
    return bom


def sync_bom_components(
    *,
    bom: BillOfMaterials,
    component_rows: list[dict],
    user_id: int,
) -> None:
    """Replace BoM component lines from form submission."""
    bom.components.clear()
    for row in component_rows:
        if not row.get("component_product_id") or not row.get("quantity_required"):
            continue
        comp = BOMComponent(
            bom_id=bom.id,
            component_product_id=int(row["component_product_id"]),
            quantity_required=Decimal(str(row["quantity_required"])),
        )
        db.session.add(comp)

    log_field_change(
        user_id=user_id,
        module="bom",
        record_type="BillOfMaterials",
        record_id=bom.id,
        field_name="components",
        old_value=None,
        new_value=f"{len(component_rows)} lines",
    )


def sync_bom_operations(
    *,
    bom: BillOfMaterials,
    operation_rows: list[dict],
    user_id: int,
) -> None:
    bom.operations.clear()
    for row in operation_rows:
        if not row.get("operation_name") or not row.get("work_center_id"):
            continue
        op = BOMOperation(
            bom_id=bom.id,
            operation_name=row["operation_name"].strip(),
            work_center_id=int(row["work_center_id"]),
            duration_minutes=int(row.get("duration_minutes") or 0),
        )
        db.session.add(op)

    log_field_change(
        user_id=user_id,
        module="bom",
        record_type="BillOfMaterials",
        record_id=bom.id,
        field_name="operations",
        old_value=None,
        new_value=f"{len(operation_rows)} lines",
    )


def bom_to_dict(bom: BillOfMaterials) -> dict:
    """JSON-serialisable BoM for frontend ingredient injection."""
    return {
        "id": bom.id,
        "bom_number": bom.bom_number,
        "finished_product_id": bom.finished_product_id,
        "quantity": bom.quantity,
        "components": [
            {
                "component_product_id": c.component_product_id,
                "component_name": c.component_product.name,
                "quantity_required": float(c.quantity_required),
            }
            for c in bom.components
        ],
        "operations": [
            {
                "operation_name": o.operation_name,
                "work_center_id": o.work_center_id,
                "work_center_name": o.work_center.name,
                "duration_minutes": o.duration_minutes,
            }
            for o in bom.operations
        ],
    }

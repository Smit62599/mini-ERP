"""
Generate large demo datasets for Shiv Furniture Works Mini ERP.

Run from project root with the virtual environment activated:

    .\\.venv\\Scripts\\python.exe generate_data.py

Requires: pip install faker  (included in requirements.txt)
"""

from __future__ import annotations

import random
from decimal import Decimal

from faker import Faker

from app import create_app
from extensions import db
from sqlalchemy import text
from models import (
    AuditLog,
    BOMComponent,
    BillOfMaterials,
    Customer,
    ManufacturingOrder,
    ManufacturingOrderStatus,
    ProcurementType,
    Product,
    SalesOrder,
    SalesOrderLine,
    SalesOrderStatus,
    User,
    UserRole,
    Vendor,
    WorkCenter,
)

fake = Faker("en_IN")

# ---------------------------------------------------------------------------
# Scale knobs – adjust for faster/slower runs
# ---------------------------------------------------------------------------
USER_COUNT = 500          # 1 admin + 499 staff
PRODUCT_COUNT_RAW = 150
PRODUCT_COUNT_FG = 50
CUSTOMER_COUNT = 120
VENDOR_COUNT = 40
SALES_ORDER_COUNT = 800
MO_COUNT = 120


def _money(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


def _qty(value: float | int) -> Decimal:
    return Decimal(str(value))


def reset_database() -> None:
    """Clear all ERP data without dropping tables (avoids products↔bom FK cycle)."""
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    if not inspector.get_table_names():
        db.create_all()
        return

    db.session.execute(
        text(
            """
            TRUNCATE TABLE
              audit_logs,
              stock_ledger,
              work_orders,
              mo_components,
              manufacturing_orders,
              sales_order_lines,
              sales_orders,
              purchase_order_lines,
              purchase_orders,
              bom_operations,
              bom_components,
              bom,
              products,
              customers,
              user_field_permissions,
              users,
              vendors,
              work_centers
            RESTART IDENTITY CASCADE
            """
        )
    )
    db.session.commit()


def generate_massive_data() -> None:
    app = create_app()
    with app.app_context():
        print("STEP 1: Clearing existing data…")
        reset_database()

        # ------------------------------------------------------------------
        # Users (1 admin + sales / manufacturing staff)
        # ------------------------------------------------------------------
        print(f"STEP 2: Creating {USER_COUNT} users…")
        admin = User(
            name="System Administrator",
            email="admin@shivfurniture.com",
            address="100 Enterprise HQ, Mumbai, 400001",
            mobile_number="+919999999999",
            position="Admin",
            role=UserRole.ADMIN,
        )
        admin.set_password("admin123")
        db.session.add(admin)

        sales_users: list[User] = []
        mfg_users: list[User] = []
        role_pool = [
            (UserRole.SALES, "Salesperson", "Sales Manager", 0.55),
            (UserRole.MANUFACTURING, "Manufacturing Operator", "Manufacturing Operator", 0.30),
            (UserRole.OWNER, "Business Owner", "Business Owner", 0.15),
        ]

        for i in range(USER_COUNT - 1):
            roll = random.random()
            cumulative = 0.0
            chosen_role = UserRole.SALES
            position = "Salesperson"
            for role, pos_a, pos_b, weight in role_pool:
                cumulative += weight
                if roll <= cumulative:
                    chosen_role = role
                    position = pos_a if random.random() < 0.7 else pos_b
                    break

            user = User(
                name=fake.name(),
                email=fake.unique.email(),
                address=fake.address().replace("\n", ", ")[:512],
                mobile_number=fake.phone_number()[:15],
                position=position,
                role=chosen_role,
            )
            user.set_password("password123")
            db.session.add(user)
            if chosen_role == UserRole.SALES:
                sales_users.append(user)
            elif chosen_role == UserRole.MANUFACTURING:
                mfg_users.append(user)

        db.session.flush()
        if not sales_users:
            sales_users = [admin]
        if not mfg_users:
            mfg_users = [admin]

        # ------------------------------------------------------------------
        # Vendors & customers
        # ------------------------------------------------------------------
        print(f"STEP 3: Creating {VENDOR_COUNT} vendors and {CUSTOMER_COUNT} customers…")
        vendors: list[Vendor] = []
        for _ in range(VENDOR_COUNT):
            v = Vendor(name=fake.company(), contact_info=fake.phone_number()[:256])
            vendors.append(v)
            db.session.add(v)

        customers: list[Customer] = []
        for _ in range(CUSTOMER_COUNT):
            c = Customer(
                name=fake.company(),
                address=fake.address().replace("\n", ", ")[:512],
                contact_info=fake.phone_number()[:256],
            )
            customers.append(c)
            db.session.add(c)

        db.session.flush()

        # ------------------------------------------------------------------
        # Work centers (for optional BoM routing)
        # ------------------------------------------------------------------
        wc_names = ["Assembly Line", "Paint Floor", "Packaging Unit", "Saw Bench", "QC Station"]
        work_centers: list[WorkCenter] = []
        for name in wc_names:
            wc = WorkCenter(name=name, description=f"{name} – main floor")
            work_centers.append(wc)
            db.session.add(wc)
        db.session.flush()

        # ------------------------------------------------------------------
        # Products
        # ------------------------------------------------------------------
        print(f"STEP 4: Creating {PRODUCT_COUNT_RAW + PRODUCT_COUNT_FG} products…")
        products: list[Product] = []
        material_types = ["Panel", "Frame", "Leg", "Screw Pack", "Varnish", "Handle", "Fabric", "Foam"]

        for i in range(PRODUCT_COUNT_RAW):
            cost = random.uniform(2.0, 75.0)
            p = Product(
                name=f"{fake.color_name().title()} {random.choice(material_types)} RM-{i+1:03d}",
                sales_price=_money(cost * 1.1),
                cost_price=_money(cost),
                on_hand_qty=_qty(random.randint(200, 1500)),
                reserved_qty=Decimal("0"),
                procure_on_demand=random.choice([True, False]),
                procurement_type=ProcurementType.PURCHASE,
                vendor_id=random.choice(vendors).id,
            )
            products.append(p)
            db.session.add(p)

        furniture = ["Executive Table", "Ergonomic Chair", "Filing Cabinet", "Bookshelf", "Conference Desk"]
        finished_goods: list[Product] = []
        for i in range(PRODUCT_COUNT_FG):
            cost = random.uniform(40.0, 180.0)
            p = Product(
                name=f"Premium {fake.word().title()} {random.choice(furniture)} FG-{i+1:03d}",
                sales_price=_money(cost * random.uniform(2.0, 3.5)),
                cost_price=_money(cost),
                on_hand_qty=_qty(random.randint(5, 40)),
                reserved_qty=Decimal("0"),
                procure_on_demand=True,
                procurement_type=ProcurementType.MANUFACTURE,
            )
            finished_goods.append(p)
            products.append(p)
            db.session.add(p)

        db.session.flush()
        raw_materials = [p for p in products if p.procurement_type == ProcurementType.PURCHASE]

        # ------------------------------------------------------------------
        # Bill of Materials
        # ------------------------------------------------------------------
        print("STEP 5: Building Bill of Materials for finished goods…")
        for idx, product in enumerate(finished_goods):
            bom = BillOfMaterials(
                bom_number=f"BOM-{idx+1:04d}",
                finished_product_id=product.id,
                quantity=1,
            )
            db.session.add(bom)
            db.session.flush()
            product.bom_id = bom.id

            for comp in random.sample(raw_materials, k=min(len(raw_materials), random.randint(2, 5))):
                db.session.add(
                    BOMComponent(
                        bom_id=bom.id,
                        component_product_id=comp.id,
                        quantity_required=_qty(random.randint(1, 12)),
                    )
                )

        db.session.commit()

        # ------------------------------------------------------------------
        # Sales orders
        # ------------------------------------------------------------------
        print(f"STEP 6: Inserting {SALES_ORDER_COUNT} sales orders…")
        so_statuses = [
            (SalesOrderStatus.DRAFT, 0.10),
            (SalesOrderStatus.CONFIRMED, 0.25),
            (SalesOrderStatus.PARTIALLY_DELIVERED, 0.10),
            (SalesOrderStatus.DELIVERED, 0.50),
            (SalesOrderStatus.CANCELLED, 0.05),
        ]

        for i in range(SALES_ORDER_COUNT):
            status = random.choices(
                [s for s, _ in so_statuses],
                weights=[w for _, w in so_statuses],
                k=1,
            )[0]
            customer = random.choice(customers)
            so = SalesOrder(
                so_number=f"SO-{i+1:06d}",
                customer_id=customer.id,
                customer_address=customer.address,
                sales_person_id=random.choice(sales_users).id,
                status=status,
                total_amount=Decimal("0"),
            )
            db.session.add(so)
            db.session.flush()

            order_total = Decimal("0")
            for prod in random.sample(finished_goods, k=random.randint(1, min(4, len(finished_goods)))):
                qty = _qty(random.randint(1, 8))
                delivered = qty if status == SalesOrderStatus.DELIVERED else (
                    _qty(float(qty) * random.uniform(0.3, 0.8))
                    if status == SalesOrderStatus.PARTIALLY_DELIVERED
                    else Decimal("0")
                )
                reserved = qty if status in (
                    SalesOrderStatus.CONFIRMED,
                    SalesOrderStatus.PARTIALLY_DELIVERED,
                    SalesOrderStatus.DELIVERED,
                ) else Decimal("0")

                db.session.add(
                    SalesOrderLine(
                        sales_order_id=so.id,
                        product_id=prod.id,
                        ordered_qty=qty,
                        delivered_qty=delivered,
                        reserved_qty=reserved,
                        sales_price=prod.sales_price,
                    )
                )
                line_total = (delivered if status in (
                    SalesOrderStatus.PARTIALLY_DELIVERED,
                    SalesOrderStatus.DELIVERED,
                ) else qty) * prod.sales_price
                order_total += line_total

                if status == SalesOrderStatus.CONFIRMED:
                    prod.reserved_qty += qty
                elif status == SalesOrderStatus.DELIVERED:
                    prod.on_hand_qty = max(Decimal("0"), prod.on_hand_qty - qty)
                    prod.reserved_qty = max(Decimal("0"), prod.reserved_qty - qty)

            so.total_amount = order_total

            if i > 0 and i % 200 == 0:
                db.session.commit()
                print(f"   … {i} sales orders committed")

        db.session.commit()

        # ------------------------------------------------------------------
        # Manufacturing orders + audit logs
        # ------------------------------------------------------------------
        print(f"STEP 7: Creating {MO_COUNT} manufacturing orders…")
        mo_weights = [
            (ManufacturingOrderStatus.DRAFT, 0.15),
            (ManufacturingOrderStatus.CONFIRMED, 0.25),
            (ManufacturingOrderStatus.IN_PROGRESS, 0.20),
            (ManufacturingOrderStatus.DONE, 0.35),
            (ManufacturingOrderStatus.CANCELLED, 0.05),
        ]

        for i in range(MO_COUNT):
            target = random.choice(finished_goods)
            status = random.choices(
                [s for s, _ in mo_weights],
                weights=[w for _, w in mo_weights],
                k=1,
            )[0]
            mo = ManufacturingOrder(
                mo_number=f"MO-{i+1:06d}",
                finished_product_id=target.id,
                quantity=random.randint(10, 100),
                bom_id=target.bom_id,
                assignee_id=random.choice(mfg_users).id,
                status=status,
            )
            db.session.add(mo)
            db.session.flush()

            db.session.add(
                AuditLog(
                    user_id=admin.id,
                    module="manufacturing",
                    record_type="ManufacturingOrder",
                    record_id=mo.id,
                    action="status_change",
                    field_changed="status",
                    old_value=ManufacturingOrderStatus.DRAFT.value,
                    new_value=status.value,
                )
            )

        db.session.commit()

        print("\nSUCCESS: Demo dataset loaded.")
        print("  Admin login: admin@shivfurniture.com / admin123")
        print("  Other users: password123")
        print("  Start app:   flask --app app run")


if __name__ == "__main__":
    generate_massive_data()

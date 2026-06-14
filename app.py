"""
Application factory for Shiv Furniture Works.

Using a factory pattern lets us:
  • Create multiple app instances (e.g. tests vs. dev server)
  • Register blueprints without circular imports
  • Initialise extensions against a fully-configured app object
"""

import os

from flask import Flask, redirect, url_for

from config import config_by_name
from extensions import db, login_manager
from models import ManufacturingOrderStatus, SalesOrderStatus, User, UserRole
from routes.audit_api import audit_bp
from routes.admin_users import admin_users_bp
from routes.auth import auth_bp
from routes.user_pages import user_pages_bp
from routes.bom import bom_bp
from routes.manufacturing_routes import manufacturing_bp
from routes.products import products_bp
from routes.purchase import purchase_bp
from routes.sales import sales_bp


def create_app(config_name: str | None = None) -> Flask:
    """Build and configure the Flask application."""
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config_by_name.get(config_name, config_by_name["development"]))

    # --- Bind extensions -------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)

    # --- Register blueprints ---------------------------------------------
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_pages_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(bom_bp)
    app.register_blueprint(purchase_bp)
    app.register_blueprint(manufacturing_bp)
    app.register_blueprint(audit_bp)

    @app.template_filter("so_status_label")
    def so_status_label(status: SalesOrderStatus) -> str:
        """Jinja filter – human-readable sales order status labels."""
        labels = {
            SalesOrderStatus.DRAFT: "Draft",
            SalesOrderStatus.CONFIRMED: "Confirmed",
            SalesOrderStatus.PARTIALLY_DELIVERED: "Partially Delivered",
            SalesOrderStatus.DELIVERED: "Fully Delivered",
            SalesOrderStatus.CANCELLED: "Cancelled",
        }
        return labels.get(status, status.value.replace("_", " ").title())

    @app.template_filter("mo_status_label")
    def mo_status_label(status: ManufacturingOrderStatus) -> str:
        """Jinja filter – human-readable manufacturing order status labels."""
        labels = {
            ManufacturingOrderStatus.DRAFT: "Draft",
            ManufacturingOrderStatus.CONFIRMED: "Confirmed",
            ManufacturingOrderStatus.IN_PROGRESS: "In Progress",
            ManufacturingOrderStatus.DONE: "Done",
            ManufacturingOrderStatus.CANCELLED: "Cancelled",
        }
        return labels.get(status, status.value.replace("_", " ").title())

    @app.template_filter("product_label")
    def product_label(product) -> str:
        """Safe product name for historical lines after master delete (SET NULL)."""
        return product.name if product else "[Deleted Product]"

    # --- Root redirect ---------------------------------------------------
    @app.route("/")
    def index():
        """Always land on the login page (wireframe entry point)."""
        return redirect(url_for("auth.login"))

    @app.route("/login")
    def login_alias():
        return redirect(url_for("auth.login"))

    # --- CLI: initialise database + seed admin user ----------------------
    @app.cli.command("init-db")
    def init_db():
        """Create all tables and seed a default admin account."""
        db.create_all()
        if not User.query.filter_by(email="admin@shivfurniture.com").first():
            admin = User(
                name="System Administrator",
                email="admin@shivfurniture.com",
                role=UserRole.ADMIN,
                address="Shiv Furniture Works, Main Office",
                mobile_number="+91 90000 00001",
                position="Admin",
            )
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
            print("Database initialised. Admin: admin@shivfurniture.com / admin123")
        else:
            print("Database tables created (admin already exists).")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Populate sample master data, BoM, and MOs for hackathon demos."""
        from decimal import Decimal
        from datetime import datetime, timedelta, timezone

        from models import (
            BOMComponent,
            BOMOperation,
            BillOfMaterials,
            Customer,
            ManufacturingOrder,
            ManufacturingOrderStatus,
            MOComponent,
            Product,
            ProcurementType,
            Vendor,
            WorkCenter,
            WorkOrder,
            WorkOrderStatus,
        )
        from services.bom_service import generate_bom_number

        if not User.query.filter_by(email="sales@shivfurniture.com").first():
            sales_user = User(
                name="Ravi Jadeja",
                email="sales@shivfurniture.com",
                role=UserRole.SALES,
                address="12 Market Road, Ahmedabad",
                mobile_number="+91 98765 43210",
                position="Sales Manager",
            )
            sales_user.set_password("sales123")
            db.session.add(sales_user)
            print("Sales user: sales@shivfurniture.com / sales123")

        if not User.query.filter_by(email="salman@shivfurniture.com").first():
            sales_user_2 = User(
                name="Salman Sheikh",
                email="salman@shivfurniture.com",
                role=UserRole.SALES,
                position="Salesperson",
            )
            sales_user_2.set_password("sales123")
            db.session.add(sales_user_2)
            print("Sales user: salman@shivfurniture.com / sales123")

        if not User.query.filter_by(email="owner@shivfurniture.com").first():
            owner_user = User(
                name="Business Owner",
                email="owner@shivfurniture.com",
                role=UserRole.OWNER,
                position="Business Owner",
            )
            owner_user.set_password("owner123")
            db.session.add(owner_user)
            print("Owner user (read-only): owner@shivfurniture.com / owner123")

        if not User.query.filter_by(email="operator@shivfurniture.com").first():
            mfg_user = User(
                name="Vikram Operator",
                email="operator@shivfurniture.com",
                role=UserRole.MANUFACTURING,
                position="Manufacturing Operator",
            )
            mfg_user.set_password("mfg123")
            db.session.add(mfg_user)
            print("Manufacturing user: operator@shivfurniture.com / mfg123")

        if Customer.query.count() == 0:
            db.session.add_all([
                Customer(name="Suzuki India", address="Delhi, India", contact_info="9876543210"),
                Customer(name="MRF Ltd.", address="Chennai, India", contact_info="9876543211"),
            ])

        if Vendor.query.count() == 0:
            db.session.add(Vendor(name="Gupta Timber Supplies", contact_info="timber@gupta.com"))

        if WorkCenter.query.count() == 0:
            db.session.add_all([
                WorkCenter(name="Saw Bench", description="Cutting station"),
                WorkCenter(name="Assembly Line", description="Final assembly"),
            ])

        if Product.query.count() == 0:
            vendor = Vendor.query.first()
            db.session.add_all([
                Product(
                    name="Office Chair – Ergonomic",
                    sales_price=Decimal("4500.00"),
                    cost_price=Decimal("2800.00"),
                    on_hand_qty=Decimal("50"),
                    reserved_qty=Decimal("0"),
                    procure_on_demand=False,
                ),
                Product(
                    name="Oak Plank – Raw",
                    sales_price=Decimal("800.00"),
                    cost_price=Decimal("400.00"),
                    on_hand_qty=Decimal("100"),
                    reserved_qty=Decimal("0"),
                    procure_on_demand=False,
                ),
                Product(
                    name="Desk Hardware Kit",
                    sales_price=Decimal("500.00"),
                    cost_price=Decimal("250.00"),
                    on_hand_qty=Decimal("200"),
                    reserved_qty=Decimal("0"),
                    procure_on_demand=False,
                ),
                Product(
                    name="Executive Desk – Oak",
                    sales_price=Decimal("12000.00"),
                    cost_price=Decimal("7500.00"),
                    on_hand_qty=Decimal("5"),
                    reserved_qty=Decimal("0"),
                    procure_on_demand=True,
                    procurement_type=ProcurementType.MANUFACTURE,
                ),
                Product(
                    name="Filing Cabinet – 4 Drawer",
                    sales_price=Decimal("3200.00"),
                    cost_price=Decimal("1900.00"),
                    on_hand_qty=Decimal("0"),
                    reserved_qty=Decimal("0"),
                    procure_on_demand=True,
                    procurement_type=ProcurementType.PURCHASE,
                    vendor_id=vendor.id if vendor else None,
                ),
            ])

        desk = Product.query.filter(Product.name.like("%Executive Desk%")).first()
        plank = Product.query.filter(Product.name.like("%Oak Plank%")).first()
        hardware = Product.query.filter(Product.name.like("%Hardware Kit%")).first()
        saw = WorkCenter.query.filter_by(name="Saw Bench").first()
        assembly = WorkCenter.query.filter_by(name="Assembly Line").first()

        if desk and plank and hardware and saw and assembly and BillOfMaterials.query.count() == 0:
            bom = BillOfMaterials(
                bom_number=generate_bom_number(),
                finished_product_id=desk.id,
                quantity=1,
            )
            db.session.add(bom)
            db.session.flush()
            db.session.add_all([
                BOMComponent(bom_id=bom.id, component_product_id=plank.id, quantity_required=Decimal("4")),
                BOMComponent(bom_id=bom.id, component_product_id=hardware.id, quantity_required=Decimal("1")),
            ])
            db.session.add_all([
                BOMOperation(bom_id=bom.id, operation_name="Cut Oak Planks", work_center_id=saw.id, duration_minutes=60),
                BOMOperation(bom_id=bom.id, operation_name="Assemble Desk", work_center_id=assembly.id, duration_minutes=120),
            ])
            desk.bom_id = bom.id

        # Incremental seed: raw materials + BoM when upgrading existing databases.
        vendor = Vendor.query.first() or Vendor(name="Gupta Timber Supplies", contact_info="timber@gupta.com")
        if not Vendor.query.first():
            db.session.add(vendor)
            db.session.flush()
        if not Product.query.filter(Product.name.like("%Oak Plank%")).first():
            db.session.add_all([
                Product(name="Oak Plank – Raw", sales_price=Decimal("800"), cost_price=Decimal("400"),
                        on_hand_qty=Decimal("100"), reserved_qty=Decimal("0"), procure_on_demand=False),
                Product(name="Desk Hardware Kit", sales_price=Decimal("500"), cost_price=Decimal("250"),
                        on_hand_qty=Decimal("200"), reserved_qty=Decimal("0"), procure_on_demand=False),
            ])
        desk = Product.query.filter(Product.name.like("%Executive Desk%")).first()
        plank = Product.query.filter(Product.name.like("%Oak Plank%")).first()
        hardware = Product.query.filter(Product.name.like("%Hardware Kit%")).first()
        saw = WorkCenter.query.filter_by(name="Saw Bench").first()
        assembly = WorkCenter.query.filter_by(name="Assembly Line").first()
        if desk and plank and hardware and saw and assembly and BillOfMaterials.query.count() == 0:
            bom = BillOfMaterials(bom_number=generate_bom_number(), finished_product_id=desk.id, quantity=1)
            db.session.add(bom)
            db.session.flush()
            db.session.add_all([
                BOMComponent(bom_id=bom.id, component_product_id=plank.id, quantity_required=Decimal("4")),
                BOMComponent(bom_id=bom.id, component_product_id=hardware.id, quantity_required=Decimal("1")),
            ])
            db.session.add_all([
                BOMOperation(bom_id=bom.id, operation_name="Cut Oak Planks", work_center_id=saw.id, duration_minutes=60),
                BOMOperation(bom_id=bom.id, operation_name="Assemble Desk", work_center_id=assembly.id, duration_minutes=120),
            ])
            desk.bom_id = bom.id
            desk.procurement_type = ProcurementType.MANUFACTURE
            desk.procure_on_demand = True
        cabinet = Product.query.filter(Product.name.like("%Filing Cabinet%")).first()
        if cabinet and vendor and not cabinet.vendor_id:
            cabinet.vendor_id = vendor.id

        operator = User.query.filter_by(email="operator@shivfurniture.com").first()
        chair = Product.query.filter(Product.name.like("%Office Chair%")).first()
        bom = BillOfMaterials.query.first()

        if ManufacturingOrder.query.count() == 0 and desk and chair and operator and bom:
            now = datetime.now(timezone.utc)
            mo1 = ManufacturingOrder(
                mo_number="MO-000001",
                finished_product_id=desk.id,
                quantity=2,
                bom_id=bom.id,
                assignee_id=operator.id,
                status=ManufacturingOrderStatus.IN_PROGRESS,
                started_at=now - timedelta(hours=3),
                created_at=now - timedelta(days=1),
            )
            db.session.add(mo1)
            db.session.flush()
            db.session.add_all([
                MOComponent(manufacturing_order_id=mo1.id, component_product_id=plank.id, required_qty=Decimal("8"), consumed_qty=Decimal("0")),
                MOComponent(manufacturing_order_id=mo1.id, component_product_id=hardware.id, required_qty=Decimal("2"), consumed_qty=Decimal("0")),
            ])
            db.session.add_all([
                WorkOrder(manufacturing_order_id=mo1.id, operation_name="Cut Oak Planks", work_center_id=saw.id, expected_duration=120, status=WorkOrderStatus.IN_PROGRESS),
                WorkOrder(manufacturing_order_id=mo1.id, operation_name="Assemble Desk", work_center_id=assembly.id, expected_duration=240, status=WorkOrderStatus.IN_PROGRESS),
            ])

            mo2 = ManufacturingOrder(
                mo_number="MO-000002",
                finished_product_id=chair.id,
                quantity=10,
                assignee_id=operator.id,
                status=ManufacturingOrderStatus.CONFIRMED,
                created_at=now - timedelta(hours=6),
            )
            db.session.add(mo2)

            mo4 = ManufacturingOrder(
                mo_number="MO-000004",
                finished_product_id=chair.id,
                quantity=5,
                assignee_id=operator.id,
                status=ManufacturingOrderStatus.DRAFT,
                created_at=now,
            )
            db.session.add(mo4)

        db.session.commit()
        print("Demo data seeded.")

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Apply schema patches (safe to run multiple times)."""
        from sqlalchemy import text

        db.session.execute(
            text(
                "ALTER TABLE sales_order_lines "
                "ADD COLUMN IF NOT EXISTS reserved_qty NUMERIC(12,3) NOT NULL DEFAULT 0"
            )
        )
        db.session.commit()
        print("Schema upgraded: sales_order_lines.reserved_qty added.")

    @app.cli.command("upgrade-erp")
    def upgrade_erp():
        """Apply full ERP schema upgrades (BoM, MO components, PO, vendors)."""
        from sqlalchemy import text

        db.session.execute(text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'MANUFACTURING'"))
        for val in ("PRODUCTION_CONSUMPTION", "PRODUCTION_RECEIPT"):
            db.session.execute(
                text(f"ALTER TYPE stock_transaction_type ADD VALUE IF NOT EXISTS '{val}'")
            )
        db.create_all()
        db.session.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS vendor_id INTEGER REFERENCES vendors(id)"
        ))
        db.session.execute(text(
            "ALTER TABLE manufacturing_orders ADD COLUMN IF NOT EXISTS bom_id INTEGER REFERENCES bom(id)"
        ))
        db.session.commit()
        print("ERP schema upgraded. Run: flask --app app seed-demo")

    @app.cli.command("upgrade-extensions")
    def upgrade_extensions():
        """Product SET NULL deletes, PO receiving, purchase_receipt ledger type."""
        from sqlalchemy import text

        db.session.execute(
            text("ALTER TYPE stock_transaction_type ADD VALUE IF NOT EXISTS 'PURCHASE_RECEIPT'")
        )
        db.session.execute(
            text("ALTER TYPE purchase_order_status ADD VALUE IF NOT EXISTS 'DONE'")
        )
        db.session.execute(
            text(
                "ALTER TABLE purchase_order_lines "
                "ADD COLUMN IF NOT EXISTS received_qty NUMERIC(12,3) NOT NULL DEFAULT 0"
            )
        )

        fk_patches = [
            ("sales_order_lines", "product_id"),
            ("purchase_order_lines", "product_id"),
            ("bom_components", "component_product_id"),
            ("mo_components", "component_product_id"),
        ]
        for table, column in fk_patches:
            db.session.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL"))
            db.session.execute(
                text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_fkey")
            )
            db.session.execute(
                text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_fkey "
                    f"FOREIGN KEY ({column}) REFERENCES products(id) ON DELETE SET NULL"
                )
            )

        db.create_all()
        db.session.commit()
        print("Feature extensions schema applied.")

    @app.cli.command("upgrade-users")
    def upgrade_users():
        """Add profile fields to users table (address, mobile, position)."""
        from sqlalchemy import text

        db.session.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address VARCHAR(512)")
        )
        db.session.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS mobile_number VARCHAR(32)")
        )
        db.session.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS position VARCHAR(64)")
        )
        db.session.commit()
        print("User profile columns added. Existing rows may have NULL contact fields.")

    @app.cli.command("upgrade-user-permissions")
    def upgrade_user_permissions():
        """Create user_field_permissions table for admin RBAC UI."""
        db.create_all()
        db.session.commit()
        print("User field permissions table ready.")

    @app.cli.command("upgrade-manufacturing")
    def upgrade_manufacturing():
        """Create manufacturing_orders table and add manufacturing role enum value."""
        from sqlalchemy import text

        # PostgreSQL stores enum member NAMES (ADMIN, SALES, …) – must match UserRole.
        db.session.execute(
            text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'MANUFACTURING'")
        )
        db.create_all()  # creates manufacturing_orders + new enum type
        db.session.commit()
        print("Manufacturing schema ready. Run: flask --app app seed-demo")

    @app.cli.command("repair-mo")
    def repair_mo():
        """
        Backfill BoM components/work orders on draft MOs missing lines.
        Cancels legacy MOs that were created before the BoM module existed.
        """
        from models import BillOfMaterials, ManufacturingOrder, ManufacturingOrderStatus
        from services.manufacturing_service import explode_bom_into_mo

        fixed = 0
        cancelled = 0
        for mo in ManufacturingOrder.query.order_by(ManufacturingOrder.id).all():
            if mo.components:
                continue

            bom_id = mo.bom_id or (mo.product.bom_id if mo.product else None)
            if mo.status == ManufacturingOrderStatus.DRAFT and bom_id:
                bom = db.session.get(BillOfMaterials, bom_id)
                if bom:
                    mo.bom_id = bom_id
                    explode_bom_into_mo(mo=mo, bom=bom)
                    fixed += 1
                    print(f"  Fixed draft {mo.mo_number} from BoM {bom.bom_number}")
                continue

            if mo.status not in (
                ManufacturingOrderStatus.DONE,
                ManufacturingOrderStatus.CANCELLED,
            ):
                old = mo.status.value
                mo.status = ManufacturingOrderStatus.CANCELLED
                cancelled += 1
                print(f"  Cancelled legacy {mo.mo_number} (was {old}, no BoM lines)")

        db.session.commit()
        print(f"repair-mo complete: {fixed} fixed, {cancelled} legacy MOs cancelled.")

    @app.cli.command("repair-inventory")
    def repair_inventory():
        """
        Fix corrupted reserved_qty values caused by the pre-fix cancel bug.

        Recalculates product.reserved_qty from the stock ledger and backfills
        per-line reserved_qty for active sales orders.
        """
        from decimal import Decimal

        from models import (
            Product,
            SalesOrder,
            SalesOrderLine,
            SalesOrderStatus,
            StockLedger,
            StockReferenceType,
            StockTransactionType,
        )

        # 1. Rebuild each product's reserved_qty from the immutable ledger.
        for product in Product.query.all():
            net = Decimal("0")
            for entry in product.stock_ledger_entries:
                if entry.transaction_type == StockTransactionType.RESERVE:
                    net += entry.quantity_change
                elif entry.transaction_type in (
                    StockTransactionType.UNRESERVE,
                    StockTransactionType.DELIVERY,
                ):
                    net += entry.quantity_change  # already negative
            old = product.reserved_qty
            product.reserved_qty = max(net, Decimal("0"))
            if old != product.reserved_qty:
                print(f"  {product.name}: reserved_qty {old} → {product.reserved_qty}")

        # 2. Backfill line.reserved_qty for open orders from their ledger entries.
        active = (SalesOrderStatus.CONFIRMED, SalesOrderStatus.PARTIALLY_DELIVERED)
        lines = (
            SalesOrderLine.query.join(SalesOrder)
            .filter(SalesOrder.status.in_(active))
            .all()
        )
        for line in lines:
            entries = StockLedger.query.filter_by(
                reference_type=StockReferenceType.SALES_ORDER,
                reference_id=line.sales_order_id,
                product_id=line.product_id,
            ).all()
            net = Decimal("0")
            for entry in entries:
                if entry.transaction_type == StockTransactionType.RESERVE:
                    net += entry.quantity_change
                elif entry.transaction_type in (
                    StockTransactionType.UNRESERVE,
                    StockTransactionType.DELIVERY,
                ):
                    net += entry.quantity_change
            line.reserved_qty = max(net, Decimal("0"))

        db.session.commit()
        print("Inventory repaired successfully.")

    # --- Error handlers --------------------------------------------------
    @app.errorhandler(403)
    def forbidden(_error):
        return "Access denied – insufficient permissions.", 403

    @app.errorhandler(404)
    def not_found(_error):
        return "Resource not found.", 404

    return app


# Allow `flask --app app run` when this module is the entry point.
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)

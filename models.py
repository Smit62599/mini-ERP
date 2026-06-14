"""
SQLAlchemy ORM models for Shiv Furniture Works.

Design principles:
  • Strict foreign keys with ON DELETE RESTRICT to preserve audit integrity.
  • Enumerated status/role columns stored as PostgreSQL-native ENUM types
    so invalid values are rejected at the database layer, not just Python.
  • `free_to_use_qty` is intentionally NOT a column – it is derived at
    query time as (on_hand_qty - reserved_qty) to avoid stale cached values.
"""

import enum
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


# ---------------------------------------------------------------------------
# Enumerations – backed by PostgreSQL ENUM types for DB-level validation
# ---------------------------------------------------------------------------


class UserRole(enum.Enum):
    """RBAC roles for Shiv Furniture Works."""

    ADMIN = "admin"          # Full system access, can change user roles
    SALES = "sales"          # Sales module read/write
    MANUFACTURING = "manufacturing"  # Production floor – MO module
    OWNER = "owner"          # Read-only dashboard / executive view


class SalesOrderStatus(enum.Enum):
    """Lifecycle states for a Sales Order (matches wireframe)."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class ProcurementType(enum.Enum):
    """How a product is replenished when stock is insufficient."""

    PURCHASE = "purchase"
    MANUFACTURE = "manufacturing"  # PG enum name MANUFACTURE; value per ERP spec


class StockTransactionType(enum.Enum):
    """Direction of inventory movement in the Stock Ledger."""

    RESERVE = "reserve"
    UNRESERVE = "unreserve"
    DELIVERY = "delivery"
    RECEIPT = "receipt"
    ADJUSTMENT = "adjustment"
    PRODUCTION_CONSUMPTION = "production_consumption"
    PRODUCTION_RECEIPT = "production_receipt"
    PURCHASE_RECEIPT = "purchase_receipt"


class StockReferenceType(enum.Enum):
    """Polymorphic pointer – what business document caused the movement."""

    SALES_ORDER = "sales_order"
    PURCHASE_ORDER = "purchase_order"
    MANUFACTURING_ORDER = "manufacturing_order"
    MANUAL = "manual"


class ManufacturingOrderStatus(enum.Enum):
    """Lifecycle states for a Manufacturing Order (factory floor dashboard)."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "inprogress"
    DONE = "done"
    CANCELLED = "cancelled"


class WorkOrderStatus(enum.Enum):
    """Lifecycle states for an MO operation line."""

    PENDING = "pending"
    IN_PROGRESS = "inprogress"
    DONE = "done"
    CANCELLED = "cancelled"


class PurchaseOrderStatus(enum.Enum):
    """Lifecycle states for a Purchase Order."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    RECEIVED = "received"
    DONE = "done"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# User – Authentication & RBAC
# ---------------------------------------------------------------------------


class User(db.Model, UserMixin):
    """
    System user with role-based access control.

    Email is the login identifier and must remain immutable after creation
    (enforced in the route layer, not the DB, per wireframe RBAC rules).
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    address = db.Column(db.String(512), nullable=True)
    mobile_number = db.Column(db.String(32), nullable=True)
    position = db.Column(db.String(64), nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(
        db.Enum(UserRole, name="user_role", create_constraint=True),
        nullable=False,
        default=UserRole.SALES,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # --- Relationships ---------------------------------------------------
    sales_orders = db.relationship(
        "SalesOrder", back_populates="sales_person", lazy="dynamic"
    )
    audit_logs = db.relationship("AuditLog", back_populates="user", lazy="dynamic")
    stock_ledger_entries = db.relationship(
        "StockLedger", back_populates="created_by_user", lazy="dynamic"
    )
    manufacturing_orders_assigned = db.relationship(
        "ManufacturingOrder", back_populates="assignee", lazy="dynamic"
    )
    field_permissions = db.relationship(
        "UserFieldPermission",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # --- Password helpers ------------------------------------------------
    def set_password(self, plain_password: str) -> None:
        """Hash and store password – never persist plaintext."""
        self.password_hash = generate_password_hash(plain_password)

    def check_password(self, plain_password: str) -> bool:
        return check_password_hash(self.password_hash, plain_password)

    @property
    def position_label(self) -> str:
        """Human-readable job title for profile UI."""
        if self.position:
            return self.position
        return self.role.value.replace("_", " ").title()

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"


class UserFieldPermission(db.Model):
    """
    Granular CRUD permissions per module field (admin-managed).

    Stored as JSON keyed by module → field_key → {create, view, edit, delete}.
    """

    __tablename__ = "user_field_permissions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    permissions = db.Column(db.JSON, nullable=False, default=dict)

    user = db.relationship("User", back_populates="field_permissions")

    def __repr__(self) -> str:
        return f"<UserFieldPermission user_id={self.user_id}>"


# ---------------------------------------------------------------------------
# Vendor – suppliers for purchase procurement
# ---------------------------------------------------------------------------


class Vendor(db.Model):
    """Supplier master data for auto-generated Purchase Orders."""

    __tablename__ = "vendors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False, unique=True)
    contact_info = db.Column(db.String(256), nullable=True)

    products = db.relationship("Product", back_populates="vendor")
    purchase_orders = db.relationship("PurchaseOrder", back_populates="vendor")

    def __repr__(self) -> str:
        return f"<Vendor {self.name}>"


# ---------------------------------------------------------------------------
# Work Center – factory capacity units for routing
# ---------------------------------------------------------------------------


class WorkCenter(db.Model):
    """Physical or logical production station (saw bench, assembly line, etc.)."""

    __tablename__ = "work_centers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.String(512), nullable=True)

    bom_operations = db.relationship("BOMOperation", back_populates="work_center")
    work_orders = db.relationship("WorkOrder", back_populates="work_center")

    def __repr__(self) -> str:
        return f"<WorkCenter {self.name}>"


# ---------------------------------------------------------------------------
# Product – Inventory master data
# ---------------------------------------------------------------------------


class Product(db.Model):
    """
    Inventory item with on-hand and reserved quantities.

    `free_to_use_qty` is a computed property, NOT a stored column.
    Storing it would create a race-condition vector under concurrent orders.
    """

    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False, unique=True)
    sales_price = db.Column(db.Numeric(12, 2), nullable=False)
    cost_price = db.Column(db.Numeric(12, 2), nullable=False)
    on_hand_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    reserved_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    procure_on_demand = db.Column(db.Boolean, nullable=False, default=False)
    procurement_type = db.Column(
        db.Enum(ProcurementType, name="procurement_type", create_constraint=True),
        nullable=True,
    )
    vendor_id = db.Column(
        db.Integer, db.ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True
    )
    bom_id = db.Column(
        db.Integer, db.ForeignKey("bom.id", ondelete="RESTRICT"), nullable=True
    )

    # --- Relationships ---------------------------------------------------
    vendor = db.relationship("Vendor", back_populates="products")
    bill_of_materials = db.relationship(
        "BillOfMaterials", back_populates="finished_product", foreign_keys="BillOfMaterials.finished_product_id"
    )
    default_bom = db.relationship("BillOfMaterials", foreign_keys=[bom_id])
    order_lines = db.relationship("SalesOrderLine", back_populates="product")
    stock_ledger_entries = db.relationship("StockLedger", back_populates="product")
    manufacturing_orders = db.relationship(
        "ManufacturingOrder",
        back_populates="product",
        foreign_keys="ManufacturingOrder.finished_product_id",
        lazy="dynamic",
    )
    bom_components = db.relationship(
        "BOMComponent", back_populates="component_product", foreign_keys="BOMComponent.component_product_id"
    )
    mo_components = db.relationship(
        "MOComponent", back_populates="component_product", foreign_keys="MOComponent.component_product_id"
    )
    purchase_order_lines = db.relationship("PurchaseOrderLine", back_populates="product")

    @property
    def free_to_use_qty(self):
        """
        Available stock = on_hand minus reserved.

        `reserved_qty` is maintained by state-machine transitions (SO confirm,
        MO confirm, delivery, production) – never stored as a duplicate column.
        """
        return self.on_hand_qty - self.reserved_qty

    def __repr__(self) -> str:
        return f"<Product {self.name} on_hand={self.on_hand_qty}>"


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class Customer(db.Model):
    """Business customer – referenced by Sales Orders."""

    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    address = db.Column(db.String(512), nullable=True)
    contact_info = db.Column(db.String(256), nullable=True)

    sales_orders = db.relationship("SalesOrder", back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer {self.name}>"


# ---------------------------------------------------------------------------
# Bill of Materials – master production template
# ---------------------------------------------------------------------------


class BillOfMaterials(db.Model):
    """BoM template linking a finished product to components and operations."""

    __tablename__ = "bom"

    id = db.Column(db.Integer, primary_key=True)
    bom_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    finished_product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity = db.Column(db.Integer, nullable=False, default=1)

    finished_product = db.relationship(
        "Product", back_populates="bill_of_materials", foreign_keys=[finished_product_id]
    )
    components = db.relationship(
        "BOMComponent",
        back_populates="bom",
        cascade="all, delete-orphan",
        order_by="BOMComponent.id",
    )
    operations = db.relationship(
        "BOMOperation",
        back_populates="bom",
        cascade="all, delete-orphan",
        order_by="BOMOperation.id",
    )
    manufacturing_orders = db.relationship("ManufacturingOrder", back_populates="bom")

    def __repr__(self) -> str:
        return f"<BillOfMaterials {self.bom_number}>"


class BOMComponent(db.Model):
    """Raw material / sub-assembly line on a BoM template."""

    __tablename__ = "bom_components"

    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(
        db.Integer, db.ForeignKey("bom.id", ondelete="CASCADE"), nullable=False
    )
    component_product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    quantity_required = db.Column(db.Numeric(12, 3), nullable=False)

    bom = db.relationship("BillOfMaterials", back_populates="components")
    component_product = db.relationship(
        "Product", back_populates="bom_components", foreign_keys=[component_product_id]
    )


class BOMOperation(db.Model):
    """Routing step on a BoM template."""

    __tablename__ = "bom_operations"

    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(
        db.Integer, db.ForeignKey("bom.id", ondelete="CASCADE"), nullable=False
    )
    operation_name = db.Column(db.String(256), nullable=False)
    work_center_id = db.Column(
        db.Integer, db.ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=False
    )
    duration_minutes = db.Column(db.Integer, nullable=False)

    bom = db.relationship("BillOfMaterials", back_populates="operations")
    work_center = db.relationship("WorkCenter", back_populates="bom_operations")


# ---------------------------------------------------------------------------
# Purchase Order – auto-generated on SO confirm shortfall (purchase path)
# ---------------------------------------------------------------------------


class PurchaseOrder(db.Model):
    """Purchase Order header – created manually or by procure-on-demand hook."""

    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    vendor_id = db.Column(
        db.Integer, db.ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    status = db.Column(
        db.Enum(PurchaseOrderStatus, name="purchase_order_status", create_constraint=True),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    vendor = db.relationship("Vendor", back_populates="purchase_orders")
    lines = db.relationship(
        "PurchaseOrderLine",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
    )

    @property
    def is_locked(self) -> bool:
        return self.status in (PurchaseOrderStatus.DONE, PurchaseOrderStatus.CANCELLED)

    @property
    def status_label(self) -> str:
        labels = {
            PurchaseOrderStatus.DRAFT: "Draft",
            PurchaseOrderStatus.CONFIRMED: "Confirmed",
            PurchaseOrderStatus.RECEIVED: "Received",
            PurchaseOrderStatus.DONE: "Done",
            PurchaseOrderStatus.CANCELLED: "Cancelled",
        }
        return labels.get(self.status, self.status.value.title())


class PurchaseOrderLine(db.Model):
    """Product line on a Purchase Order."""

    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    quantity = db.Column(db.Numeric(12, 3), nullable=False)
    received_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)

    purchase_order = db.relationship("PurchaseOrder", back_populates="lines")
    product = db.relationship("Product", back_populates="purchase_order_lines")

    @property
    def ordered_qty(self):
        """Alias for template clarity (quantity = ordered amount)."""
        return self.quantity


# ---------------------------------------------------------------------------
# Sales Order header & lines
# ---------------------------------------------------------------------------


class SalesOrder(db.Model):
    """
    Sales Order header.

    `so_number` is a human-readable reference (e.g. SO-000001) generated
    at creation time.  `total_amount` is recalculated whenever lines change.
    """

    __tablename__ = "sales_orders"

    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_address = db.Column(db.String(512), nullable=True)
    sales_person_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status = db.Column(
        db.Enum(SalesOrderStatus, name="sales_order_status", create_constraint=True),
        nullable=False,
        default=SalesOrderStatus.DRAFT,
    )
    total_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # --- Relationships ---------------------------------------------------
    customer = db.relationship("Customer", back_populates="sales_orders")
    sales_person = db.relationship("User", back_populates="sales_orders")
    lines = db.relationship(
        "SalesOrderLine",
        back_populates="sales_order",
        cascade="all, delete-orphan",
        lazy="joined",
        order_by="SalesOrderLine.id",
    )

    def recalculate_total(self) -> None:
        """
        Sum line totals.  Uses delivered_qty when the order has been
        partially or fully delivered (per wireframe pricing rules).
        """
        total = 0
        for line in self.lines:
            qty = (
                line.delivered_qty
                if self.status
                in (
                    SalesOrderStatus.PARTIALLY_DELIVERED,
                    SalesOrderStatus.DELIVERED,
                )
                else line.ordered_qty
            )
            total += qty * line.sales_price
        self.total_amount = total

    @property
    def is_fully_delivered(self) -> bool:
        """True when every line has delivered_qty == ordered_qty."""
        if not self.lines:
            return False
        return all(line.delivered_qty >= line.ordered_qty for line in self.lines)

    @property
    def status_label(self) -> str:
        """Human-readable status for UI (wireframe uses 'Fully Delivered')."""
        labels = {
            SalesOrderStatus.DRAFT: "Draft",
            SalesOrderStatus.CONFIRMED: "Confirmed",
            SalesOrderStatus.PARTIALLY_DELIVERED: "Partially Delivered",
            SalesOrderStatus.DELIVERED: "Fully Delivered",
            SalesOrderStatus.CANCELLED: "Cancelled",
        }
        return labels.get(self.status, self.status.value.replace("_", " ").title())

    def __repr__(self) -> str:
        return f"<SalesOrder {self.so_number} status={self.status.value}>"


class SalesOrderLine(db.Model):
    """
    Individual product line on a Sales Order.

    `sales_price` is a SNAPSHOT of Product.sales_price at the moment the
    line is created.  Subsequent product price changes must NOT retroactively
    alter confirmed orders – this is standard ERP price-locking behaviour.
    """

    __tablename__ = "sales_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    sales_order_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    ordered_qty = db.Column(db.Numeric(12, 3), nullable=False)
    delivered_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    # Stock actually reserved for this line at confirm time (may be < ordered_qty
    # when procure_on_demand filled only partial available stock).
    reserved_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    # Price snapshot – populated from Product.sales_price at line creation.
    sales_price = db.Column(db.Numeric(12, 2), nullable=False)

    # --- Relationships ---------------------------------------------------
    sales_order = db.relationship("SalesOrder", back_populates="lines")
    product = db.relationship("Product", back_populates="order_lines")

    @property
    def line_total(self):
        """Line total based on current order delivery state."""
        order = self.sales_order
        if order.status in (
            SalesOrderStatus.PARTIALLY_DELIVERED,
            SalesOrderStatus.DELIVERED,
        ):
            return self.delivered_qty * self.sales_price
        return self.ordered_qty * self.sales_price

    @property
    def is_fully_delivered(self) -> bool:
        """True when this line's delivered quantity meets or exceeds ordered."""
        return self.delivered_qty >= self.ordered_qty

    def __repr__(self) -> str:
        return f"<SalesOrderLine product={self.product_id} qty={self.ordered_qty}>"


# ---------------------------------------------------------------------------
# Stock Ledger – immutable inventory movement journal
# ---------------------------------------------------------------------------


class StockLedger(db.Model):
    """
    Append-only journal of every inventory quantity change.

    We never UPDATE or DELETE ledger rows – corrections are new ADJUSTMENT
    entries.  This gives a complete, auditable history for reconciliation.
    """

    __tablename__ = "stock_ledger"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_type = db.Column(
        db.Enum(StockTransactionType, name="stock_transaction_type", create_constraint=True),
        nullable=False,
    )
    reference_type = db.Column(
        db.Enum(StockReferenceType, name="stock_reference_type", create_constraint=True),
        nullable=False,
    )
    reference_id = db.Column(db.Integer, nullable=False)
    quantity_change = db.Column(db.Numeric(12, 3), nullable=False)
    resulting_on_hand_qty = db.Column(db.Numeric(12, 3), nullable=False)
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    product = db.relationship("Product", back_populates="stock_ledger_entries")
    created_by_user = db.relationship("User", back_populates="stock_ledger_entries")

    def __repr__(self) -> str:
        return (
            f"<StockLedger product={self.product_id} "
            f"type={self.transaction_type.value} delta={self.quantity_change}>"
        )


# ---------------------------------------------------------------------------
# Audit Log – field-level change tracking
# ---------------------------------------------------------------------------


class AuditLog(db.Model):
    """
    Permanent audit trail for status shifts and field changes.

    Each row captures WHO changed WHAT, WHEN, and the before/after values.
    Rows are insert-only – never updated or deleted.
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    module = db.Column(db.String(64), nullable=False)       # e.g. "sales"
    record_type = db.Column(db.String(64), nullable=False)  # e.g. "SalesOrder"
    record_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(64), nullable=False)       # e.g. "status_change"
    field_changed = db.Column(db.String(128), nullable=True)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog {self.module}/{self.record_type}#{self.record_id} {self.action}>"


# ---------------------------------------------------------------------------
# Manufacturing Order – production floor work orders
# ---------------------------------------------------------------------------


class ManufacturingOrder(db.Model):
    """
    Manufacturing Order (MO) – tells operators what to build and when.

    Linked to a finished `Product`, BoM template, and assigned floor `User`.
    """

    __tablename__ = "manufacturing_orders"

    id = db.Column(db.Integer, primary_key=True)
    mo_number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    finished_product_id = db.Column(
        "product_id",
        db.Integer,
        db.ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity = db.Column(db.Integer, nullable=False)
    bom_id = db.Column(
        db.Integer, db.ForeignKey("bom.id", ondelete="RESTRICT"), nullable=True
    )
    assignee_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status = db.Column(
        db.Enum(
            ManufacturingOrderStatus,
            name="manufacturing_order_status",
            create_constraint=True,
        ),
        nullable=False,
        default=ManufacturingOrderStatus.DRAFT,
    )
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    product = db.relationship(
        "Product",
        back_populates="manufacturing_orders",
        foreign_keys=[finished_product_id],
    )
    bom = db.relationship("BillOfMaterials", back_populates="manufacturing_orders")
    assignee = db.relationship("User", back_populates="manufacturing_orders_assigned")
    components = db.relationship(
        "MOComponent",
        back_populates="manufacturing_order",
        cascade="all, delete-orphan",
        order_by="MOComponent.id",
    )
    work_orders = db.relationship(
        "WorkOrder",
        back_populates="manufacturing_order",
        cascade="all, delete-orphan",
        order_by="WorkOrder.id",
    )

    @property
    def is_locked(self) -> bool:
        """Header fields immutable once confirmed."""
        return self.status != ManufacturingOrderStatus.DRAFT

    @property
    def is_readonly(self) -> bool:
        """All fields locked when done or cancelled."""
        return self.status in (
            ManufacturingOrderStatus.DONE,
            ManufacturingOrderStatus.CANCELLED,
        )

    @property
    def status_label(self) -> str:
        labels = {
            ManufacturingOrderStatus.DRAFT: "Draft",
            ManufacturingOrderStatus.CONFIRMED: "Confirmed",
            ManufacturingOrderStatus.IN_PROGRESS: "In Progress",
            ManufacturingOrderStatus.DONE: "Done",
            ManufacturingOrderStatus.CANCELLED: "Cancelled",
        }
        return labels.get(self.status, self.status.value.replace("_", " ").title())

    @property
    def status_badge_class(self) -> str:
        mapping = {
            ManufacturingOrderStatus.DRAFT: "bg-secondary",
            ManufacturingOrderStatus.CONFIRMED: "bg-info text-dark",
            ManufacturingOrderStatus.IN_PROGRESS: "bg-warning text-dark",
            ManufacturingOrderStatus.DONE: "bg-success",
            ManufacturingOrderStatus.CANCELLED: "bg-danger",
        }
        return mapping.get(self.status, "bg-secondary")

    @property
    def duration_display(self) -> str:
        """Aggregate expected vs. real minutes from work orders."""
        if not self.work_orders:
            return "—"
        expected = sum(wo.expected_duration for wo in self.work_orders)
        if self.status == ManufacturingOrderStatus.DONE:
            actual = sum(wo.real_duration for wo in self.work_orders)
            return f"{expected}m → {actual}m"
        if self.status == ManufacturingOrderStatus.IN_PROGRESS:
            return f"{expected}m (in progress)"
        return f"{expected}m"

    def __repr__(self) -> str:
        return f"<ManufacturingOrder {self.mo_number} status={self.status.value}>"


class MOComponent(db.Model):
    """Component consumption line on a Manufacturing Order."""

    __tablename__ = "mo_components"

    id = db.Column(db.Integer, primary_key=True)
    manufacturing_order_id = db.Column(
        db.Integer,
        db.ForeignKey("manufacturing_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    component_product_id = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    required_qty = db.Column(db.Numeric(12, 3), nullable=False)
    consumed_qty = db.Column(db.Numeric(12, 3), nullable=False, default=0)

    manufacturing_order = db.relationship("ManufacturingOrder", back_populates="components")
    component_product = db.relationship(
        "Product", back_populates="mo_components", foreign_keys=[component_product_id]
    )


class WorkOrder(db.Model):
    """Operation routing line on a Manufacturing Order."""

    __tablename__ = "work_orders"

    id = db.Column(db.Integer, primary_key=True)
    manufacturing_order_id = db.Column(
        db.Integer,
        db.ForeignKey("manufacturing_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_name = db.Column(db.String(256), nullable=False)
    work_center_id = db.Column(
        db.Integer, db.ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=False
    )
    expected_duration = db.Column(db.Integer, nullable=False)
    real_duration = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(
        db.Enum(WorkOrderStatus, name="work_order_status", create_constraint=True),
        nullable=False,
        default=WorkOrderStatus.PENDING,
    )

    manufacturing_order = db.relationship("ManufacturingOrder", back_populates="work_orders")
    work_center = db.relationship("WorkCenter", back_populates="work_orders")

"""
User permission schema and persistence for the admin user-management UI.

Wireframe defines field-level Create / View / Edit / Delete per ERP module.
Some cells are system-controlled (auto compute, recomputed, not possible).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from extensions import db
from models import User, UserFieldPermission, UserRole

ADMIN_POSITIONS = (
    "Admin",
    "Sales Manager",
    "Salesperson",
    "Manufacturing Operator",
    "Business Owner",
)

POSITION_ROLE_MAP: dict[str, UserRole] = {
    "Admin": UserRole.ADMIN,
    "Sales Manager": UserRole.SALES,
    "Salesperson": UserRole.SALES,
    "Manufacturing Operator": UserRole.MANUFACTURING,
    "Business Owner": UserRole.OWNER,
}

MODULE_LABELS = {
    "sales": "Sales",
    "purchase": "Purchase",
    "manufacturing": "Manufacturing",
    "product": "Product",
}

PERMISSION_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "sales": [
        {"field": "Customer", "key": "customer", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Customer Address", "key": "customer_address", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Sales Person", "key": "sales_person", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Product", "key": "product", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Ordered Quantity", "key": "ordered_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Delivered Quantity", "key": "delivered_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Sales Price", "key": "sales_price", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Status", "key": "status", "create": True, "view": True, "edit": True, "delete": False},
        {"field": "Total", "key": "total", "create": True, "view": True, "edit": "recomputed", "delete": False},
        {"field": "Creation Date", "key": "creation_date", "create": "auto_compute", "view": True, "edit": False, "delete": False},
    ],
    "purchase": [
        {"field": "Vendor", "key": "vendor", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Vendor Address", "key": "vendor_address", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Responsible Person", "key": "responsible_person", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Product", "key": "product", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Ordered Quantity", "key": "ordered_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Received Quantity", "key": "received_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Cost Price", "key": "cost_price", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Total", "key": "total", "create": True, "view": True, "edit": "recomputed", "delete": False},
        {"field": "Creation Date", "key": "creation_date", "create": "auto_compute", "view": True, "edit": False, "delete": False},
    ],
    "manufacturing": [
        {"field": "Product to Manufacture", "key": "product_to_manufacture", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Product Quantity", "key": "product_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "BoM", "key": "bom", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Responsible Person", "key": "responsible_person", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Finished Quantity", "key": "finished_qty", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Creation Date", "key": "creation_date", "create": "auto_compute", "view": True, "edit": False, "delete": False},
    ],
    "product": [
        {"field": "Product", "key": "product", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Sales Price", "key": "sales_price", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Cost Price", "key": "cost_price", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "On Hand Qty", "key": "on_hand_qty", "create": True, "view": True, "edit": True, "delete": False},
        {"field": "Free To Use Qty", "key": "free_to_use_qty", "create": True, "view": True, "edit": "system_computed", "delete": False},
        {"field": "Procure On Demand", "key": "procure_on_demand", "create": "not_possible", "view": True, "edit": True, "delete": True},
        {"field": "Procurement Method", "key": "procurement_method", "create": "not_possible", "view": True, "edit": True, "delete": True},
        {"field": "Vendor", "key": "vendor", "create": True, "view": True, "edit": True, "delete": True},
        {"field": "Bill of Materials (BoM)", "key": "bom", "create": True, "view": True, "edit": True, "delete": True},
    ],
}

ACTIONS = ("create", "view", "edit", "delete")

LABEL_DISPLAY = {
    "auto_compute": "Auto Compute",
    "recomputed": "Recomputed",
    "system_computed": "System Computed",
    "not_possible": "Not possible",
}

# Legacy seed values → wireframe position labels
LEGACY_POSITION_MAP = {
    "Sales": "Salesperson",
    "Manufacturing": "Manufacturing Operator",
    "Owner": "Business Owner",
}

ROLE_DEFAULT_POSITION: dict[UserRole, str] = {
    UserRole.ADMIN: "Admin",
    UserRole.SALES: "Salesperson",
    UserRole.MANUFACTURING: "Manufacturing Operator",
    UserRole.OWNER: "Business Owner",
}


def resolve_admin_position(user: User) -> str:
    """Map stored user data to the position dropdown value (wireframe labels)."""
    if user.position:
        if user.position in ADMIN_POSITIONS:
            return user.position
        return LEGACY_POSITION_MAP.get(user.position, user.position)
    return ROLE_DEFAULT_POSITION.get(user.role, "Salesperson")


def default_permissions_for_role(role: UserRole) -> dict[str, dict[str, dict[str, bool]]]:
    """Build default permission map from schema."""
    result: dict[str, dict[str, dict[str, bool]]] = {}
    for module, rows in PERMISSION_SCHEMA.items():
        result[module] = {}
        for row in rows:
            field_perms: dict[str, bool] = {}
            for action in ACTIONS:
                val = row.get(action)
                field_perms[action] = bool(val) if isinstance(val, bool) else False
            result[module][row["key"]] = field_perms
    return result


def get_user_permissions(user: User) -> dict[str, dict[str, dict[str, bool]]]:
    """Return merged permissions for template rendering."""
    defaults = default_permissions_for_role(user.role)
    if user.field_permissions and user.field_permissions.permissions:
        saved = user.field_permissions.permissions
        merged = deepcopy(defaults)
        for module, fields in saved.items():
            if module not in merged:
                merged[module] = {}
            for field_key, actions in fields.items():
                if field_key not in merged[module]:
                    merged[module][field_key] = {}
                for action, val in actions.items():
                    if action in ACTIONS and isinstance(val, bool):
                        merged[module][field_key][action] = val
        return merged
    return defaults


def build_module_rows(module: str, user: User) -> list[dict[str, Any]]:
    """Merge schema rows with saved booleans for the permissions table."""
    perms = get_user_permissions(user)
    rows = []
    for spec in PERMISSION_SCHEMA[module]:
        key = spec["key"]
        saved = perms.get(module, {}).get(key, {})
        row = {"field": spec["field"], "key": key, "cells": {}}
        for action in ACTIONS:
            schema_val = spec.get(action)
            if isinstance(schema_val, bool):
                row["cells"][action] = {
                    "type": "checkbox",
                    "value": saved.get(action, schema_val),
                    "name": f"perm_{module}_{key}_{action}",
                }
            else:
                row["cells"][action] = {"type": "label", "value": schema_val}
        rows.append(row)
    return rows


def parse_permissions_from_form(form_data) -> dict[str, dict[str, dict[str, bool]]]:
    """Read permission checkboxes posted from the admin form."""
    result: dict[str, dict[str, dict[str, bool]]] = {}
    for module, rows in PERMISSION_SCHEMA.items():
        result[module] = {}
        for row in rows:
            key = row["key"]
            field_perms: dict[str, bool] = {}
            for action in ACTIONS:
                if not isinstance(row.get(action), bool):
                    continue
                field_name = f"perm_{module}_{key}_{action}"
                field_perms[action] = field_name in form_data
            result[module][key] = field_perms
    return result


def save_user_admin_settings(
    *,
    user: User,
    position: str,
    permissions: dict[str, dict[str, dict[str, bool]]],
) -> None:
    """Persist position, role mapping, and field permissions."""
    if position not in POSITION_ROLE_MAP:
        raise ValueError(f"Invalid position: {position}")

    user.position = position
    user.role = POSITION_ROLE_MAP[position]

    if user.field_permissions is None:
        user.field_permissions = UserFieldPermission(user_id=user.id, permissions=permissions)
        db.session.add(user.field_permissions)
    else:
        user.field_permissions.permissions = permissions

    db.session.commit()


def create_user_with_defaults(
    *,
    name: str,
    email: str,
    address: str,
    mobile_number: str,
    position: str,
    password: str,
) -> User:
    """Admin-created user with default field permissions for their role."""
    if position not in POSITION_ROLE_MAP:
        raise ValueError(f"Invalid position: {position}")

    existing = db.session.scalar(select(User).where(User.email == email))
    if existing:
        raise ValueError("An account with this email already exists.")

    user = User(
        name=name,
        email=email,
        address=address,
        mobile_number=mobile_number,
        position=position,
        role=POSITION_ROLE_MAP[position],
    )
    user.set_password(password)
    user.field_permissions = UserFieldPermission(
        permissions=default_permissions_for_role(user.role)
    )
    db.session.add(user)
    db.session.commit()
    return user

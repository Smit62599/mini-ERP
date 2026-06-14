"""Sales order row-level access control (admin vs salesperson isolation)."""

from flask import abort
from flask_login import current_user

from models import SalesOrder, UserRole


def apply_sales_order_list_filter(query):
    """
    Sales users see ONLY their own orders.
    Admins (and other roles) see the full enterprise dataset.
    """
    if current_user.role == UserRole.SALES:
        return query.filter(SalesOrder.sales_person_id == current_user.id)
    return query


def assert_sales_order_access(order: SalesOrder | None) -> SalesOrder:
    """Return 403 if a salesperson attempts to access another user's order."""
    if order is None:
        abort(404)
    if current_user.role == UserRole.ADMIN:
        return order
    if current_user.role == UserRole.SALES:
        if order.sales_person_id != current_user.id:
            abort(403)
        return order
    return order

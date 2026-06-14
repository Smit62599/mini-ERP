"""
RBAC decorators.

Role checks happen at the route layer (presentation) while business rules
live in services.  This separation keeps authorization concerns out of
domain logic and makes unit testing services easier.
"""

from functools import wraps

from flask import abort
from flask_login import current_user

from models import UserRole


def role_required(*allowed_roles: UserRole):
    """
    Restrict a view to users whose role is in `allowed_roles`.

    Usage:
        @role_required(UserRole.ADMIN, UserRole.SALES)
        def my_view(): ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in allowed_roles:
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped

    return decorator

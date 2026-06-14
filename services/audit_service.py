"""
Audit logging service.

Centralises all AuditLog inserts so every module records changes in a
consistent format.  Callers pass the acting user, module name, and the
before/after values – this service handles persistence.
"""

from extensions import db
from models import AuditLog


def log_audit(
    *,
    user_id: int,
    module: str,
    record_type: str,
    record_id: int,
    action: str,
    field_changed: str | None = None,
    old_value=None,
    new_value=None,
) -> AuditLog:
    """
    Insert a permanent audit row within the current database transaction.

    Values are coerced to strings so JSON/complex types don't break the
    TEXT columns.  The caller's transaction (commit/rollback) governs
    whether this row is persisted – we do NOT commit here.
    """
    entry = AuditLog(
        user_id=user_id,
        module=module,
        record_type=record_type,
        record_id=record_id,
        action=action,
        field_changed=field_changed,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
    )
    db.session.add(entry)
    return entry


def log_status_change(
    *,
    user_id: int,
    module: str,
    record_type: str,
    record_id: int,
    old_status: str,
    new_status: str,
) -> AuditLog:
    """
    Convenience wrapper for status transitions – the most common audit event.

    Every status shift (Draft→Confirmed, Confirmed→Delivered, etc.) MUST
    go through this helper so the audit trail is complete.
    """
    return log_audit(
        user_id=user_id,
        module=module,
        record_type=record_type,
        record_id=record_id,
        action="status_change",
        field_changed="status",
        old_value=old_status,
        new_value=new_status,
    )


def log_field_change(
    *,
    user_id: int,
    module: str,
    record_type: str,
    record_id: int,
    field_name: str,
    old_value,
    new_value,
) -> AuditLog:
    """Track a non-status field mutation (e.g. sales_price on a line item)."""
    return log_audit(
        user_id=user_id,
        module=module,
        record_type=record_type,
        record_id=record_id,
        action="field_update",
        field_changed=field_name,
        old_value=old_value,
        new_value=new_value,
    )

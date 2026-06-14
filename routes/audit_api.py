"""Async audit log API for modal viewers on form pages."""

from flask import Blueprint, jsonify
from flask_login import login_required

from models import AuditLog

audit_bp = Blueprint("audit_api", __name__, url_prefix="/api/audit")


@audit_bp.route("/<module>/<record_type>/<int:record_id>")
@login_required
def record_logs(module: str, record_type: str, record_id: int):
    logs = (
        AuditLog.query.filter_by(
            module=module,
            record_type=record_type,
            record_id=record_id,
        )
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return jsonify(
        [
            {
                "id": log.id,
                "user": log.user.name if log.user else "System",
                "action": log.action,
                "field_changed": log.field_changed,
                "old_value": log.old_value,
                "new_value": log.new_value,
                "created_at": log.created_at.strftime("%d %b %Y %H:%M:%S"),
            }
            for log in logs
        ]
    )

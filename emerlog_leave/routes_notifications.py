from flask import Blueprint, jsonify, request, session

from .database import get_db
from .services import login_required, role_required

bp = Blueprint("request_notifications", __name__)


@bp.route("/requests/seen", methods=["POST"])
@login_required
@role_required("admin")
def mark_requests_seen():
    payload = request.get_json(silent=True) or {}
    request_ids = []

    for value in payload.get("request_ids", []):
        try:
            request_id = int(value)
        except (TypeError, ValueError):
            continue
        if request_id > 0 and request_id not in request_ids:
            request_ids.append(request_id)

    if not request_ids:
        return jsonify({"ok": True, "seen": 0})

    conn = get_db()
    placeholders = ",".join("?" for _ in request_ids)
    cur = conn.execute(
        f"""
        UPDATE request_notifications
        SET seen_at = CURRENT_TIMESTAMP
        WHERE recipient_user_id = ?
          AND seen_at IS NULL
          AND request_id IN ({placeholders})
        """,
        (session["user_id"], *request_ids),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return jsonify({"ok": True, "seen": changed})

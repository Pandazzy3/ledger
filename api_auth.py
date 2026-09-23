"""API authentication helpers — token-based, no browser session required."""
from datetime import datetime
from functools import wraps

from flask import request, jsonify, g

from models import db, ApiToken


def require_token(f):
    """Decorator: require a valid Bearer token in the Authorization header.

    Usage:
        Authorization: Bearer <token>
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Missing Bearer token"}), 401

        token_value = header[len("Bearer "):].strip()
        if not token_value:
            return jsonify({"error": "Empty token"}), 401

        token = ApiToken.query.filter_by(token=token_value).first()
        if not token:
            return jsonify({"error": "Invalid token"}), 401

        token.last_used_at = datetime.utcnow()
        db.session.commit()

        g.current_user = token.user
        g.api_token = token
        return f(*args, **kwargs)
    return wrapper
import secrets
from datetime import datetime, timezone

import bcrypt
from fastapi import HTTPException, Request
from loguru import logger

from app import db
from app.config import DEFAULT_TAG_DEFINITIONS

SESSION_COOKIE_NAME = "wevents_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash - treat as a failed verification, not a crash
        return False


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(token, user_id)
    return token


def delete_session(token: str):
    db.delete_session(token)


def get_user_from_token(token: str) -> dict | None:
    row = db.get_session_with_user(token)
    if row is None:
        return None
    db.touch_session(token)
    return row


def get_current_user(request: Request) -> dict:
    ''' FastAPI dependency - resolves the session cookie to a user row,
    raising 401 if missing/invalid so the frontend knows to show the
    login screen.
    '''
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "not authenticated")
    user = get_user_from_token(token)
    if user is None:
        raise HTTPException(401, "session expired or invalid")
    return user


def create_user(username: str, password: str) -> int:
    ''' Creates a user and seeds their default tag_definitions (the
    starting manual-entry buttons from spec §4), so a freshly created
    household member isn't looking at an empty Tags tab.
    '''
    user_id = db.create_user(username, hash_password(password))
    for tag, label, category, is_duration, sort_order in DEFAULT_TAG_DEFINITIONS:
        db.add_tag_definition(user_id, tag, label, category, is_duration=is_duration, sort_order=sort_order)
    return user_id


def bootstrap_admin_if_configured(admin_username: str | None, admin_password: str | None):
    ''' On startup, if no users exist yet and ADMIN_USERNAME/ADMIN_PASSWORD
    are set, create the first account automatically. There's no public
    signup page by design (this is meant to be reached only over
    Tailscale by household members) - after this first account exists,
    logged-in users can add further accounts from the UI.
    '''
    existing = db.count_users()
    if existing > 0:
        logger.info(f"Skipping bootstrap - {existing} user(s) already exist")
        return
    if not admin_username or not admin_password:
        logger.warning(
            "No users exist and ADMIN_USERNAME/ADMIN_PASSWORD are not both set - "
            "no account was created. Login will fail until one exists; set both "
            "env vars and restart, or create a user directly in the SQLite database."
        )
        return
    create_user(admin_username, admin_password)
    logger.info(f"Bootstrapped initial user: {admin_username!r}")
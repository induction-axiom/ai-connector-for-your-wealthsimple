"""One answer to "can the connector reach Wealthsimple?", shared by every entry point."""

from datetime import datetime


def connection_state(state, now):
    """not_connected, reconnect_required, connected or unknown, from the connector document.

    rotation_pending is set just before a refresh token is spent and cleared once the new
    session is saved. Left set with no sync running, nobody is finishing that rotation, and
    the old token may already be spent: only signing in again recovers.
    """
    session = state.get("session") or {}
    lease_until = (state.get("sync") or {}).get("lease_until")
    rotating = isinstance(lease_until, datetime) and lease_until > now
    if session.get("signed_out") is True:
        return "not_connected"
    if (session.get("reconnect_required") is True
            or (session.get("rotation_pending") is True and not rotating)):
        return "reconnect_required"
    if isinstance(session.get("secret_version"), str) and session["secret_version"]:
        return "connected"
    return "unknown"

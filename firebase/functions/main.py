"""Thin Firebase entry points for the private portfolio service."""

import json
import logging
import os
import firebase_admin
from firebase_functions import https_fn, scheduler_fn
from firebase_functions.params import StringParam

from portfolio_service import RECONNECT_ERRORS, MfaRequired, PortfolioError, PortfolioService


REGION = os.environ.get("CONNECTOR_REGION", "us-east4")
SYNC_SERVICE_ACCOUNT = StringParam("SYNC_SERVICE_ACCOUNT")

firebase_admin.initialize_app()

service = PortfolioService(
    secret_id=os.environ.get("SESSION_SECRET_ID", "wealthsimple-session"),
    database=os.environ.get("PORTFOLIO_DATABASE", "(default)"),
    currency=os.environ.get("VIEW_CURRENCY", "CAD"),
    stale_seconds=int(os.environ.get("STALE_SECONDS", "25200")),
)


# Every outcome is a 200 with {"result": code}; callers are IAM-authenticated, and one
# vocabulary of codes flows unchanged to the dashboard and the AI tools.
def _refresh_outcome(code):
    if code in RECONNECT_ERRORS:
        return {"result": "reconnect_required", "error": code}
    if code in {"refresh_cooldown", "sync_already_running"}:
        return {"result": code}
    return {"result": "refresh_failed", "error": code}


RECONNECT_OUTCOMES = {"mfa_required", "login_rejected", "rate_limited_stop",
                      "sync_already_running", "reconnect_input_invalid"}


def _json_response(value, status=200):
    return https_fn.Response(
        json.dumps(value, ensure_ascii=False),
        status=status,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


# Data is synced when your AI or the dashboard asks. This schedule only keeps the
# Wealthsimple sign-in alive, so an unused week doesn't mean signing in again.
@scheduler_fn.on_schedule(
    schedule="every 12 hours",
    region=REGION,
    service_account=SYNC_SERVICE_ACCOUNT,
    timeout_sec=60,
    memory=512,
    min_instances=0,
    max_instances=1,
    concurrency=1,
    retry_count=0,
)
def keep_session_alive(_event):
    try:
        service.keep_session_alive()
        logging.info("wealthsimple_session_refreshed")
    except PortfolioError as error:
        if str(error) == "signed_out":
            return  # nothing to keep alive
        logging.error("wealthsimple_session_refresh_failed code=%s", str(error))
        raise


@https_fn.on_request(
    region=REGION,
    service_account=SYNC_SERVICE_ACCOUNT,
    invoker="private",
    timeout_sec=300,
    memory=512,
    min_instances=0,
    max_instances=1,
    concurrency=1,
)
def request_refresh(request):
    if request.method != "POST":
        return _json_response({"error": "method_not_allowed"}, 405)
    body = request.get_json(silent=True) or {}
    target = body.get("target")
    if target not in {"portfolio", "activities"}:
        return _json_response({"error": "refresh_target_invalid"}, 400)
    try:
        # Only the dashboard asks for force; it skips reusing a fresh activity pass.
        return _json_response(service.sync(cooldown_seconds=600) if target == "portfolio"
                              else service.sync_activity(force=body.get("force") is True))
    except PortfolioError as error:
        return _json_response(_refresh_outcome(str(error)))


@https_fn.on_request(
    region=REGION,
    service_account=SYNC_SERVICE_ACCOUNT,
    invoker="private",
    timeout_sec=60,
    memory=512,
    min_instances=0,
    max_instances=1,
    concurrency=1,
)
def reconnect_now(request):
    if request.method != "POST":
        return _json_response({"error": "method_not_allowed"}, 405)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json_response({"error": "reconnect_input_invalid"}, 400)
    try:
        if body.get("sign_out") is True:
            return _json_response(service.sign_out())
        return _json_response(service.reconnect(
            body.get("username"), body.get("password"), body.get("otp")))
    except MfaRequired as error:
        # Where the code went, so the dashboard asks for the right one; null when unknown.
        return _json_response({"result": "mfa_required", "method": error.method,
                               "hint": error.hint})
    except PortfolioError as error:
        code = str(error)
        return _json_response({"result": code} if code in RECONNECT_OUTCOMES
                              else {"result": "reconnect_failed", "error": code})

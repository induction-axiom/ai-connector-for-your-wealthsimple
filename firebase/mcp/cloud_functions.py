"""Call the IAM-private connector functions; they answer {"result": code, ...}."""

import json
from urllib.request import Request, build_opener, HTTPRedirectHandler

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.id_token import fetch_id_token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CloudFunctions:
    def __init__(self, refresh_url, reconnect_url):
        self.refresh_url = refresh_url
        self.reconnect_url = reconnect_url

    @staticmethod
    def _post(url, body, timeout):
        token = fetch_id_token(AuthRequest(), url)
        request = Request(url, data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            return json.load(response)

    def refresh(self, target, force=False):
        """force skips reusing a fresh activity pass; only the dashboard sets it."""
        try:
            return self._post(self.refresh_url,
                              {"target": target, **({"force": True} if force else {})}, 280)
        except Exception:
            # Timeout or transport failure: the sync may still have finished.
            return {"result": "refresh_result_unknown"}

    def sign_out(self):
        try:
            return self._post(self.reconnect_url, {"sign_out": True}, 55)
        except Exception:
            return {"result": "sign_out_failed"}

    def reconnect(self, username, password, otp=None):
        try:
            return self._post(self.reconnect_url,
                              {"username": username, "password": password, "otp": otp}, 55)
        except Exception:
            return {"result": "reconnect_failed"}

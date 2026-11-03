"""SAP Service Layer transport shared by the purchase-order sink."""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.target_sdk.client import HotglueSink


class SapAPIError(FatalAPIError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"SAP request failed (HTTP {status_code}); check SAP permissions and payload")


class SapSink(HotglueSink):
    timeout = (5, 30)
    unified_schema = None
    allows_externalid = ["BuyOrders"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The pinned SDK otherwise shares processed hashes between sink instances.
        self.processed_hashes = []
        self.previous_state = None
        self.summary_init = False
        self._session_cookie = None
        self._session_expires_at = 0

    def get_previous_state(self):
        previous = super().get_previous_state()
        if previous:
            # Adopt the SDK's filtered copy, not its stale pre-filter constructor copy.
            self._state = previous
        return previous

    @property
    def base_url(self):
        return self.config["api_url"].rstrip("/") + "/"

    @property
    def http_headers(self):
        if self._session_cookie is None or time.monotonic() >= self._session_expires_at:
            self._login()
        return {"Content-Type": "application/json", "Cookie": self._session_cookie}

    def _login(self):
        response = requests.post(
            self.url("Login"),
            json={
                "CompanyDB": self.config["company_db"],
                "UserName": self.config["username"],
                "Password": self.config["password"],
            },
            timeout=self.timeout,
            allow_redirects=False,
        )
        self.validate_response(response)
        data = self.read_json(response)
        session_id = data.get("SessionId")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            raise FatalAPIError("SAP Login did not return a valid SessionId")
        try:
            minutes = float(data.get("SessionTimeout", 30))
            if not 0 < minutes <= 1440:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise FatalAPIError("SAP Login returned an invalid SessionTimeout") from error
        self._session_cookie = f"B1SESSION={session_id}"
        route = response.cookies.get("ROUTEID")
        if route:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", route):
                raise FatalAPIError("SAP Login returned an invalid routing cookie")
            self._session_cookie += f"; ROUTEID={route}"
        self._session_expires_at = time.monotonic() + max(0, minutes * 60 - 30)

    def validate_response(self, response):
        status = response.status_code
        if status == 401:
            raise InvalidCredentialsError("SAP authentication failed; no automatic write retry")
        if status == 429 or status in (500, 502, 503, 504):
            raise RetriableAPIError(f"Transient SAP HTTP {status}", response)
        if not 200 <= status < 300:
            raise SapAPIError(status)

    @staticmethod
    def read_json(response):
        try:
            data = response.json()
        except ValueError as error:
            raise FatalAPIError("SAP returned malformed JSON") from error
        if not isinstance(data, dict) or data.get("error"):
            raise FatalAPIError("SAP returned an invalid or rejected response")
        return data

    def _request(self, http_method, endpoint, params=None, request_data=None, headers=None, verify=True):
        if not verify:
            raise ValueError("TLS verification cannot be disabled")
        response = requests.request(
            method=http_method,
            url=self.url(endpoint),
            params=params,
            json=request_data,
            headers={**self.http_headers, **(headers or {})},
            timeout=self.timeout,
            allow_redirects=False,
        )
        self.validate_response(response)
        return response

    @staticmethod
    def _retry_delay(error, attempt):
        delay = random.uniform(0, 2 ** attempt)
        response = getattr(error, "response", None)
        value = response.headers.get("Retry-After") if response is not None else None
        if value:
            try:
                delay = max(delay, float(value))
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(value)
                    delay = max(delay, (deadline - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30, max(0, delay))

    def request_api(self, http_method, endpoint=None, params=None, request_data=None, headers=None, verify=True):
        method = http_method.upper()
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                return self._request(method, endpoint or self.endpoint, params, request_data, headers, verify)
            except requests.exceptions.SSLError as error:
                raise FatalAPIError("SAP TLS verification failed") from error
            except (RetriableAPIError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                if attempt + 1 == attempts:
                    raise
                time.sleep(self._retry_delay(error, attempt))

    def invalidate_record_state(self, external_id):
        bookmarks = self.latest_state["bookmarks"][self.name]
        superseded = {row.get("hash") for row in bookmarks if row.get("externalId") == external_id}
        self.latest_state["bookmarks"][self.name] = [row for row in bookmarks if row.get("externalId") != external_id]
        self.processed_hashes = [value for value in self.processed_hashes if value not in superseded]

    def update_state(self, state, is_duplicate=False, record=None):
        if state.get("externalId"):
            self.invalidate_record_state(state["externalId"])
        super().update_state(state, is_duplicate=is_duplicate, record=record)
        self._target.persist_state(self.latest_state)
        # The SDK catches per-record errors; make them fail the job before STATE.
        if not state.get("success"):
            raise FatalAPIError("SAP order failed; retained input and target state are required for retry")

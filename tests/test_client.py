import json

import pytest
import requests
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.exceptions import ConfigValidationError, FatalAPIError, RetriableAPIError

from target_sap.client import SapAPIError
from target_sap.target import TargetSap


PARAMS = {"$filter": "ItemCode eq 'SKU-DEMO'"}


def test_safe_get_retries_transient_failures_with_bounded_delay(sink, sap, monkeypatch):
    sap.statuses["GET Items"] = [503, 429]
    delays = []
    monkeypatch.setattr("target_sap.client.time.sleep", delays.append)
    response = sink.request_api("GET", "Items", params=PARAMS)
    assert response.json()["value"] == [{"ItemCode": "SKU-DEMO", "PurchaseItemsPerUnit": 6}]
    assert len([row for row in sap.requests if row["endpoint"] == "Items"]) == 3
    assert len(delays) == 2 and all(0 <= delay <= 30 for delay in delays)


def test_safe_get_stops_after_three_attempts(sink, sap, monkeypatch):
    sap.statuses["GET Items"] = [503, 503, 503, 200]
    monkeypatch.setattr("target_sap.client.time.sleep", lambda _: None)
    with pytest.raises(RetriableAPIError):
        sink.request_api("GET", "Items", params=PARAMS)
    assert len([row for row in sap.requests if row["endpoint"] == "Items"]) == 3


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_permanent_errors_are_not_retried(sink, sap, status):
    sap.statuses["GET Items"] = [status, 200]
    with pytest.raises(SapAPIError) as error:
        sink.request_api("GET", "Items", params=PARAMS)
    assert error.value.status_code == status
    assert len([row for row in sap.requests if row["endpoint"] == "Items"]) == 1


def test_401_is_not_retried_or_silently_reauthenticated(sink, sap):
    sap.statuses["GET Items"] = [401, 200]
    with pytest.raises(InvalidCredentialsError):
        sink.request_api("GET", "Items", params=PARAMS)
    assert [row["endpoint"] for row in sap.requests] == ["Login", "Items"]


def test_tls_verification_is_enforced(sink, sap, monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    with pytest.raises(FatalAPIError, match="TLS verification failed"):
        sink.request_api("GET", "Items", params=PARAMS)
    assert not sap.requests


def test_known_session_expiry_logs_in_before_next_read(sink, sap):
    sink.request_api("GET", "Items", params=PARAMS)
    sink._session_expires_at = 0
    response = sink.request_api("GET", "Items", params=PARAMS)
    assert response.json()["value"][0]["PurchaseItemsPerUnit"] == 6
    assert [row["endpoint"] for row in sap.requests] == ["Login", "Items", "Login", "Items"]


@pytest.mark.parametrize("method,attempts", [("GET", 3), ("POST", 1), ("PATCH", 1)])
def test_transport_timeout_never_blindly_retries_a_write(sink, monkeypatch, method, attempts):
    calls = []

    def timed_out(*args, **kwargs):
        calls.append(method)
        raise requests.exceptions.ReadTimeout("synthetic timeout")

    monkeypatch.setattr(sink, "_request", timed_out)
    monkeypatch.setattr("target_sap.client.time.sleep", lambda _: None)
    with pytest.raises(requests.exceptions.ReadTimeout):
        sink.request_api(method, "PurchaseOrders")
    assert calls == [method] * attempts


def test_retry_after_cannot_block_unboundedly(sink):
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "999999"
    assert sink._retry_delay(RetriableAPIError("rate limited", response), 0) == 30


@pytest.mark.parametrize("base_url", ["http://localhost/b1s/v2/", "https://user:pass@localhost/b1s/v2/", "https://localhost/b1s/v1/", "https://localhost/b1s/v2/?token=bad"])
def test_unsafe_service_root_rejected_before_authentication(config, sap, base_url):
    config["base_url"] = base_url
    with pytest.raises(ConfigValidationError):
        TargetSap(config=config)
    assert not sap.requests


def test_create_503_is_not_reposted(cli, source_record, sap, config):
    sap.statuses["POST PurchaseOrders"] = [503, 201]
    failed = cli([source_record])
    assert failed.returncode != 0 and failed.stdout == ""
    posts = [row for row in sap.requests if row["method"] == "POST" and row["endpoint"] == "PurchaseOrders"]
    assert len(posts) == 1
    with open(config["state_path"]) as state:
        assert json.load(state)["sap_pending_creates"] == {"900001": True}


def test_redirect_is_rejected_without_sending_session_to_redirect_target(sink, sap):
    sap.statuses["GET Items"] = [302]
    with pytest.raises(SapAPIError) as error:
        sink.request_api("GET", "Items", params=PARAMS)
    assert error.value.status_code == 302
    assert [row["endpoint"] for row in sap.requests] == ["Login", "Items"]

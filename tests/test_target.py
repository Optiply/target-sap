import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hotglue_singer_sdk.exceptions import FatalAPIError

from target_sap.target import TargetSap

from conftest import ROOT


def state_file(config):
    return json.loads(Path(config["state_path"]).read_text())


def writes(sap):
    return [request for request in sap.requests if request["method"] in ("POST", "PATCH") and request["endpoint"] != "Login"]


def test_cli_create_warm_and_cold_replay(cli, source_record, sap, config):
    result = cli(input_text=(ROOT / "tests/fixtures/buy_orders.jsonl").read_text())
    assert result.returncode == 0, result.stderr
    emitted = json.loads(result.stdout)
    assert emitted["bookmarks"]["BuyOrders"][0]["id"] == 5001
    assert emitted["bookmarks"]["BuyOrders"][0]["externalId"] == "900001"
    assert emitted["bookmarks"]["BuyOrders"][0]["success"] is True
    order = sap.orders[5001]
    assert {key: order[key] for key in ("CardCode", "DocDueDate", "U_OPT_PO_HeaderID")} == {
        "CardCode": "VENDOR-DEMO", "DocDueDate": "2026-10-17", "U_OPT_PO_HeaderID": "900001",
    }
    assert {key: order["DocumentLines"][0][key] for key in ("ItemCode", "Quantity", "LineTotal", "U_OPT_PO_LineID")} == {
        "ItemCode": "SKU-DEMO", "Quantity": 4, "LineTotal": 120, "U_OPT_PO_LineID": "900101",
    }
    assert set(writes(sap)[0]["body"]) == {"CardCode", "DocDueDate", "U_OPT_PO_HeaderID", "DocumentLines"}
    warm = cli([source_record])
    assert warm.returncode == 0, warm.stderr
    Path(config["state_path"]).unlink()
    cold = cli([source_record])
    assert cold.returncode == 0, cold.stderr
    assert list(sap.orders) == [5001]
    assert [row["method"] for row in writes(sap)] == ["POST"]
    print("CLI smoke: DocEntry=5001; Saturday retained; quantity=24/6=4; LineTotal=120; warm/cold replay=one POST")


def test_cli_patch_matches_line_ids_preserves_sap_fields_and_accepts_204(cli, source_record, sap):
    lines = json.loads(source_record["line_items"])
    lines.append({"line_id": "900102", "sku": "SKU-DEMO", "quantity": 12, "sub_total_price": 80})
    source_record["line_items"] = lines
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    source_record["created_at"] = "2026-10-18T12:00:00Z"
    lines[1].update(quantity=18, sub_total_price=90)
    source_record["line_items"] = [lines[1], lines[0]]
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    order = sap.orders[5001]
    assert order["DocDueDate"] == "2026-10-18"
    assert order["Comments"] == "SAP owned"
    assert order["DocDate"] == "2026-10-01"
    assert [(row["LineNum"], row["U_OPT_PO_LineID"], row["Quantity"], row["LineTotal"]) for row in order["DocumentLines"]] == [
        (10, "900101", 4, 120), (20, "900102", 3, 90),
    ]
    assert all(row["WarehouseCode"] == "SAP-WAREHOUSE" for row in order["DocumentLines"])
    patch = writes(sap)[-1]
    assert patch["method"] == "PATCH"
    assert [row["LineNum"] for row in patch["body"]["DocumentLines"]] == [20, 10]
    assert json.loads(result.stdout)["summary"]["BuyOrders"]["updated"] == 1


def test_cli_update_adds_new_line_without_deleting_omitted_lines(cli, source_record, sap):
    assert cli([source_record]).returncode == 0
    source_record["line_items"] = [{"line_id": "900102", "sku": "00042", "quantity": 8, "sub_total_price": 32}]
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    assert [(row["LineNum"], row["ItemCode"], row["Quantity"], row["U_OPT_PO_LineID"]) for row in sap.orders[5001]["DocumentLines"]] == [
        (10, "SKU-DEMO", 4, "900101"), (20, "00042", 2, "900102"),
    ]


@pytest.mark.parametrize("change", ["closed", "cancelled", "received", "line_closed", "unreferenced", "supplier", "item"])
def test_cli_refuses_unsafe_updates(cli, source_record, sap, change):
    assert cli([source_record]).returncode == 0
    order = sap.orders[5001]
    if change == "closed":
        order["DocumentStatus"] = "bost_Close"
    elif change == "cancelled":
        order["Cancelled"] = "tYES"
    elif change == "received":
        order["DocumentLines"][0]["RemainingOpenQuantity"] = 3
    elif change == "line_closed":
        order["DocumentLines"][0]["LineStatus"] = "bost_Close"
    elif change == "unreferenced":
        order["DocumentLines"][0].pop("U_OPT_PO_LineID")
    elif change == "supplier":
        source_record["supplier_remoteId"] = "OTHER-VENDOR"
    else:
        order["DocumentLines"][0]["ItemCode"] = "00042"
    lines = json.loads(source_record["line_items"])
    lines[0]["quantity"] = 30
    source_record["line_items"] = lines
    before = copy.deepcopy(order)
    result = cli([source_record])
    assert result.returncode != 0
    assert result.stdout == ""
    assert sap.orders[5001] == before
    assert [row["method"] for row in writes(sap)] == ["POST"]


def test_cli_lost_create_response_reconciles_without_second_post(cli, source_record, sap, config):
    sap.create_mode = "accept_then_disconnect"
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bookmarks"]["BuyOrders"][0]["id"] == 5001
    assert state_file(config)["sap_pending_creates"] == {}
    assert len(writes(sap)) == 1


def test_cli_unresolved_create_survives_restart_without_blind_retry(cli, source_record, sap, config):
    assert cli([source_record]).returncode == 0
    second = copy.deepcopy(source_record)
    second["id"] = second["externalid"] = "900002"
    sap.create_mode = "lose_without_create"
    failed = cli([second])
    assert failed.returncode != 0 and failed.stdout == ""
    assert state_file(config)["sap_pending_creates"] == {"900002": True}
    sap.create_mode = "normal"
    retry = cli([second])
    assert retry.returncode != 0 and retry.stdout == ""
    assert len(writes(sap)) == 2
    # Simulate the original transaction becoming visible after the failed lookup.
    committed = copy.deepcopy(sap.orders[5001])
    committed.update(DocEntry=5002, U_OPT_PO_HeaderID="900002")
    sap.orders[5002] = committed
    recovered = cli([second])
    assert recovered.returncode == 0, recovered.stderr
    assert state_file(config)["sap_pending_creates"] == {}
    assert [row["externalId"] for row in state_file(config)["bookmarks"]["BuyOrders"]] == ["900001", "900002"]
    assert len(writes(sap)) == 2


def test_cli_mixed_failure_keeps_success_but_emits_no_success_state(cli, source_record, sap, config):
    invalid = copy.deepcopy(source_record)
    invalid["id"] = invalid["externalid"] = "900002"
    invalid["line_items"] = [{"line_id": "900102", "sku": None, "quantity": 12, "sub_total_price": 30}]
    result = cli([source_record, invalid])
    assert result.returncode != 0 and result.stdout == ""
    saved = state_file(config)
    assert saved["bookmarks"]["BuyOrders"][0]["id"] == 5001
    assert [row["success"] for row in saved["bookmarks"]["BuyOrders"]] == [True, False]
    assert list(sap.orders) == [5001]


def test_cli_partial_document_is_not_acknowledged(cli, source_record, sap, config):
    source_record["line_items"] = [
        {"line_id": "900101", "sku": "SKU-DEMO", "quantity": 24, "sub_total_price": 120},
        {"line_id": "900102", "sku": "00042", "quantity": 8, "sub_total_price": 32},
    ]
    sap.truncate_create = True
    result = cli([source_record])
    assert result.returncode != 0 and result.stdout == ""
    assert state_file(config)["sap_pending_creates"] == {"900001": True}
    assert state_file(config)["bookmarks"]["BuyOrders"][0]["success"] is False


def test_cli_rejected_create_can_be_corrected_without_pending_marker(cli, source_record, sap, config):
    sap.statuses["POST PurchaseOrders"] = [400]
    failed = cli([source_record])
    assert failed.returncode != 0 and failed.stdout == ""
    assert state_file(config)["sap_pending_creates"] == {}
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    assert list(sap.orders) == [5001]


def test_cli_authentication_failure_is_classified(cli, source_record, sap, config):
    sap.login_status = 401
    failed = cli([source_record])
    assert failed.returncode != 0 and failed.stdout == ""
    assert state_file(config)["bookmarks"]["BuyOrders"][0]["hg_error_class"] == "InvalidCredentialsError"
    assert not writes(sap)
    assert "SYNTHETIC_PASSWORD" not in failed.stderr


def test_cli_unknown_stream_fails_without_writes(cli, source_record, sap):
    failed = cli([source_record], stream="Products")
    assert failed.returncode != 0 and failed.stdout == ""
    assert not sap.requests


def test_cli_rejects_company_state_reuse(cli, source_record, sap, config):
    assert cli([source_record]).returncode == 0
    saved = state_file(config)
    saved["sap_company"]["company_db"] = "DIFFERENT_COMPANY"
    Path(config["state_path"]).write_text(json.dumps(saved))
    count = len(sap.requests)
    failed = cli([source_record])
    assert failed.returncode != 0 and failed.stdout == ""
    assert len(sap.requests) == count
    assert state_file(config)["sap_company"]["company_db"] == "DIFFERENT_COMPANY"


def test_interruption_retains_success_without_acknowledging_unread_input(config, source_record, sap, capsys):
    target = TargetSap(config=config)
    schema = (ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0]
    second = copy.deepcopy(source_record)
    second["id"] = second["externalid"] = "900002"

    def interrupted_input():
        yield schema + "\n"
        yield json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": source_record}) + "\n"
        target._shutdown_requested.set()
        yield json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": second}) + "\n"

    with pytest.raises(FatalAPIError, match="interrupted"):
        target.listen(interrupted_input())
    assert capsys.readouterr().out == ""
    assert list(sap.orders) == [5001]
    assert state_file(config)["bookmarks"]["BuyOrders"][0]["id"] == 5001


def test_cli_imported_failed_state_is_retryable_without_local_state_file(cli, source_record, sap, config):
    sap.statuses["POST PurchaseOrders"] = [400]
    failed = cli([source_record])
    assert failed.returncode != 0
    checkpoint = Path(config["state_path"]).with_name("checkpoint.json")
    checkpoint.write_text(json.dumps(state_file(config)))
    Path(config["state_path"]).unlink()
    recovered = cli([source_record], state=checkpoint)
    assert recovered.returncode == 0, recovered.stderr
    assert [(row["externalId"], row["success"], row["id"]) for row in json.loads(recovered.stdout)["bookmarks"]["BuyOrders"]] == [("900001", True, 5001)]


def test_cli_same_state_file_lock_blocks_concurrent_writer(cli, source_record, config, sap):
    import fcntl

    with open(config["state_path"] + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failed = cli([source_record])
    assert failed.returncode != 0 and failed.stdout == ""
    assert not sap.requests


def test_cli_matching_closed_order_is_idempotent_on_cold_replay(cli, source_record, config, sap):
    assert cli([source_record]).returncode == 0
    sap.orders[5001]["DocumentStatus"] = "bost_Close"
    Path(config["state_path"]).unlink()
    recovered = cli([source_record])
    assert recovered.returncode == 0, recovered.stderr
    assert [row["method"] for row in writes(sap)] == ["POST"]


@pytest.mark.parametrize("ending", ["failure", "interruption"])
def test_timed_intermediate_drain_cannot_acknowledge_an_eventually_failed_job(config, source_record, sap, capsys, ending):
    target = TargetSap(config=config)
    target._MAX_RECORD_AGE_IN_MINUTES = -1
    schema = (ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0]
    second = copy.deepcopy(source_record)
    second["id"] = second["externalid"] = "900002"
    second["line_items"] = [{"line_id": "900102", "sku": None, "quantity": 12, "sub_total_price": 20}]

    def input_with_intermediate_state():
        yield schema + "\n"
        yield json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": source_record}) + "\n"
        yield json.dumps({"type": "STATE", "value": {"checkpoint": "not-eof"}}) + "\n"
        if ending == "interruption":
            target._shutdown_requested.set()
        yield json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": second}) + "\n"

    with pytest.raises(FatalAPIError):
        target.listen(input_with_intermediate_state())
    assert capsys.readouterr().out == ""
    assert list(sap.orders) == [5001]


def test_timed_drains_acknowledge_once_only_after_successful_eof(config, source_record, sap, capsys):
    target = TargetSap(config=config)
    target._MAX_RECORD_AGE_IN_MINUTES = -1
    schema = (ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0]
    record = json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": source_record})
    checkpoint = json.dumps({"type": "STATE", "value": {"checkpoint": "not-eof"}})
    target.listen(iter([schema + "\n", record + "\n", checkpoint + "\n", checkpoint + "\n"]))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["bookmarks"]["BuyOrders"][0]["id"] == 5001
    assert list(sap.orders) == [5001]


def test_cli_empty_job_restart_then_first_order_uses_valid_sdk_state(cli, source_record, sap):
    assert cli([]).returncode == 0
    assert cli([]).returncode == 0
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    assert list(sap.orders) == [5001]


def test_cli_snapshot_docentry_is_only_a_hint_not_write_authority(cli, source_record, sap, config, monkeypatch):
    snapshots = Path(config["state_path"]).parent / "snapshots"
    snapshots.mkdir()
    (snapshots / "BuyOrders_synthetic-flow.snapshot.csv").write_text("InputId,RemoteId\n900001,7777\n")
    monkeypatch.setenv("SNAPSHOT_DIR", str(snapshots))
    foreign = {"DocEntry": 7777, "U_OPT_PO_HeaderID": "OTHER-ORDER", "CardCode": "OTHER-VENDOR", "DocumentLines": []}
    sap.orders[7777] = copy.deepcopy(foreign)
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    assert sap.orders[7777] == foreign
    assert sap.orders[5001]["U_OPT_PO_HeaderID"] == "900001"
    assert json.loads(result.stdout)["bookmarks"]["BuyOrders"][0]["id"] == 5001
    assert [row["method"] for row in writes(sap)] == ["POST"]


def test_process_death_after_durable_marker_cannot_authorize_recreate(cli, source_record, sap, config):
    config_path = Path(config["state_path"]).with_name("crash-config.json")
    config_path.write_text(json.dumps(config))
    script = """
import os
import sys
from target_sap.target import TargetSap
target = TargetSap(config=sys.argv[1])
persist = target.persist_state
def crash_after_marker(state):
    persist(state)
    if state.get("sap_pending_creates"):
        os._exit(73)
target.persist_state = crash_after_marker
target.listen()
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        input=(ROOT / "tests/fixtures/buy_orders.jsonl").read_text(),
        capture_output=True, text=True, env=os.environ.copy(), timeout=30,
    )
    assert killed.returncode == 73 and killed.stdout == ""
    assert state_file(config)["sap_pending_creates"] == {"900001": True}
    assert not writes(sap)
    empty_retry = cli([])
    assert empty_retry.returncode != 0 and empty_retry.stdout == ""
    retry = cli([source_record])
    assert retry.returncode != 0 and retry.stdout == ""
    assert state_file(config)["sap_pending_creates"] == {"900001": True}
    assert not writes(sap)


@pytest.mark.parametrize("same_job", [False, True])
def test_order_can_revert_to_a_previous_successful_quantity_and_total(cli, source_record, sap, same_job):
    changed = copy.deepcopy(source_record)
    changed["line_items"] = [{"line_id": "900101", "sku": "SKU-DEMO", "quantity": 30, "sub_total_price": 150}]
    if same_job:
        result = cli([source_record, changed, source_record])
        assert result.returncode == 0, result.stderr
    else:
        for record in (source_record, changed, source_record):
            result = cli([record])
            assert result.returncode == 0, result.stderr
    line = sap.orders[5001]["DocumentLines"][0]
    assert (line["Quantity"], line["LineTotal"]) == (4, 120)
    assert [row["method"] for row in writes(sap)] == ["POST", "PATCH", "PATCH"]


def test_committed_patch_with_failed_readback_cannot_leave_old_hash_replayable(cli, source_record, sap, config, monkeypatch, capsys):
    assert cli([source_record]).returncode == 0
    changed = copy.deepcopy(source_record)
    changed["line_items"] = [{"line_id": "900101", "sku": "SKU-DEMO", "quantity": 30, "sub_total_price": 150}]
    sap.statuses["GET PurchaseOrders(5001)"] = [200, 503, 503, 503]
    monkeypatch.setattr("target_sap.client.time.sleep", lambda _: None)
    target = TargetSap(config=config)
    schema = (ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0]
    with pytest.raises(FatalAPIError):
        target.listen(iter([schema + "\n", json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": changed}) + "\n"]))
    assert capsys.readouterr().out == ""
    assert sap.orders[5001]["DocumentLines"][0]["Quantity"] == 5
    recovered = cli([source_record])
    assert recovered.returncode == 0, recovered.stderr
    assert sap.orders[5001]["DocumentLines"][0]["Quantity"] == 4


def test_process_death_after_patch_before_bookmark_requires_reconciliation(cli, source_record, sap, config):
    assert cli([source_record]).returncode == 0
    changed = copy.deepcopy(source_record)
    changed["line_items"] = [{"line_id": "900101", "sku": "SKU-DEMO", "quantity": 30, "sub_total_price": 150}]
    config_path = Path(config["state_path"]).with_name("crash-config.json")
    config_path.write_text(json.dumps(config))
    script = """
import os
import sys
from target_sap.sinks import BuyOrders
from target_sap.target import TargetSap
class CrashAfterPatch(BuyOrders):
    def update_state(self, state, *args, **kwargs):
        if state.get("is_updated"):
            os._exit(74)
        return super().update_state(state, *args, **kwargs)
target = TargetSap(config=sys.argv[1])
target.get_sink_class = lambda stream_name: CrashAfterPatch
target.listen()
"""
    schema = (ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0]
    killed = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        input=schema + "\n" + json.dumps({"type": "RECORD", "stream": "BuyOrders", "record": changed}) + "\n",
        capture_output=True, text=True, env=os.environ.copy(), timeout=30,
    )
    assert killed.returncode == 74 and killed.stdout == ""
    assert sap.orders[5001]["DocumentLines"][0]["Quantity"] == 5
    recovered = cli([source_record])
    assert recovered.returncode == 0, recovered.stderr
    assert sap.orders[5001]["DocumentLines"][0]["Quantity"] == 4
    assert [row["method"] for row in writes(sap)] == ["POST", "PATCH", "PATCH"]

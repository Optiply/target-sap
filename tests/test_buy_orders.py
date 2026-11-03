import copy
import json

import pytest

from target_sap_b1.sinks import source_identity


def prepared(sink, record):
    return sink.preprocess_record({**record, "externalId": str(record["externalid"])}, None)


@pytest.mark.parametrize("field,value", [
    ("sku", None), ("sku", 42), ("sku", " SKU-DEMO"),
    ("quantity", 0), ("quantity", -1), ("quantity", float("inf")),
    ("quantity", float("nan")), ("quantity", True),
    ("sub_total_price", None), ("sub_total_price", -1), ("sub_total_price", "NaN"),
    ("line_id", None),
])
def test_invalid_line_rejects_entire_order_before_any_sap_call(sink, source_record, sap, field, value):
    lines = json.loads(source_record["line_items"])
    valid = copy.deepcopy(lines[0])
    valid["line_id"] = "900102"
    lines.append(valid)
    lines[1][field] = value
    source_record["line_items"] = lines
    with pytest.raises(ValueError):
        prepared(sink, source_record)
    assert not sap.requests


@pytest.mark.parametrize("lines", [[], "not-json", {}, [1], [
    {"line_id": "same", "sku": "SKU-DEMO", "quantity": 1, "sub_total_price": 1},
    {"line_id": "same", "sku": "SKU-DEMO", "quantity": 1, "sub_total_price": 1},
]])
def test_invalid_line_collection_has_no_side_effects(sink, source_record, sap, lines):
    source_record["line_items"] = lines
    with pytest.raises(ValueError):
        prepared(sink, source_record)
    assert not sap.requests


@pytest.mark.parametrize("field,value", [("created_at", None), ("created_at", "2026-02-30"), ("supplier_remoteId", 12), ("supplier_remoteId", "")])
def test_invalid_header_has_no_side_effects(sink, source_record, sap, field, value):
    source_record[field] = value
    with pytest.raises(ValueError):
        prepared(sink, source_record)
    assert not sap.requests


@pytest.mark.parametrize("factor", [None, 0, -1, "NaN", float("inf")])
def test_invalid_purchase_unit_never_creates_partial_order(sink, source_record, sap, factor):
    sap.factors["SKU-DEMO"] = factor
    with pytest.raises(ValueError):
        sink.upsert_record(prepared(sink, source_record), None)
    assert not sap.orders
    assert not [request for request in sap.requests if request["endpoint"] == "PurchaseOrders" and request["method"] == "POST"]


def test_missing_item_never_uses_default_purchase_unit(sink, source_record, sap):
    sap.factors.clear()
    with pytest.raises(Exception, match="missing or ambiguous"):
        sink.upsert_record(prepared(sink, source_record), None)
    assert not sap.orders


def test_odata_references_are_escaped_and_leading_zero_sku_is_preserved(cli, source_record, sap):
    source_record["id"] = source_record["externalid"] = "order'1"
    source_record["supplier_remoteId"] = "00007"
    source_record["line_items"] = [
        {"line_id": "line'1", "sku": "O'NEIL", "quantity": 8, "sub_total_price": 50},
        {"line_id": "line2", "sku": "00042", "quantity": 12, "sub_total_price": 60},
    ]
    result = cli([source_record])
    assert result.returncode == 0, result.stderr
    order = sap.orders[5001]
    assert order["CardCode"] == "00007"
    assert order["U_OPT_PO_HeaderID"] == "order'1"
    assert [(line["ItemCode"], line["Quantity"], line["U_OPT_PO_LineID"]) for line in order["DocumentLines"]] == [("O'NEIL", 4, "line'1"), ("00042", 3, "line2")]
    assert {request["query"].get("$filter") for request in sap.requests if request["method"] == "GET"} >= {
        "U_OPT_PO_HeaderID eq 'order''1'", "ItemCode eq 'O''NEIL'", "ItemCode eq '00042'",
    }


def test_duplicate_header_reference_never_writes(sink, source_record, sap):
    sap.duplicate_reference = True
    with pytest.raises(Exception, match="Multiple SAP orders"):
        sink.upsert_record(prepared(sink, source_record), None)
    assert not sap.orders


def test_state_must_be_durable_before_create(sink, source_record, sap, monkeypatch):
    def unavailable_state(_state):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(sink._target, "persist_state", unavailable_state)
    with pytest.raises(OSError, match="disk full"):
        sink.upsert_record(prepared(sink, source_record), None)
    assert not sap.orders
    assert not [request for request in sap.requests if request["method"] == "POST" and request["endpoint"] == "PurchaseOrders"]


@pytest.mark.parametrize("identity", [None, "", False, 1.5])
def test_invalid_source_identity_is_not_sap_docentry(source_record, identity):
    source_record["id"] = identity
    with pytest.raises(ValueError):
        source_identity(source_record)


def test_source_identity_mismatch_rejected(source_record):
    source_record["id"] = "5001"
    with pytest.raises(ValueError, match="disagree"):
        source_identity(source_record)

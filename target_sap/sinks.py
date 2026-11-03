"""PacoVerpakkingen purchase orders, keyed by Optiply header/line references."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests
from hotglue_etl_exceptions import InvalidCredentialsError
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError

from target_sap.client import SapAPIError, SapSink


def _text(value, field, allow_integer=False):
    if allow_integer and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(c in value for c in "\r\n"):
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace")
    return value


def source_identity(record):
    external_id = _text(record.get("externalid"), "externalid", allow_integer=True)
    if _text(record.get("id"), "id", allow_integer=True) != external_id:
        raise ValueError("Source id and externalid disagree")
    return external_id


def _number(value, field, positive=False):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or not math.isfinite(float(result)) or result < 0 or (positive and result == 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError(f"{field} must be a finite {'positive' if positive else 'non-negative'} number") from error


def _date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("created_at must be an ISO date or datetime")
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise ValueError("created_at must be an ISO date or datetime") from error


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


class BuyOrders(SapSink):
    name = "BuyOrders"
    endpoint = "PurchaseOrders"

    def preprocess_record(self, record, context):
        lines = record.get("line_items")
        if isinstance(lines, str):
            try:
                lines = json.loads(lines)
            except ValueError as error:
                raise ValueError("line_items must contain valid JSON") from error
        if not isinstance(lines, list) or not lines:
            raise ValueError("line_items must contain at least one line")
        mapped = []
        seen = set()
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("Every line must be an object")
            line_id = _text(line.get("line_id"), "line_id", allow_integer=True)
            if line_id in seen:
                raise ValueError("Duplicate line_id")
            seen.add(line_id)
            mapped.append({
                "ItemCode": _text(line.get("sku"), "sku"),
                "Quantity": float(_number(line.get("quantity"), "quantity", positive=True)),
                "LineTotal": float(_number(line.get("sub_total_price"), "sub_total_price")),
                "U_OPT_PO_LineID": line_id,
            })
        return {
            "externalId": _text(record.get("externalId"), "externalId"),
            "CardCode": _text(record.get("supplier_remoteId"), "supplier_remoteId"),
            "DocDueDate": _date(record.get("created_at")),
            "DocumentLines": mapped,
        }

    def _values(self, endpoint, params):
        data = self.read_json(self.request_api("GET", endpoint=endpoint, params=params))
        values = data.get("value")
        if not isinstance(values, list) or data.get("@odata.nextLink") or data.get("odata.nextLink"):
            raise FatalAPIError("SAP lookup is malformed or not unique")
        if any(not isinstance(row, dict) for row in values):
            raise FatalAPIError("SAP lookup returned a malformed entity")
        return values

    def _lookup(self, external_id):
        values = self._values("PurchaseOrders", {
            "$filter": f"U_OPT_PO_HeaderID eq {_literal(external_id)}",
            "$select": "DocEntry",
            "$top": 2,
        })
        if len(values) > 1:
            raise FatalAPIError("Multiple SAP orders have the same Optiply header reference")
        return self._entry(values[0].get("DocEntry")) if values else None

    @staticmethod
    def _entry(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
            raise FatalAPIError("SAP did not return a valid DocEntry")
        return int(value)

    def _document(self, entry, external_id):
        data = self.read_json(self.request_api("GET", endpoint=f"PurchaseOrders({entry})"))
        if self._entry(data.get("DocEntry")) != entry or str(data.get("U_OPT_PO_HeaderID")) != external_id:
            raise FatalAPIError("SAP document identity does not match the source order")
        return data

    def _body(self, record):
        factors = {}
        lines = []
        for line in record["DocumentLines"]:
            sku = line["ItemCode"]
            if sku not in factors:
                items = self._values("Items", {
                    "$filter": f"ItemCode eq {_literal(sku)}",
                    "$select": "ItemCode,PurchaseItemsPerUnit",
                    "$top": 2,
                })
                if len(items) != 1 or items[0].get("ItemCode") != sku:
                    raise FatalAPIError("SAP item lookup is missing or ambiguous")
                factors[sku] = _number(items[0].get("PurchaseItemsPerUnit"), "PurchaseItemsPerUnit", positive=True)
            quantity = Decimal(str(line["Quantity"])) / factors[sku]
            converted = float(quantity)
            if not math.isfinite(converted) or converted <= 0:
                raise ValueError("Converted SAP quantity is not finite and positive")
            lines.append({**line, "Quantity": converted})
        return {
            "CardCode": record["CardCode"],
            "DocDueDate": record["DocDueDate"],
            "U_OPT_PO_HeaderID": record["externalId"],
            "DocumentLines": lines,
        }

    @staticmethod
    def _remote_lines(document):
        rows = document.get("DocumentLines")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FatalAPIError("SAP document has malformed lines")
        result = {}
        for row in rows:
            identity = row.get("U_OPT_PO_LineID")
            if identity in (None, ""):
                continue
            identity = _text(identity, "SAP line reference", allow_integer=True)
            if identity in result:
                raise FatalAPIError("SAP document has ambiguous line references")
            result[identity] = row
        return result

    def _matches(self, document, body):
        if document.get("CardCode") != body["CardCode"] or _date(document.get("DocDueDate")) != body["DocDueDate"]:
            return False
        remote = self._remote_lines(document)
        for line in body["DocumentLines"]:
            saved = remote.get(line["U_OPT_PO_LineID"])
            if saved is None or saved.get("ItemCode") != line["ItemCode"]:
                return False
            for field in ("Quantity", "LineTotal"):
                if abs(_number(saved.get(field), field) - Decimal(str(line[field]))) > Decimal("0.000001"):
                    return False
        return True

    def _patch_body(self, document, body):
        if document.get("CardCode") != body["CardCode"]:
            raise FatalAPIError("Cannot change the SAP supplier of an existing order")
        if document.get("DocumentStatus") != "bost_Open" or document.get("Cancelled") != "tNO":
            raise FatalAPIError("Cannot modify a closed or cancelled SAP order")
        remote = self._remote_lines(document)
        if len(remote) != len(document["DocumentLines"]):
            raise FatalAPIError("Cannot update historical SAP lines without stable line references")
        line_numbers = set()
        for line in document["DocumentLines"]:
            number = line.get("LineNum")
            if isinstance(number, bool) or not isinstance(number, int) or number < 0 or number in line_numbers:
                raise FatalAPIError("SAP line numbers are missing or ambiguous")
            line_numbers.add(number)
            if line.get("LineStatus") != "bost_Open" or _number(line.get("RemainingOpenQuantity"), "RemainingOpenQuantity") != _number(line.get("Quantity"), "Quantity"):
                raise FatalAPIError("Cannot modify received or closed SAP lines")
        lines = []
        for line in body["DocumentLines"]:
            saved = remote.get(line["U_OPT_PO_LineID"])
            if saved and saved.get("ItemCode") != line["ItemCode"]:
                raise FatalAPIError("Cannot change the item of an existing SAP line")
            lines.append({**line, **({"LineNum": saved["LineNum"]} if saved else {})})
        return {"DocDueDate": body["DocDueDate"], "DocumentLines": lines}

    def _confirm(self, entry, record, body):
        if entry is None:
            raise FatalAPIError("SAP write is unresolved; an empty lookup does not permit another create")
        document = self._document(entry, record["externalId"])
        if not self._matches(document, body):
            raise FatalAPIError("SAP readback does not confirm every requested order field and line")
        self.latest_state.setdefault("sap_pending_creates", {}).pop(record["externalId"], None)
        self._target.persist_state(self.latest_state)
        return entry, True, {}

    def upsert_record(self, record, context):
        external_id = record["externalId"]
        entry = self._lookup(external_id)
        body = self._body(record)
        pending = self.latest_state.setdefault("sap_pending_creates", {})
        if entry is not None:
            document = self._document(entry, external_id)
            if self._matches(document, body):
                pending.pop(external_id, None)
                self._target.persist_state(self.latest_state)
                return entry, True, {"existing": True}
            payload = self._patch_body(document, body)
            method, endpoint = "PATCH", f"PurchaseOrders({entry})"
            # A committed PATCH must not leave an older payload hash eligible to skip replay.
            self.invalidate_record_state(external_id)
            self._target.persist_state(self.latest_state)
        else:
            if external_id in pending:
                raise FatalAPIError("Previous SAP create is unresolved; reconcile it before retrying")
            # Persist before POST so a restart cannot mistake a lost response for no write.
            pending[external_id] = True
            self._target.persist_state(self.latest_state)
            method, endpoint, payload = "POST", "PurchaseOrders", body
        response = None
        try:
            response = self.request_api(method, endpoint=endpoint, request_data=payload)
        except (InvalidCredentialsError, SapAPIError) as error:
            if method == "POST" and (isinstance(error, InvalidCredentialsError) or error.status_code in (400, 403, 404, 422)):
                pending.pop(external_id, None)
                self._target.persist_state(self.latest_state)
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, RetriableAPIError):
            if entry is None:
                entry = self._lookup(external_id)
        if response is not None and method == "POST":
            try:
                entry = self._entry(self.read_json(response).get("DocEntry"))
            except FatalAPIError:
                entry = self._lookup(external_id)
        elif response is not None and response.content:
            self.read_json(response)
        result = self._confirm(entry, record, body)
        return result[0], result[1], {"is_updated": True} if method == "PATCH" else result[2]

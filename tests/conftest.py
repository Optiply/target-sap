from __future__ import annotations

import copy
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SNAPSHOT_DIR", str(ROOT / ".test-work" / "snapshots"))


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / ".secrets").mkdir()
    monkeypatch.chdir(run)
    monkeypatch.delenv("JOB_ID", raising=False)
    monkeypatch.delenv("STREAMING_JOB", raising=False)
    monkeypatch.setenv("FLOW", "synthetic-flow")
    return run


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory):
    folder = tmp_path_factory.mktemp("synthetic-tls")
    cert, key = folder / "trust-bundle", folder / "tls-material"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True)
    return cert, key


@pytest.fixture
def sap(tls_material, monkeypatch):
    cert, key = tls_material
    state = SimpleNamespace(
        orders={}, requests=[], factors={"SKU-DEMO": 6, "00042": 4, "O'NEIL": 2},
        statuses=defaultdict(list), create_mode="normal", truncate_create=False,
        duplicate_reference=False, login_status=200, next_entry=5001,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body=None, headers=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def begin(self):
            parsed = urlsplit(self.path)
            endpoint = parsed.path.removeprefix("/b1s/v2/")
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            state.requests.append({
                "method": self.command, "endpoint": endpoint, "body": body,
                "query": query, "cookie": self.headers.get("Cookie"),
            })
            if endpoint != "Login" and self.headers.get("Cookie") != "B1SESSION=synthetic-session; ROUTEID=.node1":
                self.respond(401, {"error": {"code": -1}})
                return None
            queued = state.statuses[f"{self.command} {endpoint}"]
            if queued:
                status = queued.pop(0)
                if status >= 300:
                    self.respond(status, {"error": {"code": -1}}, {"Retry-After": "0", "Location": "/b1s/v2/UnexpectedRedirect"})
                    return None
            return endpoint, query, body

        @staticmethod
        def filter_value(query, field):
            expression = query.get("$filter", "")
            prefix = field + " eq '"
            if not expression.startswith(prefix) or not expression.endswith("'"):
                raise AssertionError("Expected escaped OData string equality")
            return expression[len(prefix):-1].replace("''", "'")

        def do_GET(self):
            request = self.begin()
            if request is None:
                return
            endpoint, query, _ = request
            if endpoint == "Items":
                sku = self.filter_value(query, "ItemCode")
                rows = [{"ItemCode": sku, "PurchaseItemsPerUnit": state.factors[sku]}] if sku in state.factors else []
                self.respond(200, {"value": rows})
            elif endpoint == "PurchaseOrders":
                reference = self.filter_value(query, "U_OPT_PO_HeaderID")
                rows = [{"DocEntry": entry} for entry, order in state.orders.items() if order["U_OPT_PO_HeaderID"] == reference]
                if state.duplicate_reference:
                    rows = [{"DocEntry": 5001}, {"DocEntry": 5002}]
                self.respond(200, {"value": rows})
            elif endpoint.startswith("PurchaseOrders("):
                entry = int(endpoint[len("PurchaseOrders("):-1])
                self.respond(200, state.orders[entry]) if entry in state.orders else self.respond(404, {"error": {"code": -1}})
            else:
                self.respond(404, {"error": {"code": -1}})

        def disconnect(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def do_POST(self):
            request = self.begin()
            if request is None:
                return
            endpoint, _, body = request
            if endpoint == "Login":
                if state.login_status != 200:
                    self.respond(state.login_status, {"error": {"code": -1}})
                elif body != {"CompanyDB": "SYNTHETIC_COMPANY", "UserName": "SYNTHETIC_USER", "Password": "SYNTHETIC_PASSWORD"}:
                    self.respond(401, {"error": {"code": -1}})
                else:
                    self.respond(200, {"SessionId": "synthetic-session", "SessionTimeout": 30}, {"Set-Cookie": "ROUTEID=.node1; Path=/"})
                return
            if endpoint != "PurchaseOrders":
                self.respond(404, {"error": {"code": -1}})
                return
            if state.create_mode == "lose_without_create":
                self.disconnect()
                return
            entry = state.next_entry
            state.next_entry += 1
            order = copy.deepcopy(body)
            order.update({"DocEntry": entry, "DocNum": 7100 + entry, "DocumentStatus": "bost_Open", "Cancelled": "tNO", "Comments": "SAP owned", "DocDate": "2026-10-01"})
            for index, line in enumerate(order["DocumentLines"]):
                line.update({"LineNum": 10 + index * 10, "LineStatus": "bost_Open", "RemainingOpenQuantity": line["Quantity"], "WarehouseCode": "SAP-WAREHOUSE"})
            if state.truncate_create:
                order["DocumentLines"].pop()
            state.orders[entry] = order
            if state.create_mode == "accept_then_disconnect":
                self.disconnect()
            else:
                self.respond(201, order)

        def do_PATCH(self):
            request = self.begin()
            if request is None:
                return
            endpoint, _, body = request
            entry = int(endpoint[len("PurchaseOrders("):-1])
            order = state.orders[entry]
            order["DocDueDate"] = body["DocDueDate"]
            for change in body["DocumentLines"]:
                if "LineNum" in change:
                    line = next(row for row in order["DocumentLines"] if row["LineNum"] == change["LineNum"])
                    line.update(change)
                    line["RemainingOpenQuantity"] = line["Quantity"]
                else:
                    line = copy.deepcopy(change)
                    line.update({"LineNum": max(row["LineNum"] for row in order["DocumentLines"]) + 10, "LineStatus": "bost_Open", "RemainingOpenQuantity": line["Quantity"], "WarehouseCode": "SAP-WAREHOUSE"})
                    order["DocumentLines"].append(line)
            self.respond(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state.base_url = f"https://localhost:{server.server_port}/b1s/v2/"
    state.cert = cert
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def source_record():
    return json.loads((ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[1])["record"]


@pytest.fixture
def config(sap, isolated_workdir):
    return {
        "api_url": sap.base_url, "company_db": "SYNTHETIC_COMPANY",
        "username": "SYNTHETIC_USER", "password": "SYNTHETIC_PASSWORD",
        "state_path": str(isolated_workdir / "target-state.json"),
    }


@pytest.fixture
def cli(config, isolated_workdir):
    path = isolated_workdir / "config.json"
    path.write_text(json.dumps(config))
    schema = json.loads((ROOT / "tests/fixtures/buy_orders.jsonl").read_text().splitlines()[0])

    def run(records=None, stream="BuyOrders", input_text=None, state=None):
        if input_text is None:
            message = copy.deepcopy(schema)
            message["stream"] = stream
            messages = [message] + [{"type": "RECORD", "stream": stream, "record": record} for record in records or []]
            messages.append({"type": "STATE", "value": {"synthetic_checkpoint": "before-sap"}})
            input_text = "".join(json.dumps(message) + "\n" for message in messages)
        return subprocess.run(
            [str(Path(sys.executable).with_name("target-sap")), "--config", str(path)] + (["--state", str(state)] if state else []),
            input=input_text, text=True, capture_output=True, cwd=isolated_workdir,
            env=os.environ.copy(), timeout=30,
        )

    return run


@pytest.fixture
def sink(config):
    from target_sap.sinks import BuyOrders
    from target_sap.target import TargetSap

    target = TargetSap(config=config)
    instance = BuyOrders(target, "BuyOrders", {"type": "object", "properties": {}}, [])
    instance.init_state()
    yield instance
    target._shutdown_requested.set()
    target.signal_listener_thread.join(timeout=1)

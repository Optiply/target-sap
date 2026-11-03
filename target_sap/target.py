"""HotGlue Singer entry point for SAP Business One purchase orders."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from hotglue_singer_sdk import typing as th
from hotglue_singer_sdk.exceptions import ConfigValidationError, FatalAPIError
from hotglue_singer_sdk.helpers.capabilities import AlertingLevel
from hotglue_singer_sdk.target_sdk.target import TargetHotglue

from target_sap.sinks import BuyOrders, source_identity


class TargetSap(TargetHotglue):
    name = "target-sap"
    alerting_level = AlertingLevel.WARNING
    SINK_TYPES = [BuyOrders]
    MAX_PARALLELISM = 1
    config_jsonschema = th.PropertiesList(
        th.Property("api_url", th.StringType, required=True),
        th.Property("company_db", th.StringType, required=True),
        th.Property("username", th.StringType, required=True),
        th.Property("password", th.StringType, required=True),
        th.Property("state_path", th.StringType, default="target-state.json"),
    ).to_dict()
    for _field in config_jsonschema["properties"].values():
        _field["minLength"] = 1
    config_jsonschema["properties"]["password"]["secret"] = True
    config_jsonschema["properties"]["username"]["secret"] = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.incremental_target_state_path = str(self.state_path)
        self._input_complete = False

    def _validate_config(self, *args, **kwargs):
        result = super()._validate_config(*args, **kwargs)
        url = urlsplit(self.config.get("api_url", ""))
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or not url.path.rstrip("/").endswith("/b1s/v2")
        ):
            raise ConfigValidationError("api_url must be an HTTPS Service Layer root ending /b1s/v2/")
        if os.environ.get("STREAMING_JOB") == "True":
            raise ConfigValidationError("target-sap supports scheduled exports, not streaming jobs")
        return result

    @property
    def state_path(self) -> Path:
        return Path(self.config.get("state_path", "target-state.json"))

    @property
    def company_scope(self) -> dict:
        # Keep the persisted scope format stable when configuration keys change.
        return {
            "base_url": self.config["api_url"].rstrip("/"),
            "company_db": self.config["company_db"],
        }

    def get_sink_class(self, stream_name):
        if stream_name != "BuyOrders":
            raise FatalAPIError(f"Unsupported Singer stream: {stream_name}")
        return BuyOrders

    def get_record_id(self, sink_name, record, relation_fields=None):
        record["externalId"] = source_identity(record)
        record.pop("id")
        return super().get_record_id(sink_name, record, relation_fields)

    def persist_state(self, state: dict) -> None:
        """Use the SDK state file for both results and unresolved-create markers."""
        state["sap_company"] = self.company_scope
        self._latest_state = state
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
                temp_path = Path(output.name)
                json.dump(state, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _save_failed_job_state(self):
        self.persist_state(self._latest_state)

    def _graceful_shutdown(self):
        self.persist_state(self._latest_state)
        raise FatalAPIError("SAP export interrupted before all input was acknowledged")

    def _process_lines(self, file_input):
        counter = super()._process_lines(file_input)
        self._input_complete = True
        return counter

    def _write_state_message(self, state):
        if self._shutdown_requested.is_set():
            self._graceful_shutdown()
        self.persist_state(state)
        # Timed SDK drains must not acknowledge an input stream that can still fail.
        if not self._input_complete:
            return
        bookmarks = state.get("bookmarks", {}).get("BuyOrders", [])
        if state.get("sap_pending_creates") or any(not row.get("success") for row in bookmarks):
            raise FatalAPIError("Unresolved SAP orders prevent successful STATE acknowledgement")
        super()._write_state_message(state)

    def listen(self, file_input=None):
        self.incremental_target_state_path = str(self.state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{self.state_path}.lock")
        try:
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise FatalAPIError("Another SAP target holds this state file") from error
                saved = self._state
                if self.state_path.exists():
                    with self.state_path.open() as previous:
                        saved = json.load(previous)
                if not isinstance(saved, dict) or (saved and saved.get("sap_company") != self.company_scope):
                    raise FatalAPIError("State is unscoped or belongs to another SAP company; do not reuse it")
                self._state = self._latest_state = saved
                self.persist_state(saved)
                super().listen(file_input)
        finally:
            self._shutdown_requested.set()
            self.signal_listener_thread.join(timeout=1)


if __name__ == "__main__":
    TargetSap.cli()

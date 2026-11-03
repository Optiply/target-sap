# target-sap

PacoVerpakkingen-only HotGlue Singer target for SAP Business One Service Layer
OData v4 (`/b1s/v2/`). Python 3.10; HotGlue Singer SDK **1.0.38**.

## Contract

Only the exact Singer stream `BuyOrders` is supported. Other streams fail the
job. The SDK owns Singer parsing, schema validation, record hashes, snapshots,
bookmarks and output state; the sink owns SAP mapping and reconciliation.

| Input | SAP field / SDK identity |
| --- | --- |
| `id` and `externalid` (must agree) | `U_OPT_PO_HeaderID`; SDK `externalId` |
| `supplier_remoteId` | `CardCode`, verbatim; no supplier-name parsing |
| `created_at` | `DocDueDate`, received calendar date, without date shifting |
| `line_items[].line_id` | `U_OPT_PO_LineID` |
| `line_items[].sku` | `ItemCode`, verbatim; numeric codes are rejected |
| `line_items[].quantity` | `Quantity / Items.PurchaseItemsPerUnit` |
| `line_items[].sub_total_price` | `LineTotal`, unchanged |

`line_items` can be a JSON array or a JSON-encoded array. Every supplied line
must be valid before SAP writes. Duplicate SKUs are separate lines when their
line IDs differ. Missing/duplicate IDs, missing SKUs, malformed dates, empty
orders, non-finite numbers and missing/non-positive purchase units fail closed.
No missing-item fallback, item/supplier creation, currency/tax/warehouse mapping,
retries of writes, line deletion, or weekend-shift configuration is included.
An input `id` is never used as SAP `DocEntry`.

The target finds orders by `U_OPT_PO_HeaderID` and verifies company/document
identity. Create is one complete `POST PurchaseOrders`. Updates use `PATCH`,
match `U_OPT_PO_LineID` to `LineNum`, preserve omitted lines/SAP-owned fields,
and refuse supplier/item changes or changes to closed, cancelled, received or
historically unreferenced lines. `RemainingOpenQuantity`, `Quantity`,
`LineStatus`, `DocumentStatus` and `Cancelled` must establish that an update is
safe. An already-matching order needs no mutation, even if now closed. Every
write requires a full document readback confirming all supplied fields/lines.

## Install and check

From this directory, using an installed Python **3.10** and Poetry:

```sh
POETRY_VIRTUALENVS_IN_PROJECT=true poetry env use python3.10
POETRY_VIRTUALENVS_IN_PROJECT=true poetry sync
poetry run pytest
poetry run target-sap --help
```

Tests use synthetic data and a trusted local HTTPS server, never SAP or Optiply.
Temporary evidence, caches and generated test TLS files stay in ignored folders.
The lockfile records all runtime/development dependencies. Direct dependencies
come from PyPI: `hotglue-singer-sdk` and `requests` (Apache-2.0), `pytest` (MIT,
development only); the build backend is `poetry-core` (MIT).

## Configuration and execution

Copy `config.example.json` to ignored `config.json`, then have the operator set
its connection fields through the project's approved secret mechanism. Never
commit credentials, customer messages or state. `username` and `password` are
marked secret in the configuration schema. HTTPS with certificate verification
is mandatory, including Login; redirects are not followed. For a private CA,
use the standard Requests `REQUESTS_CA_BUNDLE` setting, not disabled verification.

The SAP user needs Login, item purchase-unit reads, and purchase-order
read/create/update permissions in precisely the configured `company_db`.
`B1SESSION` and optional `ROUTEID` remain in memory. Connection/read timeouts are
5/30 seconds. GETs have at most three attempts with jitter and Retry-After capped
at 30 seconds; 401 and permanent 4xx fail without retry. POST/PATCH are never
blindly retried. HTTP bodies containing SAP errors do not count as success.

For local execution, contain the pinned SDK's `../.secrets/user_signal.listener`
artifact in an ignored working directory:

```sh
mkdir -p .local/run .local/.secrets
# From .local/run, using the installed entry point:
../../.venv/bin/target-sap --config ../../config.json < approved-buy-orders.jsonl
```

The example state path is relative to that working directory. Input must be an
approved Singer SCHEMA/RECORD/STATE stream; the synthetic fixture in
`tests/fixtures/buy_orders.jsonl` is for local tests, not a production order.
The SDK emits its normal raw target-result state object, not a bespoke Singer
reader or a different state envelope. Any record failure aborts with a nonzero
exit code and no successful output state. State is emitted only after successful
EOF, including when SDK timed drains occur. Latest successful deliveries remain
in the durable SDK state file for retry. Streaming jobs are not supported.

## State, retry and ambiguous writes

`state_path` is the SDK incremental target state file, written atomically with
fsync. It contains SDK results plus the SAP URL/company scope and
`sap_pending_creates`. A marker is durable **before** each create. After a lost
response, the target only acknowledges the order if a reference lookup and full
readback prove delivery. An empty lookup leaves the marker unresolved and never
authorizes another create, including after a restart. Definitive SAP rejection
(400/401/403/404/422) clears a just-created marker; no successful bookmark is
recorded. A confirmed successful write clears its marker.

Only the latest delivery hash/bookmark per source order remains eligible for SDK
deduplication. Before PATCH, its old cache entry is durably invalidated, so a
committed update followed by failed readback or process death cannot make an
older payload look delivered. Quantity/total changes can safely return A → B → A.

Keep this file across retries, protect it as customer data, and never clear an
unresolved marker merely to make a job green. Replay the same retained input. If
SAP later exposes the accepted document, its reference reconciles the retry.
Otherwise the operator must verify SAP transaction completion/non-creation before
approving any state repair. The target does not automate repairs. Unscoped state
or state from another company is rejected. If both `--state` and the local state
file exist, the durable local file is authoritative; do not replace newer state
with an older checkpoint. SDK snapshots are lookup hints, not authority to write
an unverified `DocEntry`.

The state file is process-locked and SDK parallelism is one. **The job runner must
also serialize all exports for the same SAP company across hosts and state
paths.** SAP UDF references are not a uniqueness constraint. Never run parallel
writers or discard state during an unresolved request; otherwise a late accepted
POST can race a second create. A successful cold replay finds existing SAP UDFs;
cold replay with lost unresolved state cannot prove duplicate safety.

## Integration and rollout boundaries

This repository changes no ETL. Before enabling it, the ETL owner must:

1. Add `or target.startswith("sap")` to the default buy-order target allowlist.
2. Retain existing line `sub_total_price` (derived from `subtotalValue`).

Keep the existing ETL weekend-to-Monday shift. No target shift is added.
Validate both SAP UDFs and their compatible string/reference types, purchase-unit
semantics, update status fields, receipt/update concurrency, currencies and actual
permissions in a separately approved acceptance run. Status checks are pre-write
guards, not an atomic lock against SAP receipts. Do not manually clear/change
Optiply UDF references. Input already dropped/rounded/defaulted by
the ETL cannot be reconstructed by this target. The job runner must retain the
target state and must not publish ETL/header checkpoints after target failure;
that platform behavior is outside this package and needs runtime evidence before
cutover. No live tenant verification, deployment or checkpoint-publication proof
is supplied by local tests.

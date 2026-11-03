# target-sap

`target-sap` is a Singer target for SAP Business One Service Layer OData v4,
currently supporting PacoVerpakkingen buy-order exports.

Built with the [HotGlue Singer SDK](https://pypi.org/project/hotglue-singer-sdk/)
(`hotglue-singer-sdk==1.0.38`).

## Installation

Install from source using Python **3.10** and Poetry **2**. From this directory:

```bash
POETRY_VIRTUALENVS_IN_PROJECT=true poetry env use python3.10
POETRY_VIRTUALENVS_IN_PROJECT=true poetry sync
```

## Configuration

### Accepted Config Options

| Setting | Required | Description |
| --- | --- | --- |
| `api_url` | Yes | HTTPS SAP Service Layer URL ending in `/b1s/v2/`. |
| `company_db` | Yes | SAP company database used for Login and all order operations. |
| `username` | Yes | SAP Service Layer username. |
| `password` | Yes | SAP Service Layer password. |
| `state_path` | No | Durable target state file. Defaults to `target-state.json`, relative to the working directory. Retain it across retries. |

The connection fields match `tap-sap-b1`: `api_url`, `company_db`, `username` and
`password`. Rename `base_url` to `api_url` in older target configurations; the old
configuration key is no longer supported. Keep the existing target state file.
Unlike the tap's default `/b1s/v1`, this target still requires `/b1s/v2/`.

### Example Configuration

Use [config.example.json](config.example.json) as the starting point for an
ignored `config.json`:

```json
{
  "api_url": "https://sap.example.invalid:50000/b1s/v2/",
  "company_db": "SYNTHETIC_COMPANY",
  "username": "SYNTHETIC_USER",
  "password": "SYNTHETIC_PASSWORD",
  "state_path": "../target-state.json"
}
```

Set real connection values through the project's approved secret mechanism.
Never commit credentials, customer input or state.

### Source Authentication and Authorization

The SAP user needs Login, item purchase-unit reads, and purchase-order
read/create/update permissions in the configured company. Session cookies stay
in memory. HTTPS certificate verification is mandatory; for a private CA, set
`REQUESTS_CA_BUNDLE` rather than disabling verification. Redirects are not followed.

## Usage

### Executing the Target Directly

```bash
poetry run target-sap --version
poetry run target-sap --help
```

The target accepts approved Singer SCHEMA/RECORD/STATE messages for the exact
stream `BuyOrders`. Other streams and streaming jobs are unsupported.

For local execution, contain the SDK's `../.secrets/user_signal.listener` artifact
in an ignored working directory:

```bash
mkdir -p .local/run .local/.secrets
(
  cd .local/run
  ../../.venv/bin/target-sap --config ../../config.json < /path/to/buy-orders.jsonl
)
```

Successful SDK bookmarks contain `id` (SAP `DocEntry`) and `externalId` (Optiply
buy-order ID). Output state is emitted only after successful EOF. A record failure
aborts with a nonzero exit code and no successful output state.

### Buy-order Mapping

| Input | SAP field / SDK identity |
| --- | --- |
| `id` and `externalid` (must agree) | `U_OPT_PO_HeaderID`; SDK `externalId` |
| `supplier_remoteId` | `CardCode`, verbatim; no supplier-name parsing |
| `created_at` | `DocDueDate`, received calendar date, without date shifting |
| `line_items[].line_id` | `U_OPT_PO_LineID` |
| `line_items[].sku` | `ItemCode`, verbatim; numeric codes are rejected |
| `line_items[].quantity` | `Quantity / Items.PurchaseItemsPerUnit` |
| `line_items[].sub_total_price` | `LineTotal`, unchanged |

`line_items` accepts an array or a JSON-encoded array. Every line must be valid
before writing; duplicate SKUs remain separate when their line IDs differ.
Missing/duplicate IDs, missing SKUs, malformed dates, empty orders, non-finite
numbers and missing/non-positive purchase units fail the order.

Orders are found by `U_OPT_PO_HeaderID`, never by treating an input ID as SAP
`DocEntry`. Creates send one complete order. Updates match `U_OPT_PO_LineID` to
`LineNum`, preserve omitted lines and SAP-owned fields, and reject supplier/item
changes or changes to closed, cancelled, received or unreferenced lines.
Status checks are pre-write guards, not an atomic lock against SAP receipts.
An already-matching order needs no write, even if closed.

Item/supplier creation, line deletion, currency/tax/warehouse mapping and target
weekend-shift configuration are not supported.

### State and Retries

- Retain `state_path` and the same input across retries. State is written
  atomically, scoped to the SAP URL/company, and process-locked. Unscoped or
  different-company state is rejected; a local state file takes precedence over
  `--state`. SDK snapshots do not authorize writes to an unverified `DocEntry`.
- Serialize **all exports for the same SAP company across hosts and state
  paths**. Local locking is not a distributed lock; SAP UDF references are not
  uniqueness constraints. Do not manually clear/change those references.
- GETs have at most three attempts with jitter and `Retry-After` capped at
  30 seconds. Connection/read timeouts are 5/30 seconds. Authentication and
  permanent 4xx errors fail without retry. POST/PATCH are never blindly retried.
- Every write requires full document readback. A durable pending-create marker
  prevents another POST after an ambiguous response, even after restart or an
  empty lookup. Definitive rejection clears a just-created marker; unresolved
  creates require SAP reconciliation before an operator approves any state repair.
  Never discard unresolved state merely to retry; losing it defeats duplicate safety.
- Prior delivery hashes are invalidated durably before PATCH, so returning to a
  previous quantity/total after an update or interrupted write is reconciled.

### Integration Prerequisites

Before enabling the target, the ETL owner must:

1. Add `or target.startswith("sap")` to the default buy-order target allowlist.
2. Retain line `sub_total_price` (derived from `subtotalValue`).

Keep the existing ETL weekend-to-Monday shift; the target adds no date shift.
The runner must retain target state and must not publish ETL/header checkpoints
after target failure. Before cutover, validate SAP UDF types, purchase-unit
semantics, update status fields, receipt/update concurrency, currencies and
permissions in a separately approved acceptance run. Local tests do not establish
live SAP compatibility or runner checkpoint behavior.

## Developer Resources

### Create and Run Tests

Tests live in `tests/` and use synthetic data and a trusted local HTTPS server,
never live SAP or Optiply. The fixture `tests/fixtures/buy_orders.jsonl` is for
local tests, not a production order.

```bash
poetry run pytest
poetry run target-sap --help
```

Test artifacts, caches and generated TLS files stay in ignored directories.

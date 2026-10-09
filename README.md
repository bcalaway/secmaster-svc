# secmaster-svc

The market data platform's security master: instrument identity, short names (`UST-10Y-CMT`), aliases, and the map from a source's key (`UST-PAR` / `BC_10YEAR`) to an instrument. quote-svc resolves observations to instruments through it, and mkt-api adds its short names to every answer. CMT instruments come from a versioned seed file applied by a job. The plan and status live in mkt-data's [docs/phase-2.md](https://github.com/bcalaway/mkt-data/blob/main/docs/phase-2.md) (Part B, step B4).

It runs on the home platform's AWS hub (`bcalaway/nyc_pa_aws_gitops`) as a registry app (`apps/registry.yml`: own Postgres database, Airflow pipelines, no Authentik client, no previews). Started from `templates/python` there, whose README explains the template's pieces; [docs/app-platform.md](https://github.com/bcalaway/nyc_pa_aws_gitops/blob/main/docs/app-platform.md) is the platform contract.

## How it runs

- **Container:** one process with HTTP on 8000 and gRPC on 9090, internal only: no Traefik route and no DNS record. Other services reach it on the `home-platform` network as `secmaster-svc:8000` / `secmaster-svc:9090`.
- **Database:** `secmaster-svc` on the hub's Postgres 16 (role, database and password created by the platform, `/home-platform/postgres/secmaster-svc-password`). Schema changes are Alembic migrations, applied when the container starts.
- **CI/CD:** `ci.yml` runs the platform's `app-ci.yml` on every PR (`ci / Build, test, lint` is required on `main`). `cd.yml` builds and pushes to ECR on merge, then deploys to the hub. Docs-only merges don't deploy.
- **Secrets:** anything under `/home-platform/secmaster-svc/` in SSM arrives in the container's environment at deploy time (the Airflow job token arrives as `AIRFLOW_TOKEN`).

## Data model

`app/models.py`, migration 0002:

- **`instrument`:** hidden integer `sec_id`, `type` (`cmt_yield` for now), currency, country, `curve` (`UST`), `tenor` (ISO 8601 duration: `P10Y`, `P6W`), the calendar it follows (a calendar-svc name, `SIFMA-US`), status and description.
- **`instrument_name`:** each instrument's one current short name (`UST-10Y-CMT`) and its aliases (`UST-6W-CMT` for `UST-1.5M-CMT`). Names are upper-case and unique for good: a rename keeps the old name as an alias.
- **`identifier`:** `(scheme, value)` → `sec_id`, with optional `valid_from`/`valid_to`. For CMTs the scheme is an mkt-data source name and the value that source's key (`UST-PAR` / `BC_10YEAR`, `H15-TCM` / `RIFLGFCY10_N.B`), plus `FRED` / `DGS10` for reference. That's the map quote-svc turns observations into quotes with.
- **`instrument_note`:** dated notes that explain a series without splitting it (the 2021 method change, the 20-year's 1987–1993 gap).
- **`seed_run`:** each application of a seed file.

Nothing is deleted: a name, identifier or note the seed stops listing gets `removed_at` and stops resolving.

## The CMT seed

`seeds/cmt.toml` is the source of truth for the 14 Treasury CMTs (`UST-1M-CMT` … `UST-30Y-CMT`): their attributes, names, identifiers and notes. `app/seed.py` applies every `seeds/*.toml` at each container start (`start.sh`, after migrations), on `POST /jobs/seed`, and from the manual DAG `secmaster_svc__seed`. It's idempotent: instruments are found by any listed name (so a rename keeps the `sec_id`), attributes and notes update in place, dropped rows are closed, and instruments the file doesn't list are never touched. A name or identifier that belongs to an unlisted instrument is a conflict: the run fails, changes nothing, and is recorded. Change the file in a reviewed PR.

## Futures

CME's rates and FX futures (mkt-data's [docs/phase-4.md](https://github.com/bcalaway/mkt-data/blob/main/docs/phase-4.md), step 2a). `seeds/futures.toml` lists each product with its specs as CME's spec page states them (verbatim, cited, effective-dated), its listing cycle, the rules every contract date follows and where each rule comes from. `app/futures.py` generates the contracts from those rules and calendar-svc's business days (pure functions); `app/futures_load.py` stores them, daily from the DAG `secmaster_svc__futures` (`POST /jobs/futures`):

- **Products:** instruments of type `fut_product`, named by their Bloomberg root (`TY`), identified by CME's code (`CME` / `ZN`); specs in `futures_spec`, the rest of the seed entry in `futures_product.info`.
- **Contracts:** `fut_treasury`, `fut_stir`, `fut_fx`, named `TYZ26` (Bloomberg's ticker with a two-digit year), with CME's code (`CME` / `ZNZ6`) valid while listed. Their dates (first and last trading day; first intention, first notice and delivery days; reference period and final settlement; FX settlement) are `futures_contract` rows, with the rule behind each date and history like `security_terms`.
- **Generics:** `TY1`, `TY2` (scheme `GENERIC`) with validity: a Treasury contract rolls on its first intention day, others after their last trading day. `GetInstrument` resolves `TY1` as of a date, like an on-the-run name.
- **FIGIs and Bloomberg tickers** (step 2b, `app/futures_figi.py`, `POST /jobs/futures-figi`, the DAG's second task): each listed contract is asked on OpenFIGI by our ticker (`TYZ6`, and `TYZ26`) in the product's market sector (`Curncy` for FX, `Comdty` for rates, since BPV6 Comdty is a soybean future) and by CME's code (`ZNZ6`). When they agree, or our ticker is found in the sector, it keeps the FIGI and Bloomberg's live ticker (`TICKER` / `TYZ6 Comdty`, valid while listed); when CME's code maps to another root, it stores nothing and reports Bloomberg's root, so the seed can be fixed. Answers are in `futures_figi_lookup`; the metric is `secmaster_svc_futures_figi_lookups{product,outcome}`.

## APIs

**gRPC** (`proto/securities.proto`, `secmaster-svc:9090`), service `secmaster_svc.Securities`:
- `GetInstrument`: by `sec_id`, or by short name or alias (case-insensitive), with identifiers and notes; NOT_FOUND otherwise.
- `ListInstruments`: by type and curve, in tenor order.
- `Resolve`: a scheme and a batch of values (optionally `as_of`) → matches with `sec_id` and short name, plus the unknown values. This is what quote-svc calls.
- `Search`: names, aliases, identifier values and descriptions.

**Job API** (`app/jobs.py`, bearer `AIRFLOW_TOKEN`; the GETs also take `READ_TOKEN`): `POST /jobs/seed`, `POST /jobs/futures`, `POST /jobs/futures-figi`, `GET /jobs/instruments`, `GET /jobs/instruments/{sec_id or name}`, `GET /jobs/resolve?scheme=&value=&value=`, `GET /jobs/search?q=`.

**Metrics** (`GET /metrics`, scraped as `secmaster-svc:8000`): `secmaster_svc_instruments{type,curve,status}`, `secmaster_svc_identifiers{scheme}`, `secmaster_svc_aliases`, `secmaster_svc_instruments_without_short_name` (should be 0), `secmaster_svc_seed_ok{seed}`, `secmaster_svc_seed_last_success_timestamp_seconds{seed}`, `secmaster_svc_seed_last_changed{seed}`, `secmaster_svc_futures_contracts{product,status}`, `secmaster_svc_futures_ok`, `secmaster_svc_futures_last_success_timestamp_seconds`. Unmapped source keys are counted by quote-svc, which sees them.

## Local development

```
pip install -r requirements.txt -r requirements-dev.txt
./gen_proto.sh
uvicorn app.main:app --reload
```

`POSTGRES_PASSWORD` is optional locally; without it the app runs with no database.

Tests and lint: `pytest` and `ruff check app/ tests/ migrations/`, or `docker build --target test .` / `--target lint .`, which is what CI runs. Where PyPI is blocked, `scripts/sandbox-test.sh` runs everything but `tests/test_grpc.py`.

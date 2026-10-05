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

## APIs

**gRPC** (`proto/securities.proto`, `secmaster-svc:9090`), service `secmaster_svc.Securities`:
- `GetInstrument`: by `sec_id`, or by short name or alias (case-insensitive), with identifiers and notes; NOT_FOUND otherwise.
- `ListInstruments`: by type and curve, in tenor order.
- `Resolve`: a scheme and a batch of values (optionally `as_of`) → matches with `sec_id` and short name, plus the unknown values. This is what quote-svc calls.
- `Search`: names, aliases, identifier values and descriptions.

**Job API** (`app/jobs.py`, bearer `AIRFLOW_TOKEN`; the GETs also take `READ_TOKEN`): `POST /jobs/seed`, `GET /jobs/instruments`, `GET /jobs/instruments/{sec_id or name}`, `GET /jobs/resolve?scheme=&value=&value=`, `GET /jobs/search?q=`.

**Metrics** (`GET /metrics`, scraped as `secmaster-svc:8000`): `secmaster_svc_instruments{type,curve,status}`, `secmaster_svc_identifiers{scheme}`, `secmaster_svc_aliases`, `secmaster_svc_instruments_without_short_name` (should be 0), `secmaster_svc_seed_ok{seed}`, `secmaster_svc_seed_last_success_timestamp_seconds{seed}`, `secmaster_svc_seed_last_changed{seed}`. Unmapped source keys are counted by quote-svc, which sees them.

## Local development

```
pip install -r requirements.txt -r requirements-dev.txt
./gen_proto.sh
uvicorn app.main:app --reload
```

`POSTGRES_PASSWORD` is optional locally; without it the app runs with no database.

Tests and lint: `pytest` and `ruff check app/ tests/ migrations/`, or `docker build --target test .` / `--target lint .`, which is what CI runs. Where PyPI is blocked, `scripts/sandbox-test.sh` runs everything but `tests/test_grpc.py`.

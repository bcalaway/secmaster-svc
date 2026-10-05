# secmaster-svc

The market data platform's security master: instrument identity, short names (`UST-10Y-CMT`), aliases, and the map from a source's key (`UST-PAR` / `BC_10YEAR`) to an instrument. quote-svc resolves observations to instruments through it, and mkt-api adds its short names to every answer. CMT instruments come from a versioned seed file applied by a job. The plan and status live in mkt-data's [docs/phase-2.md](https://github.com/bcalaway/mkt-data/blob/main/docs/phase-2.md) (Part B, step B4).

It runs on the home platform's AWS hub (`bcalaway/nyc_pa_aws_gitops`) as a registry app (`apps/registry.yml`: own Postgres database, Airflow pipelines, no Authentik client, no previews). Started from `templates/python` there, whose README explains the template's pieces; [docs/app-platform.md](https://github.com/bcalaway/nyc_pa_aws_gitops/blob/main/docs/app-platform.md) is the platform contract. The template's `Item` model, `/db-check`, `/login` and `ExampleService.Ping` are still examples until step B4 replaces them.

## How it runs

- **Container:** one process with HTTP on 8000 and gRPC on 9090, internal only: no Traefik route and no DNS record. Other services reach it on the `home-platform` network as `secmaster-svc:8000` / `secmaster-svc:9090`.
- **Database:** `secmaster-svc` on the hub's Postgres 16 (role, database and password created by the platform, `/home-platform/postgres/secmaster-svc-password`). Schema changes are Alembic migrations, applied when the container starts.
- **CI/CD:** `ci.yml` runs the platform's `app-ci.yml` on every PR (`ci / Build, test, lint` is required on `main`). `cd.yml` builds and pushes to ECR on merge, then deploys to the hub. Docs-only merges don't deploy.
- **Secrets:** anything under `/home-platform/secmaster-svc/` in SSM arrives in the container's environment at deploy time (the Airflow job token arrives as `AIRFLOW_TOKEN`).

## Local development

```
pip install -r requirements.txt -r requirements-dev.txt
./gen_proto.sh
uvicorn app.main:app --reload
```

`POSTGRES_PASSWORD` is optional locally; without it the app runs with no database.

Tests and lint: `pytest` and `ruff check app/ tests/ migrations/`, or `docker build --target test .` / `--target lint .`, which is what CI runs. Where PyPI is blocked, `scripts/sandbox-test.sh` runs everything but `tests/test_grpc.py`.

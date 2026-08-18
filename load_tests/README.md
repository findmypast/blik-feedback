# Local load testing

This harness exercises the synthetic `Blik Scale Test` organisation. It refuses
to run against any host except `localhost`, `127.0.0.1`, or `::1`.

## Prepare local data

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
docker compose -f docker-compose.yml -f docker-compose.local.yml exec web \
  python manage.py seed_scale_test --reset --users 100 --teams 12
```

Create a team, individual, or entire-organisation cycle in the UI, then verify it:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml exec web \
  python manage.py validate_scale_test
```

## Run

Start with a read-only baseline:

```bash
uv run --group load locust -f load_tests/locustfile.py \
  --host http://localhost:8000 --headless \
  --users 10 --spawn-rate 2 --run-time 2m \
  --csv load_tests/results/baseline
```

The read-only baseline deliberately reuses the scale administrator account.
Blik limits login attempts to five per minute, so ramping many distinct fresh
sessions at once will correctly trigger HTTP 429 responses. To measure a varied
role mix, provide known-active accounts with `LOAD_TEST_EMAILS` and use a spawn
rate compatible with that security limit.

Increase gradually to 25 and then 50 users. The command exits unsuccessfully if
the failure ratio exceeds 1% or the p95 response time exceeds 1500 ms. Override
these with `LOAD_TEST_MAX_FAILURE_RATIO` and `LOAD_TEST_MAX_P95_MS`.

Assessment submission changes local data and is deliberately opt-in:

```bash
LOAD_TEST_ENABLE_WRITES=1 uv run --group load locust \
  -f load_tests/locustfile.py --host http://localhost:8000 \
  --headless --users 10 --spawn-rate 1 --run-time 2m \
  --csv load_tests/results/writes
```

All seeded accounts use `blik-test-password`. Set `LOAD_TEST_EMAILS` to a
comma-separated set of active synthetic accounts when a scenario requires one
unique account per virtual user. Never enable writes against shared or valuable
data; reseed afterward to restore a deterministic baseline.

## Workload mix

- Dashboard: 50%
- Teams: 17%
- Cycles: 17%
- Reports: 8%
- Assessment discovery/submission: 8% (submission only with writes enabled)

Review the generated CSV files for failures, median, p95, p99, and request rate.

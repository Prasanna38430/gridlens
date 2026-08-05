# gridlens

A bitemporal, replayable lakehouse over European electricity market data.

The problem: ENTSO-E and RTE revise figures after publishing them.
Near-real-time generation and load numbers get corrected days or weeks later.
If you overwrite on ingest, every historical report silently changes and
settlement numbers stop reconciling with the counterparty.

So gridlens keeps two clocks on every row:

- `valid_time`, the settlement period the row describes
- `known_at`, when we learned it

Bronze is append-only. A revision is a new row, never an update. Anything
built on top can be read as it stands today, or as it stood on a given date.

## Status

Day 1 of 30. Nothing works yet.

## Intended stack

AWS eu-west-3. Apache Iceberg on S3, Glue Data Catalog, Athena, dbt for
silver and gold. Ingestion on Lambda. Streaming on Redpanda into Spark
Structured Streaming. Airflow self-hosted for orchestration. Terraform for
infrastructure.

Steady state has to stay under 5 EUR a month, which rules out NAT gateways,
EC2, RDS, MWAA, MSK and Kinesis.

## Local development

Needs Python 3.12 and uv.

    uv sync
    uv run pytest

## Known limitations

Nothing is deployed and no data has been ingested. Decisions get recorded in
`docs/adr/` as they are made.

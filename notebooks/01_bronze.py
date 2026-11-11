# PARAMETERS
run_id = ""

# %%
import json
import re
import uuid
from datetime import datetime, timezone
from pyspark.sql import functions as F, types as T

LH = "{{lakehouse_abfss}}"
SOURCES = {
    "bronze_media_spend": "media_spend.csv",
    "bronze_sales": "sales.csv",
    "bronze_calendar": "calendar.csv",
    "bronze_campaigns": "campaigns.csv",
}
run_id = run_id or f"manual-{uuid.uuid4()}"

LOG_SCHEMA = T.StructType([T.StructField(c, t) for c, t in [
    ("run_id", T.StringType()), ("layer", T.StringType()), ("step", T.StringType()), ("status", T.StringType()),
    ("rows_in", T.LongType()), ("rows_out", T.LongType()), ("started_at", T.TimestampType()),
    ("ended_at", T.TimestampType()), ("message", T.StringType())]])


def log(step, status, rows_in, rows_out, started_at, message=""):
    row = (run_id, "bronze", step, status, rows_in, rows_out, started_at, datetime.now(timezone.utc), message[:4000])
    spark.createDataFrame([row], LOG_SCHEMA).write.format("delta").mode("append").save(f"{LH}/Tables/dbo/ops_run_log")


def snake(c):
    return re.sub(r"_+", "_", re.sub(r"[^0-9a-zA-Z]+", "_", c)).strip("_").lower()

# %%
failures, infra_errors = [], []
for table, file in SOURCES.items():
    started = datetime.now(timezone.utc)
    try:
        path = f"{LH}/Files/landing/{file}"
        source_rows = spark.read.text(path).count() - 1  # physical lines minus header
        df = spark.read.option("header", True).option("inferSchema", False).csv(path)
        df = df.toDF(*[snake(c) for c in df.columns])
        src_cols = df.columns
        df = (df.withColumn("_row_hash", F.xxhash64(*[F.coalesce(F.col(c), F.lit("<null>")) for c in src_cols]))
                .withColumn("_source_file", F.lit(file))
                .withColumn("_ingest_ts", F.current_timestamp())
                .withColumn("_batch_id", F.lit(run_id)))
        df.write.format("delta").mode("overwrite").option("overwriteSchema", True).save(f"{LH}/Tables/dbo/{table}")
        loaded = spark.read.format("delta").load(f"{LH}/Tables/dbo/{table}").count()
        status = "SUCCEEDED" if loaded == source_rows else "FAILED"
        log(f"load {table}", status, source_rows, loaded, started, f"{file}: source {source_rows}, loaded {loaded}")
        if status == "FAILED":
            failures.append(f"{table}: source {source_rows} != loaded {loaded}")
    except Exception as e:
        log(f"load {table}", "ERROR", None, None, started, repr(e))
        infra_errors.append(f"{table}: {e!r}")

# %%
result = {"run_id": run_id, "status": "FAILED_CHECKS" if failures else "OK", "failed_checks": failures}
notebookutils.fs.put(f"{LH}/Files/_audit/bronze_{run_id}.json", json.dumps(result), True)
# Unreadable files or failed writes stop the pipeline; count mismatches are reported, not raised
if infra_errors:
    raise RuntimeError("Bronze load error: " + "; ".join(infra_errors))
print(result)
notebookutils.notebook.exit(json.dumps(result))

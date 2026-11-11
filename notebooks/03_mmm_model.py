# PARAMETERS
run_id = ""

# %%
# INCLUDE scripts/mmm_core.py

# %%
import json
import uuid
from datetime import datetime, timezone
import pandas as pd
from pyspark.sql import functions as F

LH = "{{lakehouse_abfss}}"
TBL = f"{LH}/Tables/dbo"
run_id = run_id or f"manual-{uuid.uuid4()}"
started = datetime.now(timezone.utc)


def read(t):
    return spark.read.format("delta").load(f"{TBL}/{t}")


def save(pdf, t):
    sdf = spark.createDataFrame(pdf).withColumn("run_id", F.lit(run_id))
    sdf.write.format("delta").mode("overwrite").option("overwriteSchema", True).save(f"{TBL}/{t}")
    return len(pdf)

# %%
# Region-week modelling frame from Silver
spend = (read("fact_media_daily").groupBy("week_start", "region").pivot("channel").agg(F.sum("spend")).toPandas())
channels = [c for c in spend.columns if c not in ("week_start", "region")]
spend = spend.rename(columns={c: f"spend_{c}" for c in channels})
sales = read("fact_sales_daily")
rev = sales.groupBy("week_start", "region").agg(F.sum("revenue").alias("revenue")).toPandas()
# Price index: average of each product's price relative to its list (max) price
price = (sales.join(sales.groupBy("product").agg(F.max("avg_price").alias("list_price")), "product")
         .groupBy("week_start", "region").agg(F.avg(F.col("avg_price") / F.col("list_price")).alias("price_raw"))
         .toPandas())
ctl = (read("fact_calendar_daily").groupBy("week_start", "region")
       .agg(F.max("is_holiday").cast("double").alias("holiday"), F.max("promo_flag").cast("double").alias("promo"),
            F.avg("competitor_index").alias("competitor_index")).toPandas())
weekly = rev.merge(spend, on=["week_start", "region"]).merge(ctl, on=["week_start", "region"]) \
            .merge(price, on=["week_start", "region"]).rename(columns={"week_start": "week"})
weekly["week"] = pd.to_datetime(weekly["week"])
weekly["price_index"] = weekly.price_raw / weekly.groupby("region").price_raw.transform("median")
weekly = weekly.fillna({f"spend_{c}": 0.0 for c in channels})
print(weekly.shape, channels)

# %%
model = MMM(weekly, channels, holdout_weeks=12).fit()
metrics = model.metrics()
parts = model.predict_parts()
roi = model.roi()
curves = model.response_curves()
scenarios = model.optimise()
print(metrics)
print(roi.round(3).to_string())

# %%
# Long-format decomposition: one row per week x region x component (Baseline or channel)
decomp = parts.melt(id_vars=["week", "region", "actual", "predicted", "is_holdout"],
                    value_vars=["baseline"] + channels, var_name="component", value_name="contribution")
decomp["component"] = decomp.component.replace({"baseline": "Baseline"})
decomp["week"] = decomp.week.dt.date
closure = float((parts.baseline + parts[channels].sum(axis=1) - parts.predicted).abs().max())

truth = json.loads(notebookutils.fs.head(f"{LH}/Files/reference/ground_truth.json", 100000))
roi["true_roi"] = roi.channel.map({c: v["target_roi"] for c, v in truth["channels"].items()})
roi["true_decay"] = roi.channel.map({c: v["decay"] for c, v in truth["channels"].items()})
roi["roi_error_pct"] = (roi.roi / roi.true_roi - 1) * 100
roi["spend_share"] = roi.spend / roi.spend.sum()
roi["contribution_share"] = roi.contribution / roi.contribution.sum()

metrics_row = pd.DataFrame([{**metrics, "decomposition_max_gap": closure, "weeks": int(weekly.week.nunique()),
                             "holdout_weeks": 12, "channels": len(channels),
                             "media_contribution": float(roi.contribution.sum()),
                             "true_media_contribution": float(sum(v["true_contribution_weekly_model"]
                                                                  for v in truth["channels"].values())),
                             "max_abs_roi_error_pct": float(roi.roi_error_pct.abs().max()),
                             "fitted_at": started.isoformat()}])
fit = parts.groupby(["week", "is_holdout"])[["actual", "predicted"]].sum().reset_index()
fit["week"] = fit.week.dt.date

rows = {"mmm_decomposition": save(decomp, "mmm_decomposition"), "mmm_channel_roi": save(roi, "mmm_channel_roi"),
        "mmm_response_curves": save(curves, "mmm_response_curves"),
        "mmm_budget_scenarios": save(scenarios, "mmm_budget_scenarios"),
        "mmm_model_fit": save(fit, "mmm_model_fit"), "mmm_model_metrics": save(metrics_row, "mmm_model_metrics")}

# %%
from pyspark.sql import types as T
LOG_SCHEMA = T.StructType([T.StructField(c, t) for c, t in [
    ("run_id", T.StringType()), ("layer", T.StringType()), ("step", T.StringType()), ("status", T.StringType()),
    ("rows_in", T.LongType()), ("rows_out", T.LongType()), ("started_at", T.TimestampType()),
    ("ended_at", T.TimestampType()), ("message", T.StringType())]])
result = {"run_id": run_id, "status": "OK", **{k: round(v, 4) for k, v in metrics.items()},
          "decomposition_max_gap": closure, "rows": rows}
spark.createDataFrame([(run_id, "model", "fit mmm", "SUCCEEDED", len(weekly), len(decomp), started,
                        datetime.now(timezone.utc), json.dumps(result)[:4000])], LOG_SCHEMA) \
     .write.format("delta").mode("append").save(f"{TBL}/ops_run_log")
print(result)
notebookutils.notebook.exit(json.dumps(result))

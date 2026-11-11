# PARAMETERS
run_id = ""

# %%
# INCLUDE scripts/ml_core.py

# %%
import json
import uuid
from datetime import datetime, timezone
import pandas as pd
from pyspark.sql import functions as F, types as T

LH = "{{lakehouse_abfss}}"
TBL = f"{LH}/Tables/dbo"
run_id = run_id or f"manual-{uuid.uuid4()}"
started = datetime.now(timezone.utc)


def read(t):
    return spark.read.format("delta").load(f"{TBL}/{t}")


def save(pdf, t):
    pdf = pdf.copy()
    for c in pdf.columns:
        if pd.api.types.is_datetime64_any_dtype(pdf[c]):
            pdf[c] = pdf[c].dt.date
        elif pdf[c].dtype == object:
            pdf[c] = pdf[c].astype(str).where(pdf[c].notna(), None)
    sdf = spark.createDataFrame(pdf).withColumn("run_id", F.lit(run_id))
    sdf.write.format("delta").mode("overwrite").option("overwriteSchema", True).save(f"{TBL}/{t}")
    return len(pdf)

# %%
# Campaign-day rows from Silver (summed over regions inside ml_core) and the MMM's same-budget channel ratios
daily = (read("fact_media_daily").join(read("dim_campaign").select("campaign", "objective"), "campaign")
         .join(read("dim_date").select("date_sk", F.col("full_date").alias("date")), "date_sk")
         .groupBy("date", "channel", "platform", "campaign", "objective")
         .agg(*[F.sum(c).alias(c) for c in ["spend", "impressions", "clicks", "conversions", "attributed_revenue"]])
         .toPandas())
opt = read("mmm_budget_scenarios").where(F.col("scenario") == "+0%").toPandas()
channel_ratio = (opt.set_index("channel").optimal_spend / opt.set_index("channel").current_spend).to_dict()
print(daily.shape, channel_ratio)

out = run(daily, channel_ratio)
rec = out["recommendations"]

# %%
# Evaluation against the planted issues (read only here, after recommendations are final)
truth = json.loads(notebookutils.fs.head(f"{LH}/Files/reference/campaign_truth.json", 100000))
ev = evaluate(rec, truth)
planted, healthy = ev[ev.planted_issue != "none"], ev[ev.planted_issue == "none"]
recall, false_pos = int(planted.correct.sum()), int((~healthy.correct).sum())
print(ev.to_string())

weekly = out["weekly"][["week", "campaign", "platform", "channel", "objective", "spend", "impressions", "clicks",
                        "conversions", "attributed_revenue", "ctr", "cvr", "cpm", "cpa", "cpc", "platform_roas"]]
rows = {
    "ml_campaign_weekly": save(weekly, "ml_campaign_weekly"),
    "ml_campaign_profile": save(out["profile"], "ml_campaign_profile"),
    "ml_trends": save(out["trends"], "ml_trends"),
    "ml_changepoints": save(out["changepoints"] if len(out["changepoints"]) else
                            pd.DataFrame(columns=["campaign", "kpi", "week", "before", "after", "shift", "bic_gain"]),
                            "ml_changepoints"),
    "ml_segments": save(out["segments"], "ml_segments"),
    "ml_recommendations": save(rec.drop(columns=["weight"]), "ml_recommendations"),
    "ml_evaluation": save(ev, "ml_evaluation"),
}

# %%
LOG_SCHEMA = T.StructType([T.StructField(c, t) for c, t in [
    ("run_id", T.StringType()), ("layer", T.StringType()), ("step", T.StringType()), ("status", T.StringType()),
    ("rows_in", T.LongType()), ("rows_out", T.LongType()), ("started_at", T.TimestampType()),
    ("ended_at", T.TimestampType()), ("message", T.StringType())]])
result = {"run_id": run_id, "status": "OK", "campaigns": int(rec.campaign.nunique()),
          "recall": f"{recall}/{len(planted)}", "false_positives": f"{false_pos}/{len(healthy)}",
          "segments_k": int(out["segments"].k.iloc[0]), "silhouette": round(float(out["segments"].silhouette.iloc[0]), 3),
          "actions": rec.action.value_counts().to_dict(), "rows": rows}
spark.createDataFrame([(run_id, "ml", "campaign intelligence", "SUCCEEDED", len(daily), len(rec), started,
                        datetime.now(timezone.utc), json.dumps(result)[:4000])], LOG_SCHEMA) \
     .write.format("delta").mode("append").save(f"{TBL}/ops_run_log")
print(result)
notebookutils.notebook.exit(json.dumps(result))

# PARAMETERS
run_id = ""

# %%
import json
import uuid
from datetime import datetime, timezone
from functools import reduce
from pyspark.sql import functions as F, types as T

LH = "{{lakehouse_abfss}}"
TBL = f"{LH}/Tables/dbo"
run_id = run_id or f"manual-{uuid.uuid4()}"
started = datetime.now(timezone.utc)

REGIONS = ["North", "South", "East", "West", "Central"]
# channel -> (group, colour used across the report, sort order)
CHANNELS = {"TV": ("Offline", "#7B2FF7", 1), "OOH": ("Offline", "#F72585", 2), "Paid Search": ("Online", "#00B4D8", 3),
            "Social": ("Online", "#06D6A0", 4), "Display": ("Online", "#FFB703", 5), "Email": ("Online", "#FB5607", 6)}
PRODUCTS = ["Core", "Premium", "Accessories"]
# platform -> (channel, platform type)
PLATFORMS = {"Linear TV": ("TV", "Broadcast"), "Connected TV": ("TV", "Streaming"),
             "Billboards": ("OOH", "Static"), "Transit": ("OOH", "Static"), "Digital OOH": ("OOH", "Digital"),
             "Google Ads": ("Paid Search", "Search engine"), "Microsoft Ads": ("Paid Search", "Search engine"),
             "Meta": ("Social", "Social network"), "TikTok": ("Social", "Social network"),
             "LinkedIn": ("Social", "Social network"), "YouTube": ("Social", "Online video"),
             "The Trade Desk": ("Display", "Programmatic DSP"), "DV360": ("Display", "Programmatic DSP"),
             "Amazon DSP": ("Display", "Retail media DSP"), "Klaviyo": ("Email", "CRM")}
DATE_FORMATS = ["yyyy-MM-dd", "MM/dd/yyyy", "M/d/yyyy"]

LOG_SCHEMA = T.StructType([T.StructField(c, t) for c, t in [
    ("run_id", T.StringType()), ("layer", T.StringType()), ("step", T.StringType()), ("status", T.StringType()),
    ("rows_in", T.LongType()), ("rows_out", T.LongType()), ("started_at", T.TimestampType()),
    ("ended_at", T.TimestampType()), ("message", T.StringType())]])


def log(step, status, rows_in, rows_out, message=""):
    row = (run_id, "silver", step, status, rows_in, rows_out, started, datetime.now(timezone.utc), message[:4000])
    spark.createDataFrame([row], LOG_SCHEMA).write.format("delta").mode("append").save(f"{TBL}/ops_run_log")


def read(t):
    return spark.read.format("delta").load(f"{TBL}/{t}")


def write(df, t, mode="overwrite"):
    df.write.format("delta").mode(mode).option("overwriteSchema", mode == "overwrite").save(f"{TBL}/{t}")
    return read(t).count()


def parse_date(c):
    return F.coalesce(*[F.to_date(F.col(c), fmt) for fmt in DATE_FORMATS])


def num(c):
    return F.col(c).cast("double")

# %%
# Each source: typed columns plus (rule, severity, column, failing condition)
campaign_master = read("bronze_campaigns").select("campaign", F.col("platform").alias("master_platform"),
                                                  F.col("objective").alias("master_objective"))
media = (read("bronze_media_spend").join(campaign_master, "campaign", "left")
         .withColumn("d", parse_date("date")).withColumn("spend_n", num("spend"))
         .withColumn("impr_n", num("impressions")).withColumn("clicks_n", num("clicks"))
         .withColumn("conv_n", num("conversions")).withColumn("views_n", num("video_views"))
         .withColumn("attr_rev_n", num("attributed_revenue")))
media_rules = [
    ("date_unparseable", "CRITICAL", "date", F.col("d").isNull()),
    ("region_unknown", "CRITICAL", "region", ~F.col("region").isin(REGIONS)),
    ("channel_unknown", "CRITICAL", "channel", ~F.col("channel").isin(list(CHANNELS))),
    ("spend_not_non_negative_number", "CRITICAL", "spend", F.col("spend_n").isNull() | (F.col("spend_n") < 0)),
    ("impressions_negative", "WARNING", "impressions", F.col("impr_n") < 0),
    ("platform_unknown", "CRITICAL", "platform", ~F.col("platform").isin(list(PLATFORMS))),
    ("platform_channel_mismatch", "CRITICAL", "platform",
     F.create_map(*[x for k, (ch, _) in PLATFORMS.items() for x in (F.lit(k), F.lit(ch))])[F.col("platform")]
     != F.col("channel")),
    ("campaign_unknown", "CRITICAL", "campaign", F.col("master_platform").isNull()),
    ("campaign_platform_mismatch", "CRITICAL", "campaign", F.col("master_platform") != F.col("platform")),
    ("clicks_exceed_impressions", "WARNING", "clicks", F.col("clicks_n") > F.col("impr_n")),
    ("conversions_exceed_clicks", "WARNING", "conversions", (F.col("conv_n") > F.col("clicks_n")) & (F.col("clicks_n") > 0)),
]
sales = (read("bronze_sales")
         .withColumn("d", parse_date("date")).withColumn("revenue_n", num("revenue"))
         .withColumn("units_n", num("units")).withColumn("price_n", num("avg_price")))
sales_rules = [
    ("date_unparseable", "CRITICAL", "date", F.col("d").isNull()),
    ("region_unknown", "CRITICAL", "region", ~F.col("region").isin(REGIONS)),
    ("product_unknown", "CRITICAL", "product", ~F.col("product").isin(PRODUCTS)),
    ("revenue_not_non_negative_number", "CRITICAL", "revenue", F.col("revenue_n").isNull() | (F.col("revenue_n") < 0)),
    ("price_not_positive", "WARNING", "avg_price", F.col("price_n").isNull() | (F.col("price_n") <= 0)),
]
cal = (read("bronze_calendar")
       .withColumn("d", parse_date("date")).withColumn("comp_n", num("competitor_index")))
cal_rules = [
    ("date_unparseable", "CRITICAL", "date", F.col("d").isNull()),
    ("region_unknown", "CRITICAL", "region", ~F.col("region").isin(REGIONS)),
    ("competitor_index_missing", "WARNING", "competitor_index", F.col("comp_n").isNull()),
]


def apply_rules(df, rules, source):
    for rule, sev, col, cond in rules:
        df = df.withColumn(f"dq_{rule}", F.coalesce(cond, F.lit(False)))
    crit = [f"dq_{r}" for r, s, _, _ in rules if s == "CRITICAL"]
    df = df.withColumn("is_quarantined", reduce(lambda a, b: a | b, [F.col(c) for c in crit])).cache()
    exc = [df.where(F.col(f"dq_{rule}")).select(
        F.lit(run_id).alias("run_id"), F.lit("silver").alias("layer"), F.lit(source).alias("source_table"),
        F.col("_row_hash").alias("source_row_hash"), F.col("region").alias("business_key"),
        F.lit(rule).alias("rule_name"), F.lit(sev).alias("severity"), F.lit(col).alias("column_name"),
        F.col(col).cast("string").alias("raw_value"),
        F.lit("QUARANTINED" if sev == "CRITICAL" else "LOADED_WITH_WARNING").alias("action"),
        F.current_timestamp().alias("logged_at")) for rule, sev, col, _ in rules]
    raw_cols = [c for c in df.columns if not c.startswith("dq_") and not c.startswith("_") and c not in
                ("d", "spend_n", "impr_n", "clicks_n", "conv_n", "views_n", "attr_rev_n", "revenue_n", "units_n",
                 "price_n", "comp_n", "is_quarantined", "master_platform", "master_objective")]
    quarantine = df.where("is_quarantined").select(
        F.lit(run_id).alias("run_id"), F.lit(source).alias("source_table"), F.col("_row_hash").alias("source_row_hash"),
        F.concat_ws(",", *[F.when(F.col(f"dq_{r}"), F.lit(r)) for r, _, _, _ in rules]).alias("failed_rules"),
        F.to_json(F.struct(*raw_cols)).alias("raw_record"), F.current_timestamp().alias("quarantined_at"))
    return df.where("NOT is_quarantined"), reduce(lambda a, b: a.unionByName(b), exc), quarantine


media_ok, media_exc, media_q = apply_rules(media, media_rules, "bronze_media_spend")
sales_ok, sales_exc, sales_q = apply_rules(sales, sales_rules, "bronze_sales")
cal_ok, cal_exc, cal_q = apply_rules(cal, cal_rules, "bronze_calendar")

exceptions = media_exc.unionByName(sales_exc).unionByName(cal_exc)
exceptions.cache()
exception_rows = exceptions.count()
write(exceptions, "ops_dq_exceptions", mode="append")  # appends nothing on a clean run, but keeps the table present
quarantine_rows = write(media_q.unionByName(sales_q).unionByName(cal_q), "silver_quarantine")

# %%
week_start = F.to_date(F.date_trunc("week", F.col("d")))  # Spark weeks start on Monday
dates = F.sequence(F.to_date(F.lit("2022-12-26")), F.to_date(F.lit("2026-01-04")))
dim_date = (spark.range(1).select(F.explode(dates).alias("d"))
            .select(F.date_format("d", "yyyyMMdd").cast("int").alias("date_sk"), F.col("d").alias("full_date"),
                    week_start.alias("week_start"), F.year("d").alias("year"), F.quarter("d").alias("quarter"),
                    F.month("d").alias("month"), F.date_format("d", "MMM").alias("month_name"),
                    F.weekofyear("d").alias("iso_week"), F.date_format("d", "EEE").alias("day_name")))
dim_region = spark.createDataFrame([(r,) for r in REGIONS], ["region"])
dim_channel = spark.createDataFrame([(c, g, col, s) for c, (g, col, s) in CHANNELS.items()],
                                    ["channel", "channel_group", "channel_color", "channel_sort"])
dim_product = spark.createDataFrame([(p,) for p in PRODUCTS], ["product"])
dim_campaign = read("bronze_campaigns").select("campaign", "platform", "channel", "objective")
dim_platform = spark.createDataFrame([(k, ch, t) for k, (ch, t) in PLATFORMS.items()],
                                     ["platform", "channel", "platform_type"])

date_sk = F.date_format("d", "yyyyMMdd").cast("int").alias("date_sk")
fact_media = media_ok.select(date_sk, week_start.alias("week_start"), "region", "channel", "platform", "campaign",
                             F.col("spend_n").alias("spend"), F.col("impr_n").alias("impressions"),
                             F.col("clicks_n").alias("clicks"), F.col("conv_n").alias("conversions"),
                             F.col("views_n").alias("video_views"), F.col("attr_rev_n").alias("attributed_revenue"),
                             F.col("_row_hash").alias("source_row_hash"),
                             F.lit(run_id).alias("_batch_id"))
fact_sales = sales_ok.select(date_sk, week_start.alias("week_start"), "region", "product",
                             F.col("revenue_n").alias("revenue"), F.col("units_n").alias("units"),
                             F.col("price_n").alias("avg_price"), F.col("_row_hash").alias("source_row_hash"),
                             F.lit(run_id).alias("_batch_id"))
fact_cal = cal_ok.select(date_sk, week_start.alias("week_start"), "region", F.coalesce("holiday", F.lit("")).alias("holiday"),
                         (F.coalesce("holiday", F.lit("")) != "").cast("int").alias("is_holiday"),
                         F.col("promo_flag").cast("int").alias("promo_flag"), F.col("comp_n").alias("competitor_index"),
                         F.col("_row_hash").alias("source_row_hash"))

counts = {t: write(df, t) for t, df in [
    ("dim_date", dim_date), ("dim_region", dim_region), ("dim_channel", dim_channel), ("dim_product", dim_product), ("dim_platform", dim_platform), ("dim_campaign", dim_campaign),
    ("fact_media_daily", fact_media), ("fact_sales_daily", fact_sales), ("fact_calendar_daily", fact_cal)]}

# %%
# Reconcile Bronze -> Silver: every Bronze row is in its fact or in quarantine, and money totals agree
q = read("silver_quarantine")


def qcount(src):
    return q.where(F.col("source_table") == src).count()


def qsum(src, field):
    return q.where(F.col("source_table") == src).select(
        F.get_json_object("raw_record", f"$.{field}").cast("double").alias("v")).agg(F.sum("v")).first()[0] or 0.0


b_media, b_sales, b_cal = read("bronze_media_spend"), read("bronze_sales"), read("bronze_calendar")
spend_b = b_media.agg(F.sum(num("spend"))).first()[0] or 0.0
spend_s = read("fact_media_daily").agg(F.sum("spend")).first()[0] or 0.0
rev_b = b_sales.agg(F.sum(num("revenue"))).first()[0] or 0.0
rev_s = read("fact_sales_daily").agg(F.sum("revenue")).first()[0] or 0.0
checks = {
    "media_rows": b_media.count() == counts["fact_media_daily"] + qcount("bronze_media_spend"),
    "sales_rows": b_sales.count() == counts["fact_sales_daily"] + qcount("bronze_sales"),
    "calendar_rows": b_cal.count() == counts["fact_calendar_daily"] + qcount("bronze_calendar"),
    "spend_total": abs(spend_b - spend_s - qsum("bronze_media_spend", "spend")) < 0.01,
    "revenue_total": abs(rev_b - rev_s - qsum("bronze_sales", "revenue")) < 0.01,
    "no_orphan_dates": read("fact_media_daily").join(read("dim_date"), "date_sk", "left_anti").count() == 0,
}
failed = [k for k, ok in checks.items() if not ok]
summary = {"run_id": run_id, "counts": counts, "quarantine_rows": quarantine_rows, "exception_rows": exception_rows,
           "spend_bronze": round(spend_b, 2), "spend_silver": round(spend_s, 2),
           "revenue_bronze": round(rev_b, 2), "revenue_silver": round(rev_s, 2), "checks": checks}
log("build silver", "FAILED" if failed else "SUCCEEDED", b_media.count() + b_sales.count() + b_cal.count(),
    counts["fact_media_daily"] + counts["fact_sales_daily"] + counts["fact_calendar_daily"],
    json.dumps({k: v for k, v in summary.items() if k != "checks"} | {"failed_checks": failed}, default=str))
notebookutils.fs.put(f"{LH}/Files/_audit/silver_{run_id}.json", json.dumps(summary, default=str), True)
print(summary)
notebookutils.notebook.exit(json.dumps({"run_id": run_id, "status": "FAILED_CHECKS" if failed else "OK",
                                        "failed_checks": failed, "quarantine_rows": quarantine_rows}))

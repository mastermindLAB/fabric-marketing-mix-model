"""Fit the MMM on the local CSVs and compare recovered ROI with ground truth (no Fabric needed)."""
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mmm_core import MMM  # noqa: E402

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
spend = pd.read_csv(f"{D}/media_spend.csv", parse_dates=["date"])
sales = pd.read_csv(f"{D}/sales.csv", parse_dates=["date"])
cal = pd.read_csv(f"{D}/calendar.csv", parse_dates=["date"])
truth = json.load(open(f"{D}/ground_truth.json"))


def week(d):
    return d - pd.to_timedelta(d.dt.day_of_week, unit="D")


spend["week"], sales["week"], cal["week"] = week(spend.date), week(sales.date), week(cal.date)
sw = spend.pivot_table(index=["week", "region"], columns="channel", values="spend", aggfunc="sum")
sw.columns = [f"spend_{c}" for c in sw.columns]
rev = sales.groupby(["week", "region"]).revenue.sum().rename("revenue")
price = sales.assign(w=sales.revenue).groupby(["week", "region"]).apply(
    lambda g: (g.avg_price / g.avg_price.groupby(g["product"]).transform("max")).mean()).rename("price_raw")
ctl = cal.groupby(["week", "region"]).agg(holiday=("holiday", lambda s: float((s.fillna("") != "").any())),
                                          promo=("promo_flag", "max"), competitor_index=("competitor_index", "mean"))
weekly = pd.concat([rev, sw, ctl, price], axis=1).reset_index()
weekly["price_index"] = weekly.price_raw / weekly.groupby("region").price_raw.transform("median")

channels = list(truth["channels"])
t0 = time.time()
m = MMM(weekly, channels).fit()
print(f"fit {time.time() - t0:.0f}s", m.metrics())
roi = m.roi()
roi["true_roi"] = roi.channel.map({c: v["target_roi"] for c, v in truth["channels"].items()})
roi["true_decay"] = roi.channel.map({c: v["decay"] for c, v in truth["channels"].items()})
print(roi[["channel", "roi", "true_roi", "marginal_roi", "decay", "true_decay", "hill_shape", "half_saturation"]]
      .round(2).to_string(index=False))
p = m.predict_parts()
print("decomposition closes:", float((p.baseline + p[channels].sum(axis=1) - p.predicted).abs().max()))
opt = m.optimise()
print(opt.groupby("scenario")[["current_contribution", "optimal_contribution"]].sum().round(0))

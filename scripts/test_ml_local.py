"""Run the campaign ML locally and score it against data/campaign_truth.json."""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_core  # noqa: E402

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
daily = pd.read_csv(f"{D}/media_spend.csv")
# Channel ratios from the MMM optimizer's same-budget scenario (optimal / current spend)
ratio = {"TV": 0.93, "OOH": 0.6, "Paid Search": 1.17, "Social": 1.3, "Display": 0.6, "Email": 1.5}
out = ml_core.run(daily, ratio)
ev = ml_core.evaluate(out["recommendations"], json.load(open(f"{D}/campaign_truth.json")))
pd.set_option("display.width", 250, "display.max_colwidth", 200)
print(ev.to_string(index=False))
planted = ev[ev.planted_issue != "none"]
healthy = ev[ev.planted_issue == "none"]
print(f"\nrecall {planted.correct.sum()}/{len(planted)}, false positives {(~healthy.correct).sum()}/{len(healthy)}")
print(out["segments"].groupby("segment").campaign.apply(list).to_string(), out["segments"].k.iloc[0], round(out["segments"].silhouette.iloc[0], 3))
r = out["recommendations"]
print(r[["campaign", "reason", "action", "segment", "within_channel_change_pct", "budget_change_pct"]].round(3).to_string(index=False))
print(r.groupby("channel")[["recent_spend", "recommended_spend"]].sum().assign(
    ratio=lambda x: x.recommended_spend / x.recent_spend).round(3))
for c in ["TikTok Spring Creators", "Meta Retargeting Cart", "DV360 Prospecting"]:
    print("\n", c, ":", r.set_index("campaign").evidence[c])

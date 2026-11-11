"""Generate a synthetic marketing mix dataset with known ground truth (fixed seed).

Outputs to data/:
  media_spend.csv     daily spend, impressions, clicks by region x channel
  sales.csv           daily revenue, units, average price by region x product
  calendar.csv        daily holidays, promotions by region, competitor index
  ground_truth.json   true adstock decay, saturation, coefficients and ROI per channel

True response is built at region-week grain:
  revenue = base_r * trend * seasonality * (1 + holiday + promo) - competitor + price
            + sum_c beta_c * size_r * hill(adstock_c(spend_rc) / mean_rc) + noise
then spread to days with a day-of-week pattern.
"""
import json
import os

import numpy as np
import pandas as pd

rng = np.random.default_rng(20261108)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
START, WEEKS = pd.Timestamp("2023-01-02"), 156  # Mondays, 3 years

REGIONS = {"North": 1.25, "South": 0.95, "East": 1.10, "West": 1.00, "Central": 0.70}
PRODUCTS = {"Core": (0.55, 42.0), "Premium": (0.30, 89.0), "Accessories": (0.15, 19.0)}  # share, price
# channel: annual national spend, weekly decay, hill shape, half-saturation (x mean adstock), target ROI, CPM, CTR
CHANNELS = {
    "TV":          dict(spend=6.0e6, decay=0.60, shape=2.0, half=1.1, roi=1.6, cpm=18.0, ctr=0.000),
    "Paid Search": dict(spend=4.0e6, decay=0.10, shape=1.2, half=1.4, roi=3.2, cpm=35.0, ctr=0.045),
    "Social":      dict(spend=3.0e6, decay=0.30, shape=1.5, half=1.2, roi=2.4, cpm=9.0,  ctr=0.012),
    "Display":     dict(spend=2.0e6, decay=0.20, shape=1.3, half=1.0, roi=0.9, cpm=4.5,  ctr=0.004),
    "OOH":         dict(spend=1.5e6, decay=0.50, shape=2.2, half=1.0, roi=1.1, cpm=6.0,  ctr=0.000),
    "Email":       dict(spend=0.5e6, decay=0.05, shape=1.0, half=1.5, roi=4.5, cpm=1.5,  ctr=0.030),
}
# Geo-holdout experiments: (region, channel) -> weeks with spend switched off, used to separate
# always-on channels from baseline demand
GEO_HOLDOUTS = {("Central", "Paid Search"): (60, 68), ("South", "Email"): (90, 98),
                ("West", "Social"): (110, 118), ("East", "Display"): (30, 38)}
BASE_WEEKLY = 1.30e6  # national base revenue per week before region scaling

weeks = pd.date_range(START, periods=WEEKS, freq="W-MON")
t = np.arange(WEEKS)
woy = weeks.isocalendar().week.to_numpy().astype(int)


def flighting(name):
    """Weekly national spend pattern (sums to annual spend per 52 weeks)."""
    if name == "TV":            # bursts: 4 weeks on, 3 off, heavier in Q4
        w = np.where((t % 7) < 4, 1.0, 0.08) * (1 + 0.6 * (woy >= 40))
    elif name == "OOH":         # quarterly 6-week campaigns
        w = np.where((woy % 13) < 6, 1.0, 0.05)
    elif name == "Paid Search": # always on, follows demand
        w = 1 + 0.35 * np.sin(2 * np.pi * (woy - 10) / 52) + rng.normal(0, 0.08, WEEKS)
    elif name == "Social":
        w = 1 + 0.25 * np.sin(2 * np.pi * (woy + 5) / 52) + rng.normal(0, 0.25, WEEKS)
    elif name == "Display":
        w = rng.gamma(2.0, 0.5, WEEKS)
    else:                       # Email: steady with promo spikes
        w = 1 + 0.5 * (rng.random(WEEKS) < 0.15)
    w = np.clip(w, 0.02, None)
    return w / w.sum() * (CHANNELS[name]["spend"] * WEEKS / 52)


def adstock(x, decay):
    out = np.zeros_like(x)
    for i in range(len(x)):
        out[i] = x[i] + (decay * out[i - 1] if i else 0)
    return out


def hill(x, shape, half):
    return x ** shape / (x ** shape + half ** shape)


# ---- weekly ground truth by region ----
size_total = sum(REGIONS.values())
national = {c: flighting(c) for c in CHANNELS}
seasonality = 1 + 0.12 * np.sin(2 * np.pi * (woy - 14) / 52) + 0.06 * np.cos(4 * np.pi * woy / 52)
trend = 1 + 0.10 * t / WEEKS  # +10% over three years
holiday_week = np.isin(woy, [47, 48, 51, 52]).astype(float)  # Black Friday, Cyber week, Christmas
competitor = np.clip(100 + np.cumsum(rng.normal(0, 1.2, WEEKS)), 85, 120)

rows, contrib = [], {c: 0.0 for c in CHANNELS}
# Scale betas so each channel hits its target ROI over the full period
hill_sums = {c: 0.0 for c in CHANNELS}
region_media = {}
for r, size in REGIONS.items():
    for c, p in CHANNELS.items():
        # Regional campaigns differ from the national plan (persistent AR(1) deviation, about +/-25%),
        # which is the geo variation an MMM needs to separate media from seasonality
        dev = np.zeros(WEEKS)
        for i in range(WEEKS):
            dev[i] = 0.8 * (dev[i - 1] if i else 0) + rng.normal(0, 0.15)
        spend_rc = national[c] * size / size_total * np.clip(1 + dev, 0.4, 1.8)
        for (er, ec), (w0, w1) in GEO_HOLDOUTS.items():  # channel switched off in one region
            if er == r and ec == c:
                spend_rc[w0:w1] = 0.0
        ad = adstock(spend_rc, p["decay"])
        h = hill(ad / ad.mean(), p["shape"], p["half"])
        region_media[(r, c)] = (spend_rc, h)
        hill_sums[c] += (h * size).sum()
beta = {c: p["roi"] * sum(region_media[(r, c)][0].sum() for r in REGIONS) / hill_sums[c] for c, p in CHANNELS.items()}

for r, size in REGIONS.items():
    promo = (rng.random(WEEKS) < 0.10).astype(float)
    price_index = 1 - 0.08 * promo + rng.normal(0, 0.01, WEEKS)
    base = BASE_WEEKLY * size / size_total * len(REGIONS) / len(REGIONS)
    baseline = base * trend * seasonality * (1 + 0.25 * holiday_week + 0.10 * promo)
    baseline -= base * 0.004 * (competitor - 100)
    baseline -= base * 0.8 * (price_index - 1)
    media_total = np.zeros(WEEKS)
    for c in CHANNELS:
        spend_rc, h = region_media[(r, c)]
        effect = beta[c] * size * h
        contrib[c] += effect.sum()
        media_total += effect
    noise = rng.normal(0, 0.025, WEEKS) * baseline
    revenue = baseline + media_total + noise
    rows.append(pd.DataFrame({"week": weeks, "region": r, "revenue": revenue, "baseline": baseline,
                              "media": media_total, "promo": promo, "price_index": price_index}))
weekly = pd.concat(rows)

# ---- spread to days ----
dow = np.array([0.13, 0.13, 0.14, 0.14, 0.16, 0.17, 0.13])  # Mon..Sun
days = pd.date_range(START, periods=WEEKS * 7, freq="D")
day_week = days - pd.to_timedelta(days.day_of_week, unit="D")
week_idx = np.repeat(np.arange(WEEKS), 7)


def day_shares(noise_sd):
    """Day-of-week shares with noise, normalised so each week's days sum to exactly 1."""
    raw = dow[days.day_of_week] * rng.normal(1, noise_sd, len(days)).clip(0.5, 1.5)
    return raw / np.bincount(week_idx, raw)[week_idx]


spend_rows, sales_rows = [], []
for r in REGIONS:
    wk = weekly[weekly.region == r].set_index("week")
    for c, p in CHANNELS.items():
        spend_w = pd.Series(region_media[(r, c)][0], index=weeks)
        daily = spend_w.reindex(day_week).to_numpy() * day_shares(0.05)
        impressions = daily / p["cpm"] * 1000 * rng.normal(1, 0.04, len(days))
        clicks = impressions * p["ctr"] * rng.normal(1, 0.08, len(days))
        spend_rows.append(pd.DataFrame({"date": days.date, "region": r, "channel": c, "spend": daily.round(2),
                                        "impressions": impressions.round(0), "clicks": clicks.clip(0).round(0)}))
    rev_day = wk.revenue.reindex(day_week).to_numpy() * day_shares(0.03)
    mix = np.array([rng.normal(share, 0.02, len(days)).clip(0.05) for share, _ in PRODUCTS.values()])
    mix = mix / mix.sum(axis=0)  # product split sums to 1 every day, so revenue totals stay exact
    for k, (prod, (share, price)) in enumerate(PRODUCTS.items()):
        price_day = price * wk.price_index.reindex(day_week).to_numpy()
        rev = rev_day * mix[k]
        sales_rows.append(pd.DataFrame({"date": days.date, "region": r, "product": prod,
                                        "revenue": rev.round(2), "units": (rev / price_day).round(0),
                                        "avg_price": price_day.round(2)}))

spend = pd.concat(spend_rows)

# ---- platform split: each channel's daily spend divided across its platforms (sums stay exact) ----
# channel -> [(platform, spend share, CPM multiplier, CTR, conversion rate per click, video view rate)]
PLATFORMS = {
    "TV": [("Linear TV", 0.70, 1.0, 0.0, 0.0, 0.0), ("Connected TV", 0.30, 1.6, 0.002, 0.020, 0.92)],
    "OOH": [("Billboards", 0.50, 1.0, 0.0, 0.0, 0.0), ("Transit", 0.25, 0.8, 0.0, 0.0, 0.0),
            ("Digital OOH", 0.25, 1.4, 0.0, 0.0, 0.0)],
    "Paid Search": [("Google Ads", 0.82, 1.0, 0.048, 0.055, 0.0), ("Microsoft Ads", 0.18, 0.8, 0.038, 0.050, 0.0)],
    "Social": [("Meta", 0.45, 1.0, 0.011, 0.025, 0.25), ("TikTok", 0.25, 0.8, 0.009, 0.018, 0.35),
               ("LinkedIn", 0.12, 3.0, 0.006, 0.030, 0.10), ("YouTube", 0.18, 1.2, 0.004, 0.015, 0.45)],
    "Display": [("The Trade Desk", 0.40, 1.0, 0.004, 0.010, 0.0), ("DV360", 0.35, 1.1, 0.0035, 0.009, 0.05),
                ("Amazon DSP", 0.25, 1.3, 0.005, 0.030, 0.0)],
    "Email": [("Klaviyo", 1.00, 1.0, 0.020, 0.010, 0.0)],
}
AOV = 65.0  # average order value used for platform-attributed revenue
# Campaigns under each platform: (campaign, objective, share within platform, planted issue, issue weeks)
# Planted issues change platform KPIs only (never channel spend), and are written to campaign_truth.json,
# which the ML step must not read.
CAMPAIGNS = {
    "Linear TV": [("TV Brand Always-on", "Awareness", 0.7, None, None), ("TV Holiday Burst", "Awareness", 0.3, None, None)],
    "Connected TV": [("CTV Streaming Reach", "Awareness", 0.6, None, None),
                     ("CTV Retargeting", "Consideration", 0.4, "cpm_inflation", (60, 155))],
    "Billboards": [("Billboards City Centre", "Awareness", 1.0, None, None)],
    "Transit": [("Transit Commuter", "Awareness", 1.0, None, None)],
    "Digital OOH": [("DOOH Shopping Malls", "Awareness", 1.0, None, None)],
    "Google Ads": [("Search Brand", "Brand search", 0.30, None, None),
                   ("Search Non-brand Generic", "Non-brand search", 0.50, "cpm_inflation", (52, 155)),
                   ("Search Competitor Terms", "Conquest", 0.20, "bad_inventory", None)],
    "Microsoft Ads": [("Bing Brand", "Brand search", 0.5, None, None), ("Bing Non-brand", "Non-brand search", 0.5, None, None)],
    "Meta": [("Meta Prospecting Lookalike", "Prospecting", 0.50, None, None),
             ("Meta Retargeting Cart", "Retargeting", 0.35, "saturation", (40, 155)),
             ("Meta Advantage+ Shopping", "Conversion", 0.15, "underfunded_winner", None)],
    "TikTok": [("TikTok Spring Creators", "Prospecting", 0.6, "creative_fatigue", (20, 155)),
               ("TikTok Spark Ads", "Prospecting", 0.4, None, None)],
    "LinkedIn": [("LinkedIn B2B Leads", "Lead generation", 1.0, None, None)],
    "YouTube": [("YouTube Product Demo", "Consideration", 0.5, "step_up", (80, 155)),
                ("YouTube Bumper Reach", "Awareness", 0.5, None, None)],
    "The Trade Desk": [("TTD Open Exchange Reach", "Prospecting", 0.6, "bad_inventory", None),
                       ("TTD Curated PMP", "Prospecting", 0.4, None, None)],
    "DV360": [("DV360 Prospecting", "Prospecting", 0.6, "step_down", (100, 155)),
              ("DV360 Video", "Consideration", 0.4, None, None)],
    "Amazon DSP": [("Amazon Retail Retargeting", "Retargeting", 0.2, "underfunded_winner", None),
                   ("Amazon Audiences", "Prospecting", 0.8, None, None)],
    "Klaviyo": [("Email Newsletter", "Retention", 0.6, None, None), ("Email Abandoned Cart", "Retargeting", 0.4, None, None)],
}
EXPECTED_REASON = {"cpm_inflation": "COST_INFLATION", "bad_inventory": "LOW_QUALITY_TRAFFIC",
                   "saturation": "DIMINISHING_RETURNS", "underfunded_winner": "UNDERFUNDED_WINNER",
                   "creative_fatigue": "CTR_DECLINING", "step_up": "IMPROVED_AFTER_CHANGE",
                   "step_down": "CVR_DROP_CHANGEPOINT"}


def issue_multipliers(issue, span, wk):
    """Per-row multipliers (spend weight, CPM, CTR, CVR) for a planted issue at week index wk."""
    one = np.ones(len(wk))
    if issue is None:
        return one, one, one, one
    w0, w1 = span if span else (0, WEEKS - 1)
    prog = np.clip((wk - w0) / max(1, w1 - w0), 0, 1)  # 0 before the issue starts, 1 at its end
    if issue == "cpm_inflation":      # auction costs rise 50%
        return one, 1 + 0.5 * prog, one, one
    if issue == "bad_inventory":      # low-quality placements: weak CTR and CVR throughout
        return one, one, one * 0.45, one * 0.5
    if issue == "saturation":         # budget grows 2.5x, conversion rate falls with frequency
        grow = 1 + 1.5 * prog
        return grow, one, one, grow ** -0.75
    if issue == "underfunded_winner":  # strong conversion economics on a small budget
        return one, one, one * 1.3, one * 1.9
    if issue == "creative_fatigue":   # CTR decays to half as the same creative keeps running
        return one, one, 1 - 0.5 * prog, one
    if issue == "step_up":            # new creative at week w0 lifts CTR and CVR
        return one, one, np.where(wk >= w0, 1.15, 0.65), np.where(wk >= w0, 1.15, 0.8)
    if issue == "step_down":          # landing page change at week w0 cuts CVR 50%
        return one, one, one, np.where(wk >= w0, 0.5, 1.0)
    raise ValueError(issue)


prng = np.random.default_rng(7)  # separate stream, so channel-level data and the model are unchanged
plat_kpis = {p: (c, share, cpm_m, ctr, cvr, vtr) for c, plats in PLATFORMS.items()
             for p, share, cpm_m, ctr, cvr, vtr in plats}
campaign_rows, campaign_truth = [], []
for c, plats in PLATFORMS.items():
    ch = spend[spend.channel == c].reset_index(drop=True)
    wk = ((pd.to_datetime(ch.date) - START).dt.days // 7).to_numpy()
    combos = [(p, *camp) for p, *_ in plats for camp in CAMPAIGNS[p]]
    weights, mults = [], []
    for p, camp, objective, cshare, issue, span in combos:
        sw, cpm_x, ctr_x, cvr_x = issue_multipliers(issue, span, wk)
        weights.append(plat_kpis[p][1] * cshare * sw * prng.normal(1, 0.10, len(ch)).clip(0.6, 1.4))
        mults.append((cpm_x, ctr_x, cvr_x))
        if issue:
            campaign_truth.append({"campaign": camp, "platform": p, "channel": c, "issue": issue,
                                   "expected_reason": EXPECTED_REASON[issue],
                                   "weeks": list(span) if span else None})
    w = np.column_stack(weights)
    w = w / w.sum(axis=1, keepdims=True)
    part = np.round(ch.spend.to_numpy()[:, None] * w, 2)
    part[:, 0] += np.round(ch.spend.to_numpy() - part.sum(axis=1), 2)  # cents residual keeps channel totals exact
    for k, (p, camp, objective, cshare, issue, span) in enumerate(combos):
        _, _, cpm_m, ctr, cvr, vtr = plat_kpis[p]
        cpm_x, ctr_x, cvr_x = mults[k]
        impressions = part[:, k] / (CHANNELS[c]["cpm"] * cpm_m * cpm_x) * 1000 * prng.normal(1, 0.04, len(ch))
        clicks = impressions * ctr * ctr_x * prng.normal(1, 0.08, len(ch)).clip(0)
        conversions = clicks * cvr * cvr_x * prng.normal(1, 0.15, len(ch)).clip(0)
        campaign_rows.append(pd.DataFrame({
            "date": ch.date, "region": ch.region, "channel": c, "platform": p, "campaign": camp,
            "objective": objective, "spend": part[:, k],
            "impressions": impressions.round(0), "clicks": clicks.round(0), "conversions": conversions.round(0),
            "video_views": (impressions * vtr * prng.normal(1, 0.05, len(ch)).clip(0)).round(0),
            "attributed_revenue": (conversions * AOV * prng.normal(1, 0.10, len(ch)).clip(0)).round(2)}))
spend = pd.concat(campaign_rows).sort_values(["date", "region", "channel", "platform", "campaign"]).reset_index(drop=True)
healthy = [camp for p in CAMPAIGNS for camp, _, _, issue, _ in CAMPAIGNS[p]
           if issue is None and plat_kpis[p][3] > 0]  # click-based healthy campaigns, for false positives

sales = pd.concat(sales_rows)
holiday_names = {47: "Black Friday", 48: "Cyber Week", 51: "Christmas", 52: "Holiday Season"}
cal = []
for r in REGIONS:
    wk = weekly[weekly.region == r].set_index("week")
    promo_day = wk.promo.reindex(day_week).to_numpy()
    cal.append(pd.DataFrame({
        "date": days.date, "region": r,
        "holiday": [holiday_names.get(int(d.isocalendar().week), "") for d in days],
        "promo_flag": promo_day.astype(int),
        "competitor_index": pd.Series(competitor, index=weeks).reindex(day_week).round(2).to_numpy(),
    }))
calendar = pd.concat(cal)

os.makedirs(OUT, exist_ok=True)
spend.to_csv(f"{OUT}/media_spend.csv", index=False)
# Campaign master (reference data Silver validates against)
pd.DataFrame([{"campaign": camp, "platform": p, "channel": plat_kpis[p][0], "objective": obj}
              for p in CAMPAIGNS for camp, obj, *_ in CAMPAIGNS[p]]).to_csv(f"{OUT}/campaigns.csv", index=False)
sales.to_csv(f"{OUT}/sales.csv", index=False)
calendar.to_csv(f"{OUT}/calendar.csv", index=False)

total_spend = spend.groupby("channel").spend.sum()
truth = {
    "geo_holdouts": {f"{r}|{c}": [int(a), int(b)] for (r, c), (a, b) in GEO_HOLDOUTS.items()},
    "weeks": WEEKS, "start": str(START.date()), "regions": REGIONS, "seed": 20261108,
    "total_revenue": float(sales.revenue.sum()), "total_spend": float(spend.spend.sum()),
    "channels": {c: {"decay": p["decay"], "hill_shape": p["shape"], "half_saturation": p["half"],
                     "beta": beta[c], "target_roi": p["roi"],
                     "true_contribution_weekly_model": contrib[c],
                     "daily_file_spend": float(total_spend[c])} for c, p in CHANNELS.items()},
}
json.dump(truth, open(f"{OUT}/ground_truth.json", "w"), indent=2)
json.dump({"planted": campaign_truth, "healthy_click_campaigns": healthy},
          open(f"{OUT}/campaign_truth.json", "w"), indent=2)
print(f"media_spend {len(spend):,} rows, sales {len(sales):,} rows, calendar {len(calendar):,} rows")
print(f"revenue {sales.revenue.sum():,.0f}, spend {spend.spend.sum():,.0f}, "
      f"media share {sum(contrib.values()) / weekly.revenue.sum():.1%}")
for c in CHANNELS:
    print(f"  {c:12s} spend {total_spend[c]:>12,.0f}  true ROI {CHANNELS[c]['roi']}")

"""Campaign intelligence: trends, changepoints, peer benchmarks, segments, and evidence-backed recommendations.

Pure numpy/pandas/scipy/sklearn so it runs locally (tests) and inside the Fabric notebook (included verbatim).
Input: campaign-week frame with spend, impressions, clicks, conversions, attributed_revenue.
All evidence uses platform-reported KPIs (not incremental); cross-channel budget comes from the MMM optimizer.
"""
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

KPIS = ["ctr", "cvr", "cpm", "cpa"]
MIN_EFFECT = 0.20      # minimum start-to-end change for a trend to count
FDR_Q = 0.05           # Benjamini-Hochberg false discovery rate
MIN_SEGMENT = 10       # weeks either side of a changepoint
MIN_SHIFT = 0.30       # minimum mean shift for a changepoint to count
RECENT_WEEKS = 12      # horizon for budget recommendations


def add_kpis(df):
    d = df.copy()
    d["ctr"] = d.clicks / d.impressions.replace(0, np.nan)
    d["cvr"] = d.conversions / d.clicks.replace(0, np.nan)
    d["cpm"] = d.spend / d.impressions.replace(0, np.nan) * 1000
    d["cpa"] = d.spend / d.conversions.replace(0, np.nan)
    d["cpc"] = d.spend / d.clicks.replace(0, np.nan)
    d["platform_roas"] = d.attributed_revenue / d.spend.replace(0, np.nan)
    return d


def bh(pvals):
    """Benjamini-Hochberg adjusted q-values."""
    p = np.asarray(pvals, dtype=float)
    order = np.argsort(p)
    ranked = p[order] * len(p) / (np.arange(len(p)) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty_like(q)
    out[order] = np.minimum(q, 1)
    return out


def trends(weekly):
    """Theil-Sen slope and Kendall tau per campaign x KPI, with FDR control and a minimum effect size."""
    rows = []
    for camp, g in weekly.groupby("campaign"):
        g = g.sort_values("week")
        t = np.arange(len(g))
        for k in KPIS:
            y = g[k].to_numpy(dtype=float)
            ok = np.isfinite(y)
            if ok.sum() < 20:
                continue
            slope, intercept, _, _ = stats.theilslopes(y[ok], t[ok])
            tau, p = stats.kendalltau(t[ok], y[ok])
            start, end = intercept, intercept + slope * t[ok].max()
            rows.append({"campaign": camp, "kpi": k, "slope_per_week": slope, "tau": tau, "p_value": p,
                         "start_value": start, "end_value": end,
                         "relative_change": (end - start) / abs(start) if start else np.nan,
                         "first_week": g.week.iloc[0], "last_week": g.week.iloc[-1]})
    t = pd.DataFrame(rows)
    t["q_value"] = bh(t.p_value.fillna(1))
    t["significant"] = (t.q_value < FDR_Q) & (t.relative_change.abs() >= MIN_EFFECT)
    return t


def changepoint(y, weeks):
    """Single mean-shift changepoint by exhaustive split with a BIC penalty; None if not supported."""
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(y)
    y, weeks = y[ok], np.asarray(weeks)[ok]
    n = len(y)
    if n < 2 * MIN_SEGMENT:
        return None
    sse0 = ((y - y.mean()) ** 2).sum()
    best = None
    for i in range(MIN_SEGMENT, n - MIN_SEGMENT):
        a, b = y[:i], y[i:]
        sse = ((a - a.mean()) ** 2).sum() + ((b - b.mean()) ** 2).sum()
        if best is None or sse < best[0]:
            best = (sse, i)
    sse1, i = best
    bic0 = n * np.log(sse0 / n) + 1 * np.log(n)
    bic1 = n * np.log(sse1 / n) + 3 * np.log(n)
    before, after = y[:i].mean(), y[i:].mean()
    shift = (after - before) / abs(before) if before else 0
    # Step vs gradual: a true step leaves both segments flat
    flat = all(abs(stats.kendalltau(np.arange(len(s)), s)[0]) < 0.25 for s in (y[:i], y[i:]))
    if bic1 < bic0 - 10 and abs(shift) >= MIN_SHIFT and flat:
        return {"week": pd.Timestamp(weeks[i]), "before": before, "after": after, "shift": shift, "bic_gain": bic0 - bic1}
    return None


def campaign_profile(weekly):
    """Whole-period and recent KPIs per campaign, benchmarked against the channel's spend-weighted median."""
    agg = weekly.groupby(["campaign", "platform", "channel", "objective"])[
        ["spend", "impressions", "clicks", "conversions", "attributed_revenue"]].sum().reset_index()
    agg = add_kpis(agg)
    recent_cut = weekly.week.max() - pd.Timedelta(weeks=RECENT_WEEKS)
    rec = weekly[weekly.week > recent_cut].groupby("campaign")[["spend", "conversions", "clicks", "impressions"]].sum()
    agg["recent_spend"] = agg.campaign.map(rec.spend)
    agg["recent_cpa"] = agg.campaign.map(rec.spend / rec.conversions.replace(0, np.nan))
    agg["spend_share_in_channel"] = agg.spend / agg.groupby("channel").spend.transform("sum")
    agg["campaigns_in_channel"] = agg.groupby("channel").campaign.transform("count")
    for k in ["ctr", "cvr", "cpa", "cpm", "recent_cpa"]:
        med = agg.groupby("channel")[k].transform("median")
        agg[f"{k}_vs_channel"] = agg[k] / med
    # Spend vs CVR correlation over time: negative means conversion rate falls when budget rises
    corr = {}
    for camp, g in weekly.groupby("campaign"):
        g = g[np.isfinite(g.cvr)]
        corr[camp] = stats.spearmanr(g.spend, g.cvr) if len(g) > 20 else (np.nan, np.nan)
    agg["spend_cvr_rho"] = agg.campaign.map({k: v[0] for k, v in corr.items()})
    agg["spend_cvr_p"] = agg.campaign.map({k: v[1] for k, v in corr.items()})
    agg["click_based"] = agg.clicks > 0
    return agg


def segment(profile, trend_tbl):
    """KMeans on standardised efficiency and trend features for click-based campaigns; k by silhouette."""
    piv = trend_tbl.pivot(index="campaign", columns="kpi", values="relative_change")
    p = profile[profile.click_based].set_index("campaign")
    feats = pd.DataFrame({
        # Recent CPA, so a campaign that recovered (or broke) is grouped by how it performs now
        "log_cpa_vs_channel": np.log(p.recent_cpa_vs_channel),
        "log_ctr_vs_channel": np.log(p.ctr_vs_channel),
        "log_cvr_vs_channel": np.log(p.cvr_vs_channel),
        "ctr_trend": piv.reindex(p.index)["ctr"].clip(-1, 1),
        "cpa_trend": piv.reindex(p.index)["cpa"].clip(-1, 2),
    }).fillna(0)
    X = StandardScaler().fit_transform(feats)
    best = None
    for k in range(3, 7):
        lab = KMeans(n_clusters=k, n_init=20, random_state=42).fit_predict(X)
        sc = silhouette_score(X, lab)
        if best is None or sc > best[0]:
            best = (sc, k, lab)
    sil, k, lab = best
    feats["cluster"] = lab
    cent = feats.groupby("cluster").mean()
    # Deterministic names from centroids: best CPA, worst CPA, most deteriorating, rest steady
    names = {}
    names[cent.log_cpa_vs_channel.idxmin()] = "Efficient winners"
    rest = cent.drop(index=list(names))
    names[rest.log_cpa_vs_channel.idxmax()] = "Underperformers"
    rest = cent.drop(index=list(names))
    if len(rest):
        names[rest.cpa_trend.idxmax()] = "Deteriorating"
    for c in cent.index:
        names.setdefault(c, "Steady performers")
    feats["segment"] = feats.cluster.map(names)
    feats["silhouette"] = sil
    feats["k"] = k
    return feats.reset_index()


def diagnose(profile, trend_tbl, cps, segs):
    """Primary reason per campaign, with every supporting statistic written into the evidence text."""
    sig = trend_tbl[trend_tbl.significant].set_index(["campaign", "kpi"])
    seg = segs.set_index("campaign").segment.to_dict()
    out = []
    for _, r in profile.iterrows():
        c = r.campaign
        if not r.click_based:
            out.append({"campaign": c, "reason": "NOT_CLICK_TRACKED", "action": "Hold",
                        "evidence": f"{r.platform} has no click or conversion tracking; judged on MMM channel ROI only "
                                    f"(CPM {r.cpm:,.2f}).", "confidence": "n/a"})
            continue

        def tr(k):
            return sig.loc[(c, k)] if (c, k) in sig.index else None

        def fmt_trend(k, t):
            return (f"{k.upper()} {t.relative_change:+.0%} from {t.start_value:,.4g} to {t.end_value:,.4g} "
                    f"({t.first_week:%Y-%m-%d} to {t.last_week:%Y-%m-%d}, Theil-Sen slope {t.slope_per_week:+.3g}/wk, "
                    f"q={t.q_value:.3g})")
        cp_cvr, cp_ctr = cps.get((c, "cvr")), cps.get((c, "ctr"))
        reason, action, ev, conf = "HEALTHY", "Hold", [], "medium"
        if cp_cvr and cp_cvr["shift"] < 0:
            reason, action, conf = "CVR_DROP_CHANGEPOINT", "Investigate", "high"
            ev.append(f"CVR stepped from {cp_cvr['before']:.2%} to {cp_cvr['after']:.2%} ({cp_cvr['shift']:+.0%}) in the week of "
                      f"{cp_cvr['week']:%Y-%m-%d} and stayed flat on both sides (BIC gain {cp_cvr['bic_gain']:.0f}); "
                      f"a sudden step points to a landing page, tracking or offer change, not gradual fatigue")
        elif (cp_ctr and cp_ctr["shift"] > 0) or (cp_cvr and cp_cvr["shift"] > 0):
            cp = cp_ctr if cp_ctr and cp_ctr["shift"] > 0 else cp_cvr
            kk = "CTR" if cp is cp_ctr else "CVR"
            reason, action, conf = "IMPROVED_AFTER_CHANGE", "Scale", "high"
            ev.append(f"{kk} stepped up from {cp['before']:.3%} to {cp['after']:.3%} ({cp['shift']:+.0%}) in the week of "
                      f"{cp['week']:%Y-%m-%d}; post-change CPA {r.recent_cpa:,.2f} vs channel median "
                      f"{r.cpa / r.cpa_vs_channel:,.2f}")
        elif tr("ctr") is not None and tr("ctr").relative_change < 0:
            reason, action, conf = "CTR_DECLINING", "Refresh creative", "high"
            ev.append(fmt_trend("ctr", tr("ctr")))
            ev.append("CVR has no significant trend, so people who click still convert: the ad is wearing out, "
                      "not the offer" if tr("cvr") is None else fmt_trend("cvr", tr("cvr")))
        elif tr("cpm") is not None and tr("cpm").relative_change > 0:
            reason, action, conf = "COST_INFLATION", "Reduce", "high"
            ev.append(fmt_trend("cpm", tr("cpm")))
            if tr("cpa") is not None:
                ev.append(fmt_trend("cpa", tr("cpa")))
            ev.append("CTR and CVR are stable, so the same results cost more: review bids, keywords or audiences")
        elif tr("cvr") is not None and tr("cvr").relative_change < 0 and r.spend_cvr_rho < -0.3 and r.spend_cvr_p < 0.01:
            reason, action, conf = "DIMINISHING_RETURNS", "Reduce", "high"
            ev.append(fmt_trend("cvr", tr("cvr")))
            ev.append(f"weekly spend and CVR move in opposite directions (Spearman rho {r.spend_cvr_rho:.2f}, "
                      f"p={r.spend_cvr_p:.1g}): more budget is reaching the same audience; cap frequency or shift budget")
        elif r.ctr_vs_channel < 0.6 and r.cvr_vs_channel < 0.7:
            reason, action, conf = "LOW_QUALITY_TRAFFIC", "Reduce", "high"
            ev.append(f"CTR {r.ctr:.3%} is {r.ctr_vs_channel:.0%} and CVR {r.cvr:.2%} is {r.cvr_vs_channel:.0%} of the "
                      f"{r.channel} median for the whole period, with no trend: placements or terms attract low-intent traffic; "
                      f"CPA {r.cpa:,.2f} vs channel median {r.cpa / r.cpa_vs_channel:,.2f}")
        elif r.cpa_vs_channel < 0.7 and r.spend_share_in_channel < 1 / r.campaigns_in_channel:
            reason, action, conf = "UNDERFUNDED_WINNER", "Scale", "high"
            ev.append(f"CPA {r.cpa:,.2f} is {r.cpa_vs_channel:.0%} of the {r.channel} median and platform ROAS "
                      f"{r.platform_roas:.2f}, yet it gets only {r.spend_share_in_channel:.0%} of {r.channel} spend "
                      f"(an even split would be {1 / r.campaigns_in_channel:.0%})")
        else:
            ev.append(f"No significant trend or changepoint; CPA {r.cpa:,.2f} is {r.cpa_vs_channel:.0%} of the "
                      f"{r.channel} median")
        out.append({"campaign": c, "reason": reason, "action": action, "evidence": "; ".join(ev) + ".",
                    "confidence": conf, "segment": seg.get(c)})
    return pd.DataFrame(out)


ACTION_MULT = {"Scale": 1.30, "Hold": 1.00, "Investigate": 0.85, "Refresh creative": 0.85, "Reduce": 0.75}


def recommend(profile, diag, channel_ratio):
    """Next-12-week budgets: campaign actions inside each channel, rescaled so every channel lands on the
    MMM optimizer's same-budget ratio (cross-channel decision from the MMM, within-channel from KPIs)."""
    rec = profile[["campaign", "platform", "channel", "objective", "recent_spend", "cpa", "recent_cpa",
                   "platform_roas", "ctr", "cvr", "cpm", "cpc"]].merge(diag, on="campaign")
    rec["weight"] = rec.recent_spend * rec.action.map(ACTION_MULT)
    target = rec.groupby("channel").recent_spend.transform("sum") * rec.channel.map(channel_ratio).fillna(1.0)
    rec["recommended_spend"] = rec.weight / rec.groupby("channel").weight.transform("sum") * target
    rec["budget_change"] = rec.recommended_spend - rec.recent_spend
    rec["budget_change_pct"] = rec.budget_change / rec.recent_spend
    # Within-channel view (channel budget held fixed): the sign always matches the campaign action
    within = rec.weight / rec.groupby("channel").weight.transform("sum") * rec.groupby("channel").recent_spend.transform("sum")
    rec["within_channel_change_pct"] = within / rec.recent_spend - 1
    rec["channel_ratio"] = rec.channel.map(channel_ratio).fillna(1.0)
    return rec


def evaluate(diag, truth):
    """Score diagnoses against planted issues (used only after the run, never inside recommendations)."""
    d = diag.set_index("campaign")
    rows = []
    for t in truth["planted"]:
        got = d.reason.get(t["campaign"], "MISSING")
        rows.append({"campaign": t["campaign"], "planted_issue": t["issue"], "expected_reason": t["expected_reason"],
                     "detected_reason": got, "correct": got == t["expected_reason"]})
    for c in truth["healthy_click_campaigns"]:
        got = d.reason.get(c, "MISSING")
        rows.append({"campaign": c, "planted_issue": "none", "expected_reason": "HEALTHY", "detected_reason": got,
                     "correct": got == "HEALTHY"})
    return pd.DataFrame(rows)


def run(daily, channel_ratio):
    """daily: date-level campaign rows (date, channel, platform, campaign, objective, spend, ...)."""
    d = daily.copy()
    d["week"] = pd.to_datetime(d.date) - pd.to_timedelta(pd.to_datetime(d.date).dt.day_of_week, unit="D")
    weekly = d.groupby(["week", "campaign", "platform", "channel", "objective"])[
        ["spend", "impressions", "clicks", "conversions", "attributed_revenue"]].sum().reset_index()
    weekly = add_kpis(weekly)
    weekly = weekly[weekly.spend > 0]
    profile = campaign_profile(weekly)
    clicky = weekly[weekly.campaign.isin(profile[profile.click_based].campaign)]
    trend_tbl = trends(clicky)
    cps = {}
    for camp, g in clicky.groupby("campaign"):
        g = g.sort_values("week")
        for k in ["ctr", "cvr"]:
            cp = changepoint(g[k], g.week)
            if cp:
                cps[(camp, k)] = cp
    segs = segment(profile, trend_tbl)
    diag = diagnose(profile, trend_tbl, cps, segs)
    rec = recommend(profile, diag, channel_ratio)
    cp_tbl = pd.DataFrame([{"campaign": c, "kpi": k, **v} for (c, k), v in cps.items()])
    return {"weekly": weekly, "profile": profile, "trends": trend_tbl, "changepoints": cp_tbl,
            "segments": segs, "recommendations": rec}

"""Marketing mix model core: geometric adstock + Hill saturation + bounded linear regression.

Pure numpy/pandas/scipy so it runs locally (tests) and inside the Fabric notebook (included verbatim).
Input: region-week frame with revenue, spend_<channel> columns and controls.
"""
import itertools

import numpy as np
import pandas as pd
from scipy.optimize import lsq_linear

DECAYS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
SHAPES = [1.0, 1.5, 2.0, 2.5]
HALVES = [0.8, 1.0, 1.2, 1.5]
CONTROLS = ["trend", "sin1", "cos1", "sin2", "cos2", "trend_sin1", "holiday", "promo", "competitor", "price_index"]


def adstock(x, decay):
    out = np.empty_like(x, dtype=float)
    carry = 0.0
    for i, v in enumerate(x):
        carry = v + decay * carry
        out[i] = carry
    return out


def hill(x, shape, half):
    return x ** shape / (x ** shape + half ** shape)


class MMM:
    def __init__(self, weekly, channels, holdout_weeks=12):
        self.df = weekly.sort_values(["region", "week"]).reset_index(drop=True)
        self.channels = channels
        self.regions = sorted(self.df.region.unique())
        weeks = sorted(self.df.week.unique())
        self.train_weeks, self.test_weeks = weeks[:-holdout_weeks], weeks[-holdout_weeks:]
        # Region size = share of average weekly revenue; the model works on revenue per unit of size
        mean_rev = self.df.groupby("region").revenue.mean()
        self.size = (mean_rev / mean_rev.mean()).to_dict()
        self.df["size"] = self.df.region.map(self.size)
        t = (self.df.week - self.df.week.min()).dt.days / 7
        woy = self.df.week.dt.isocalendar().week.astype(int)
        self.df["trend"] = t / t.max()
        self.df["sin1"], self.df["cos1"] = np.sin(2 * np.pi * woy / 52), np.cos(2 * np.pi * woy / 52)
        self.df["sin2"], self.df["cos2"] = np.sin(4 * np.pi * woy / 52), np.cos(4 * np.pi * woy / 52)
        self.df["trend_sin1"] = self.df.trend * self.df.sin1
        self.df["competitor"] = self.df.competitor_index - 100
        self.df["price_index"] = self.df.price_index - 1
        self.params = {c: (0.3, 1.5, 1.0) for c in channels}
        self.train = self.df.week.isin(self.train_weeks).to_numpy()

    def media_feature(self, c, decay, shape, half, spend_scale=1.0):
        """Hill(adstock(spend)/region mean adstock) per region-week; spend_scale moves spend for what-if."""
        out = np.empty(len(self.df))
        for r in self.regions:
            m = (self.df.region == r).to_numpy()
            base_ad = adstock(self.df.loc[m, f"spend_{c}"].to_numpy(), decay)
            ad = adstock(self.df.loc[m, f"spend_{c}"].to_numpy() * spend_scale, decay)
            out[m] = hill(ad / base_ad.mean(), shape, half)
        return out

    def design(self, params, scales=None):
        scales = scales or {}
        media = np.column_stack([self.media_feature(c, *params[c], scales.get(c, 1.0)) for c in self.channels])
        ctrl = self.df[CONTROLS].to_numpy(dtype=float)
        return np.column_stack([np.ones(len(self.df)), ctrl, media])

    def _fit(self, params, mask):
        X = self.design(params)
        y = (self.df.revenue / self.df["size"]).to_numpy()
        lo = np.r_[np.full(1 + len(CONTROLS), -np.inf), np.zeros(len(self.channels))]
        res = lsq_linear(X[mask], y[mask], bounds=(lo, np.inf), lsmr_tol="auto")
        sse = float(((X[mask] @ res.x - y[mask]) ** 2).sum())
        return res.x, sse

    def fit(self, passes=3):
        """Coordinate search over each channel's (decay, shape, half), minimising training error."""
        for _ in range(passes):
            for c in self.channels:
                best = None
                for combo in itertools.product(DECAYS, SHAPES, HALVES):
                    trial = {**self.params, c: combo}
                    _, sse = self._fit(trial, self.train)
                    if best is None or sse < best[0]:
                        best = (sse, combo)
                self.params[c] = best[1]
        self.coef, _ = self._fit(self.params, self.train)
        return self

    def predict_parts(self, scales=None):
        """Per region-week: baseline, each channel's contribution, predicted revenue (all in revenue units)."""
        X = self.design(self.params, scales)
        size = self.df["size"].to_numpy()
        nb = 1 + len(CONTROLS)
        parts = pd.DataFrame({"week": self.df.week, "region": self.df.region, "actual": self.df.revenue})
        parts["baseline"] = X[:, :nb] @ self.coef[:nb] * size
        for i, c in enumerate(self.channels):
            parts[c] = X[:, nb + i] * self.coef[nb + i] * size
        parts["predicted"] = parts.baseline + parts[self.channels].sum(axis=1)
        parts["is_holdout"] = ~self.train
        return parts

    def metrics(self):
        p = self.predict_parts()
        nat = p.groupby(["week", "is_holdout"])[["actual", "predicted"]].sum().reset_index()
        def mape(d): return float((abs(d.actual - d.predicted) / d.actual).mean())
        tr, te = nat[~nat.is_holdout], nat[nat.is_holdout]
        ss_res = ((tr.actual - tr.predicted) ** 2).sum()
        ss_tot = ((tr.actual - tr.actual.mean()) ** 2).sum()
        return {"train_mape": mape(tr), "holdout_mape": mape(te), "train_r2": float(1 - ss_res / ss_tot)}

    def roi(self):
        p = self.predict_parts()
        spend = {c: self.df[f"spend_{c}"].sum() for c in self.channels}
        rows = []
        for c in self.channels:
            contrib = p[c].sum()
            up = self.predict_parts({c: 1.01})[c].sum()
            rows.append({"channel": c, "spend": spend[c], "contribution": contrib, "roi": contrib / spend[c],
                         "marginal_roi": (up - contrib) / (0.01 * spend[c]),
                         "decay": self.params[c][0], "hill_shape": self.params[c][1],
                         "half_saturation": self.params[c][2]})
        return pd.DataFrame(rows)

    def response_curves(self, multipliers=np.round(np.arange(0, 2.05, 0.05), 2)):
        rows = []
        for c in self.channels:
            spend = self.df[f"spend_{c}"].sum()
            for m in multipliers:
                contrib = self.predict_parts({c: m})[c].sum() if m > 0 else 0.0
                rows.append({"channel": c, "spend_multiplier": float(m), "spend": spend * m,
                             "contribution": contrib, "is_current": bool(abs(m - 1) < 1e-9)})
        return pd.DataFrame(rows)

    def optimise(self, budget_changes=(-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3), step=0.02, lo=0.5, hi=2.0):
        """Greedy reallocation on response curves: each step buys the next slice with the best marginal return."""
        curves = self.response_curves(np.round(np.arange(0, 2.0001, step / 2), 3))
        spend0 = {c: self.df[f"spend_{c}"].sum() for c in self.channels}
        total0 = sum(spend0.values())

        def contrib(c, m):
            cc = curves[curves.channel == c]
            return float(np.interp(m, cc.spend_multiplier, cc.contribution))

        rows = []
        for change in budget_changes:
            budget = total0 * (1 + change)
            mult = {c: lo for c in self.channels}
            remaining = budget - sum(spend0[c] * lo for c in self.channels)
            slice_ = total0 * 0.005
            while remaining > 1e-6:
                best, best_gain = None, -1
                for c in self.channels:
                    if mult[c] >= hi:
                        continue
                    dm = min(slice_, remaining) / spend0[c]
                    gain = contrib(c, min(hi, mult[c] + dm)) - contrib(c, mult[c])
                    if gain > best_gain:
                        best, best_gain = c, gain
                if best is None:
                    break
                dm = min(slice_, remaining) / spend0[best]
                new = min(hi, mult[best] + dm)
                remaining -= (new - mult[best]) * spend0[best]
                mult[best] = new
            for c in self.channels:
                rows.append({"scenario": f"{change:+.0%}", "budget_change": change, "channel": c,
                             "current_spend": spend0[c], "optimal_spend": spend0[c] * mult[c],
                             "current_contribution": contrib(c, 1.0), "optimal_contribution": contrib(c, mult[c])})
        return pd.DataFrame(rows)

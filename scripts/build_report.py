"""Create/update the PBIR report 'Marketing Mix Insights' on sm_marketing_mix via Fabric REST.

Usage: build_report.py [--test]   (--test publishes one page with every visual type, for render checks)
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fabric_nb as f  # noqa: E402

NAME = "Marketing Mix Insights"
SM_NAME = "sm_marketing_mix"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
INDIGO, INK, CANVAS, MUTED = "#1E1B4B", "#1F2937", "#F5F3FF", "#C7D2FE"
CHANNEL_COLORS = {"TV": "#7B2FF7", "OOH": "#F72585", "Paid Search": "#00B4D8", "Social": "#06D6A0",
                  "Display": "#FFB703", "Email": "#FB5607"}
COMPONENT_COLORS = {"Baseline": "#94A3B8", **CHANNEL_COLORS}
REGION_COLORS = {"North": "#4361EE", "South": "#F72585", "East": "#06D6A0", "West": "#FFB703", "Central": "#7B2FF7"}
KPI_COLORS = ["#4361EE", "#7B2FF7", "#06D6A0", "#F72585", "#FB5607"]
THEME = {
    "name": "Vibrant Marketing",
    "dataColors": ["#4361EE", "#F72585", "#06D6A0", "#FFB703", "#7B2FF7", "#00B4D8", "#FB5607", "#94A3B8"],
    "background": "#FFFFFF", "foreground": INK, "tableAccent": "#7B2FF7",
    "good": "#06D6A0", "neutral": "#FFB703", "bad": "#F72585",
    "textClasses": {
        "callout": {"fontSize": 26, "fontFace": "Segoe UI Semibold", "color": INDIGO},
        "title": {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": INK},
        "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#6B7280"},
    },
    "visualStyles": {
        "*": {"*": {
            "background": [{"show": True, "color": {"solid": {"color": "#FFFFFF"}}, "transparency": 0}],
            "border": [{"show": True, "color": {"solid": {"color": "#E0E7FF"}}, "radius": 12}],
            "dropShadow": [{"show": True, "preset": "BottomRight", "transparency": 85}],
            "title": [{"show": True, "fontColor": {"solid": {"color": INK}}, "fontSize": 12}],
            "padding": [{"top": 10, "bottom": 10, "left": 12, "right": 12}],
        }},
        "page": {"*": {"background": [{"color": {"solid": {"color": CANVAS}}, "transparency": 0}]}},
    },
}


def lit(v):
    if isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (int, float)):
        s = f"{v}D"
    else:
        s = f"'{v}'"
    return {"expr": {"Literal": {"Value": s}}}


def color(c):
    return {"solid": {"color": lit(c)}}


def C(entity, prop):
    return ("Column", entity, prop)


def M(entity, prop):
    return ("Measure", entity, prop)


def field(ref):
    kind, entity, prop = ref[:3]
    return {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def proj(ref):
    p = {"field": field(ref[:3]), "queryRef": f"{ref[1]}.{ref[2]}", "nativeQueryRef": ref[2]}
    if len(ref) > 3:
        p["displayName"] = ref[3]  # friendly column header
    return p


def named(ref, label):
    return (*ref, label)


def series_colors(column, mapping):
    """Fix one colour per category value (channel, region, component) so it matches on every page."""
    return [{"properties": {"fill": color(hexc)},
             "selector": {"data": [{"scopeId": {"Comparison": {
                 "ComparisonKind": 0, "Left": field(column), "Right": {"Literal": {"Value": f"'{val}'"}}}}}]}}
            for val, hexc in mapping.items()]


def measure_colors(mapping):
    """Fix the colour of each measure series, e.g. Actual vs Predicted."""
    return [{"properties": {"fill": color(hexc)}, "selector": {"metadata": f"{m[1]}.{m[2]}"}}
            for m, hexc in mapping]


def heatmap(measure, lo="#EDE9FE", hi="#7B2FF7"):
    return [{"properties": {"backColor": {"solid": {"color": {"expr": {"FillRule": {
        "Input": field(measure),
        "FillRule": {"linearGradient2": {"min": {"color": {"Literal": {"Value": f"'{lo}'"}}},
                                         "max": {"color": {"Literal": {"Value": f"'{hi}'"}}},
                                         "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}}}}}}}}}},
             "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                          "metadata": f"{measure[1]}.{measure[2]}"}}]


class Page:
    def __init__(self, name, title, subtitle):
        self.name, self.title, self.visuals, self.n = name, title, [], 0
        self.add(0, 0, 1280, 64, {
            "visualType": "textbox",
            "objects": {"general": [{"properties": {"paragraphs": [
                {"textRuns": [{"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "17pt", "color": "#FFFFFF"}}]},
                {"textRuns": [{"value": subtitle, "textStyle": {"fontSize": "9pt", "color": MUTED}}]}]}}]},
            "visualContainerObjects": {"background": [{"properties": {"show": lit(True), "color": color(INDIGO),
                                                                      "transparency": lit(0)}}],
                                       "padding": [{"properties": {"top": lit(2), "bottom": lit(0), "left": lit(20)}}]},
        }, chrome=False)
        # Accent stripe under the header in the channel palette
        for i, hexc in enumerate(CHANNEL_COLORS.values()):
            self.add(i * 1280 / 6, 64, 1280 / 6, 4, {"visualType": "shape", "objects": {
                "fill": [{"properties": {"fillColor": color(hexc), "transparency": lit(0)}}],
                "line": [{"properties": {"show": lit(False)}}]}}, chrome=False)

    def add(self, x, y, w, h, visual, title=None, chrome=True):
        self.n += 1
        vco = dict(visual.get("visualContainerObjects", {}))
        if title:
            vco["title"] = [{"properties": {"show": lit(True), "text": lit(title)}}]
        if not chrome:
            vco["border"] = [{"properties": {"show": lit(False)}}]
            vco["dropShadow"] = [{"properties": {"show": lit(False)}}]
            vco.setdefault("background", [{"properties": {"show": lit(False)}}])
        if vco:
            visual["visualContainerObjects"] = vco
        self.visuals.append({
            "$schema": f"{SCHEMA}/visualContainer/2.0.0/schema.json",
            "name": f"{self.name}_v{self.n:02d}",
            "position": {"x": x, "y": y, "z": self.n * 1000, "width": w, "height": h, "tabOrder": self.n * 1000},
            "visual": visual,
        })

    def chart(self, x, y, w, h, vtype, title, roles, sort=None, objects=None):
        q = {"queryState": {role: {"projections": [proj(r) for r in refs]} for role, refs in roles.items()}}
        if sort:
            q["sortDefinition"] = {"sort": [{"field": field(sort[0]), "direction": sort[1]}]}
        v = {"visualType": vtype, "query": q, "drillFilterOtherVisuals": True}
        if objects:
            v["objects"] = objects
        self.add(x, y, w, h, v, title)

    def card(self, x, y, w, h, measure, accent, units=0):
        self.chart(x, y, w, h, "card", None, {"Values": [measure]}, objects={
            "labels": [{"properties": {"color": color(accent), "fontSize": lit(24), "labelDisplayUnits": lit(units)}}],
            "categoryLabels": [{"properties": {"show": lit(True), "color": color("#6B7280"), "fontSize": lit(10)}}],
        })
        self.visuals[-1]["visual"]["visualContainerObjects"] = {
            "title": [{"properties": {"show": lit(False)}}],
            "border": [{"properties": {"show": lit(True), "color": color(accent), "radius": lit(12)}}],
        }

    def slicer(self, x, y, w, h, column, title, mode="Dropdown"):
        self.chart(x, y, w, h, "slicer", title, {"Values": [column]}, objects={
            "data": [{"properties": {"mode": lit(mode)}}],
            "header": [{"properties": {"show": lit(False)}}],
        })
        # Hide the blank member Direct Lake adds to dimension slicers
        self.visuals[-1]["filterConfig"] = {"filters": [{
            "name": f"{self.name}_nb{self.n}", "field": field(column), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "d", "Entity": column[1], "Type": 0}],
                       "Where": [{"Condition": {"Not": {"Expression": {"Comparison": {
                           "ComparisonKind": 0,
                           "Left": {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": column[2]}},
                           "Right": {"Literal": {"Value": "null"}}}}}}}]}}]}


WEEK, REGION, CHANNEL = C("Week", "week_start"), C("Region", "region"), C("Channel", "channel")
COMPONENT = C("Component", "component")


PLATFORM, CAMPAIGN = C("Platform", "platform"), C("Campaign", "campaign")
SEGMENT_COLORS = {"Efficient winners": "#06D6A0", "Steady performers": "#4361EE", "Deteriorating": "#FFB703",
                  "Underperformers": "#F72585", "Not click-tracked": "#CBD5E1"}


def in_filter(name, column, values):
    """Visual-level categorical filter (column IN values)."""
    return {"name": name, "field": field(column), "type": "Categorical",
            "filter": {"Version": 2, "From": [{"Name": "f", "Entity": column[1], "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": column[2]}}],
                           "Values": [[{"Literal": {"Value": f"'{v}'"}}] for v in values]}}}]}}


def legend(pos="Top"):
    return [{"properties": {"show": lit(True), "position": lit(pos)}}]


def overview():
    p = Page("overview", "Marketing Mix Insights", "What media drives revenue, what it returns, and where the next dollar should go")
    for i, (m, u) in enumerate([(M("Sales", "Total Revenue"), 1e6), (M("Media", "Media Spend"), 1e6),
                                (M("Media", "Media Contribution"), 1e6), (M("Media", "ROAS"), 0),
                                (M("Weekly Trend", "Revenue YoY %"), 0)]):
        p.card(16 + i * 252, 80, 240, 92, m, KPI_COLORS[i], u)
    for i, (c, t) in enumerate([(C("Week", "year"), "Year"), (REGION, "Region"), (CHANNEL, "Channel")]):
        p.slicer(16 + i * 422, 180, 410, 72, c, t)
    p.chart(16, 260, 828, 240, "lineClusteredColumnComboChart", "Weekly revenue vs media spend",
            {"Category": [WEEK], "Y": [M("Media", "Media Spend")], "Y2": [M("Sales", "Total Revenue")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Media", "Media Spend"), "#C7D2FE"),
                                                                      (M("Sales", "Total Revenue"), "#7B2FF7")])})
    p.chart(856, 260, 408, 240, "donutChart", "Media spend mix",
            {"Category": [CHANNEL], "Y": [M("Media", "Media Spend")]},
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS), "legend": legend("Right")})
    p.chart(16, 512, 620, 196, "stackedAreaChart", "Revenue trend by region (4-week average)",
            {"Category": [WEEK], "Series": [REGION], "Y": [M("Weekly Trend", "Revenue 4wk Avg")]},
            objects={"dataPoint": series_colors(REGION, REGION_COLORS), "legend": legend()})
    p.chart(648, 512, 616, 196, "clusteredBarChart", "Modelled ROAS by channel",
            {"Category": [CHANNEL], "Y": [M("Media", "ROAS")]}, sort=(M("Media", "ROAS"), "Descending"),
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS), "labels": [{"properties": {"show": lit(True)}}]})
    return p


def decomposition():
    p = Page("decomp", "Sales Decomposition", "Weekly revenue split into baseline demand and each channel's contribution")
    for i, (m, u) in enumerate([(M("Decomposition", "Total Contribution"), 1e6), (M("Decomposition", "Media Driven %"), 0),
                                (M("Media", "Media Contribution"), 1e6), (M("Sales", "Total Revenue"), 1e6)]):
        p.card(16 + i * 316, 80, 304, 92, m, KPI_COLORS[i], u)
    p.chart(16, 184, 1248, 280, "stackedAreaChart", "Revenue decomposition over time",
            {"Category": [WEEK], "Series": [COMPONENT], "Y": [M("Decomposition", "Total Contribution")]},
            objects={"dataPoint": series_colors(COMPONENT, COMPONENT_COLORS), "legend": legend()})
    p.chart(16, 476, 620, 232, "waterfallChart", "How revenue builds up: baseline plus each channel",
            {"Category": [COMPONENT], "Y": [M("Decomposition", "Total Contribution")]},
            objects={"sentimentColors": [{"properties": {"increaseFill": color("#06D6A0"), "totalFill": color("#7B2FF7")}}]})
    p.chart(648, 476, 616, 232, "hundredPercentStackedColumnChart", "Contribution mix by year",
            {"Category": [C("Week", "year")], "Series": [COMPONENT], "Y": [M("Decomposition", "Total Contribution")]},
            objects={"dataPoint": series_colors(COMPONENT, COMPONENT_COLORS), "legend": legend()})
    return p


def roi_page():
    p = Page("roi", "Channel ROI", "Return on every dollar by channel, and where money is over or under invested")
    p.chart(16, 80, 620, 300, "clusteredBarChart", "Modelled ROI vs marginal ROI (next dollar)",
            {"Category": [CHANNEL], "Y": [M("Channel ROI", "Modelled ROI"), M("Channel ROI", "Marginal ROI")]},
            sort=(M("Channel ROI", "Modelled ROI"), "Descending"),
            objects={"legend": legend(), "labels": [{"properties": {"show": lit(True)}}],
                     "dataPoint": measure_colors([(M("Channel ROI", "Modelled ROI"), "#7B2FF7"),
                                                  (M("Channel ROI", "Marginal ROI"), "#06D6A0")])})
    p.chart(648, 80, 616, 300, "scatterChart", "Spend vs contribution (bubble = ROI)",
            {"Category": [CHANNEL], "X": [M("Channel ROI", "ROI Spend")], "Y": [M("Channel ROI", "ROI Contribution")],
             "Size": [M("Channel ROI", "Modelled ROI")]},
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS), "categoryLabels": [{"properties": {"show": lit(True)}}]})
    p.chart(16, 392, 620, 316, "clusteredColumnChart", "Share of spend vs share of contribution",
            {"Category": [CHANNEL], "Y": [M("Channel ROI", "Spend Share"), M("Channel ROI", "Contribution Share")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Channel ROI", "Spend Share"), "#C7D2FE"),
                                                                      (M("Channel ROI", "Contribution Share"), "#F72585")])})
    p.chart(648, 392, 616, 316, "treemap", "Contribution by channel",
            {"Group": [CHANNEL], "Values": [M("Channel ROI", "ROI Contribution")]},
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS)})
    return p


def curves():
    p = Page("curves", "Response Curves", "Diminishing returns: what each channel delivers as spend moves from 0x to 2x today")
    p.chart(16, 80, 1248, 400, "lineChart", "Contribution by spend level (1.0 = current spend)",
            {"Category": [C("Response Curves", "spend_multiplier")], "Series": [CHANNEL],
             "Y": [M("Response Curves", "Curve Contribution")]},
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS), "legend": legend(),
                     "categoryAxis": [{"properties": {"axisType": lit("Scalar")}}]})
    p.chart(16, 492, 1248, 216, "tableEx", "Saturation and carryover by channel",
            {"Values": [CHANNEL, C("Channel ROI", "decay"), C("Channel ROI", "hill_shape"),
                        C("Channel ROI", "half_saturation"), M("Channel ROI", "Modelled ROI"),
                        M("Channel ROI", "Marginal ROI"), M("Channel ROI", "True ROI")]})
    return p


def optimizer():
    p = Page("optimizer", "Budget Optimizer", "Pick a total budget: the model reallocates it across channels for maximum revenue")
    p.slicer(16, 80, 1248, 64, C("Budget Scenarios", "scenario"), "Total budget vs today", mode="Basic")
    p.visuals[-1]["visual"]["objects"]["general"] = [{"properties": {"orientation": lit(1)}}]
    p.visuals[-1]["visual"]["objects"]["selection"] = [{"properties": {"singleSelect": lit(True)}}]
    for i, (m, u) in enumerate([(M("Budget Scenarios", "Optimal Spend"), 1e6),
                                (M("Budget Scenarios", "Optimal Contribution"), 1e6),
                                (M("Budget Scenarios", "Contribution Uplift"), 1e6),
                                (M("Budget Scenarios", "Contribution Uplift %"), 0),
                                (M("Budget Scenarios", "Optimal ROI"), 0)]):
        p.card(16 + i * 252, 156, 240, 92, m, KPI_COLORS[i], u)
    p.chart(16, 260, 620, 448, "clusteredBarChart", "Current vs optimal spend by channel",
            {"Category": [CHANNEL], "Y": [M("Budget Scenarios", "Current Spend"), M("Budget Scenarios", "Optimal Spend")]},
            objects={"legend": legend(), "labels": [{"properties": {"show": lit(True), "labelDisplayUnits": lit(1e6)}}],
                     "dataPoint": measure_colors([(M("Budget Scenarios", "Current Spend"), "#C7D2FE"),
                                                  (M("Budget Scenarios", "Optimal Spend"), "#7B2FF7")])})
    p.chart(648, 260, 616, 448, "clusteredColumnChart", "Expected media contribution: current plan vs optimised",
            {"Category": [CHANNEL], "Y": [M("Budget Scenarios", "Current Contribution"),
                                          M("Budget Scenarios", "Optimal Contribution")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Budget Scenarios", "Current Contribution"), "#FFD6E8"),
                                                                      (M("Budget Scenarios", "Optimal Contribution"), "#F72585")])})
    return p


def regional():
    p = Page("regional", "Regional Trends", "Revenue momentum, year-over-year growth and media efficiency by region")
    p.slicer(16, 80, 1248, 56, C("Week", "year"), "Year", mode="Basic")
    p.visuals[-1]["visual"]["objects"]["general"] = [{"properties": {"orientation": lit(1)}}]
    p.chart(16, 148, 828, 270, "lineChart", "Weekly revenue, 4-week average",
            {"Category": [WEEK], "Series": [REGION], "Y": [M("Weekly Trend", "Revenue 4wk Avg")]},
            objects={"dataPoint": series_colors(REGION, REGION_COLORS), "legend": legend()})
    p.chart(856, 148, 408, 270, "clusteredBarChart", "Revenue YoY % by region",
            {"Category": [REGION], "Y": [M("Weekly Trend", "Revenue YoY %")]}, sort=(M("Weekly Trend", "Revenue YoY %"), "Descending"),
            objects={"dataPoint": series_colors(REGION, REGION_COLORS), "labels": [{"properties": {"show": lit(True)}}]})
    p.chart(16, 430, 828, 278, "pivotTable", "ROAS heatmap: region x channel",
            {"Rows": [REGION], "Columns": [CHANNEL], "Values": [M("Media", "ROAS")]},
            objects={"values": heatmap(M("Media", "ROAS"))})
    p.chart(856, 430, 408, 278, "donutChart", "Revenue by region",
            {"Category": [REGION], "Y": [M("Sales", "Total Revenue")]},
            objects={"dataPoint": series_colors(REGION, REGION_COLORS), "legend": legend("Right")})
    return p


def model_fit():
    p = Page("fit", "Model Fit and Accuracy", "How well the model explains revenue, tested on 12 weeks it never saw")
    for i, (m, u) in enumerate([(M("Model Metrics", "Holdout MAPE"), 0), (M("Model Metrics", "Model R2"), 0),
                                (M("Model Fit", "Fit Error %"), 0), (M("Model Metrics", "Max ROI Error %"), 0)]):
        p.card(16 + i * 316, 80, 304, 92, m, KPI_COLORS[i], u)
    p.chart(16, 184, 1248, 280, "lineChart", "Actual vs predicted weekly revenue",
            {"Category": [WEEK], "Y": [M("Model Fit", "Actual Revenue"), M("Model Fit", "Predicted Revenue")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Model Fit", "Actual Revenue"), "#1E1B4B"),
                                                                      (M("Model Fit", "Predicted Revenue"), "#F72585")])})
    p.chart(16, 476, 620, 232, "clusteredColumnChart", "Recovered ROI vs true ROI (synthetic ground truth)",
            {"Category": [CHANNEL], "Y": [M("Channel ROI", "Modelled ROI"), M("Channel ROI", "True ROI")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Channel ROI", "Modelled ROI"), "#7B2FF7"),
                                                                      (M("Channel ROI", "True ROI"), "#FFB703")])})
    p.chart(648, 476, 616, 232, "tableEx", "Campaign ML check: planted issues vs detected (synthetic ground truth)",
            {"Values": [C("ML Evaluation", "campaign"), C("ML Evaluation", "planted_issue"),
                        C("ML Evaluation", "detected_reason"), C("ML Evaluation", "correct")]})
    return p


def platform_kpis():
    p = Page("platforms", "Platform and Funnel KPIs", "Impressions to clicks to conversions by platform: CTR, CPC, CPA, CPM and platform-reported ROAS")
    for i, (m, u) in enumerate([(M("Media", "Total Impressions"), 1e6), (M("Media", "Total Clicks"), 1e6),
                                (M("Media", "Total Conversions"), 1e3), (M("Media", "CPA"), 0)]):
        p.card(16 + i * 316, 80, 304, 84, m, KPI_COLORS[i], u)
    for i, m in enumerate([M("Media", "CTR"), M("Media", "CVR"), M("Media", "CPC"), M("Media", "CPM")]):
        p.card(16 + i * 316, 172, 304, 84, m, KPI_COLORS[(i + 1) % 5])
    p.chart(16, 268, 828, 440, "tableEx", "Platform scorecard (platform-reported, not incremental)",
            {"Values": [named(C("Platform", "channel"), "Channel"), named(PLATFORM, "Platform"),
                        named(M("Media", "Media Spend"), "Spend"), named(M("Media", "Total Impressions"), "Impressions"),
                        named(M("Media", "Total Clicks"), "Clicks"), M("Media", "CTR"),
                        named(M("Media", "Total Conversions"), "Conversions"), M("Media", "CVR"), M("Media", "CPC"),
                        M("Media", "CPA"), M("Media", "CPM"), M("Media", "Platform ROAS")]},
            sort=(M("Media", "Media Spend"), "Descending"),
            objects={"values": heatmap(M("Media", "Platform ROAS"), lo="#FFF7E6", hi="#06D6A0")})
    p.chart(856, 268, 408, 214, "clusteredBarChart", "CPA by platform (lower is better)",
            {"Category": [PLATFORM], "Y": [M("Media", "CPA")]}, sort=(M("Media", "CPA"), "Ascending"),
            objects={"labels": [{"properties": {"show": lit(True)}}],
                     "dataPoint": [{"properties": {"fill": color("#7B2FF7")}}]})
    p.chart(856, 494, 408, 214, "clusteredColumnChart", "Platform-reported ROAS vs MMM incremental ROAS",
            {"Category": [CHANNEL], "Y": [named(M("Media", "Platform ROAS"), "Platform-reported ROAS"),
                                          named(M("Media", "ROAS"), "MMM incremental ROAS")]},
            objects={"legend": legend(), "dataPoint": measure_colors([(M("Media", "Platform ROAS"), "#FFB703"),
                                                                      (M("Media", "ROAS"), "#7B2FF7")])})
    return p


def campaign_insights():
    p = Page("campaigns", "Campaign Insights (ML)", "Trends, changepoints and segments find what is breaking or winning, and where to move budget, with the evidence")
    for i, (m, u) in enumerate([(M("Recommendations", "Campaigns Flagged"), 0), (M("Recommendations", "Campaigns To Scale"), 0),
                                (M("Recommendations", "Recommended 12wk Spend"), 1e6), (M("ML Evaluation", "Detection Recall"), 0),
                                (M("ML Evaluation", "False Positives"), 0)]):
        p.card(16 + i * 252, 80, 240, 84, m, KPI_COLORS[i], u)
    p.chart(16, 176, 620, 262, "scatterChart", "Campaign segments: recent CPA vs CTR (bubble = recent spend)",
            {"Category": [CAMPAIGN], "Series": [C("Campaign", "segment")], "X": [M("Recommendations", "Recent CPA")],
             "Y": [M("Recommendations", "Lifetime CTR")], "Size": [M("Recommendations", "Recent 12wk Spend")]},
            objects={"dataPoint": series_colors(C("Campaign", "segment"), SEGMENT_COLORS), "legend": legend()})
    p.chart(648, 176, 616, 262, "lineChart", "Deteriorating campaigns: weekly CPA",
            {"Category": [WEEK], "Series": [CAMPAIGN], "Y": [M("Campaign Weekly", "Campaign CPA")]},
            objects={"legend": legend()})
    p.visuals[-1]["filterConfig"] = {"filters": [in_filter("deteriorating", C("Campaign", "segment"), ["Deteriorating"])]}
    p.chart(16, 450, 1248, 258, "tableEx", "Recommendations with data evidence (next 12 weeks)",
            {"Values": [named(C("Recommendations", "action"), "Action"), named(CAMPAIGN, "Campaign"),
                        named(C("Recommendations", "platform"), "Platform"), named(C("Recommendations", "reason"), "Reason"),
                        named(C("Recommendations", "within_channel_change_pct"), "Within channel"),
                        named(M("Recommendations", "Budget Shift"), "Budget shift"),
                        named(C("Recommendations", "evidence"), "Evidence (computed from data)")]},
            sort=(C("Recommendations", "action"), "Ascending"),
            objects={"values": [{"properties": {"wordWrap": lit(True), "fontSize": lit(8)}}],
                     "columnHeaders": [{"properties": {"wordWrap": lit(True)}}],
                     "columnWidth": [{"properties": {"value": lit(w)}, "selector": {"metadata": m}} for m, w in [
                         ("Recommendations.action", 70), ("Campaign.campaign", 150), ("Recommendations.platform", 85),
                         ("Recommendations.reason", 140), ("Recommendations.within_channel_change_pct", 60),
                         ("Sum(Recommendations.budget_change)", 70), ("Recommendations.Budget Shift", 70),
                         ("Recommendations.evidence", 590)]]})
    return p


def data_quality():
    p = Page("dataquality", "Data Quality and Controls", "Run log, reconciliation, quarantined records and alerts for every pipeline run")
    for i, m in enumerate([M("Run Log", "Runs"), M("Recon Results", "Checks Run"), M("Recon Results", "Check Pass Rate"),
                           M("DQ Exceptions", "Critical Exceptions"), M("Alerts", "Alert Count")]):
        p.card(16 + i * 252, 80, 240, 92, m, KPI_COLORS[i])
    p.chart(16, 184, 620, 256, "pivotTable", "Reconciliation checks by status",
            {"Rows": [C("Recon Results", "check_name")], "Columns": [C("Recon Results", "status")],
             "Values": [M("Recon Results", "Checks Run")]})
    p.chart(648, 184, 616, 256, "clusteredBarChart", "Exceptions by rule",
            {"Category": [C("DQ Exceptions", "rule_name")], "Series": [C("DQ Exceptions", "severity")],
             "Y": [M("DQ Exceptions", "Exceptions")]})
    p.chart(16, 452, 620, 256, "tableEx", "Alerts needing root cause analysis",
            {"Values": [C("Alerts", "raised_at"), C("Alerts", "alert_type"), C("Alerts", "summary"),
                        C("Alerts", "rca_hint"), C("Alerts", "delivery_status")]})
    p.chart(648, 452, 616, 256, "tableEx", "Pipeline run log",
            {"Values": [C("Run Log", "ended_at"), C("Run Log", "layer"), C("Run Log", "step"), C("Run Log", "status"),
                        C("Run Log", "rows_in"), C("Run Log", "rows_out")]})
    return p


def test_page():
    """One of each new visual type, to confirm role names and colour selectors render."""
    p = Page("test", "Visual render test", "combo, stacked area, waterfall, scatter, treemap, series colours")
    p.chart(16, 80, 620, 300, "lineClusteredColumnComboChart", "combo",
            {"Category": [WEEK], "Y": [M("Media", "Media Spend")], "Y2": [M("Sales", "Total Revenue")]})
    p.chart(648, 80, 616, 300, "stackedAreaChart", "stacked area",
            {"Category": [WEEK], "Series": [COMPONENT], "Y": [M("Decomposition", "Total Contribution")]},
            objects={"dataPoint": series_colors(COMPONENT, COMPONENT_COLORS)})
    p.chart(16, 392, 410, 316, "waterfallChart", "waterfall",
            {"Category": [COMPONENT], "Y": [M("Decomposition", "Total Contribution")]})
    p.chart(436, 392, 410, 316, "scatterChart", "scatter",
            {"Category": [CHANNEL], "X": [M("Channel ROI", "ROI Spend")], "Y": [M("Channel ROI", "ROI Contribution")],
             "Size": [M("Channel ROI", "Modelled ROI")]}, objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS)})
    p.chart(856, 392, 408, 316, "treemap", "treemap",
            {"Group": [CHANNEL], "Values": [M("Channel ROI", "ROI Contribution")]},
            objects={"dataPoint": series_colors(CHANNEL, CHANNEL_COLORS)})
    return p


def parts(sm_id, test):
    pages = [test_page()] if test else [overview(), decomposition(), roi_page(), platform_kpis(), campaign_insights(),
                                       curves(), optimizer(), regional(), model_fit(), data_quality()]
    files = {
        "definition.pbir": {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                            "version": "4.0", "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={sm_id}"}}},
        "definition/version.json": {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"},
        "definition/report.json": {
            "$schema": f"{SCHEMA}/report/3.1.0/schema.json",
            "themeCollection": {"customTheme": {"name": "VibrantMarketing.json", "reportVersionAtImport": {
                "visual": "2.4.0", "report": "3.0.0", "page": "2.3.0"}, "type": "RegisteredResources"}},
            "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources",
                                  "items": [{"name": "VibrantMarketing.json", "path": "VibrantMarketing.json",
                                             "type": "CustomTheme"}]}],
            "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True},
        },
        "StaticResources/RegisteredResources/VibrantMarketing.json": THEME,
        "definition/pages/pages.json": {"$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
                                        "pageOrder": [p.name for p in pages], "activePageName": pages[0].name},
    }
    for p in pages:
        files[f"definition/pages/{p.name}/page.json"] = {
            "$schema": f"{SCHEMA}/page/2.0.0/schema.json", "name": p.name, "displayName": p.title,
            "displayOption": "FitToPage", "height": 720, "width": 1280}
        for v in p.visuals:
            files[f"definition/pages/{p.name}/visuals/{v['name']}/visual.json"] = v
    return [{"path": k, "payload": base64.b64encode(json.dumps(v, indent=1).encode()).decode(),
             "payloadType": "InlineBase64"} for k, v in files.items()]


def main():
    test = "--test" in sys.argv
    _, _, sms = f.call("GET", f"{f.API}/workspaces/{f.WS}/semanticModels")
    sm_id = next(i["id"] for i in sms["value"] if i["displayName"] == SM_NAME)
    definition = {"parts": parts(sm_id, test)}
    _, _, reps = f.call("GET", f"{f.API}/workspaces/{f.WS}/reports")
    rep = next((i for i in reps["value"] if i["displayName"] == NAME), None)
    if rep:
        st, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/reports/{rep['id']}/updateDefinition", {"definition": definition})
    else:
        st, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/reports", {"displayName": NAME, "definition": definition})
    if st == 202:
        f.wait_lro(h)
    _, _, reps = f.call("GET", f"{f.API}/workspaces/{f.WS}/reports")
    rep = next(i for i in reps["value"] if i["displayName"] == NAME)
    print("report", rep["id"])
    print(f"https://app.fabric.microsoft.com/groups/{f.WS}/reports/{rep['id']}")


if __name__ == "__main__":
    main()

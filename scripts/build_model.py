"""Create/update the Direct Lake semantic model sm_marketing_mix over wh_marketing_gold (TMDL via Fabric REST).

Usage: build_model.py   (reads column types live from the Warehouse via sqlcmd)
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fabric_nb as f  # noqa: E402

NAME = "sm_marketing_mix"
CFG = f.CONFIG
TYPES = {"varchar": "string", "bigint": "int64", "int": "int64", "date": "dateTime", "datetime2": "dateTime",
         "decimal": "double", "float": "double"}
I, D, M, P = "#,##0", "#,##0.00", "#,##0,,.0M", "0.0%"

# model table -> (schema.table, measures [(name, dax, format)])
TABLES = {
    "Week": ("gold.dim_week", []),
    "Region": ("gold.dim_region", []),
    "Channel": ("gold.dim_channel", []),
    "Component": ("gold.dim_component", []),
    "Sales": ("gold.fact_weekly_sales", [
        ("Total Revenue", "SUM(Sales[revenue])", I),
        ("Total Units", "SUM(Sales[units])", I),
        ("Avg Order Value", "DIVIDE([Total Revenue], [Total Units])", D),
    ]),
    "Media": ("gold.fact_weekly_media", [
        ("Media Spend", "SUM(Media[spend])", I),
        ("Total Impressions", "SUM(Media[impressions])", I),
        ("Total Clicks", "SUM(Media[clicks])", I),
        ("Media Contribution", "SUM(Media[contribution])", I),
        ("ROAS", "DIVIDE([Media Contribution], [Media Spend])", D),
        ("CPM", "DIVIDE([Media Spend], [Total Impressions]) * 1000", D),
        ("CTR", "DIVIDE([Total Clicks], [Total Impressions])", "0.00%"),
        ("Total Conversions", "SUM(Media[conversions])", I),
        ("Total Video Views", "SUM(Media[video_views])", I),
        ("Platform Revenue", "SUM(Media[attributed_revenue])", I),
        ("CPC", "DIVIDE([Media Spend], [Total Clicks])", D),
        ("CPA", "DIVIDE([Media Spend], [Total Conversions])", D),
        ("CVR", "DIVIDE([Total Conversions], [Total Clicks])", "0.00%"),
        ("Platform ROAS", "DIVIDE([Platform Revenue], [Media Spend])", D),
        ("VTR", "DIVIDE([Total Video Views], [Total Impressions])", "0.0%"),
        ("CPV", "DIVIDE([Media Spend], [Total Video Views])", "#,##0.000"),
    ]),
    "Platform": ("gold.dim_platform", []),
    "Campaign": ("gold.dim_campaign", []),
    "Campaign Weekly": ("gold.fact_campaign_weekly", [
        ("Campaign CPA", "DIVIDE(SUM('Campaign Weekly'[spend]), SUM('Campaign Weekly'[conversions]))", D),
        ("Campaign CTR", "DIVIDE(SUM('Campaign Weekly'[clicks]), SUM('Campaign Weekly'[impressions]))", "0.00%"),
        ("Campaign CVR", "DIVIDE(SUM('Campaign Weekly'[conversions]), SUM('Campaign Weekly'[clicks]))", "0.00%"),
        ("Campaign CPM", "DIVIDE(SUM('Campaign Weekly'[spend]), SUM('Campaign Weekly'[impressions])) * 1000", D),
    ]),
    "Recommendations": ("gold.agg_campaign_recommendations", [
        ("Recent 12wk Spend", "SUM(Recommendations[recent_spend])", I),
        ("Recommended 12wk Spend", "SUM(Recommendations[recommended_spend])", I),
        ("Budget Shift", "SUM(Recommendations[budget_change])", I),
        ("Campaigns Flagged", "CALCULATE(COUNTROWS(Recommendations), Recommendations[action] <> \"Hold\") + 0", I),
        ("Campaigns To Scale", "CALCULATE(COUNTROWS(Recommendations), Recommendations[action] = \"Scale\") + 0", I),
        ("Recent CPA", "DIVIDE(SUMX(Recommendations, Recommendations[recent_cpa] * Recommendations[recent_spend]), "
                       "SUM(Recommendations[recent_spend]))", D),
        ("Lifetime CTR", "AVERAGE(Recommendations[ctr])", "0.00%"),
    ]),
    "Campaign Trends": ("gold.agg_campaign_trends", []),
    "Changepoints": ("gold.agg_campaign_changepoints", []),
    "ML Evaluation": ("gold.agg_ml_evaluation", [
        ("Detection Recall", "DIVIDE(CALCULATE(SUM('ML Evaluation'[correct]), 'ML Evaluation'[planted_issue] <> \"none\"), "
                             "CALCULATE(COUNTROWS('ML Evaluation'), 'ML Evaluation'[planted_issue] <> \"none\"))", P),
        ("False Positives", "CALCULATE(COUNTROWS('ML Evaluation'), 'ML Evaluation'[planted_issue] = \"none\", "
                            "'ML Evaluation'[correct] = 0) + 0", I),
    ]),
    "Weekly Trend": ("gold.fact_weekly_trend", [
        ("Revenue 4wk Avg", "SUM('Weekly Trend'[revenue_ma4])", I),
        ("Spend 4wk Avg", "SUM('Weekly Trend'[spend_ma4])", I),
        ("Revenue Last Year", "SUM('Weekly Trend'[revenue_last_year])", I),
        ("Revenue YoY %", "VAR cy = SUMX(FILTER('Weekly Trend', NOT ISBLANK('Weekly Trend'[revenue_last_year])), 'Weekly Trend'[revenue]) "
                          "RETURN DIVIDE(cy - [Revenue Last Year], [Revenue Last Year])", P),
    ]),
    "Decomposition": ("gold.fact_decomposition", [
        ("Total Contribution", "SUM(Decomposition[contribution])", I),
        ("Media Driven %", "DIVIDE(CALCULATE([Total Contribution], Component[component_group] <> \"Baseline\"), "
                           "CALCULATE([Total Contribution], REMOVEFILTERS(Component)))", P),
    ]),
    "Model Fit": ("gold.fact_model_fit", [
        ("Actual Revenue", "SUM('Model Fit'[actual])", I),
        ("Predicted Revenue", "SUM('Model Fit'[predicted])", I),
        ("Fit Error %", "DIVIDE(SUMX('Model Fit', ABS('Model Fit'[actual] - 'Model Fit'[predicted])), [Actual Revenue])", P),
    ]),
    "Channel ROI": ("gold.agg_channel_roi", [
        ("Modelled ROI", "DIVIDE(SUM('Channel ROI'[contribution]), SUM('Channel ROI'[spend]))", D),
        ("Marginal ROI", "DIVIDE(SUMX('Channel ROI', 'Channel ROI'[marginal_roi] * 'Channel ROI'[spend]), "
                         "SUM('Channel ROI'[spend]))", D),
        ("True ROI", "DIVIDE(SUMX('Channel ROI', 'Channel ROI'[true_roi] * 'Channel ROI'[spend]), "
                     "SUM('Channel ROI'[spend]))", D),
        ("Spend Share", "SUM('Channel ROI'[spend_share])", P),
        ("Contribution Share", "SUM('Channel ROI'[contribution_share])", P),
        ("ROI Spend", "SUM('Channel ROI'[spend])", I),
        ("ROI Contribution", "SUM('Channel ROI'[contribution])", I),
    ]),
    "Response Curves": ("gold.agg_response_curves", [
        ("Curve Contribution", "SUM('Response Curves'[contribution])", I),
        ("Curve Spend", "SUM('Response Curves'[spend])", I),
    ]),
    "Budget Scenarios": ("gold.agg_budget_scenarios", [
        ("Current Spend", "IF(HASONEVALUE('Budget Scenarios'[scenario]), SUM('Budget Scenarios'[current_spend]), CALCULATE(SUM('Budget Scenarios'[current_spend]), 'Budget Scenarios'[scenario] = \"+0%\"))", I),
        ("Optimal Spend", "IF(HASONEVALUE('Budget Scenarios'[scenario]), SUM('Budget Scenarios'[optimal_spend]), CALCULATE(SUM('Budget Scenarios'[optimal_spend]), 'Budget Scenarios'[scenario] = \"+0%\"))", I),
        ("Current Contribution", "IF(HASONEVALUE('Budget Scenarios'[scenario]), SUM('Budget Scenarios'[current_contribution]), CALCULATE(SUM('Budget Scenarios'[current_contribution]), 'Budget Scenarios'[scenario] = \"+0%\"))", I),
        ("Optimal Contribution", "IF(HASONEVALUE('Budget Scenarios'[scenario]), SUM('Budget Scenarios'[optimal_contribution]), CALCULATE(SUM('Budget Scenarios'[optimal_contribution]), 'Budget Scenarios'[scenario] = \"+0%\"))", I),
        ("Contribution Uplift", "[Optimal Contribution] - [Current Contribution]", I),
        ("Contribution Uplift %", "DIVIDE([Contribution Uplift], [Current Contribution])", P),
        ("Optimal ROI", "DIVIDE([Optimal Contribution], [Optimal Spend])", D),
    ]),
    "Model Metrics": ("gold.agg_model_metrics", [
        ("Holdout MAPE", "MAX('Model Metrics'[holdout_mape])", "0.00%"),
        ("Model R2", "MAX('Model Metrics'[train_r2])", "0.000"),
        ("Max ROI Error %", "DIVIDE(MAX('Model Metrics'[max_abs_roi_error_pct]), 100)", P),
    ]),
    "Run Log": ("ops.pipeline_run_log", [
        ("Runs", "DISTINCTCOUNT('Run Log'[run_id])", I),
        ("Failed Steps", "CALCULATE(COUNTROWS('Run Log'), 'Run Log'[status] IN {\"FAILED\", \"ERROR\"}) + 0", I),
    ]),
    "Recon Results": ("ops.recon_results", [
        ("Checks Run", "COUNTROWS('Recon Results')", I),
        ("Checks Failed", "CALCULATE(COUNTROWS('Recon Results'), 'Recon Results'[status] = \"FAIL\")", I),
        ("Check Pass Rate", "DIVIDE([Checks Run] - [Checks Failed], [Checks Run])", P),
    ]),
    "DQ Exceptions": ("ops.dq_exceptions", [
        ("Exceptions", "COUNTROWS('DQ Exceptions') + 0", I),
        ("Critical Exceptions", "CALCULATE(COUNTROWS('DQ Exceptions'), 'DQ Exceptions'[severity] = \"CRITICAL\") + 0", I),
    ]),
    "Alerts": ("ops.alert_log", [("Alert Count", "COUNTROWS(Alerts) + 0", I)]),
}
SORT_BY = {("Week", "month_name"): "month", ("Channel", "channel"): "channel_sort",
           ("Component", "component"): "component_sort", ("Budget Scenarios", "scenario"): "scenario_sort",
           ("Recommendations", "action"): "action_sort"}
HIDE = {"spend", "impressions", "clicks", "contribution", "revenue", "units", "revenue_ma4", "spend_ma4",
        "revenue_last_year", "spend_last_year", "actual", "predicted", "current_spend", "optimal_spend",
        "current_contribution", "optimal_contribution", "channel_sort", "component_sort", "scenario_sort",
        "conversions", "video_views", "attributed_revenue", "recent_spend", "recommended_spend", "budget_change",
        "action_sort"}
RELATIONSHIPS = [
    ("Sales", "week_start", "Week", "week_start"), ("Sales", "region", "Region", "region"),
    ("Media", "week_start", "Week", "week_start"), ("Media", "region", "Region", "region"),
    ("Media", "channel", "Channel", "channel"), ("Media", "platform", "Platform", "platform"),
    ("Media", "campaign", "Campaign", "campaign"),
    ("'Campaign Weekly'", "week_start", "Week", "week_start"), ("'Campaign Weekly'", "campaign", "Campaign", "campaign"),
    ("Recommendations", "campaign", "Campaign", "campaign"),
    ("'Campaign Trends'", "campaign", "Campaign", "campaign"), ("Changepoints", "campaign", "Campaign", "campaign"),
    ("'Weekly Trend'", "week_start", "Week", "week_start"), ("'Weekly Trend'", "region", "Region", "region"),
    ("Decomposition", "week_start", "Week", "week_start"), ("Decomposition", "region", "Region", "region"),
    ("Decomposition", "component", "Component", "component"),
    ("'Model Fit'", "week_start", "Week", "week_start"),
    ("'Channel ROI'", "channel", "Channel", "channel"),
    ("'Response Curves'", "channel", "Channel", "channel"),
    ("'Budget Scenarios'", "channel", "Channel", "channel"),
]


def q(name):
    return f"'{name}'" if " " in name else name


def columns():
    out = subprocess.check_output([
        "sqlcmd", "-S", CFG["warehouse_server"], "-d", CFG["warehouse_name"], "--authentication-method",
        "ActiveDirectoryAzCli", "-W", "-h", "-1", "-s", "|", "-Q",
        "SET NOCOUNT ON; SELECT TABLE_SCHEMA + '.' + TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA IN ('gold','ops') ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"], text=True)
    cols = {}
    for line in out.splitlines():
        if line.count("|") == 2:
            t, c, ty = line.strip().split("|")
            cols.setdefault(t, []).append((c, TYPES[ty]))
    return cols


def table_tmdl(name, src, cols):
    schema, entity = src.split(".")
    out = [f"table {q(name)}"]
    for m, dax, fmt in TABLES[name][1]:
        out += [f"\tmeasure '{m}' = {dax}", f"\t\tformatString: {fmt}", ""]
    for c, ty in cols[src]:
        out += [f"\tcolumn {c}", f"\t\tdataType: {ty}"]
        if c in HIDE and schema == "gold":
            out.append("\t\tisHidden")
        if ty == "dateTime":
            out.append("\t\tformatString: yyyy-mm-dd")
        out += ["\t\tsummarizeBy: none", f"\t\tsourceColumn: {c}"]
        if (name, c) in SORT_BY:
            out.append(f"\t\tsortByColumn: {SORT_BY[(name, c)]}")
        out.append("")
    out += [f"\tpartition {q(name)} = entity", "\t\tmode: directLake", "\t\tsource",
            f"\t\t\tentityName: {entity}", f"\t\t\tschemaName: {schema}", "\t\t\texpressionSource: DatabaseQuery", ""]
    return "\n".join(out)


def parts():
    cols = columns()
    files = {
        "definition.pbism": json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
            "version": "4.2", "settings": {}}),
        "definition/database.tmdl": "database\n\tcompatibilityLevel: 1604\n",
        "definition/model.tmdl": "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                                 "\tdiscourageImplicitMeasures\n",
        "definition/expressions.tmdl": (
            "expression DatabaseQuery =\n\t\tlet\n"
            f"\t\t\tdatabase = Sql.Database(\"{CFG['warehouse_server']}\", \"{CFG['warehouse_id']}\")\n"
            "\t\tin\n\t\t\tdatabase\n"),
        "definition/relationships.tmdl": "\n".join(
            f"relationship r{i}\n\tfromColumn: {a}.{ac}\n\ttoColumn: {b}.{bc}\n"
            for i, (a, ac, b, bc) in enumerate(RELATIONSHIPS)),
    }
    for name, (src, _) in TABLES.items():
        files[f"definition/tables/{name}.tmdl"] = table_tmdl(name, src, cols)
    return [{"path": p, "payload": base64.b64encode(c.encode()).decode(), "payloadType": "InlineBase64"}
            for p, c in files.items()]


def pbi(method, path, body=None):
    tok = subprocess.check_output(["az", "account", "get-access-token", "--resource",
                                   "https://analysis.windows.net/powerbi/api", "--query", "accessToken", "-o", "tsv"],
                                  text=True).strip()
    req = urllib.request.Request(f"https://api.powerbi.com/v1.0/myorg/groups/{f.WS}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:3000]}")


def main():
    definition = {"parts": parts()}
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/semanticModels")
    sm = next((i for i in items["value"] if i["displayName"] == NAME), None)
    if sm:
        st, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/semanticModels/{sm['id']}/updateDefinition",
                          {"definition": definition})
    else:
        st, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/semanticModels", {"displayName": NAME, "definition": definition})
    if st == 202:
        f.wait_lro(h)
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/semanticModels")
    sm = next(i for i in items["value"] if i["displayName"] == NAME)
    print("model", sm["id"])
    pbi("POST", f"/datasets/{sm['id']}/refreshes", {})
    for _ in range(30):
        time.sleep(5)
        r = pbi("GET", f"/datasets/{sm['id']}/refreshes?$top=1")["value"][0]
        if r["status"] != "Unknown":
            print("refresh", r["status"], r.get("serviceExceptionJson", ""))
            break
    res = pbi("POST", f"/datasets/{sm['id']}/executeQueries", {"queries": [{"query":
        'EVALUATE ROW("revenue", [Total Revenue], "spend", [Media Spend], "contribution", [Media Contribution], '
        '"roas", [ROAS], "decomp", [Total Contribution], "media_pct", [Media Driven %], "yoy", [Revenue YoY %], '
        '"actual", [Actual Revenue], "mape", [Holdout MAPE], "uplift0", CALCULATE([Contribution Uplift], '
        '\'Budget Scenarios\'[scenario] = "+0%"), "checks", [Checks Run], "conv", [Total Conversions], '
        '"cpa", [CPA], "platform_roas", [Platform ROAS], "flagged", [Campaigns Flagged], "recall", [Detection Recall], '
        '"fp", [False Positives], "rec_spend", [Recommended 12wk Spend], "recent_spend", [Recent 12wk Spend])'}]})
    print(json.dumps(res["results"][0]["tables"][0]["rows"], indent=1))


if __name__ == "__main__":
    main()

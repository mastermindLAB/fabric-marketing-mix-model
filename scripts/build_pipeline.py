"""Create/update the pl_marketing_mix_weekly data pipeline and optionally run it.

Usage: build_pipeline.py [--run]
Bad data or reconciliation mismatches never fail the run: they produce alert rows and take the alert branch.
Only infrastructure errors (unreadable file, failed write, Gold load error) fail an activity.
"""
import base64
import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import fabric_nb as f  # noqa: E402
import os  # noqa: E402,F401

NAME = "pl_marketing_mix_weekly"
RECIPIENT = f.CONFIG.get("alert_recipient", "")  # set in config.json
OUTLOOK_CONNECTION_ID = None  # set once an Office 365 Outlook connection exists (needs a mailbox-enabled account)


def notebook_ids():
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/notebooks")
    return {i["displayName"]: i["id"] for i in items["value"]}


def nb_activity(name, notebook_id, depends_on=None, params=None):
    return {
        "name": name, "type": "TridentNotebook",
        "dependsOn": [{"activity": depends_on, "dependencyConditions": ["Succeeded"]}] if depends_on else [],
        # Trial capacity rejects new Spark sessions when busy (HTTP 430), so retry with a long pause
        "policy": {"timeout": "0.02:00:00", "retry": 3, "retryIntervalInSeconds": 180,
                   "secureOutput": False, "secureInput": False},
        "typeProperties": {
            "notebookId": notebook_id, "workspaceId": f.WS,
            "sessionTag": "marketing_mix",  # notebooks share one Spark session (high concurrency)
            "parameters": {k: {"value": {"value": v, "type": "Expression"} if v.startswith("@") else v,
                               "type": "string"} for k, v in (params or {}).items()},
        },
    }


def alert_branch():
    if OUTLOOK_CONNECTION_ID:
        send = {
            "name": "Email alert", "type": "Office365Email", "dependsOn": [],
            "externalReferences": {"connection": OUTLOOK_CONNECTION_ID},
            "typeProperties": {
                "to": RECIPIENT,
                "subject": {"value": "@json(activity('Gold and reconcile').output.result.exitValue).alert_subject",
                            "type": "Expression"},
                "body": {"value": "@replace(json(activity('Gold and reconcile').output.result.exitValue).alert_body, '\n', '<br>')",
                         "type": "Expression"},
            },
        }
    else:
        # Placeholder until a mailbox-enabled Outlook connection exists; alert rows are already in ops.alert_log
        send = {"name": "Alert raised (email channel pending)", "type": "SetVariable", "dependsOn": [],
                "typeProperties": {"variableName": "alert_summary",
                                   "value": {"value": "@json(activity('Gold and reconcile').output.result.exitValue).alert_subject",
                                             "type": "Expression"}}}
    return {
        "name": "Alert needed", "type": "IfCondition",
        "dependsOn": [{"activity": "Gold and reconcile", "dependencyConditions": ["Succeeded"]}],
        "typeProperties": {
            "expression": {"value": "@equals(json(activity('Gold and reconcile').output.result.exitValue).overall_status, 'ALERT')",
                           "type": "Expression"},
            "ifTrueActivities": [send],
            "ifFalseActivities": [],
        },
    }


def definition():
    ids = notebook_ids()
    run = {"run_id": "@pipeline().RunId"}
    content = {"properties": {
        "description": "Landing -> Bronze -> Silver -> MMM fit -> Gold with logging, quarantine, reconciliation, model gate and alerting",
        "activities": [
            nb_activity("Bronze", ids["01_bronze"], params=run),
            nb_activity("Silver", ids["02_silver"], "Bronze", run),
            nb_activity("Fit marketing mix model", ids["03_mmm_model"], "Silver", run),
            nb_activity("Campaign ML", ids["03b_campaign_ml"], "Fit marketing mix model", run),
            nb_activity("Sync SQL endpoint", ids["04_sync_sql_endpoint"], "Campaign ML"),
            nb_activity("Gold and reconcile", ids["05_gold_reconcile"], "Sync SQL endpoint",
                        {**run, "alert_recipient": RECIPIENT}),
            alert_branch(),
        ],
        "variables": {"alert_summary": {"type": "String"}},
    }}
    return {"parts": [{"path": "pipeline-content.json", "payloadType": "InlineBase64",
                       "payload": base64.b64encode(json.dumps(content).encode()).decode()}]}


def main():
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/items?type=DataPipeline")
    pl = next((i for i in items["value"] if i["displayName"] == NAME), None)
    if pl:
        st, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/items/{pl['id']}/updateDefinition", {"definition": definition()})
    else:
        st, h, pl = f.call("POST", f"{f.API}/workspaces/{f.WS}/items",
                           {"displayName": NAME, "type": "DataPipeline", "definition": definition()})
    if st == 202:
        f.wait_lro(h)
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/items?type=DataPipeline")
    pl = next(i for i in items["value"] if i["displayName"] == NAME)
    print("pipeline", pl["id"])
    if "--run" not in sys.argv:
        return

    _, h, _ = f.call("POST", f"{f.API}/workspaces/{f.WS}/items/{pl['id']}/jobs/instances?jobType=Pipeline")
    loc = h["Location"]
    run_id = loc.rstrip("/").split("/")[-1]
    print("run", run_id)
    while True:
        time.sleep(20)
        _, _, job = f.call("GET", loc)
        if job.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            break
    print("pipeline status", job["status"], json.dumps(job.get("failureReason") or "", indent=1))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    _, _, acts = f.call("POST", f"{f.API}/workspaces/{f.WS}/datapipelines/pipelineruns/{run_id}/queryactivityruns",
                        {"filters": [], "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}],
                         "lastUpdatedAfter": "2026-01-01T00:00:00Z", "lastUpdatedBefore": now})
    for a in acts.get("value", acts if isinstance(acts, list) else []):
        out = a.get("output") or {}
        exit_value = (out.get("result") or {}).get("exitValue") if isinstance(out, dict) else None
        print(f"- {a['activityName']}: {a['status']}", (exit_value or "")[:600], (a.get("error") or {}).get("message", ""))


if __name__ == "__main__":
    main()

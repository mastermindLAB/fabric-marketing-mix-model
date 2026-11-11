# PARAMETERS
run_id = ""
alert_recipient = "{{alert_recipient}}"

# %%
import json
import struct
import uuid
import pyodbc

SERVER = "{{warehouse_server}}"
run_id = run_id or f"manual-{uuid.uuid4()}"


def connect():
    token = notebookutils.credentials.getToken("https://database.windows.net/").encode("utf-16-le")
    attrs = struct.pack(f"<I{len(token)}s", len(token), token)
    return pyodbc.connect(f"Driver={{ODBC Driver 18 for SQL Server}};Server={SERVER},1433;"
                          "Database={{warehouse_name}};Encrypt=yes", attrs_before={1256: attrs}, autocommit=True)


conn = connect()
cur = conn.cursor()

# %%
# Gold load errors are infrastructure failures and stop the pipeline (the proc logs, then rethrows)
cur.execute("EXEC ops.usp_load_gold @run_id = ?", run_id)
while cur.nextset():
    pass

# %%
# Reconciliation never throws: it records PASS/FAIL, raises alert rows, and returns one status row
cur.execute("EXEC ops.usp_reconcile @run_id = ?, @recipient = ?", run_id, alert_recipient)
row = None
while True:
    try:
        if cur.description:
            row = cur.fetchone() or row
    except pyodbc.ProgrammingError:
        pass
    if not cur.nextset():
        break
cols = ["run_id", "overall_status", "critical_exceptions", "recon_failures", "checks_run"]
result = dict(zip(cols, [str(v) if i < 2 else int(v) for i, v in enumerate(row)]))

alerts = [dict(zip([c[0] for c in cur.description], r)) for r in cur.execute(
    "SELECT alert_type, severity, summary, rca_hint FROM ops.alert_log WHERE run_id = ?", run_id).fetchall()]
result["alerts"] = [{k: str(v) for k, v in a.items()} for a in alerts]
result["alert_subject"] = (f"[Marketing Mix Fabric] {len(alerts)} alert(s) need RCA, run {run_id}" if alerts else "")
result["alert_body"] = "\n\n".join(f"{a['severity']} {a['alert_type']}\n{a['summary']}\n{a['rca_hint']}"
                                   for a in result["alerts"])
print(json.dumps(result, indent=1))
notebookutils.notebook.exit(json.dumps(result))

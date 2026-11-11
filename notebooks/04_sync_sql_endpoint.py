import json
import time
import requests

WS = "{{workspace_id}}"
SQL_ENDPOINT = "{{sql_endpoint_id}}"
API = "https://api.fabric.microsoft.com/v1"

# %%
# The Warehouse reads Lakehouse tables through the SQL endpoint; sync it so this run's Silver data is visible
headers = {"Authorization": f"Bearer {notebookutils.credentials.getToken('pbi')}", "Content-Type": "application/json"}
r = requests.post(f"{API}/workspaces/{WS}/sqlEndpoints/{SQL_ENDPOINT}/refreshMetadata", headers=headers, json={})
if r.status_code == 202:
    loc = r.headers["Location"]
    while True:
        time.sleep(int(r.headers.get("Retry-After", 5)))
        r = requests.get(loc, headers=headers)
        if r.json().get("status") in ("Succeeded", "Failed"):
            r = requests.get(loc + "/result", headers=headers) if r.json()["status"] == "Succeeded" else r
            break
r.raise_for_status()
tables = r.json().get("value", []) if r.content else []
failed = [t["tableName"] for t in tables if t.get("status") == "Failure"]
print(tables)
if failed:
    raise RuntimeError(f"SQL endpoint sync failed for {failed}")
notebookutils.notebook.exit(json.dumps({"synced": len(tables)}))

"""Force the Lakehouse SQL endpoint to sync Delta table metadata so the Warehouse sees new tables/data."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fabric_nb as f  # noqa: E402

st, h, body = f.call("POST", f"{f.API}/workspaces/{f.WS}/sqlEndpoints/{f.CONFIG['sql_endpoint_id']}/refreshMetadata", {})
if st == 202:
    f.wait_lro(h)
for t in (body or {}).get("value", []):
    print(t["tableName"], t["status"])

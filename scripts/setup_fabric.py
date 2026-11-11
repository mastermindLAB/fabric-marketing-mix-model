"""Create the marketing-mix workspace, Lakehouse (schemas on) and Warehouse, then write config.json."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fabric_nb as f  # noqa: E402

WORKSPACE = "marketing-mix-analytics"
LAKEHOUSE = "lh_marketing"
WAREHOUSE = "wh_marketing_gold"
CAPACITY_SKU_PREFIX = "FT"  # Fabric trial capacity


def find(items, name):
    return next((i for i in items if i["displayName"] == name), None)


_, _, caps = f.call("GET", f"{f.API}/capacities")
cap = next(c for c in caps["value"] if c["sku"].startswith(CAPACITY_SKU_PREFIX) and c["state"] == "Active")

_, _, wss = f.call("GET", f"{f.API}/workspaces")
ws = find(wss["value"], WORKSPACE)
if not ws:
    _, _, ws = f.call("POST", f"{f.API}/workspaces", {"displayName": WORKSPACE, "capacityId": cap["id"]})
f.WS = ws["id"]


def ensure(kind, name, body_extra=None):
    _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/{kind}")
    item = find(items["value"], name)
    if not item:
        st, h, item = f.call("POST", f"{f.API}/workspaces/{f.WS}/{kind}", {"displayName": name, **(body_extra or {})})
        if st == 202:
            f.wait_lro(h)
        for _ in range(20):
            _, _, items = f.call("GET", f"{f.API}/workspaces/{f.WS}/{kind}")
            item = find(items["value"], name)
            if item and item.get("properties"):
                break
            time.sleep(5)
    return item


lh = ensure("lakehouses", LAKEHOUSE, {"creationPayload": {"enableSchemas": True}})
wh = ensure("warehouses", WAREHOUSE)
for _ in range(30):
    _, _, lh = f.call("GET", f"{f.API}/workspaces/{f.WS}/lakehouses/{lh['id']}")
    if (lh.get("properties") or {}).get("sqlEndpointProperties", {}).get("id"):
        break
    time.sleep(10)

config = {
    "alert_recipient": os.environ.get("ALERT_RECIPIENT", "alerts@example.com"),
    "workspace_id": f.WS,
    "workspace_name": WORKSPACE,
    "capacity_id": cap["id"],
    "lakehouse_id": lh["id"],
    "lakehouse_name": LAKEHOUSE,
    "sql_endpoint_id": lh["properties"]["sqlEndpointProperties"]["id"],
    "warehouse_id": wh["id"],
    "warehouse_name": WAREHOUSE,
    "warehouse_server": wh["properties"]["connectionString"],
    "lakehouse_abfss": f"abfss://{f.WS}@onelake.dfs.fabric.microsoft.com/{lh['id']}",
}
json.dump(config, open(f.CONFIG_PATH, "w"), indent=2)
print(json.dumps(config, indent=2))
print("schemas enabled:", lh["properties"].get("defaultSchema"))

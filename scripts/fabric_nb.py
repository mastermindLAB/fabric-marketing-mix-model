"""Create/update a Fabric notebook from a local .py cell file and run it as a job.

Usage: fabric_nb.py <display_name> <cells.py> [--deploy-only]
Cells in the .py file are separated by lines that are exactly '# %%'.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
CONFIG = json.load(open(CONFIG_PATH)) if os.path.exists(CONFIG_PATH) else {}
WS = CONFIG.get("workspace_id", "")
API = "https://api.fabric.microsoft.com/v1"


def token():
    return subprocess.check_output(
        ["az", "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com",
         "--query", "accessToken", "-o", "tsv"], text=True).strip()


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, dict(r.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read().decode()[:2000]}")


def wait_lro(headers):
    loc = headers.get("Location")
    while loc:
        time.sleep(int(headers.get("Retry-After", 5)))
        st, headers, body = call("GET", loc)
        if body and body.get("status") in ("Succeeded", "Failed"):
            if body["status"] == "Failed":
                sys.exit(f"LRO failed: {body}")
            return
        if st == 200 and not body:
            return


def render(path):
    """Notebook sources use {{key}} placeholders filled from config.json, so no IDs are hard-coded."""
    src = open(path).read()
    base = os.path.dirname(CONFIG_PATH)
    # '# INCLUDE <path>' pulls a shared module (for example scripts/mmm_core.py) into the notebook
    src = "\n".join(open(os.path.join(base, line.split(None, 2)[2])).read() if line.startswith("# INCLUDE ")
                    else line for line in src.split("\n"))
    for k, v in CONFIG.items():
        src = src.replace("{{" + k + "}}", str(v))
    return src


def build_ipynb(path):
    src = render(path).split("\n# %%\n")
    # A cell starting with '# PARAMETERS' becomes the notebook's parameters cell (pipeline overrides)
    cells = [{"cell_type": "code", "execution_count": None, "outputs": [],
              "metadata": {"tags": ["parameters"]} if c.strip().startswith("# PARAMETERS") else {},
              "source": c.strip("\n").splitlines(keepends=True)} for c in src if c.strip()]
    nb = {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
          "metadata": {"kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
                       "language_info": {"name": "python"}}}
    return base64.b64encode(json.dumps(nb).encode()).decode()


def main(name, path):
    definition = {"format": "ipynb", "parts": [
        {"path": "notebook-content.ipynb", "payload": build_ipynb(path), "payloadType": "InlineBase64"}]}
    _, _, items = call("GET", f"{API}/workspaces/{WS}/notebooks")
    nb = next((i for i in items["value"] if i["displayName"] == name), None)
    if nb:
        st, h, _ = call("POST", f"{API}/workspaces/{WS}/notebooks/{nb['id']}/updateDefinition", {"definition": definition})
        if st == 202:
            wait_lro(h)
        print("updated", name, nb["id"])
    else:
        st, h, body = call("POST", f"{API}/workspaces/{WS}/notebooks", {"displayName": name, "definition": definition})
        if st == 202:
            wait_lro(h)
            _, _, items = call("GET", f"{API}/workspaces/{WS}/notebooks")
            body = next(i for i in items["value"] if i["displayName"] == name)
        nb = body
        print("created", name, nb["id"])
    if "--deploy-only" in sys.argv:
        return

    _, h, _ = call("POST", f"{API}/workspaces/{WS}/items/{nb['id']}/jobs/instances?jobType=RunNotebook")
    loc = h["Location"]
    print("job started", loc)
    while True:
        time.sleep(15)
        _, _, job = call("GET", loc)
        print("status", job.get("status"))
        if job.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            if job.get("failureReason"):
                print(json.dumps(job["failureReason"], indent=1))
            sys.exit(0 if job["status"] == "Completed" else 1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

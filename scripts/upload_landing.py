"""Upload data/*.csv to Files/landing and ground_truth.json to Files/reference via the OneLake API (uses az login)."""
import json
import os

from azure.identity import AzureCliCredential
from azure.storage.filedatalake import DataLakeServiceClient

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
FILES = {"media_spend.csv": "landing", "sales.csv": "landing", "calendar.csv": "landing",
         "campaigns.csv": "landing", "ground_truth.json": "reference", "campaign_truth.json": "reference"}

fs = DataLakeServiceClient("https://onelake.dfs.fabric.microsoft.com", credential=AzureCliCredential()) \
    .get_file_system_client(CFG["workspace_id"])
for name, folder in FILES.items():
    local = os.path.join(ROOT, "data", name)
    with open(local, "rb") as fh:
        fs.get_file_client(f"{CFG['lakehouse_id']}/Files/{folder}/{name}").upload_data(fh, overwrite=True)
    remote = fs.get_file_client(f"{CFG['lakehouse_id']}/Files/{folder}/{name}").get_file_properties().size
    size = os.path.getsize(local)
    print(f"{folder}/{name}: {remote} bytes, local {size}", "MATCH" if remote == size else "MISMATCH")

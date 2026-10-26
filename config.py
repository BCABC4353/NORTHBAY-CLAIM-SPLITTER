import os

from config_generated import BATCH_REPORT_ID, CLIENT_ID, DATAFLOW_ID, DATASET_ID, TENANT_ID, WORKSPACE_ID

GITHUB_REPO = "BCABC4353/NORTHBAY-CLAIM-SPLITTER"

API = "https://api.powerbi.com/v1.0/myorg/"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = [
    "https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Dataflow.ReadWrite.All",
    "https://analysis.windows.net/powerbi/api/Report.Read.All",
]

WAIT_SECONDS = 600
POLL_SECONDS = 10
START_TOLERANCE_SECONDS = 10

SUFFIX_ORDER = ("NBC", "TX", "911")

APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "NorthBay Claim Splitter"
)

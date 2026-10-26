from __future__ import annotations

import email.utils
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import msal
import msal_extensions
import requests

import config

INTERACTIVE_TIMEOUT_SECONDS = 300


class PbiError(Exception):
    pass


class RefreshFailedError(PbiError):
    def __init__(self, what: str, detail: str):
        self.what = what
        self.detail = detail
        super().__init__(f"The {what} refresh failed: {detail}")


class PbiTimeoutError(PbiError):
    def __init__(self, what: str):
        self.what = what
        super().__init__(f"Timed out after {config.WAIT_SECONDS // 60} minutes waiting for the {what}.")


class ExportThrottledError(PbiError):
    def __init__(self, retry_after_s: int):
        self.retry_after_s = retry_after_s
        minutes = max(1, (retry_after_s + 59) // 60)
        super().__init__(
            f"Power BI allows one invoice export every 5 minutes. Try again in about {minutes} minute(s)."
        )


@dataclass(frozen=True)
class RunInfo:
    run: str
    trip_date: str
    job: str
    invoice: str


def _token_cache():
    os.makedirs(config.APP_DATA_DIR, exist_ok=True)
    path = os.path.join(config.APP_DATA_DIR, "token_cache.bin")
    return msal_extensions.PersistedTokenCache(msal_extensions.FilePersistenceWithDataProtection(path))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    head, dot, rest = str(value).replace("Z", "+00:00").partition(".")
    v = head + rest.lstrip("0123456789") if dot else head
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _server_time(resp: requests.Response) -> datetime:
    date = resp.headers.get("Date")
    if date:
        try:
            dt = email.utils.parsedate_to_datetime(date)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _retry_after(resp: requests.Response, default: int) -> int:
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(1, int(float(value)))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                return max(1, int((when - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                pass
    return default


def _error_summary(resp: requests.Response) -> str:
    detail = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error") or body.get("pbi.error") or {}
        if isinstance(err, dict):
            code = err.get("code") or ""
            message = err.get("message") or ""
            detail = ": ".join(p for p in (code, message) if p)
        elif err:
            detail = str(err)
    return f"HTTP {resp.status_code}" + (f": {detail[:300]}" if detail else "")


def _is_busy(resp: requests.Response) -> bool:
    if resp.status_code not in (400, 409):
        return False
    text = (resp.text or "").lower()
    return "already" in text or "progress" in text


def _is_transient(resp: requests.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


class PbiClient:
    def __init__(self):
        self._app = msal.PublicClientApplication(
            config.CLIENT_ID, authority=config.AUTHORITY, token_cache=_token_cache()
        )
        self._session = requests.Session()
        self._username = ""
        self._lock = threading.Lock()

    def sign_in(self) -> str:
        self._token()
        return self._username

    def _token(self) -> str:
        with self._lock:
            result = None
            account = None
            accounts = self._app.get_accounts()
            if accounts:
                account = accounts[0]
                result = self._app.acquire_token_silent(config.SCOPES, account=account)
            if not result or "access_token" not in result:
                try:
                    result = self._app.acquire_token_interactive(
                        config.SCOPES,
                        prompt="select_account",
                        timeout=INTERACTIVE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    raise PbiError(f"Power BI sign-in failed: {type(exc).__name__}: {exc}") from exc
                account = None
            if not result or "access_token" not in result:
                detail = (result or {}).get("error_description") or (result or {}).get("error") or "no token"
                raise PbiError(f"Power BI sign-in failed: {detail}")
            claims = result.get("id_token_claims") or {}
            name = claims.get("preferred_username") or (account or {}).get("username")
            if not name:
                current = self._app.get_accounts()
                name = current[0].get("username") if current else ""
            self._username = name or self._username
            return result["access_token"]

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        headers = {"Authorization": "Bearer " + self._token()}
        try:
            return self._session.request(method, config.API + path, headers=headers, timeout=120, **kw)
        except requests.RequestException as exc:
            raise PbiError(f"Could not reach Power BI: {type(exc).__name__}") from exc

    def _wait(self, deadline: float, what: str, seconds: float | None = None) -> None:
        now = time.monotonic()
        if now >= deadline:
            raise PbiTimeoutError(what)
        delay = config.POLL_SECONDS if seconds is None else seconds
        time.sleep(max(1.0, min(delay, deadline - now)))

    def refresh_invoicer(self, on_status: Callable[[str], None]) -> None:
        self._refresh_dataflow(on_status)
        self._refresh_dataset(on_status)

    def _refresh_dataflow(self, on_status: Callable[[str], None]) -> None:
        base = f"groups/{config.WORKSPACE_ID}/dataflows/{config.DATAFLOW_ID}"
        what = "dataflow refresh"
        deadline = time.monotonic() + config.WAIT_SECONDS
        on_status("Refreshing the INVOICER dataflow...")
        while True:
            resp = self._req("POST", base + "/refreshes", json={"notifyOption": "NoNotification"})
            if resp.ok:
                break
            if _is_busy(resp):
                on_status("Waiting for a dataflow refresh that is already running...")
                self._wait(deadline, what)
                continue
            if _is_transient(resp):
                self._wait(deadline, what, _retry_after(resp, config.POLL_SECONDS))
                continue
            raise RefreshFailedError("dataflow", _error_summary(resp))
        on_status("Refreshing the INVOICER dataflow...")
        since = _server_time(resp) - timedelta(seconds=config.START_TOLERANCE_SECONDS)
        while True:
            self._wait(deadline, what)
            listing = self._req("GET", base + "/transactions")
            if _is_transient(listing):
                continue
            if not listing.ok:
                raise RefreshFailedError("dataflow", _error_summary(listing))
            mine = []
            for t in listing.json().get("value", []):
                started = _parse_time(t.get("startTime"))
                if started is not None and started >= since:
                    mine.append((started, t))
            if not mine:
                continue
            mine.sort(key=lambda pair: pair[0])
            status = str(mine[0][1].get("status") or "")
            if status in ("Success", "Succeeded", "Completed"):
                return
            if status in ("Failed", "Cancelled", "Canceled"):
                raise RefreshFailedError("dataflow", status)

    def _refresh_dataset(self, on_status: Callable[[str], None]) -> None:
        base = f"groups/{config.WORKSPACE_ID}/datasets/{config.DATASET_ID}"
        what = "dataset refresh"
        deadline = time.monotonic() + config.WAIT_SECONDS
        on_status("Refreshing the INVOICER dataset...")
        while True:
            resp = self._req("POST", base + "/refreshes", json={"notifyOption": "NoNotification"})
            if resp.ok:
                break
            if _is_busy(resp):
                on_status("Waiting for a dataset refresh that is already running...")
                self._wait(deadline, what)
                continue
            if _is_transient(resp):
                self._wait(deadline, what, _retry_after(resp, config.POLL_SECONDS))
                continue
            raise RefreshFailedError("dataset", _error_summary(resp))
        on_status("Refreshing the INVOICER dataset...")
        request_id = (resp.headers.get("RequestId") or resp.headers.get("x-ms-request-id") or "").strip().lower()
        since = _server_time(resp) - timedelta(seconds=config.START_TOLERANCE_SECONDS)
        while True:
            self._wait(deadline, what)
            listing = self._req("GET", base + "/refreshes?$top=10")
            if _is_transient(listing):
                continue
            if not listing.ok:
                raise RefreshFailedError("dataset", _error_summary(listing))
            rows = listing.json().get("value", [])
            if request_id:
                mine = [r for r in rows if str(r.get("requestId") or "").strip().lower() == request_id]
            else:
                dated = []
                for r in rows:
                    started = _parse_time(r.get("startTime"))
                    if started is not None and started >= since:
                        dated.append((started, r))
                dated.sort(key=lambda pair: pair[0])
                mine = [r for _, r in dated]
            if not mine:
                continue
            status = str(mine[0].get("status") or "")
            if status == "Completed":
                return
            if status in ("Failed", "Disabled", "Cancelled"):
                detail = mine[0].get("serviceExceptionJson") or status
                raise RefreshFailedError("dataset", str(detail)[:300])

    def invoice_lookup(self) -> dict[str, list[RunInfo]]:
        dax = (
            "EVALUATE SUMMARIZE(FILTER('Batch_History', 'Batch_History'[InvoiceNumber] <> \"\"), "
            "'Batch_History'[RunNumber], 'Batch_History'[TripDate], 'Batch_History'[Job], "
            "'Batch_History'[InvoiceNumber])"
        )
        resp = self._req(
            "POST",
            f"groups/{config.WORKSPACE_ID}/datasets/{config.DATASET_ID}/executeQueries",
            json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
        )
        if not resp.ok:
            raise PbiError(f"Invoice lookup failed: {_error_summary(resp)}")
        try:
            result = resp.json()["results"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PbiError("Invoice lookup failed: unexpected response shape.") from exc
        err = result.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else ""
            raise PbiError(f"Invoice lookup failed: {code or 'query error'}")
        try:
            rows = result["tables"][0]["rows"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PbiError("Invoice lookup failed: unexpected response shape.") from exc
        out: dict[str, list[RunInfo]] = {}
        for r in rows:
            run = str(r.get("Batch_History[RunNumber]") or "").strip()
            if not run:
                continue
            info = RunInfo(
                run=run,
                trip_date=str(r.get("Batch_History[TripDate]") or "")[:10],
                job=str(r.get("Batch_History[Job]") or ""),
                invoice=str(r.get("Batch_History[InvoiceNumber]") or ""),
            )
            out.setdefault(run.replace("-", ""), []).append(info)
        return out

    def export_invoices(self, invoices: list[str]) -> bytes:
        if not invoices:
            raise PbiError("No invoices to export.")
        base = f"groups/{config.WORKSPACE_ID}/reports/{config.BATCH_REPORT_ID}"
        body = {
            "format": "PDF",
            "paginatedReportConfiguration": {
                "parameterValues": [{"name": "INVOICE", "value": str(i)} for i in invoices]
            },
        }
        resp = self._req("POST", base + "/ExportTo", json=body)
        if resp.status_code == 429:
            raise ExportThrottledError(_retry_after(resp, 300))
        if not resp.ok:
            raise PbiError(f"Invoice export failed: {_error_summary(resp)}")
        try:
            export_id = resp.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PbiError("Invoice export failed: no export id returned.") from exc
        what = "invoice export"
        deadline = time.monotonic() + config.WAIT_SECONDS
        delay = _retry_after(resp, config.POLL_SECONDS)
        while True:
            self._wait(deadline, what, delay)
            st = self._req("GET", f"{base}/exports/{export_id}")
            delay = _retry_after(st, config.POLL_SECONDS)
            if _is_transient(st):
                continue
            if not st.ok:
                raise PbiError(f"Invoice export status failed: {_error_summary(st)}")
            data = st.json()
            status = data.get("status")
            if status == "Succeeded":
                break
            if status == "Failed":
                err = data.get("error") or {}
                message = err.get("message") or err.get("code") if isinstance(err, dict) else str(err)
                raise PbiError(f"Invoice export failed: {message or 'unknown error'}")
        f = self._req("GET", f"{base}/exports/{export_id}/file")
        if not f.ok:
            raise PbiError(f"Invoice download failed: {_error_summary(f)}")
        return f.content

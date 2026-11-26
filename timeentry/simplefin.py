"""SimpleFIN Bridge client (https://www.simplefin.org).

Flow: the user creates a *setup token* at SimpleFIN Bridge (about $15/yr), pastes it
here once; we exchange it (HTTP POST to the base64-decoded URL) for an *access URL*
of the form https://user:pass@host/path, which we keep in the local database.
Then GET {access_url}/accounts?start-date=<unix>&end-date=<unix> returns balances
and transactions for every connected account.
"""
from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import httpx

from .bank_import import ParsedTxn
from .money import round_cents

TIMEOUT = 60


@dataclass
class SFAccount:
    id: str
    name: str
    institution: str
    currency: str
    balance: int
    balance_date: dt.date | None
    transactions: list[ParsedTxn] = field(default_factory=list)


def claim_setup_token(token: str, client: httpx.Client | None = None) -> str:
    token = token.strip()
    try:
        claim_url = base64.b64decode(token + "=" * (-len(token) % 4)).decode().strip()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("That isn't a valid SimpleFIN setup token") from exc
    if not claim_url.startswith("https://"):
        raise ValueError("That isn't a valid SimpleFIN setup token")
    c = client or httpx.Client(timeout=TIMEOUT)
    r = c.post(claim_url, headers={"Content-Length": "0"})
    if r.status_code == 403:
        raise ValueError("SimpleFIN says this setup token was already used or is invalid. Create a new one.")
    r.raise_for_status()
    access_url = r.text.strip()
    if not access_url.startswith("https://"):
        raise ValueError("Unexpected response from SimpleFIN")
    return access_url


def _split_auth(access_url: str) -> tuple[str, tuple[str, str] | None]:
    parts = urlsplit(access_url)
    auth = (parts.username or "", parts.password or "") if parts.username else None
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", "")), auth


def _unix_date(value) -> dt.date | None:
    if value in (None, "", 0):
        return None
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).astimezone().date()


def parse_account_set(data: dict) -> tuple[list[SFAccount], list[str]]:
    errors = [str(e.get("message", e)) if isinstance(e, dict) else str(e)
              for e in (data.get("errors") or data.get("errlist") or [])]
    conns = {c.get("conn_id"): c.get("name", "") for c in data.get("connections") or [] if isinstance(c, dict)}
    out = []
    for a in data.get("accounts") or []:
        org = a.get("org") or {}
        inst = org.get("name") or org.get("domain") or conns.get(a.get("conn_id"), "")
        acct = SFAccount(
            id=str(a["id"]), name=a.get("name", ""), institution=inst, currency=a.get("currency", "USD"),
            balance=round_cents(Decimal(str(a.get("balance", "0"))) * 100),
            balance_date=_unix_date(a.get("balance-date")),
        )
        for t in a.get("transactions") or []:
            posted = t.get("posted") or t.get("transacted_at")
            if not posted:
                continue  # pending with no date yet
            desc = t.get("payee") or t.get("description") or ""
            acct.transactions.append(ParsedTxn(
                date=_unix_date(posted),
                amount=round_cents(Decimal(str(t["amount"])) * 100),
                description=desc, memo=t.get("memo") or (t.get("description") if t.get("payee") else "") or "",
                external_id="sf:" + str(t["id"]), pending=bool(t.get("pending")),
            ))
        out.append(acct)
    return out, errors


def fetch_accounts(access_url: str, start: dt.date, end: dt.date | None = None,
                   client: httpx.Client | None = None) -> tuple[list[SFAccount], list[str]]:
    base, auth = _split_auth(access_url)
    params = {"start-date": int(dt.datetime.combine(start, dt.time()).timestamp())}
    if end:
        params["end-date"] = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time()).timestamp())
    c = client or httpx.Client(timeout=TIMEOUT)
    r = c.get(base + "/accounts", params=params, auth=auth)
    if r.status_code == 403:
        raise ValueError("SimpleFIN refused access. The connection may have been revoked; reconnect with a new setup token.")
    r.raise_for_status()
    return parse_account_set(r.json())

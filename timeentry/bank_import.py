"""Parse bank downloads into a common shape.

Supported: OFX / QFX / QBO (OFX 1.x SGML and 2.x XML; what "Download for
Quicken / QuickBooks" produces), and CSV with auto-detected columns.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from dataclasses import dataclass, field

from .money import parse_money


@dataclass
class ParsedTxn:
    date: dt.date
    amount: int  # signed cents: + in, - out
    description: str
    memo: str = ""
    external_id: str = ""
    pending: bool = False


@dataclass
class ParsedStatement:
    transactions: list[ParsedTxn] = field(default_factory=list)
    account_number: str = ""
    balance: int | None = None
    balance_date: dt.date | None = None


# ------------------------------------------------------------------ OFX
_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.I)


def _ofx_date(value: str) -> dt.date:
    return dt.datetime.strptime(value.strip()[:8], "%Y%m%d").date()


def _fields(block: str) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2).strip() for m in _TAG.finditer(block) if m.group(2).strip()}


def parse_ofx(data: bytes | str) -> ParsedStatement:
    text = data.decode("latin-1") if isinstance(data, bytes) else data
    if "<OFX" not in text.upper():
        raise ValueError("This doesn't look like an OFX/QFX file")
    st = ParsedStatement()
    acct = re.search(r"<ACCTID>([^<\r\n]+)", text, re.I)
    if acct:
        st.account_number = acct.group(1).strip()
    ledger = re.search(r"<LEDGERBAL>(.*?)(</LEDGERBAL>|<AVAILBAL>|</STMTRS>|</CCSTMTRS>|$)", text, re.I | re.S)
    if ledger:
        f = _fields(ledger.group(1))
        if "BALAMT" in f:
            st.balance = parse_money(f["BALAMT"])
            if "DTASOF" in f:
                st.balance_date = _ofx_date(f["DTASOF"])
    for block in re.split(r"<STMTTRN>", text, flags=re.I)[1:]:
        block = re.split(r"</STMTTRN>|</BANKTRANLIST>", block, flags=re.I)[0]
        f = _fields(block)
        if "TRNAMT" not in f or "DTPOSTED" not in f:
            continue
        name = f.get("NAME") or f.get("PAYEE") or ""
        memo = f.get("MEMO", "")
        st.transactions.append(ParsedTxn(
            date=_ofx_date(f["DTPOSTED"]), amount=parse_money(f["TRNAMT"]),
            description=_unescape(name or memo), memo=_unescape(memo if name else ""),
            external_id="ofx:" + (f.get("FITID") or _hash(f["DTPOSTED"], f["TRNAMT"], name, memo)),
        ))
    return st


def _unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'").replace("&quot;", '"')


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ CSV
DATE_HEADERS = ("posting date", "post date", "posted date", "transaction date", "date", "trans. date", "trans date")
DESC_HEADERS = ("description", "payee", "name", "merchant", "details", "transaction description", "memo")
AMOUNT_HEADERS = ("amount", "transaction amount", "amount (usd)")
DEBIT_HEADERS = ("debit", "withdrawal", "withdrawals", "debit amount", "money out")
CREDIT_HEADERS = ("credit", "deposit", "deposits", "credit amount", "money in")
MEMO_HEADERS = ("memo", "notes", "reference", "check or slip #", "check number")
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d")


def _find(headers: list[str], names: tuple[str, ...], exclude: set[int] = frozenset()) -> int | None:
    norm = [h.strip().lower() for h in headers]
    for name in names:
        for i, h in enumerate(norm):
            if h == name and i not in exclude:
                return i
    return None


def _parse_date(value: str) -> dt.date:
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized date: {value!r}")


def _amount(value: str) -> int:
    v = value.strip()
    neg = v.startswith("(") and v.endswith(")")
    cents = parse_money(v.strip("()")) if v else 0
    return -cents if neg else cents


def parse_csv(data: bytes | str, flip_sign: bool = False) -> ParsedStatement:
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    # Some banks put a few lines of account info above the header row.
    header_idx = next((i for i, r in enumerate(rows[:15]) if _find(r, DATE_HEADERS) is not None), None)
    if header_idx is None:
        raise ValueError("Couldn't find a header row with a Date column")
    headers = rows[header_idx]
    di = _find(headers, DATE_HEADERS)
    ai = _find(headers, AMOUNT_HEADERS)
    dbi, cri = _find(headers, DEBIT_HEADERS), _find(headers, CREDIT_HEADERS)
    desc_i = _find(headers, DESC_HEADERS)
    memo_i = _find(headers, MEMO_HEADERS, exclude={desc_i} if desc_i is not None else set())
    if ai is None and (dbi is None or cri is None):
        raise ValueError("Need an Amount column, or Debit and Credit columns")
    st = ParsedStatement()
    seen: dict[str, int] = {}
    for r in rows[header_idx + 1:]:
        r = r + [""] * (len(headers) - len(r))
        try:
            date = _parse_date(r[di])
        except ValueError:
            continue  # footer / summary lines
        if ai is not None:
            amount = _amount(r[ai])
        else:
            amount = abs(_amount(r[cri])) - abs(_amount(r[dbi]))
        if flip_sign:
            amount = -amount
        desc = r[desc_i].strip() if desc_i is not None else ""
        memo = r[memo_i].strip() if memo_i is not None else ""
        key = _hash(date, amount, desc, memo)
        seen[key] = seen.get(key, 0) + 1  # identical rows on the same day stay distinct
        st.transactions.append(ParsedTxn(date=date, amount=amount, description=desc, memo=memo,
                                         external_id=f"csv:{key}:{seen[key]}"))
    if not st.transactions:
        raise ValueError("No transactions found in the file")
    return st


def parse_file(filename: str, data: bytes, flip_sign: bool = False) -> ParsedStatement:
    name = filename.lower()
    if name.endswith((".ofx", ".qfx", ".qbo")) or b"<OFX" in data[:4000].upper():
        return parse_ofx(data)
    return parse_csv(data, flip_sign=flip_sign)

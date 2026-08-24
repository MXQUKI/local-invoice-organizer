#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local invoice organizer.

No third-party runtime dependency is required.  The program starts a local
browser UI and keeps all data under ./data.  Optional pypdf/pdfplumber and OCR
providers can be added later; files that cannot be parsed are routed to manual
review instead of being silently accepted.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import errno
import hashlib
import html
import io
import itertools
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


APP_ROOT = Path(__file__).resolve().parent
if os.environ.get("LOCAL_INVOICE_DATA_DIR"):
    DATA_ROOT = Path(os.environ["LOCAL_INVOICE_DATA_DIR"]).expanduser()
elif sys.platform == "darwin" and (getattr(sys, "frozen", False) or ".app/Contents" in str(APP_ROOT)):
    DATA_ROOT = Path.home() / "Library" / "Application Support" / "本地发票整理"
elif getattr(sys, "frozen", False) and os.environ.get("LOCALAPPDATA"):
    DATA_ROOT = Path(os.environ["LOCALAPPDATA"]) / "本地发票整理"
else:
    DATA_ROOT = APP_ROOT / "data"
BATCH_ROOT = DATA_ROOT / "batches"
ARCHIVE_ROOT = DATA_ROOT / "archives"
DB_PATH = DATA_ROOT / "invoice.db"
PORT = 8765
OCR_CONCURRENCY = 100
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".csv", ".xlsx", ".txt"}
FILE_CATEGORIES = {"invoice", "purchase", "payment", "refund", "other"}
UPLOAD_CATEGORIES = {"invoice", "purchase"}
STATUS_BLOCKING = {"PROCESSING", "NEEDS_REVIEW", "MULTI_ORDER_REVIEW", "INCOMPLETE"}


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    value = value.replace("　", " ")
    value = re.sub(r"\s+", "", value)
    return value.strip().casefold()


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .")
    return value or fallback


def parse_money(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("￥", "").replace("¥", "").strip()
    if not text:
        return None
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None
    try:
        number = round(float(text), 2)
        return -number if negative else number
    except ValueError:
        return None


def requires_purchase_credential(invoice_amount: float | None, threshold: float) -> bool:
    """判断单张发票是否必须提供购买凭证。

    规则按单张发票执行：金额低于或等于阈值时不要求，超过阈值时要求。
    """
    return invoice_amount is not None and invoice_amount > threshold


def is_purchase_requirement_error(error: str) -> bool:
    """识别系统生成的购买凭证缺失提示，便于重新核验时清理旧提示。"""
    text = str(error)
    return text.startswith("单张发票金额") and text.endswith("需要购买凭证")


def money_text(value: Any) -> str:
    number = parse_money(value)
    return "" if number is None else f"{number:.2f}"


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    m = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", value)
    if not m:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", value)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def read_text_from_pdf(path: Path) -> str:
    """Best-effort PDF text extraction; optional libraries are not required."""
    try:
        from pypdf import PdfReader  # type: ignore

        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception:
        pass
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception:
        pass
    candidates = [
        os.environ.get("PDFTOTEXT_CMD"),
        shutil.which("pdftotext"),
        "/opt/homebrew/bin/pdftotext",
        "/usr/local/bin/pdftotext",
        "/usr/bin/pdftotext",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/bin/pdftotext"),
    ]
    for command in candidates:
        if not command or not (Path(command).exists() or shutil.which(command)):
            continue
        try:
            result = subprocess.run(
                [command, "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except Exception:
            continue
    return ""


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """Read simple XLSX files without openpyxl."""
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in root.findall("x:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//x:t", ns)))
        sheet = next((n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
        if not sheet:
            return []
        root = ET.fromstring(zf.read(sheet))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[list[str]] = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            values: list[str] = []
            for cell in row.findall("x:c", ns):
                value = cell.find("x:v", ns)
                text = value.text if value is not None and value.text is not None else ""
                if cell.attrib.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                    text = shared[int(text)]
                values.append(text)
            rows.append(values)
        return rows


def text_for_file(path: Path, category: str = "") -> tuple[str, str]:
    ext = path.suffix.casefold()
    if ext == ".pdf":
        # 固定工作流：先读取 PDF 内嵌文字；只有完全没有文字层时才转图片 OCR。
        text = read_text_from_pdf(path)
        if text.strip():
            return text, "PDF内嵌文字提取"
        try:
            from ocr_image import recognize_pdf

            result = recognize_pdf(path)
            method = result.get("method", "扫描PDF Tesseract OCR")
            warnings = result.get("warnings") or []
            if warnings:
                method += "（" + "；".join(str(item) for item in warnings) + "）"
            return result.get("text", ""), method
        except Exception:
            return "", "待OCR/人工录入"
    if ext in {".txt", ".csv"}:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace"), "文本/表格读取"
        except Exception:
            return "", "人工录入"
    if ext == ".xlsx":
        try:
            rows = read_xlsx_rows(path)
            return "\n".join("\t".join(row) for row in rows), "XLSX表格读取"
        except Exception:
            return "", "人工录入"
    if ext in {".png", ".jpg", ".jpeg"}:
        try:
            from ocr_image import recognize_image

            result = recognize_image(path, use_deepseek=category in {"purchase", "payment", "refund"})
            method = result.get("method", "Tesseract OCR")
            warnings = result.get("warnings") or []
            if warnings:
                method += "（" + "；".join(str(item) for item in warnings) + "）"
            return result.get("text", ""), method
        except Exception:
            return "", "待OCR/人工录入"
    return "", "待OCR/人工录入"


def first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.M)
        if m:
            return m.group(1).strip()
    return None


def parse_deepseek_result(text: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and ("payment_amount" in payload or "ocr_text" in payload):
            return str(payload.get("ocr_text") or ""), payload
    except (TypeError, json.JSONDecodeError):
        pass
    return text, {}


def payment_amount_from_text(text: str) -> float | None:
    """从 OCR 文本提取付款金额，支持优惠、聊天转账和多订单截图。"""
    amounts: list[float] = []
    lines = text.splitlines()

    def nearby_value(start: int) -> float | None:
        values: list[float] = []
        for future in lines[start + 1:start + 30]:
            compact = re.sub(r"\s+", "", future)
            if "展开" in compact or "交易快照" in compact:
                break
            tokens = re.findall(r"[¥￥YV]\s*([A-Za-z0-9.,-]+)", compact, re.I)
            for token in tokens:
                value = parse_ocr_amount_token(token)
                if value is not None:
                    values.append(abs(value))
        return values[-1] if values else None

    def previous_value(start: int) -> float | None:
        values: list[float] = []
        for previous in reversed(lines[max(0, start - 15):start]):
            compact = re.sub(r"\s+", "", previous)
            tokens = re.findall(r"[¥￥YV]\s*([A-Za-z0-9.,-]+)", compact, re.I)
            for token in tokens:
                value = parse_ocr_amount_token(token)
                if value is not None:
                    values.append(abs(value))
            if values:
                return values[0]
        return None

    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)
        semantic = re.search(r"实.?付款|实.?付|发票金额|支付金额|付款金额|付款|成交价|费用合计|总计|已收款|收款|转账", compact)
        if not semantic and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]{1,2})?元", compact):
            continue
        tokens = re.findall(r"[¥￥YV]\s*([A-Za-z0-9.,-]+)", compact, re.I)
        if tokens:
            value = parse_ocr_amount_token(tokens[-1])
            if value is not None:
                amounts.append(abs(value))
            if "共减" in compact or "优惠" in compact:
                prior_value = previous_value(index)
                if prior_value is not None:
                    amounts[-1] = prior_value
                final_value = nearby_value(index)
                if final_value is not None:
                    amounts[-1] = final_value
        else:
            plain = re.findall(r"-\s*([0-9]+(?:\.[0-9]{1,2})?)", compact)
            if plain:
                amounts.append(abs(parse_money(plain[-1]) or 0))
            else:
                value = first_match(compact, [r"(?:成交价|费用合计|总计|发票金额|实际付款金额|付款金额|收款|转账)[^0-9-]{0,12}(-?[0-9,]+(?:\.[0-9]{1,2})?)"])
                parsed = parse_money(value)
                if parsed is not None:
                    amounts.append(abs(parsed))
                elif re.search(r"实.?付款|实.?付|已收款|收款|转账", compact):
                    prior_value = previous_value(index)
                    if prior_value is not None:
                        amounts.append(prior_value)
        yuan = first_match(compact, [r"(-?[0-9,]+(?:\.[0-9]{1,2})?)元"])
        parsed_yuan = parse_money(yuan)
        if parsed_yuan is not None:
            amounts.append(abs(parsed_yuan))

    if not amounts and re.search(r"扫码付款|二维码付款", re.sub(r"\s+", "", text)):
        negative_values = re.findall(r"-\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
        amounts.extend(abs(parse_money(value) or 0) for value in negative_values)
    return round(sum(dict.fromkeys(amounts)), 2) if amounts else None


def parse_ocr_amount_token(value: str | None) -> float | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    # Tesseract 常把数字识别为相近字符，尤其是 YD94 -> 594、Y2D7 -> 257。
    token = token.translate(str.maketrans({"O": "0", "Q": "0", "D": "5", "S": "5", "I": "1", "L": "1", "B": "8", "Z": "2"}))
    token = re.sub(r"[^0-9.\-]", "", token)
    return parse_money(token)


def extract_fields(path: Path, category: str) -> tuple[dict[str, Any], str]:
    text, method = text_for_file(path, category)
    deepseek_fields: dict[str, Any] = {}
    if method == "DeepSeek Vision OCR":
        text, deepseek_fields = parse_deepseek_result(text)
    compact_text = re.sub(r"\s+", "", text)
    fields: dict[str, Any] = {
        "invoice_amount": None,
        "invoice_number": None,
        "buyer_name": None,
        "buyer_tax_id": None,
        "invoice_date": None,
        "payment_amount": None,
        "payment_amount_ocr": None,
        "payment_amount_ocr_raw": None,
        "payment_amount_source": None,
        "order_amount": None,
        "discount_amount": None,
        "refund_amount": None,
        "order_number": None,
        "transaction_number": None,
        "order_count": 0,
    }
    if not text and not deepseek_fields:
        return fields, method
    fields["buyer_name"] = first_match(text, [
        r"购买方(?:名称)?\s*[:：]\s*(.+)",
        r"购\s*名称\s*[:：]\s*(.+?)(?:\s{2,}销|\s+销\s*名称|$)",
    ])
    if fields["buyer_name"]:
        fields["buyer_name"] = re.split(r"\s{2,}销|\s+销\s*名称", fields["buyer_name"])[0].strip()
    fields["buyer_tax_id"] = first_match(text, [
        r"购买方(?:纳税人识别号|税号)\s*[:：]\s*([0-9A-Z]{8,30})",
        r"购方税号\s*[:：]\s*([0-9A-Z]{8,30})",
        r"购[\s\S]{0,120}?统一社会信用代码/纳税人识别号\s*[:：]?\s*([0-9A-Z]{15,30})",
    ])
    if not fields["buyer_tax_id"]:
        # 常见电子发票文字层会把“购/买/方/信/息”拆成单字，
        # 但购买方税号通常仍排在销售方税号之前。
        tax_ids = re.findall(r"统一社会信用代码/纳税人识别号\s*[:：]?\s*([0-9A-Z]{15,30})", text)
        if tax_ids:
            fields["buyer_tax_id"] = tax_ids[0]
    fields["invoice_number"] = first_match(text, [r"发票号码\s*[:：]?\s*([0-9A-Z]{6,30})", r"票号\s*[:：]?\s*([0-9A-Z]{6,30})"])
    date_value = first_match(text, [r"开票日期\s*[:：]?\s*([^\s,，]+)", r"(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})"])
    fields["invoice_date"] = parse_date(date_value)
    amount_value = first_match(text, [
        r"价税合计[\s\S]{0,120}?小写[）)]?\s*[:：]?\s*[¥￥]?\s*(-?[0-9,]+(?:\.[0-9]{1,2})?)",
        r"价税合计\s*(?:\([^)]*\)|（[^）]*）)?\s*[:：]?\s*[¥￥]?\s*(-?[0-9,]+(?:\.[0-9]{1,2})?)",
    ])
    if amount_value is not None:
        fields["invoice_amount"] = parse_money(amount_value)
    vlm_payment_amount = parse_money(deepseek_fields.get("payment_amount"))
    if deepseek_fields.get("order_numbers") and not fields["order_number"]:
        values = deepseek_fields.get("order_numbers")
        if isinstance(values, list) and values:
            fields["order_number"] = str(values[0])
    if deepseek_fields.get("dates") and not fields["invoice_date"]:
        values = deepseek_fields.get("dates")
        if isinstance(values, list) and values:
            fields["invoice_date"] = parse_date(str(values[0]))
    if vlm_payment_amount is None and isinstance(deepseek_fields.get("payment_amounts"), list):
        values = [parse_money(value) for value in deepseek_fields["payment_amounts"]]
        values = [value for value in values if value is not None]
        if values:
            vlm_payment_amount = round(sum(values), 2)
    if vlm_payment_amount is not None and category in {"purchase", "payment", "refund"}:
        fields["payment_amount"] = vlm_payment_amount
        fields["payment_amount_ocr"] = vlm_payment_amount
        fields["payment_amount_ocr_raw"] = vlm_payment_amount
        fields["payment_amount_source"] = "DeepSeek Vision"
    else:
        fields["payment_amount"] = payment_amount_from_text(text)
    fields["order_amount"] = parse_money(first_match(text, [r"订单金额\s*[:：]?\s*[¥￥]?\s*(-?[0-9,]+(?:\.[0-9]{1,2})?)"]))
    fields["discount_amount"] = parse_money(first_match(text, [r"优惠金额\s*[:：]?\s*[¥￥]?\s*(-?[0-9,]+(?:\.[0-9]{1,2})?)"]))
    fields["refund_amount"] = parse_money(first_match(text, [r"退款金额\s*[:：]?\s*[¥￥]?\s*(-?[0-9,]+(?:\.[0-9]{1,2})?)"]))
    fields["order_number"] = fields["order_number"] or first_match(text, [r"订单号\s*[:：]?\s*([0-9A-Z_-]{4,50})"])
    fields["transaction_number"] = fields["transaction_number"] or first_match(text, [r"交易号\s*[:：]?\s*([0-9A-Z_-]{4,60})"])
    # 不同 PDF 生成器可能把中文标签拆成“发 票 号 码”或在冒号前插入空格，
    # 对空白压缩后的文字再做一次字段兜底提取。
    if not fields["invoice_number"]:
        fields["invoice_number"] = first_match(compact_text, [r"发票号码[:：]?([0-9A-Z]{6,30})"])
    if not fields["buyer_name"]:
        fields["buyer_name"] = first_match(compact_text, [
            r"购买方名称[:：]?(.+?)(?=销售方名称|销名称|发票号码|开票日期|价税合计|$)",
            r"购销名称[:：]?(.+?)(?=名称[:：]|买售方|统一社会信用代码|$)",
            r"买名称[:：]?(.+?)售名称[:：]?",
            r"购名称[:：]?(.+?)销名称[:：]?",
        ])
    if not fields["buyer_tax_id"]:
        tax_ids = re.findall(r"(?:购买方)?统一社会信用代码/纳税人识别号[:：]?([0-9A-Z]{15,30})", compact_text)
        if not tax_ids:
            tax_ids = re.findall(r"统一社会信用代码/纳税人识别号.{0,80}?([0-9A-Z]{15,30})", compact_text)
        if tax_ids:
            fields["buyer_tax_id"] = tax_ids[0]
    if not fields["invoice_date"]:
        date_value = first_match(compact_text, [r"开票日期[:：]?((?:20\d{2})年\d{1,2}月\d{1,2}日)"])
        fields["invoice_date"] = parse_date(date_value)
    if fields["invoice_amount"] is None:
        amount_value = first_match(compact_text, [r"价税合计.*?小写[）)]?[:：]?¥?(-?[0-9,]+(?:\.[0-9]{1,2})?)"])
        fields["invoice_amount"] = parse_money(amount_value)
    if fields["payment_amount"] is None and method != "DeepSeek Vision OCR" and not deepseek_fields:
        fields["payment_amount"] = payment_amount_from_text(text)
    if fields["order_amount"] is None:
        order_value = first_match(compact_text, [
            r"(?:订单金额|商品金额|应付金额|订单总额)[^0-9-]{0,12}(-?[0-9,]+(?:\.[0-9]{1,2})?)",
        ])
        fields["order_amount"] = parse_money(order_value)
    if not fields["order_number"]:
        fields["order_number"] = first_match(compact_text, [r"订单(?:号|编号)[：:]?([0-9A-Z_-]{4,60})"])
    if fields["payment_amount"] is not None and not fields["payment_amount_source"]:
        fields["payment_amount_ocr"] = fields["payment_amount"]
        fields["payment_amount_ocr_raw"] = fields["payment_amount"]
        fields["payment_amount_source"] = "OCR"
    if category in {"purchase", "payment", "refund"}:
        if fields["order_number"]:
            fields["order_count"] = max(1, len(re.findall(r"订单号", text)))
        elif fields["order_amount"] is not None:
            fields["order_count"] = 1
    return fields, method


class Store:
    def __init__(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        BATCH_ROOT.mkdir(parents=True, exist_ok=True)
        ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, archive_path TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL, category TEXT NOT NULL, file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL, size INTEGER NOT NULL, recognition_method TEXT,
                fields_json TEXT NOT NULL, status TEXT NOT NULL, errors_json TEXT NOT NULL,
                review_note TEXT NOT NULL DEFAULT '', archived_name TEXT,
                created_at TEXT NOT NULL, matched_invoice_id TEXT, match_excluded INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(batch_id) REFERENCES batches(id)
            );
            """
        )
        file_columns = {row[1] for row in self.db.execute("PRAGMA table_info(files)")}
        if "matched_invoice_id" not in file_columns:
            self.db.execute("ALTER TABLE files ADD COLUMN matched_invoice_id TEXT")
        if "match_excluded" not in file_columns:
            self.db.execute("ALTER TABLE files ADD COLUMN match_excluded INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    def _batch(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["config"] = json.loads(value.pop("config_json"))
        return value

    def _file(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["fields"] = json.loads(value.pop("fields_json"))
        value["errors"] = json.loads(value.pop("errors_json"))
        return value

    def summarize_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        files = batch.get("files", [])
        invoices = [f for f in files if f["category"] == "invoice" and f["status"] != "REJECTED"]
        credentials = [f for f in files if f["category"] in {"purchase", "payment", "refund"} and f["status"] != "REJECTED"]
        purchase_threshold = float(batch.get("config", {}).get("purchase_threshold", 150))
        required_invoices = [
            f for f in invoices
            if requires_purchase_credential(parse_money(f["fields"].get("invoice_amount")), purchase_threshold)
        ]
        no_purchase_invoices = [
            f for f in invoices
            if not requires_purchase_credential(parse_money(f["fields"].get("invoice_amount")), purchase_threshold)
        ]
        invoice_total = round(sum(parse_money(f["fields"].get("invoice_amount")) or 0 for f in invoices), 2)
        required_invoice_total = round(sum(parse_money(f["fields"].get("invoice_amount")) or 0 for f in required_invoices), 2)
        no_purchase_invoice_total = round(sum(parse_money(f["fields"].get("invoice_amount")) or 0 for f in no_purchase_invoices), 2)
        matched_ids = {f.get("matched_invoice_id") for f in credentials if f.get("matched_invoice_id")}
        matched_invoice_total = round(sum(parse_money(f["fields"].get("invoice_amount")) or 0 for f in invoices if f["id"] in matched_ids), 2)
        required_ids = {f["id"] for f in required_invoices}
        manual_required_ids = {
            f.get("matched_invoice_id")
            for f in credentials
            if f.get("matched_invoice_id") in required_ids and f["fields"].get("match_mode") == "manual"
        }
        manual_required_ids.discard(None)
        check_required_ids = required_ids - manual_required_ids
        required_matched_invoice_total = round(sum(
            parse_money(f["fields"].get("invoice_amount")) or 0
            for f in required_invoices if f["id"] in matched_ids
        ), 2)
        checked_required_invoice_total = round(sum(
            parse_money(f["fields"].get("invoice_amount")) or 0
            for f in required_invoices if f["id"] in check_required_ids
        ), 2)
        payment_total = round(sum(
            parse_money(f["fields"].get("payment_amount")) or 0
            for f in credentials if f.get("matched_invoice_id") in check_required_ids
        ), 2)
        refund_total = round(sum(
            parse_money(f["fields"].get("refund_amount")) or 0
            for f in credentials if f.get("matched_invoice_id") in check_required_ids
        ), 2)
        net_total = round(payment_total - refund_total, 2)
        required_difference = round(checked_required_invoice_total - net_total, 2)
        return {
            "invoice_total": invoice_total,
            "required_invoice_total": required_invoice_total,
            "no_purchase_invoice_total": no_purchase_invoice_total,
            "matched_invoice_total": matched_invoice_total,
            "required_matched_invoice_total": required_matched_invoice_total,
            "checked_required_invoice_total": checked_required_invoice_total,
            "manual_override_invoice_total": round(sum(
                parse_money(f["fields"].get("invoice_amount")) or 0
                for f in required_invoices if f["id"] in manual_required_ids
            ), 2),
            "manual_override_invoice_count": len(manual_required_ids),
            "payment_total": payment_total,
            "refund_total": refund_total,
            "net_total": net_total,
            "required_net_total": net_total,
            "required_difference": required_difference,
            "difference": required_difference,
            "matched_invoice_count": len(matched_ids),
            "invoice_count": len(invoices),
            "matched_credential_count": sum(1 for f in credentials if f.get("matched_invoice_id")),
            "credential_count": len(credentials),
            "unmatched_invoice_count": sum(1 for f in invoices if f["id"] not in matched_ids),
            "unmatched_credential_count": sum(1 for f in credentials if not f.get("matched_invoice_id")),
        }

    def batches(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._batch(row) for row in self.db.execute("SELECT * FROM batches ORDER BY created_at DESC")]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = self._batch(row)
            batch["files"] = [self._file(f) for f in self.db.execute("SELECT * FROM files WHERE batch_id=? ORDER BY created_at", (batch_id,))]
            batch["summary"] = self.summarize_batch(batch)
            return batch

    def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        config = {
            "buyer_name": payload.get("buyer_name") or "北师香港浸会大学",
            "buyer_tax_id": payload.get("buyer_tax_id") or "521000015230002458",
            "allowed_year": int(payload.get("allowed_year") or dt.date.today().year),
            "purchase_threshold": parse_money(payload.get("purchase_threshold")) or 150.0,
            "amount_tolerance": parse_money(payload.get("amount_tolerance")) or 0.01,
        }
        name = safe_name(payload.get("name") or f"发票整理_{batch_id}")
        with self.lock:
            self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)", (batch_id, name, json_dumps(config), "PROCESSING", now_iso(), None))
            self.db.commit()
        (BATCH_ROOT / batch_id / "uploads").mkdir(parents=True, exist_ok=True)
        return self.get_batch(batch_id)  # type: ignore[return-value]

    def upload(self, batch_id: str, category: str, filename: str, content: bytes, defer_validation: bool = False) -> dict[str, Any]:
        if category not in UPLOAD_CATEGORIES:
            raise ValueError("当前只支持上传：发票、购买凭证")
        ext = Path(filename).suffix.casefold()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型：{ext or '无扩展名'}")
        batch = self.get_batch(batch_id)
        if not batch:
            raise KeyError("批次不存在")
        file_id = uuid.uuid4().hex
        original_name = safe_name(Path(filename).name, "上传文件")
        stored_name = f"{file_id}{ext}"
        path = BATCH_ROOT / batch_id / "uploads" / stored_name
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock:
            duplicate = self.db.execute("SELECT id FROM files WHERE batch_id=? AND sha256=?", (batch_id, digest)).fetchone()
            if duplicate:
                # 同批次重复上传时在识别前直接删除本次副本，避免重复 OCR/解析。
                path.unlink(missing_ok=True)
                existing = self.db.execute("SELECT * FROM files WHERE id=?", (duplicate["id"],)).fetchone()
                result = self._file(existing) if existing else {"id": duplicate["id"], "batch_id": batch_id}
                result["duplicate_removed"] = True
                result["message"] = "重复文件已直接删除，保留首次上传文件"
                return result
        fields, method = extract_fields(path, category)
        status = "PASS" if category != "invoice" and method not in {"待OCR/人工录入", "人工录入"} else ("NEEDS_REVIEW" if category == "invoice" else "PASS")
        errors: list[str] = []
        if category == "invoice" and "缺少语言包" in method:
            errors.append(method)
        if category == "invoice" and not fields.get("invoice_amount"):
            status = "NEEDS_REVIEW"
            errors.append("无法识别发票金额，请人工填写")
        if category == "purchase" and fields.get("order_amount") is None and fields.get("payment_amount") is None:
            status = "NEEDS_REVIEW"
            errors.append("无法识别购买凭证金额，请人工确认")
        if category == "payment" and fields.get("payment_amount") is None:
            status = "NEEDS_REVIEW"
            errors.append("无法识别付款金额，请人工填写")
        if category == "refund" and fields.get("refund_amount") is None:
            status = "NEEDS_REVIEW"
            errors.append("无法识别退款金额，请人工填写")
        with self.lock:
            self.db.execute(
                "INSERT INTO files (id,batch_id,original_name,stored_name,category,file_path,sha256,size,recognition_method,fields_json,status,errors_json,review_note,archived_name,created_at,matched_invoice_id,match_excluded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, batch_id, original_name, stored_name, category, str(path), digest, len(content), method, json_dumps(fields), status, json_dumps(errors), "", None, now_iso(), None, 0),
            )
            self.db.commit()
        # 批量上传时由前端在全部文件完成后统一匹配和核验，避免每个文件
        # 都重复扫描整个批次。直接调用接口时仍保持原来的即时核验行为。
        if not defer_validation:
            self.auto_match_by_amount(batch_id)
            self.validate(batch_id)
        return self.get_file(file_id)

    def delete_batch(self, batch_id: str) -> None:
        """删除指定批次、其文件和归档包，不影响其他批次。"""
        batch = self.get_batch(batch_id)
        if not batch:
            raise KeyError("批次不存在")
        batch_dir = BATCH_ROOT / batch_id
        archive_dir = ARCHIVE_ROOT / f"{safe_name(batch['name'])}_{batch_id}"
        archive_path = Path(batch["archive_path"]) if batch.get("archive_path") else None
        with self.lock:
            self.db.execute("DELETE FROM files WHERE batch_id=?", (batch_id,))
            self.db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            self.db.commit()
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        if archive_path and archive_path.is_file() and archive_path.parent.resolve() == ARCHIVE_ROOT.resolve():
            archive_path.unlink()

    def delete_file(self, file_id: str) -> dict[str, Any]:
        """删除单个发票或购买凭证，并重新核验所在批次。"""
        file = self.get_file(file_id)
        if not file:
            raise KeyError("文件不存在")
        batch = self.get_batch(file["batch_id"])
        if not batch:
            raise KeyError("批次不存在")
        file_path = Path(file["file_path"])
        archive_path = Path(batch["archive_path"]) if batch.get("archive_path") else None
        archive_dir = ARCHIVE_ROOT / f"{safe_name(batch['name'])}_{file['batch_id']}"
        with self.lock:
            # 删除发票时保留凭证，但解除其关联并禁止本次核验自动猜测新发票。
            if file["category"] == "invoice":
                self.db.execute(
                    "UPDATE files SET matched_invoice_id=NULL, match_excluded=1 WHERE batch_id=? AND matched_invoice_id=?",
                    (file["batch_id"], file_id),
                )
            self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
            # 原归档包已不再代表当前批次，必须失效。
            self.db.execute("UPDATE batches SET status=?, archive_path=NULL WHERE id=?", ("PROCESSING", file["batch_id"]))
            self.db.commit()
        try:
            safe_file_path = file_path.resolve()
            if safe_file_path.is_relative_to(BATCH_ROOT.resolve()) and safe_file_path.is_file():
                safe_file_path.unlink()
        except (OSError, ValueError):
            pass
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        if archive_path:
            try:
                if archive_path.resolve().parent == ARCHIVE_ROOT.resolve() and archive_path.is_file():
                    archive_path.unlink()
            except (OSError, ValueError):
                pass
        self.auto_match_by_amount(file["batch_id"])
        self.validate(file["batch_id"])
        return self.get_batch(file["batch_id"])  # type: ignore[return-value]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            return self._file(row) if row else None

    def match_file(self, file_id: str, invoice_id: str | None) -> dict[str, Any]:
        """将一份凭证匹配到一张发票；传空 invoice_id 则解除匹配。"""
        file = self.get_file(file_id)
        if not file:
            raise KeyError("文件不存在")
        if file["category"] == "invoice":
            raise ValueError("发票不能作为凭证进行匹配")
        if invoice_id:
            invoice = self.get_file(invoice_id)
            if not invoice or invoice["batch_id"] != file["batch_id"] or invoice["category"] != "invoice":
                raise ValueError("匹配目标必须是同一批次中的发票")
        else:
            invoice = None
        fields = dict(file["fields"])
        errors = list(file["errors"])
        status = file["status"]
        if invoice and file["status"] != "REJECTED":
            # 拖拽属于明确的人工确认：用户已经确认这份凭证归属于该发票，
            # 因此凭证默认审核通过，并作为人工覆盖项从自动差额阻断中排除。
            fields["match_mode"] = "manual"
            fields["match_confirmed_at"] = now_iso()
            status = "PASS"
            errors = []
            invoice_amount = parse_money(invoice["fields"].get("invoice_amount"))
            if file["category"] in {"purchase", "payment"} and invoice_amount is not None:
                if "payment_amount_before_manual_match" not in fields:
                    fields["payment_amount_before_manual_match"] = fields.get("payment_amount")
                    fields["payment_amount_source_before_manual_match"] = fields.get("payment_amount_source")
                fields["payment_amount"] = invoice_amount
                fields["payment_amount_source"] = "人工匹配补全（发票金额）"
                fields["payment_amount_manual_override"] = True
        elif not invoice:
            fields.pop("match_mode", None)
            fields.pop("match_confirmed_at", None)
            if fields.pop("payment_amount_manual_override", False):
                fields["payment_amount"] = fields.pop("payment_amount_before_manual_match", None)
                previous_source = fields.pop("payment_amount_source_before_manual_match", None)
                if previous_source:
                    fields["payment_amount_source"] = previous_source
                else:
                    fields.pop("payment_amount_source", None)
        if invoice and file["category"] in {"purchase", "payment"} and self.credential_amount(file) is None:
            invoice_amount = parse_money(invoice["fields"].get("invoice_amount"))
            if invoice_amount is not None:
                fields["payment_amount"] = invoice_amount
                fields["payment_amount_ocr"] = None
                fields["payment_amount_ocr_raw"] = None
                fields["payment_amount_source"] = "人工匹配补全（发票金额）"
                errors = []
                status = "PASS"
        with self.lock:
            self.db.execute("UPDATE files SET matched_invoice_id=?, match_excluded=?, fields_json=?, status=?, errors_json=? WHERE id=?", (invoice_id or None, 0 if invoice_id else 1, json_dumps(fields), status, json_dumps(errors), file_id))
            self.db.commit()
        self.validate(file["batch_id"])
        return self.get_batch(file["batch_id"])  # type: ignore[return-value]

    @staticmethod
    def credential_amount(file: dict[str, Any]) -> float | None:
        fields = file["fields"]
        for key in ("payment_amount", "order_amount", "refund_amount"):
            value = parse_money(fields.get(key))
            if value is not None:
                return value
        return None

    def auto_match_by_amount(self, batch_id: str) -> list[dict[str, str]]:
        """识别完成后，金额唯一对应时自动建立凭证-发票关系。"""
        batch = self.get_batch(batch_id)
        if not batch:
            raise KeyError("批次不存在")
        tolerance = float(batch["config"].get("amount_tolerance", 0.01))
        invoices = [
            f for f in batch["files"]
            if f["category"] == "invoice" and f["status"] != "REJECTED" and parse_money(f["fields"].get("invoice_amount")) is not None
        ]
        credentials = [
            f for f in batch["files"]
            if f["category"] in {"purchase", "payment", "refund"} and not f.get("matched_invoice_id") and not f.get("match_excluded") and f["status"] != "REJECTED"
        ]
        matches: list[dict[str, str]] = []
        used_credentials: set[str] = set()
        for invoice in invoices:
            invoice_amount = parse_money(invoice["fields"].get("invoice_amount"))
            if invoice_amount is None:
                continue
            available = [c for c in credentials if c["id"] not in used_credentials and self.credential_amount(c) is not None]
            combinations: list[tuple[dict[str, Any], ...]] = []
            for size in range(1, min(5, len(available)) + 1):
                for combo in itertools.combinations(available, size):
                    total = round(sum(self.credential_amount(c) or 0 for c in combo), 2)
                    if abs(total - invoice_amount) <= tolerance:
                        combinations.append(combo)
                if combinations:
                    break
            # 只有金额组合唯一时自动匹配；多种组合都能覆盖时交给用户选择。
            if len(combinations) != 1:
                continue
            for credential in combinations[0]:
                with self.lock:
                    self.db.execute("UPDATE files SET matched_invoice_id=?, match_excluded=0 WHERE id=?", (invoice["id"], credential["id"]))
                    self.db.commit()
                used_credentials.add(credential["id"])
                matches.append({"credential_id": credential["id"], "invoice_id": invoice["id"]})
        return matches

    def validate(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if not batch:
            raise KeyError("批次不存在")
        # 重新核验前重跑一次金额自动匹配，覆盖人工补录金额和上传顺序变化。
        self.auto_match_by_amount(batch_id)
        batch = self.get_batch(batch_id)
        config = batch["config"]
        files = batch["files"]
        invoice_numbers: dict[str, int] = {}
        for file in files:
            if file["category"] != "invoice" or file["status"] == "REJECTED":
                continue
            number = (file["fields"].get("invoice_number") or "").strip()
            if number:
                invoice_numbers[number] = invoice_numbers.get(number, 0) + 1
        total_invoice = 0.0
        purchase_threshold = float(config["purchase_threshold"])
        purchase_required_error = f"单张发票金额超过{purchase_threshold:.2f}元，需要购买凭证"
        for file in files:
            if file["status"] == "REJECTED":
                continue
            fields = file["fields"]
            errors = [
                e for e in file["errors"]
                if e not in {"购买方名称不一致", "购买方税号不一致", "开票日期不是允许年份", "重复发票号码"}
                and not is_purchase_requirement_error(e)
            ]
            status = file["status"]
            if file["category"] == "invoice":
                amount = parse_money(fields.get("invoice_amount"))
                if amount is None:
                    status, errors = "NEEDS_REVIEW", errors + ["无法识别发票金额，请人工填写"]
                else:
                    total_invoice += amount
                if fields.get("buyer_name") and normalize_text(fields["buyer_name"]) != normalize_text(config["buyer_name"]):
                    status, errors = "REJECTED", errors + ["购买方名称不一致"]
                elif not fields.get("buyer_name"):
                    status, errors = "NEEDS_REVIEW", errors + ["无法识别购买方名称"]
                if fields.get("buyer_tax_id") and normalize_text(fields["buyer_tax_id"]) != normalize_text(config["buyer_tax_id"]):
                    status, errors = "REJECTED", errors + ["购买方税号不一致"]
                elif not fields.get("buyer_tax_id"):
                    status, errors = "NEEDS_REVIEW", errors + ["无法识别购买方税号"]
                date_value = parse_date(fields.get("invoice_date"))
                if date_value and int(date_value[:4]) != int(config["allowed_year"]):
                    status, errors = "REJECTED", errors + ["开票日期不是允许年份"]
                elif not date_value:
                    status, errors = "NEEDS_REVIEW", errors + ["无法确认开票日期"]
                if fields.get("invoice_number") and invoice_numbers.get(fields["invoice_number"], 0) > 1:
                    status, errors = "REJECTED", errors + ["重复发票号码"]
                if int(fields.get("order_count") or 0) > 1:
                    status, errors = "MULTI_ORDER_REVIEW", errors + ["一张发票对应多个订单，需要人工确认"]
                if not errors and status != "MULTI_ORDER_REVIEW":
                    status = "PASS"
            errors = list(dict.fromkeys(errors))
            with self.lock:
                self.db.execute("UPDATE files SET status=?, errors_json=? WHERE id=?", (status, json_dumps(errors), file["id"]))
        # 购买凭证按“单张发票”判断，不按批次发票总额判断。
        # 例如 150 + 150 = 300 元，但两张都不超过 150 元，不触发购买凭证要求。
        refreshed_files = self.get_batch(batch_id)["files"]  # type: ignore[index]
        qualifying_invoices = [
            f for f in refreshed_files
            if f["category"] == "invoice"
            and f["status"] != "REJECTED"
            and requires_purchase_credential(parse_money(f["fields"].get("invoice_amount")), purchase_threshold)
        ]
        qualifying_invoice_ids = {f["id"] for f in qualifying_invoices}
        manual_invoice_ids = {
            f.get("matched_invoice_id")
            for f in refreshed_files
            if f["category"] in {"purchase", "payment", "refund"}
            and f["status"] != "REJECTED"
            and f.get("matched_invoice_id") in qualifying_invoice_ids
            and f["fields"].get("match_mode") == "manual"
        }
        manual_invoice_ids.discard(None)
        checked_invoice_ids = qualifying_invoice_ids - manual_invoice_ids
        checked_invoice_total = sum(
            parse_money(f["fields"].get("invoice_amount")) or 0
            for f in qualifying_invoices if f["id"] in checked_invoice_ids
        )
        matched_purchase_invoice_ids = {
            f.get("matched_invoice_id")
            for f in refreshed_files
            if f["category"] == "purchase" and f["status"] != "REJECTED" and f.get("matched_invoice_id")
        }
        for file in qualifying_invoices:
            if file["id"] not in matched_purchase_invoice_ids:
                errors = list(dict.fromkeys(file["errors"] + [purchase_required_error]))
                with self.lock:
                    self.db.execute("UPDATE files SET status=?, errors_json=? WHERE id=?", ("INCOMPLETE", json_dumps(errors), file["id"]))
        blocking = [f for f in self.get_batch(batch_id)["files"] if f["status"] in STATUS_BLOCKING]  # type: ignore[index]
        required_credentials = [
            f for f in refreshed_files
            if f["category"] in {"purchase", "payment", "refund"}
            and f["status"] != "REJECTED"
            and f.get("matched_invoice_id") in checked_invoice_ids
        ]
        payment_total = sum(
            parse_money(f["fields"].get("payment_amount")) or 0
            for f in required_credentials
        )
        refund_total = sum(
            parse_money(f["fields"].get("refund_amount")) or 0
            for f in required_credentials
        )
        if required_credentials and checked_invoice_ids and abs((payment_total - refund_total) - checked_invoice_total) > float(config["amount_tolerance"]):
            blocking.append({"status": "NEEDS_REVIEW"})
        has_incomplete = any(item.get("status") == "INCOMPLETE" for item in blocking)
        batch_status = "INCOMPLETE" if has_incomplete else ("NEEDS_REVIEW" if blocking else ("PASS" if total_invoice or not files else "NEEDS_REVIEW"))
        with self.lock:
            self.db.execute("UPDATE batches SET status=? WHERE id=?", (batch_status, batch_id))
            self.db.commit()
        return self.get_batch(batch_id)  # type: ignore[return-value]

    def review(self, file_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        file = self.get_file(file_id)
        if not file:
            raise KeyError("文件不存在")
        fields = dict(file["fields"])
        payment_amount_changed = "payment_amount" in payload and parse_money(payload.get("payment_amount")) != parse_money(fields.get("payment_amount"))
        for key in fields:
            if key in payload:
                fields[key] = parse_money(payload[key]) if key.endswith("amount") else payload[key]
        if "payment_amount" in payload:
            fields["payment_amount_source"] = "人工审核"
        if fields.get("invoice_date"):
            fields["invoice_date"] = parse_date(str(fields["invoice_date"])) or fields["invoice_date"]
        status = payload.get("status") or file["status"]
        note = str(payload.get("review_note") or file["review_note"])
        errors = [] if payload.get("status") == "PASS" else list(file["errors"])
        if status == "PASS" and file["category"] == "invoice" and not fields.get("invoice_amount"):
            status = "NEEDS_REVIEW"
            errors.append("无法识别发票金额，请人工填写")
        if status == "PASS" and file["category"] in {"purchase", "payment", "refund"}:
            has_amount = any(parse_money(fields.get(key)) is not None for key in ("payment_amount", "order_amount", "refund_amount"))
            if not has_amount:
                status = "NEEDS_REVIEW"
                errors.append("无法识别凭证金额，请人工填写或匹配发票补全")
        with self.lock:
            if payment_amount_changed:
                fields.pop("match_mode", None)
                fields.pop("match_confirmed_at", None)
                fields.pop("payment_amount_manual_override", None)
                fields.pop("payment_amount_before_manual_match", None)
                fields.pop("payment_amount_source_before_manual_match", None)
                self.db.execute("UPDATE files SET fields_json=?, status=?, errors_json=?, review_note=?, matched_invoice_id=NULL, match_excluded=0 WHERE id=?", (json_dumps(fields), status, json_dumps(errors), note, file_id))
            else:
                self.db.execute("UPDATE files SET fields_json=?, status=?, errors_json=?, review_note=? WHERE id=?", (json_dumps(fields), status, json_dumps(errors), note, file_id))
            self.db.commit()
        self.auto_match_by_amount(file["batch_id"])
        self.validate(file["batch_id"])
        return self.get_file(file_id)

    def archive(self, batch_id: str) -> Path:
        batch = self.validate(batch_id)
        if batch["status"] not in {"PASS", "REJECTED"}:
            blockers = [
                f"{file['original_name']}：{file['status']}（{'；'.join(file['errors']) or '状态未完成'}）"
                for file in batch.get("files", [])
                if file["status"] in STATUS_BLOCKING
            ]
            detail = "；".join(blockers[:5])
            if len(blockers) > 5:
                detail += f"；另有 {len(blockers) - 5} 个文件"
            if not detail and batch["status"] == "NEEDS_REVIEW":
                summary = batch.get("summary", {})
                required_total = float(summary.get("checked_required_invoice_total", summary.get("required_invoice_total", 0)) or 0)
                payment_total = float(summary.get("payment_total", 0) or 0)
                refund_total = float(summary.get("refund_total", 0) or 0)
                if required_total and (payment_total or refund_total) and abs((payment_total - refund_total) - required_total) > float(batch["config"].get("amount_tolerance", 0.01)):
                    detail = f"仍需自动核对的发票差额：付款 {payment_total:.2f} - 退款 {refund_total:.2f} ≠ 未人工确认发票 {required_total:.2f}"
            if not detail and batch["status"] == "INCOMPLETE":
                files = batch.get("files", [])
                missing = [
                    f["original_name"] for f in files
                    if f["category"] == "invoice"
                    and f["status"] != "REJECTED"
                    and requires_purchase_credential(
                        parse_money(f["fields"].get("invoice_amount")),
                        float(batch["config"].get("purchase_threshold", 150)),
                    )
                    and not any(c["category"] == "purchase" and c.get("matched_invoice_id") == f["id"] for c in files)
                ]
                if missing:
                    detail = "以下单张高额发票没有匹配购买凭证：" + "、".join(missing[:8])
            raise ValueError(f"仍有文件需要审核、补充材料或确认，暂不能打包。批次状态：{batch['status']}。{detail}")
        files = self.get_batch(batch_id)["files"]  # type: ignore[index]
        summary = self.get_batch(batch_id)["summary"]  # type: ignore[index]
        archive_dir = ARCHIVE_ROOT / f"{safe_name(batch['name'])}_{batch_id}"
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        for folder in ["01_发票", "02_购买凭证", "03_付款凭证", "04_退款凭证", "05_其他材料", "99_拒收"]:
            (archive_dir / folder).mkdir(parents=True, exist_ok=True)
        used: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        folder_by_category = {"invoice": "01_发票", "purchase": "02_购买凭证", "payment": "03_付款凭证", "refund": "04_退款凭证", "other": "05_其他材料"}
        purchase_groups: dict[str, list[dict[str, Any]]] = {}
        for item in files:
            if item["category"] == "purchase" and item.get("matched_invoice_id"):
                purchase_groups.setdefault(item["matched_invoice_id"], []).append(item)
        purchase_positions = {
            item["id"]: (index, len(group))
            for group in purchase_groups.values()
            for index, item in enumerate(group, 1)
        }
        for file in files:
            fields = file["fields"]
            ext = Path(file["original_name"]).suffix.casefold()
            if file["status"] == "REJECTED":
                filename = safe_name(file["original_name"])
                folder = "99_拒收"
            elif file["category"] == "invoice":
                amount = money_text(fields.get("invoice_amount")) or "金额未知"
                if (parse_money(fields.get("invoice_amount")) or 0) < 0 and not amount.startswith("-"):
                    amount = "-" + amount
                number = str(fields.get("invoice_number") or "编号未知")[-4:]
                filename = f"{amount}_{number}{ext}"
                folder = folder_by_category[file["category"]]
            elif file["category"] == "purchase" and file.get("matched_invoice_id"):
                matched_invoice = next((item for item in files if item["id"] == file["matched_invoice_id"]), None)
                amount = money_text(matched_invoice["fields"].get("invoice_amount")) if matched_invoice else "金额未知"
                position, group_size = purchase_positions.get(file["id"], (1, 1))
                sequence = f"-{position}" if group_size > 1 else ""
                filename = f"{amount or '金额未知'}{sequence}{ext}"
                folder = folder_by_category[file["category"]]
            else:
                amount = money_text(fields.get("payment_amount") or fields.get("order_amount") or fields.get("refund_amount"))
                token = fields.get("order_number") or fields.get("transaction_number") or Path(file["original_name"]).stem
                prefix = {"purchase": "购买凭证", "payment": "付款凭证", "refund": "退款凭证", "other": "其他材料"}[file["category"]]
                filename = f"{prefix}_{amount + '_' if amount else ''}{safe_name(str(token))}{ext}"
                folder = folder_by_category[file["category"]]
            key = f"{folder}/{filename}"
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                filename = f"{Path(filename).stem}_{used[key]}{Path(filename).suffix}"
            target = archive_dir / folder / filename
            shutil.copy2(file["file_path"], target)
            with self.lock:
                self.db.execute("UPDATE files SET archived_name=? WHERE id=?", (filename, file["id"]))
            rows.append({
                "原文件名": file["original_name"], "归档文件名": f"{folder}/{filename}", "文件类型": file["category"],
                "匹配发票": next((item["original_name"] for item in files if item["id"] == file.get("matched_invoice_id")), ""),
                "文件大小": file["size"], "文件哈希": file["sha256"], "识别方式": file["recognition_method"],
                "发票金额": fields.get("invoice_amount"), "发票号码": fields.get("invoice_number"),
                "购买方名称": fields.get("buyer_name"), "购买方税号": fields.get("buyer_tax_id"),
                "开票日期": fields.get("invoice_date"), "实际付款金额": fields.get("payment_amount"),
                "OCR付款金额": fields.get("payment_amount_ocr"), "OCR原始付款金额": fields.get("payment_amount_ocr_raw"),
                "付款金额来源": fields.get("payment_amount_source"),
                "订单金额": fields.get("order_amount"), "优惠金额": fields.get("discount_amount"),
                "退款金额": fields.get("refund_amount"), "订单数量": fields.get("order_count"),
                "批次有效发票总额": summary.get("invoice_total"), "需购买凭证发票总额": summary.get("required_invoice_total"),
                "无需购买凭证发票金额": summary.get("no_purchase_invoice_total"), "批次已匹配发票总额": summary.get("matched_invoice_total"),
                "需凭证发票已匹配金额": summary.get("required_matched_invoice_total"), "人工确认覆盖发票金额": summary.get("manual_override_invoice_total"), "批次凭证净额": summary.get("net_total"),
                "需凭证发票核对差额": summary.get("required_difference"),
                "处理状态": file["status"], "异常说明": "；".join(file["errors"]), "人工审核备注": file["review_note"], "上传时间": file["created_at"],
            })
        headers = list(rows[0].keys()) if rows else ["原文件名", "归档文件名"]
        csv_path = archive_dir / "汇总.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader(); writer.writerows(rows)
        summary_path = archive_dir / "00_金额汇总.txt"
        summary_path.write_text(
            "\n".join([
                f"有效发票总额：{summary.get('invoice_total', 0):.2f}",
                f"需购买凭证发票总额：{summary.get('required_invoice_total', 0):.2f}",
                f"无需购买凭证发票金额：{summary.get('no_purchase_invoice_total', 0):.2f}",
                f"已匹配发票总额：{summary.get('matched_invoice_total', 0):.2f}",
                f"需凭证发票已匹配金额：{summary.get('required_matched_invoice_total', 0):.2f}",
                f"人工确认覆盖发票金额：{summary.get('manual_override_invoice_total', 0):.2f}",
                f"已匹配凭证付款合计：{summary.get('payment_total', 0):.2f}",
                f"已匹配退款合计：{summary.get('refund_total', 0):.2f}",
                f"凭证净额：{summary.get('net_total', 0):.2f}",
                f"需凭证发票核对差额：{summary.get('required_difference', 0):.2f}",
                f"发票匹配：{summary.get('matched_invoice_count', 0)}/{summary.get('invoice_count', 0)}",
                f"凭证匹配：{summary.get('matched_credential_count', 0)}/{summary.get('credential_count', 0)}",
            ]) + "\n",
            encoding="utf-8",
        )
        create_xlsx(archive_dir / "00_核销汇总.xlsx", headers, rows, summary)
        zip_path = ARCHIVE_ROOT / f"{safe_name(batch['name'])}_{batch_id}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in archive_dir.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(archive_dir.parent))
        with self.lock:
            self.db.execute("UPDATE batches SET status=?, archive_path=? WHERE id=?", ("ARCHIVED", str(zip_path), batch_id))
            self.db.commit()
        return zip_path


def xlsx_col(n: int) -> str:
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def xml_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def create_xlsx(path: Path, headers: list[str], rows: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> None:
    sheet_rows: list[list[Any]] = []
    if summary:
        sheet_rows.extend([
            ["核销金额汇总", "金额"],
            ["有效发票总额", summary.get("invoice_total")],
            ["需购买凭证发票总额", summary.get("required_invoice_total")],
            ["无需购买凭证发票金额", summary.get("no_purchase_invoice_total")],
            ["已匹配发票总额", summary.get("matched_invoice_total")],
            ["需凭证发票已匹配金额", summary.get("required_matched_invoice_total")],
            ["人工确认覆盖发票金额", summary.get("manual_override_invoice_total")],
            ["已匹配凭证付款合计", summary.get("payment_total")],
            ["已匹配退款合计", summary.get("refund_total")],
            ["凭证净额", summary.get("net_total")],
            ["需凭证发票核对差额", summary.get("required_difference")],
            [],
        ])
    sheet_rows += [headers] + [[row.get(header, "") for header in headers] for row in rows]
    sheet = ["<?xml version='1.0' encoding='UTF-8' standalone='yes'?>", "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"]
    for r_idx, values in enumerate(sheet_rows, 1):
        sheet.append(f"<row r='{r_idx}'>")
        for c_idx, value in enumerate(values, 1):
            cell = f"{xlsx_col(c_idx)}{r_idx}"
            sheet.append(f"<c r='{cell}' t='inlineStr'><is><t>{xml_escape(value)}</t></is></c>")
        sheet.append("</row>")
    sheet.append("</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/xl/workbook.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'/><Override PartName='/xl/worksheets/sheet1.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'/></Types>")
        zf.writestr("_rels/.rels", "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='xl/workbook.xml'/></Relationships>")
        zf.writestr("xl/workbook.xml", "<?xml version='1.0' encoding='UTF-8'?><workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'><sheets><sheet name='核销汇总' sheetId='1' r:id='rId1'/></sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels", "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' Target='worksheets/sheet1.xml'/></Relationships>")
        zf.writestr("xl/worksheets/sheet1.xml", "".join(sheet))


MATCH_CSS = """
.progress-panel{margin:12px 0 16px;padding:14px 16px;border:1px solid #dbeafe;border-radius:12px;background:linear-gradient(120deg,#f8fbff,#eff6ff)}.progress-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.progress-title{font-weight:700}.progress-percent{font-size:18px;font-weight:800;color:#2563eb}.progress-track{height:9px;background:#dbeafe;border-radius:99px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#2563eb,#22c55e);border-radius:99px;transition:width .25s ease}.progress-stats{display:flex;gap:10px;flex-wrap:wrap;color:#64748b;font-size:12px}.progress-stats span{background:#fff;border:1px solid #e2e8f0;border-radius:99px;padding:4px 8px}.match-workbench{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:14px;margin:16px 0}.match-board,.credential-dock{border:1px solid #e2e8f0;border-radius:14px;padding:14px;background:#f8fafc}.match-board h3,.credential-dock h3{margin:0;font-size:15px}.match-subtitle{font-size:12px;color:#64748b;margin:5px 0 12px}.invoice-columns{display:grid;grid-template-columns:1fr 1fr;gap:12px}.invoice-column{min-width:0}.column-title{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700;color:#475569;margin-bottom:6px}.column-count{font-weight:500;color:#64748b}.invoice-card{background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:12px;margin:8px 0}.invoice-card.drop-target{border:2px dashed #60a5fa;background:#eff6ff}.invoice-head{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.invoice-name{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.preview-name{border:0;background:transparent;color:#1d4ed8;cursor:pointer;font:inherit;padding:0;text-align:left}.credential-chip{display:flex;justify-content:space-between;gap:8px;align-items:center;background:#f1f5f9;border-radius:8px;padding:8px;margin-top:6px;font-size:12px}.credential-chip[draggable=true]{cursor:grab}.credential-chip[draggable=true]:hover{background:#dbeafe}.credential-chip.selected{background:#dbeafe;border-color:#2563eb;box-shadow:0 0 0 1px #2563eb33}.credential-check{width:16px!important;min-width:16px;margin:0 7px 0 0!important}.credential-chip .credential-main{min-width:0;flex:1}.credential-chip strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.credential-chip small{display:block;color:#64748b;margin-top:3px}.credential-dock .credential-chip{background:#fff;border:1px solid #dbeafe}.empty-match{color:#64748b;font-size:12px;padding:16px 4px;text-align:center}.match-hint{font-size:12px;color:#64748b;margin:8px 0 0}.unlink{border:0;background:transparent;color:#2563eb;cursor:pointer;font-size:12px;white-space:nowrap}.drop-hover{background:#dbeafe!important;border-color:#2563eb!important}.dock-hint{padding:9px;background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:8px;font-size:12px;margin-bottom:10px}.selection-hint{font-size:12px;color:#1d4ed8;margin:0 0 10px}.preview-backdrop{position:fixed;inset:0;background:#0f172acc;display:flex;align-items:center;justify-content:center;padding:24px;z-index:50}.preview-box{position:relative;width:min(1100px,96vw);height:min(820px,92vh);background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px #0008}.preview-box img{display:block;width:100%;height:100%;object-fit:contain;background:#111}.preview-box iframe{border:0;width:100%;height:100%}.preview-close{position:absolute;right:12px;top:10px;width:34px;height:34px;border:0;border-radius:50%;background:#0f172acc;color:#fff;font-size:22px;cursor:pointer;z-index:2}.preview-text{height:100%;overflow:auto;padding:28px;white-space:pre-wrap;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}.batch-list{max-height:calc(100vh - 330px);overflow:auto;padding-right:4px}.right>#workspace{max-height:calc(100vh - 76px);overflow:auto}.match-board,.credential-dock{max-height:calc(100vh - 270px);overflow:auto}.credential-dock{position:sticky;top:8px}.invoice-column{max-height:calc(100vh - 390px);overflow:auto;padding-right:4px}.files-scroll{max-height:520px;overflow:auto;border:1px solid #e2e8f0;border-radius:10px}.files-scroll table{min-width:980px}.files-scroll thead th{position:sticky;top:0;background:#f8fafc;z-index:1}.progress-panel,.match-board,.credential-dock,.files-scroll,.batch-list{scrollbar-width:thin;scrollbar-color:#94a3b8 transparent}.progress-panel::-webkit-scrollbar,.match-board::-webkit-scrollbar,.credential-dock::-webkit-scrollbar,.files-scroll::-webkit-scrollbar,.batch-list::-webkit-scrollbar{width:8px;height:8px}.progress-panel::-webkit-scrollbar-thumb,.match-board::-webkit-scrollbar-thumb,.credential-dock::-webkit-scrollbar-thumb,.files-scroll::-webkit-scrollbar-thumb,.batch-list::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:99px}@media(max-width:1050px){.match-workbench{grid-template-columns:1fr}.invoice-columns{grid-template-columns:1fr}.right>#workspace{max-height:none}.match-board,.credential-dock{max-height:70vh;position:static}}
.credential-upload-panel{margin-top:10px;padding:9px 12px;border:1px solid #dbeafe;border-radius:12px;background:#f8fbff;max-height:210px;overflow-y:auto}.credential-upload-title{font-weight:700;font-size:13px}.credential-upload-subtitle{font-size:11px;color:#64748b;margin:3px 0 6px}.upload-job{display:grid;grid-template-columns:minmax(120px,1fr) 110px 92px;gap:7px;align-items:center;padding:5px 0;border-top:1px solid #e2e8f0;font-size:11px}.upload-job-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.upload-job-track{height:6px;background:#e2e8f0;border-radius:99px;overflow:hidden}.upload-job-fill{height:100%;background:#2563eb;border-radius:99px;transition:width .2s ease}.upload-job-fill.indeterminate{width:38%!important;background:linear-gradient(90deg,#2563eb,#60a5fa,#2563eb);background-size:200% 100%;animation:deepseek-progress 1.1s linear infinite}.upload-job-status{color:#64748b;text-align:right;white-space:nowrap}.upload-job-status.done{color:#15803d}.upload-job-status.error{color:#b91c1c}.error{max-height:54px;overflow-y:auto;overflow-x:hidden;white-space:normal;word-break:break-word;padding-right:3px}.files th:nth-child(2),.files td:nth-child(2){width:190px;max-width:190px}@keyframes deepseek-progress{from{background-position:100% 0}to{background-position:-100% 0}}
"""

AMOUNT_CSS = ".amount-summary{margin:10px 0 14px;padding:12px 14px;border:1px solid #bbf7d0;border-radius:12px;background:#f0fdf4}.amount-summary-title{font-weight:700;margin-bottom:8px}.amount-summary-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.amount-summary-grid div{background:#fff;border-radius:8px;padding:8px}.amount-summary-grid span{display:block;color:#64748b;font-size:11px}.amount-summary-grid strong{display:block;margin-top:3px;font-size:16px}.amount-ok strong{color:#15803d}.amount-warn strong{color:#b45309}@media(max-width:1050px){.amount-summary-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:800px){.amount-summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}"

ERROR_CSS = ".file-error{max-width:280px}.error-details{margin:2px 0;color:#9f1239;font-size:12px}.error-details summary{display:flex;align-items:center;gap:6px;min-width:0;cursor:pointer;list-style:none;padding:3px 0}.error-details summary::-webkit-details-marker{display:none}.error-details summary:before{content:'›';display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:#ffe4e6;color:#be123c;font-weight:800;transition:transform .15s ease}.error-details[open] summary:before{transform:rotate(90deg)}.error-label{font-weight:700;white-space:nowrap}.error-count{padding:2px 6px;border-radius:99px;background:#ffe4e6;color:#be123c;font-size:11px;white-space:nowrap}.error-preview{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#be123c}.error-list{max-height:110px;overflow:auto;margin:4px 0 2px 22px;padding:6px 8px;border-left:2px solid #fecdd3;background:#fff1f2;color:#9f1239;line-height:1.55;word-break:break-word}.error-list div+div{margin-top:3px}.batch-error-panel{display:flex;align-items:flex-start;gap:10px;margin:-4px 0 12px;padding:9px 11px;border:1px solid #fecdd3;border-radius:10px;background:#fff1f2;color:#9f1239}.batch-error-icon{width:20px;height:20px;flex:none;display:flex;align-items:center;justify-content:center;border-radius:50%;background:#e11d48;color:white;font-size:12px;font-weight:800}.batch-error-content{min-width:0;flex:1}.batch-error-title{font-weight:700;color:#9f1239}.batch-error-panel .error-details{margin:3px 0 0}.batch-error-panel .error-list{max-height:130px;margin-left:0}.error-details.error-success summary{color:#166534}.error-details.error-success summary:before{background:#dcfce7;color:#15803d}.error-details.error-success .error-count{background:#dcfce7;color:#166534}.toast-stack{position:fixed;right:18px;bottom:18px;z-index:80;display:grid;gap:8px;width:min(420px,calc(100vw - 36px))}.toast-error{display:flex;align-items:flex-start;gap:9px;padding:10px 12px;border:1px solid #fecdd3;border-radius:10px;background:#fff1f2;color:#9f1239;box-shadow:0 10px 25px #0f172a22;font-size:12px}.toast-error strong{display:block;margin-bottom:2px}.toast-close{margin-left:auto;border:0;background:transparent;color:#9f1239;cursor:pointer;font-size:17px;line-height:1}"

UI_CSS = """
:root{--ui-blue:#2563eb;--ui-border:#e2e8f0;--ui-muted:#64748b;--ui-surface:#fff}
html{min-height:100%;background:#f5f7fb}
body{min-height:100%;overflow-y:auto;overscroll-behavior-y:none}
button,input,select,textarea{font:inherit}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
.shell{max-width:1520px;padding:20px 24px}
.top{position:relative;z-index:2;padding:2px 0 16px}
.top h1{letter-spacing:-.02em}
.grid{align-items:start;grid-template-columns:300px minmax(0,1fr);gap:16px}
.grid>aside{position:sticky;top:14px;max-height:calc(100vh - 28px);overflow-y:auto;overscroll-behavior:contain;padding-right:3px}
.card{border-color:#dbe3ee;box-shadow:0 8px 28px #1e293b0b}
.form{gap:11px}.form label{font-weight:600}
.form input,.form select,.form textarea{background:#fff;transition:border-color .15s,box-shadow .15s}
.form input:hover,.form select:hover,.form textarea:hover{border-color:#94a3b8}
.form input:focus,.form select:focus,.form textarea:focus{border-color:#60a5fa;box-shadow:0 0 0 3px #dbeafe}
.btn{min-height:34px;transition:transform .12s,box-shadow .12s,background .12s}.btn:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 4px 10px #2563eb26}.btn.secondary:hover:not(:disabled){background:#cbd5e1}
.batch{background:#fff;transition:border-color .15s,background .15s,box-shadow .15s}.batch:hover{border-color:#93c5fd;box-shadow:0 4px 12px #2563eb12}.batch.active{box-shadow:inset 3px 0 0 var(--ui-blue)}
.batch-list{overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
.right>#workspace{max-height:calc(100vh - 34px);overscroll-behavior:contain;-webkit-overflow-scrolling:touch;scroll-padding-top:70px}
.toolbar{position:sticky;top:-18px;z-index:15;margin:-18px -18px 15px;padding:14px 18px;background:#fffffff2;border-bottom:1px solid #e2e8f0;backdrop-filter:blur(10px)}
.toolbar h2{font-size:18px}.toolbar .btn,.toolbar a.btn{white-space:nowrap}
.drop{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(180px,240px);gap:10px;align-items:center;text-align:left;padding:14px 16px}.drop br{display:none}.drop select,.drop input{margin:0}.drop input[type=file]{min-width:0}
.progress-panel,.amount-summary,.match-workbench,.credential-upload-panel{box-shadow:0 2px 8px #1e293b08}.progress-panel,.amount-summary{scroll-margin-top:70px}
.files-scroll{max-height:min(56vh,560px);overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.files tbody tr{transition:background .12s}.files tbody tr:hover{background:#f8fafc}.files td{line-height:1.35}.files td:nth-child(1){min-width:180px}.files td:nth-child(3),.files td:nth-child(4){min-width:170px}.files td input,.files td select,.files td textarea{display:block;width:100%;max-width:none;margin:0 0 6px}.files td textarea{min-height:48px;resize:vertical}.files th{white-space:nowrap}
.files-scroll,.match-board,.credential-dock,.invoice-column,.error-list,.preview-text{scrollbar-width:thin;scrollbar-color:#94a3b8 transparent}.files-scroll::-webkit-scrollbar,.match-board::-webkit-scrollbar,.credential-dock::-webkit-scrollbar,.invoice-column::-webkit-scrollbar,.error-list::-webkit-scrollbar,.preview-text::-webkit-scrollbar{width:9px;height:9px}.files-scroll::-webkit-scrollbar-thumb,.match-board::-webkit-scrollbar-thumb,.credential-dock::-webkit-scrollbar-thumb,.invoice-column::-webkit-scrollbar-thumb,.error-list::-webkit-scrollbar-thumb,.preview-text::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:99px}.files-scroll::-webkit-scrollbar-thumb:hover,.match-board::-webkit-scrollbar-thumb:hover,.credential-dock::-webkit-scrollbar-thumb:hover,.invoice-column::-webkit-scrollbar-thumb:hover,.error-list::-webkit-scrollbar-thumb:hover,.preview-text::-webkit-scrollbar-thumb:hover{background:#94a3b8}
.match-board,.credential-dock{overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.credential-dock{top:14px}.invoice-column{overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.preview-backdrop{overscroll-behavior:contain}
.pill{font-weight:650;letter-spacing:.01em}.pill.PROCESSING{background:#dbeafe;color:#1d4ed8}.pill.INCOMPLETE{background:#ffedd5;color:#9a3412}.pill.NEEDS_REVIEW,.pill.MULTI_ORDER_REVIEW{background:#fef3c7;color:#92400e}
@media(max-width:1050px){.shell{padding:16px}.grid{grid-template-columns:260px minmax(0,1fr)}.drop{grid-template-columns:1fr 1fr}.drop input[type=file]{grid-column:1/-1}.right>#workspace{max-height:calc(100vh - 24px)}}
@media(max-width:900px){.shell{padding:12px}.top{padding-bottom:12px}.grid{grid-template-columns:1fr}.grid>aside{position:static;max-height:none;overflow:visible}.right>#workspace{max-height:none;overflow:visible}.toolbar{top:0;margin:-18px -18px 15px;padding:12px 14px}.drop{grid-template-columns:1fr}.drop input[type=file]{grid-column:auto}.match-workbench{margin-top:12px}.match-board,.credential-dock{max-height:62vh}.invoice-column{max-height:48vh}.files-scroll{max-height:62vh}}
@media(max-width:520px){.top h1{font-size:23px}.top .small{display:none}.toolbar{align-items:stretch}.toolbar>div:first-child{width:100%}.toolbar .btn,.toolbar a.btn{flex:1;text-align:center}.progress-stats{gap:6px}.progress-stats span{padding:3px 6px}.amount-summary-grid{gap:6px}.files-scroll{max-height:65vh}}
"""


MATCH_JS = r"""
let selectedCredentialIds=new Set();
const uploadJobs=new Map();
let uploadRenderQueued=false;
function captureScrollState(){const selectors=['window','.grid>aside','.batch-list','#workspace','.match-board','.credential-dock','.invoice-column','.files-scroll','.error-list'];const state={};for(const selector of selectors){if(selector==='window'){state.window={x:window.scrollX,y:window.scrollY};continue}document.querySelectorAll(selector).forEach((element,index)=>{state[`${selector}:${index}`]={x:element.scrollLeft,y:element.scrollTop}})}return state}
function restoreScrollState(state){if(!state)return;requestAnimationFrame(()=>{for(const [key,position] of Object.entries(state)){if(key==='window'){window.scrollTo(position.x,position.y);continue}const split=key.lastIndexOf(':');const selector=key.slice(0,split);const index=Number(key.slice(split+1));const element=document.querySelectorAll(selector)[index];if(element){element.scrollLeft=position.x;element.scrollTop=position.y}}requestAnimationFrame(()=>{for(const [key,position] of Object.entries(state)){if(key==='window')continue;const split=key.lastIndexOf(':');const selector=key.slice(0,split);const index=Number(key.slice(split+1));const element=document.querySelectorAll(selector)[index];if(element){element.scrollLeft=position.x;element.scrollTop=position.y}}})})}
function showErrorToast(message){let stack=document.querySelector('.toast-stack');if(!stack){stack=document.createElement('div');stack.className='toast-stack';document.body.appendChild(stack)}const toast=document.createElement('div');toast.className='toast-error';toast.innerHTML=`<div><strong>处理失败</strong><div>${esc(message)}</div></div><button class="toast-close" aria-label="关闭">×</button>`;toast.querySelector('.toast-close').onclick=()=>toast.remove();stack.appendChild(toast);setTimeout(()=>toast.remove(),9000)}
function renderCredentialUploadProgress(){const host=$('#credentialUploadProgress');if(!host)return;const jobs=Array.from(uploadJobs.values());if(!jobs.length){host.innerHTML='';return}host.innerHTML=`<div class="credential-upload-panel"><div class="credential-upload-title">凭证上传与 DeepSeek 处理进度</div><div class="credential-upload-subtitle">上传完成后进入图片理解；模型不确定时会转人工复核</div>${jobs.map(job=>`<div class="upload-job"><div class="upload-job-name" title="${esc(job.name)}">${esc(job.name)}</div><div class="upload-job-track"><div class="upload-job-fill ${job.indeterminate?'indeterminate':''}" style="width:${job.progress}%"></div></div><div class="upload-job-status ${job.stage==='失败'?'error':job.stage.includes('完成')?'done':''}">${esc(job.stage)} ${job.stage==='上传中'?job.progress+'%':''}</div></div>`).join('')}</div>`}
function updateUploadJob(id,patch){const old=uploadJobs.get(id)||{};uploadJobs.set(id,{...old,...patch});if(uploadRenderQueued)return;uploadRenderQueued=true;const flush=()=>{uploadRenderQueued=false;renderCredentialUploadProgress()};if(window.requestAnimationFrame)window.requestAnimationFrame(flush);else window.setTimeout(flush,50)}
async function runUploadTasks(files,category,limit=100){let cursor=0;const workers=Array.from({length:Math.min(limit,files.length)},async()=>{while(true){const index=cursor++;if(index>=files.length)return;const file=files[index];try{await uploadCredentialFile(file,category)}catch(error){showErrorToast(file.name+'：'+error.message)}}});await Promise.all(workers)}
function uploadCredentialFile(file,category){const id=crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`;updateUploadJob(id,{name:file.name,progress:0,stage:'上传中',indeterminate:false});return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/batches/'+current.id+'/upload');xhr.setRequestHeader('X-Defer-Validation','1');xhr.upload.onprogress=event=>{if(event.lengthComputable){const progress=Math.round(event.loaded/event.total);updateUploadJob(id,{progress,stage:progress>=100?'DeepSeek 处理中':'上传中',indeterminate:progress>=100})}};xhr.onerror=()=>{updateUploadJob(id,{stage:'失败',progress:0,indeterminate:false});reject(new Error('上传失败'))};xhr.onload=async()=>{if(xhr.status<200||xhr.status>=300){updateUploadJob(id,{stage:'失败',progress:0,indeterminate:false});try{const error=JSON.parse(xhr.responseText);reject(new Error(error.error||'上传失败'))}catch(e){reject(new Error('上传失败'))}return}try{const result=JSON.parse(xhr.responseText);const method=result.recognition_method||'';const stage=result.duplicate_removed?'重复文件已删除':method.includes('DeepSeek')?'DeepSeek 已完成':method.includes('Vision')?'本地 Vision 已完成':'OCR 已完成';updateUploadJob(id,{progress:100,stage,indeterminate:false});resolve(result)}catch(e){updateUploadJob(id,{stage:'失败',progress:0,indeterminate:false});reject(new Error('服务器返回无效'))}};const fd=new FormData();fd.append('category',category);fd.append('file',file);xhr.send(fd)})}
function previewButton(file){return `<button class="preview-name" onclick="event.stopPropagation();previewFile('${file.id}')">${esc(file.original_name)}</button>`}
function credentialLabel(file){const x=file.fields||{};const amount=x.payment_amount??x.order_amount??x.refund_amount;const token=x.order_number||x.transaction_number||'';const source=x.payment_amount_source?`（${esc(x.payment_amount_source)}）`:'';const manual=x.match_mode==='manual'?' · 人工确认':'';return `<div class="credential-main"><strong>${previewButton(file)}</strong><small>${esc(file.category)}${amount!==undefined&&amount!==null?' · 金额 '+esc(amount)+source:''}${token?' · 订单 '+esc(token):''}${manual}</small></div>`}
function invoiceLabel(file){const x=file.fields||{};return `<div class="invoice-head"><div><div class="invoice-name">${previewButton(file)}</div><div class="small">发票金额：${esc(x.invoice_amount??'待补录')} · 发票号码：${esc(x.invoice_number??'待补录')}</div></div>${status(file.status)}</div>`}
function previewFile(id){const file=(current.files||[]).find(item=>item.id===id);if(!file)return;const ext=(file.original_name||'').toLowerCase().split('.').pop();let body='';const url='/api/files/'+id+'/preview';if(ext==='pdf')body=`<iframe src="${url}"></iframe>`;else if(['png','jpg','jpeg','gif','webp'].includes(ext))body=`<img src="${url}" alt="${esc(file.original_name)}">`;else if(['txt','csv'].includes(ext))body=`<iframe src="${url}"></iframe>`;else body=`<div class="preview-text">当前格式不能在页面内预览。<br><br><a href="${url}" target="_blank">打开或下载文件</a></div>`;const modal=document.createElement('div');modal.className='preview-backdrop';modal.innerHTML=`<div class="preview-box"><button class="preview-close" aria-label="关闭">×</button>${body}</div>`;modal.addEventListener('click',event=>{if(event.target===modal||event.target.classList.contains('preview-close'))modal.remove()});document.body.appendChild(modal)}
function toggleCredentialSelection(id,checked){const scrollState=captureScrollState();if(checked)selectedCredentialIds.add(id);else selectedCredentialIds.delete(id);renderMatchPools();restoreScrollState(scrollState)}
function dragCredential(event,id){const ids=selectedCredentialIds.has(id)?Array.from(selectedCredentialIds):[id];event.dataTransfer.setData('application/x-invoice-credentials',JSON.stringify(ids));event.dataTransfer.setData('text/plain',ids.join(','));event.dataTransfer.effectAllowed='move'}
function dragOverInvoice(event){event.preventDefault();event.currentTarget.classList.add('drop-hover')}
function dragLeaveInvoice(event){event.currentTarget.classList.remove('drop-hover')}
async function dropCredential(event,invoiceId){event.preventDefault();event.currentTarget.classList.remove('drop-hover');let ids=[];try{ids=JSON.parse(event.dataTransfer.getData('application/x-invoice-credentials')||'[]')}catch(e){}if(!ids.length){const id=event.dataTransfer.getData('text/plain');if(id)ids=[id]}if(!ids.length)return;await matchCredentials(ids,invoiceId)}
async function matchCredential(credentialId,invoiceId){await api('/api/files/'+credentialId+'/match',{method:'POST',body:JSON.stringify({invoice_id:invoiceId})});await selectBatch(current.id)}
async function matchCredentials(ids,invoiceId){for(const id of ids)await api('/api/files/'+id+'/match',{method:'POST',body:JSON.stringify({invoice_id:invoiceId})});selectedCredentialIds.clear();await selectBatch(current.id)}
async function unmatchCredential(credentialId){await api('/api/files/'+credentialId+'/match',{method:'POST',body:JSON.stringify({invoice_id:null})});await selectBatch(current.id)}
async function deleteFile(id){const file=(current.files||[]).find(item=>item.id===id);if(!file)return;if(!confirm(`确定删除“${file.original_name}”吗？原始文件和关联关系将被删除，此操作不可恢复。`))return;try{await api('/api/files/'+id,{method:'DELETE'});selectedCredentialIds.delete(id);await selectBatch(current.id)}catch(error){showErrorToast(error.message||'文件删除失败')}}
function renderProgress(){const host=$('#progressPanel');if(!host||!current)return;const files=current.files||[];const total=files.length;const processed=files.filter(f=>f.status!=='PROCESSING').length;const reviewed=files.filter(f=>['PASS','REJECTED','ARCHIVED'].includes(f.status)).length;const credentials=files.filter(f=>f.category!=='invoice');const matched=credentials.filter(f=>f.matched_invoice_id).length;const percent=total?Math.round(reviewed/total*100):0;const reviewText=files.filter(f=>['NEEDS_REVIEW','MULTI_ORDER_REVIEW'].includes(f.status)).length;const incomplete=files.filter(f=>f.status==='INCOMPLETE').length;host.innerHTML=`<div class="progress-panel"><div class="progress-head"><div><div class="progress-title">归档准备度</div><div class="small">${current.status==='ARCHIVED'?'归档包已生成':current.status==='PASS'?'材料已核验，可以生成归档包':'完成识别、审核和必要的凭证匹配后才能归档'}</div></div><div class="progress-percent">${percent}%</div></div><div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div><div class="progress-stats"><span>识别完成 ${processed}/${total}</span><span>审核通过/拒收 ${reviewed}/${total}</span><span>待审核 ${reviewText}</span><span>缺材料 ${incomplete}</span><span>凭证匹配 ${matched}/${credentials.length}</span></div></div>`}
function renderAmountSummary(){const host=$('#amountSummary');if(!host||!current)return;const s=current.summary||{};const money=v=>Number(v||0).toFixed(2);const difference=Number(s.required_difference??s.difference??0);host.innerHTML=`<div class="amount-summary"><div class="amount-summary-title">核对后金额</div><div class="amount-summary-grid"><div><span>有效发票总额</span><strong>¥${money(s.invoice_total)}</strong></div><div><span>需购买凭证发票</span><strong>¥${money(s.required_invoice_total)}</strong></div><div><span>无需购买凭证发票</span><strong>¥${money(s.no_purchase_invoice_total)}</strong></div><div><span>凭证净额（仅需凭证发票）</span><strong>¥${money(s.net_total)}</strong></div><div class="${Math.abs(difference)<0.01?'amount-ok':'amount-warn'}"><span>需凭证发票核对差额</span><strong>¥${money(difference)}</strong></div></div><div class="small">发票匹配 ${s.matched_invoice_count||0}/${s.invoice_count||0} · 凭证匹配 ${s.matched_credential_count||0}/${s.credential_count||0}</div></div>`}
function renderBatchBlockers(){const host=$('#progressPanel');if(!host||!current||current.status==='PASS'||current.status==='ARCHIVED')return;const files=current.files||[];const reasons=[];const threshold=Number(current.config.purchase_threshold||150);files.filter(f=>f.category==='invoice'&&f.status!=='REJECTED'&&Number(f.fields?.invoice_amount||0)>threshold).forEach(inv=>{if(!files.some(c=>c.category==='purchase'&&c.matched_invoice_id===inv.id))reasons.push(`高额发票 ${inv.original_name} 尚未匹配购买凭证`)});files.filter(f=>['NEEDS_REVIEW','MULTI_ORDER_REVIEW','INCOMPLETE'].includes(f.status)).slice(0,5).forEach(f=>reasons.push(`${f.original_name}：${(f.errors||[]).join('；')||f.status}`));const unique=[...new Set(reasons)];if(unique.length){const box=document.createElement('div');box.className='error';box.style.margin='-4px 0 12px';box.innerHTML='<strong>当前不能归档的原因：</strong><br>'+unique.map(x=>esc(x)).join('<br>');host.appendChild(box)}}
function renderMatchPools(){const host=$('#matchPools');if(!host||!current)return;const files=current.files||[];const invoices=files.filter(f=>f.category==='invoice');const credentials=files.filter(f=>f.category!=='invoice');const matchedInvoices=invoices.filter(inv=>credentials.some(c=>c.matched_invoice_id===inv.id));const unmatchedInvoices=invoices.filter(inv=>!credentials.some(c=>c.matched_invoice_id===inv.id));const unmatchedCredentials=credentials.filter(c=>!c.matched_invoice_id);selectedCredentialIds=new Set(Array.from(selectedCredentialIds).filter(id=>unmatchedCredentials.some(c=>c.id===id)));const invoiceCard=(inv,matched)=>{const cs=credentials.filter(c=>c.matched_invoice_id===inv.id);return `<div class="invoice-card ${matched?'':'drop-target'}" ${matched?'':'ondragover="dragOverInvoice(event)" ondragleave="dragLeaveInvoice(event)" ondrop="dropCredential(event,\''+inv.id+'\')"'}>${invoiceLabel(inv)}${matched?`<div class="match-hint">已匹配 ${cs.length} 份凭证</div>${cs.map(c=>`<div class="credential-chip">${credentialLabel(c)}<button class="unlink" onclick="unmatchCredential('${c.id}')">移出</button></div>`).join('')}`:'<div class="match-hint">将右侧待匹配凭证拖到此处</div>'}</div>`};const pendingHtml=unmatchedInvoices.length?unmatchedInvoices.map(inv=>invoiceCard(inv,false)).join(''):'<div class="empty-match">所有发票均已匹配或无需匹配</div>';const matchedHtml=matchedInvoices.length?matchedInvoices.map(inv=>invoiceCard(inv,true)).join(''):'<div class="empty-match">暂无已匹配发票</div>';const credentialHtml=unmatchedCredentials.length?unmatchedCredentials.map(c=>`<div class="credential-chip ${selectedCredentialIds.has(c.id)?'selected':''}" draggable="true" ondragstart="dragCredential(event,'${c.id}')"><input class="credential-check" type="checkbox" ${selectedCredentialIds.has(c.id)?'checked':''} onclick="event.stopPropagation();toggleCredentialSelection('${c.id}',this.checked)"><div class="credential-main">${credentialLabel(c)}</div></div>`).join(''):'<div class="empty-match">暂无待匹配凭证</div>';host.innerHTML=`<div class="match-workbench"><section class="match-board"><h3>发票匹配工作区</h3><p class="match-subtitle">每张发票只在这里出现一次；已匹配凭证会显示在发票下方</p><div class="invoice-columns"><div class="invoice-column"><div class="column-title"><span>待匹配发票</span><span class="column-count">${unmatchedInvoices.length}</span></div>${pendingHtml}</div><div class="invoice-column"><div class="column-title"><span>已匹配发票</span><span class="column-count">${matchedInvoices.length}</span></div>${matchedHtml}</div></div></section><aside class="credential-dock"><h3>待匹配凭证 <span class="column-count">${unmatchedCredentials.length}</span></h3><p class="match-subtitle">可勾选多份凭证，再拖动任意一份到发票卡片</p><p class="selection-hint">已选择 ${selectedCredentialIds.size} 份</p><div class="dock-hint">高额发票必须至少匹配一份购买凭证</div>${credentialHtml}</aside></div>`}

function renderErrorSummary(errors){const items=Array.from(new Set((errors||[]).filter(Boolean).map(String)));if(!items.length)return '';const preview=items[0];return `<details class="error-details"><summary><span class="error-label">需处理</span><span class="error-count">${items.length}项</span><span class="error-preview" title="${esc(preview)}">${esc(preview)}</span></summary><div class="error-list">${items.map(item=>`<div>${esc(item)}</div>`).join('')}</div></details>`}
function renderErrorPanel(title,items){const values=Array.from(new Set((items||[]).filter(Boolean).map(String)));if(!values.length)return '';return `<div class="batch-error-panel"><div class="batch-error-icon">!</div><div class="batch-error-content"><div class="batch-error-title">${esc(title)} <span class="error-count">${values.length}项</span></div><details class="error-details"><summary>展开查看具体问题</summary><div class="error-list">${values.map(item=>`<div>${esc(item)}</div>`).join('')}</div></details></div></div>`}
function renderBatchBlockers(){const host=$('#progressPanel');if(!host||!current||current.status==='PASS'||current.status==='ARCHIVED')return;const files=current.files||[];const reasons=[];const threshold=Number(current.config.purchase_threshold||150);files.filter(f=>f.category==='invoice'&&f.status!=='REJECTED'&&Number(f.fields?.invoice_amount||0)>threshold).forEach(inv=>{if(!files.some(c=>c.category==='purchase'&&c.matched_invoice_id===inv.id))reasons.push(`高额发票 ${inv.original_name} 尚未匹配购买凭证`)});files.filter(f=>['NEEDS_REVIEW','MULTI_ORDER_REVIEW','INCOMPLETE'].includes(f.status)).forEach(f=>{const errors=f.errors&&f.errors.length?f.errors:[f.status];reasons.push(`${f.original_name}：${errors.join('；')}`)});const unique=[...new Set(reasons)];if(unique.length)host.insertAdjacentHTML('beforeend',renderErrorPanel('当前不能归档',unique))}
"""


HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本地发票整理</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937;background:#f5f7fb}*{box-sizing:border-box}body{margin:0}.shell{max-width:1440px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px}.eyebrow{font-size:12px;color:#64748b;letter-spacing:.08em}.top h1{margin:5px 0;font-size:28px}.sub{margin:0;color:#64748b}.grid{display:grid;grid-template-columns:330px 1fr;gap:18px}.card{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:18px;box-shadow:0 8px 22px #1e293b0d}.card h2{font-size:16px;margin:0 0 14px}.form{display:grid;gap:10px}.form label{font-size:12px;color:#475569;display:grid;gap:5px}.form input,.form select,.form textarea{width:100%;padding:9px 10px;border:1px solid #cbd5e1;border-radius:9px;font:inherit}.btn{border:0;background:#2563eb;color:white;padding:9px 13px;border-radius:9px;cursor:pointer;font-weight:600}.btn.secondary{background:#e2e8f0;color:#1e293b}.btn.warn{background:#d97706}.btn.danger{background:#dc2626}.btn:disabled{opacity:.45;cursor:not-allowed}.batch-list{display:grid;gap:8px}.batch{padding:12px;border:1px solid #e2e8f0;border-radius:10px;cursor:pointer}.batch.active{border-color:#2563eb;background:#eff6ff}.batch-title{font-weight:700}.meta{font-size:12px;color:#64748b;margin-top:4px}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:15px}.pill{font-size:12px;padding:5px 8px;border-radius:999px;background:#e2e8f0}.pill.PASS,.pill.ARCHIVED{background:#dcfce7;color:#166534}.pill.REJECTED{background:#fee2e2;color:#991b1b}.pill.NEEDS_REVIEW,.pill.MULTI_ORDER_REVIEW,.pill.INCOMPLETE{background:#fef3c7;color:#92400e}.drop{border:2px dashed #bfdbfe;background:#eff6ff;padding:18px;text-align:center;border-radius:12px}.drop input{margin-top:10px}.files{width:100%;border-collapse:collapse;font-size:13px}.files th,.files td{padding:9px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top;text-align:left}.files th{color:#64748b;font-size:12px}.files input,.files select{max-width:145px;padding:5px;border:1px solid #cbd5e1;border-radius:6px}.error{color:#b91c1c;font-size:12px;max-width:220px}.ok{color:#166534}.empty{color:#64748b;padding:30px 0;text-align:center}.small{font-size:12px;color:#64748b}.right{min-width:0}@media(max-width:900px){.grid{grid-template-columns:1fr}.shell{padding:14px}.files{display:block;overflow:auto;white-space:nowrap}}
</style></head><body><main class="shell"><div class="top"><div><div class="eyebrow">LOCAL INVOICE WORKBENCH</div><h1>本地发票整理</h1><p class="sub">上传、识别、核验、人工审核、重命名和归档打包</p></div><div class="small">数据只保存在本机 data/ 目录</div></div><div class="grid"><aside><section class="card"><h2>新建整理批次</h2><form id="newBatch" class="form"><label>批次名称<input name="name" placeholder="例如：2026年8月办公采购" required></label><label>购买方名称<input name="buyer_name" value="北师香港浸会大学"></label><label>购买方税号<input name="buyer_tax_id" value="521000015230002458"></label><label>允许年份<input name="allowed_year" type="number"></label><label>购买凭证阈值<input name="purchase_threshold" type="number" step="0.01" value="150"></label><label>金额误差<input name="amount_tolerance" type="number" step="0.01" value="0.01"></label><div class="small">购买凭证要求由系统自动判定：仅单张发票金额超过阈值时需要。</div><button class="btn">创建整理批次</button></form></section><section class="card" style="margin-top:16px"><h2>批次</h2><div id="batchList" class="batch-list"></div></section></aside><section class="right"><section id="workspace" class="card"><div class="empty">请先创建或选择一个批次</div></section></section></div></main><script>
const $=s=>document.querySelector(s); let current=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const status=s=>`<span class="pill ${esc(s)}">${esc(s)}</span>`;
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}});const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d}
async function loadBatches(){const bs=await api('/api/batches');$('#batchList').innerHTML=bs.length?bs.map(b=>`<div class="batch ${current&&current.id===b.id?'active':''}" onclick="selectBatch('${b.id}')"><div class="batch-title">${esc(b.name)}</div><div class="meta">${esc(b.created_at)} · ${status(b.status)}</div></div>`).join(''):'<div class="empty">还没有批次</div>';}
async function selectBatch(id){current=await api('/api/batches/'+id);render();loadBatches()}
function render(){if(!current)return; const f=current.files||[]; let rows=f.map(file=>{const x=file.fields||{};return `<tr><td>${esc(file.original_name)}<div class="small">${esc(file.category)} · ${esc(file.recognition_method)}</div></td><td>${status(file.status)}<div class="error">${esc((file.errors||[]).join('；'))}</div></td><td><input data-id="${file.id}" data-k="invoice_amount" value="${esc(x.invoice_amount??'')}" placeholder="金额"><input data-id="${file.id}" data-k="invoice_number" value="${esc(x.invoice_number??'')}" placeholder="发票号码"><input data-id="${file.id}" data-k="invoice_date" value="${esc(x.invoice_date??'')}" placeholder="日期"><input data-id="${file.id}" data-k="buyer_name" value="${esc(x.buyer_name??'')}" placeholder="购买方名称"><input data-id="${file.id}" data-k="buyer_tax_id" value="${esc(x.buyer_tax_id??'')}" placeholder="购买方税号"></td><td><input data-id="${file.id}" data-k="payment_amount" value="${esc(x.payment_amount??'')}" placeholder="付款金额"><input data-id="${file.id}" data-k="order_amount" value="${esc(x.order_amount??'')}" placeholder="订单金额"><input data-id="${file.id}" data-k="refund_amount" value="${esc(x.refund_amount??'')}" placeholder="退款金额"></td><td><select data-id="${file.id}" data-k="category"><option value="invoice" ${file.category==='invoice'?'selected':''}>发票</option><option value="purchase" ${file.category==='purchase'?'selected':''}>购买凭证</option><option value="payment" ${file.category==='payment'?'selected':''}>付款凭证</option><option value="refund" ${file.category==='refund'?'selected':''}>退款凭证</option><option value="other" ${file.category==='other'?'selected':''}>其他材料</option></select><textarea data-id="${file.id}" data-k="review_note" placeholder="审核备注">${esc(file.review_note)}</textarea><button class="btn secondary" onclick="saveFile('${file.id}')">保存</button><button class="btn" onclick="passFile('${file.id}')">通过</button></td></tr>`}).join(''); $('#workspace').innerHTML=`<div class="toolbar"><div><h2 style="display:inline;margin-right:10px">${esc(current.name)}</h2>${status(current.status)}</div><button class="btn secondary" onclick="validateBatch()">重新核验</button><button class="btn" onclick="archiveBatch()" ${current.status==='PASS'||current.status==='REJECTED'?'':'disabled'}>生成 ZIP</button>${current.archive_path?`<a class="btn" href="/download/${current.id}">下载归档包</a>`:''}</div><div class="small" style="margin-bottom:12px">购买方：${esc(current.config.buyer_name)} · 税号：${esc(current.config.buyer_tax_id)} · 年份：${esc(current.config.allowed_year)} · ≥${esc(current.config.purchase_threshold)} 元需购买凭证</div><div class="drop">选择文件后上传到当前批次。请分别选择类别，可重复上传。<br><select id="uploadCategory"><option value="invoice">发票</option><option value="purchase">购买凭证</option><option value="payment">付款凭证</option><option value="refund">退款凭证</option><option value="other">其他材料</option></select><input id="uploadInput" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.csv,.xlsx,.txt"></div><div style="overflow:auto;margin-top:15px"><table class="files"><thead><tr><th>文件</th><th>状态</th><th>发票字段</th><th>凭证字段</th><th>分类 / 操作</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="empty">请上传文件</td></tr>'}</tbody></table></div>`; $('#uploadInput').addEventListener('change',uploadFiles)}
async function uploadFiles(e){const cat=$('#uploadCategory').value;for(const file of e.target.files){const fd=new FormData();fd.append('category',cat);fd.append('file',file);const r=await fetch('/api/batches/'+current.id+'/upload',{method:'POST',body:fd});if(!r.ok){const d=await r.json();alert(d.error||'上传失败')}}await selectBatch(current.id)}
function collect(id){const out={};document.querySelectorAll(`[data-id="${id}"]`).forEach(el=>{const k=el.dataset.k;if(k!=='category')out[k]=el.value}); const cat=document.querySelector(`[data-id="${id}"][data-k="category"]`); if(cat)out.category=cat.value;return out}
async function saveFile(id){await api('/api/files/'+id+'/review',{method:'POST',body:JSON.stringify(collect(id))});await selectBatch(current.id)}
async function passFile(id){await api('/api/files/'+id+'/review',{method:'POST',body:JSON.stringify({...collect(id),status:'PASS'})});await selectBatch(current.id)}
async function validateBatch(){await api('/api/batches/'+current.id+'/validate',{method:'POST'});await selectBatch(current.id)}
async function archiveBatch(){try{await api('/api/batches/'+current.id+'/archive',{method:'POST'});await selectBatch(current.id);alert('归档包已生成')}catch(e){alert(e.message)}}
$('#newBatch').addEventListener('submit',async e=>{e.preventDefault();const o=Object.fromEntries(new FormData(e.target));o.payment_required=e.target.payment_required.checked;current=await api('/api/batches',{method:'POST',body:JSON.stringify(o)});e.target.reset();await loadBatches();render()});
loadBatches();
</script></body></html>"""


STORE = Store()
OCR_SEMAPHORE = threading.BoundedSemaphore(OCR_CONCURRENCY)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalInvoiceOrganizer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            page = HTML.replace(
                '<div class="batch-title">${esc(b.name)}</div>',
                '<div class="batch-title">${esc(b.name)}</div><button class="btn danger" style="margin-top:8px;padding:5px 8px;font-size:12px" onclick="event.stopPropagation();deleteBatch(\'${b.id}\')">删除批次</button>',
            ).replace(
                "async function loadBatches()",
                MATCH_JS + "\nasync function deleteBatch(id){if(!confirm('确定删除这个批次及其全部文件和归档包吗？此操作不可恢复。'))return;await api('/api/batches/'+id,{method:'DELETE'});if(current&&current.id===id)current=null;await loadBatches();if(!current)$('#workspace').innerHTML='<div class=\"empty\">请先创建或选择一个批次</div>';else render()}\nasync function loadBatches()",
            ).replace(
                "const r=await fetch('/api/batches/'+current.id+'/upload',{method:'POST',body:fd});if(!r.ok){const d=await r.json();alert(d.error||'上传失败')}",
                "const r=await fetch('/api/batches/'+current.id+'/upload',{method:'POST',body:fd});const d=await r.json();if(!r.ok){alert(d.error||'上传失败')}else if(d.duplicate_removed){alert(d.message||'重复文件已删除')}",
            ).replace(
                "async function uploadFiles(e){const cat=$('#uploadCategory').value;for(const file of e.target.files){const fd=new FormData();fd.append('category',cat);fd.append('file',file);const r=await fetch('/api/batches/'+current.id+'/upload',{method:'POST',body:fd});if(!r.ok){const d=await r.json();alert(d.error||'上传失败')}}await selectBatch(current.id)}",
                "async function uploadFiles(e){const cat=$('#uploadCategory').value;const files=Array.from(e.target.files);await runUploadTasks(files,cat,100);await api('/api/batches/'+current.id+'/validate',{method:'POST'});await selectBatch(current.id)}",
            ).replace(
                "async function selectBatch(id){current=await api('/api/batches/'+id);render();loadBatches()}",
                "async function selectBatch(id){const scrollState=captureScrollState();current=await api('/api/batches/'+id);render();await loadBatches();restoreScrollState(scrollState)}",
            ).replace(
                "≥${esc(current.config.purchase_threshold)} 元需购买凭证",
                "单张发票金额超过${esc(current.config.purchase_threshold)}元需购买凭证",
            ).replace(
                "选择文件后上传到当前批次。请分别选择类别，可重复上传。",
                "选择文件后上传到当前批次。当前仅支持发票和购买凭证，可重复上传。",
            ).replace(
                '<option value="invoice">发票</option><option value="purchase">购买凭证</option><option value="payment">付款凭证</option><option value="refund">退款凭证</option><option value="other">其他材料</option>',
                '<option value="invoice">发票</option><option value="purchase">购买凭证</option>',
            ).replace(
                "<option value=\"invoice\" ${file.category==='invoice'?'selected':''}>发票</option><option value=\"purchase\" ${file.category==='purchase'?'selected':''}>购买凭证</option><option value=\"payment\" ${file.category==='payment'?'selected':''}>付款凭证</option><option value=\"refund\" ${file.category==='refund'?'selected':''}>退款凭证</option><option value=\"other\" ${file.category==='other'?'selected':''}>其他材料</option>",
                "<option value=\"invoice\" ${file.category==='invoice'?'selected':''}>发票</option><option value=\"purchase\" ${file.category!=='invoice'?'selected':''}>购买凭证</option>",
            ).replace(
                "onclick=\"archiveBatch()\" ${current.status==='PASS'||current.status==='REJECTED'?'':'disabled'}",
                "onclick=\"archiveBatch()\"",
            ).replace(
                "</style>",
                MATCH_CSS + AMOUNT_CSS + ERROR_CSS + UI_CSS + "</style>",
            ).replace(
                '<div class="drop">',
                '<div id="progressPanel"></div><div id="amountSummary"></div><div id="matchPools"></div><div id="credentialUploadProgress"></div><div class="drop">',
            ).replace(
                "$('#uploadInput').addEventListener('change',uploadFiles)}",
                "renderProgress();renderAmountSummary();renderBatchBlockers();renderMatchPools();renderCredentialUploadProgress();$('#uploadInput').addEventListener('change',uploadFiles)}",
            ).replace(
                "${esc(file.original_name)}<div class=\"small\">",
                "<button class=\"preview-name\" onclick=\"previewFile('${file.id}')\">${esc(file.original_name)}</button><div class=\"small\">",
            ).replace(
                '<div class="error">${esc((file.errors||[]).join(\'；\'))}</div>',
                '<div class="file-error">${renderErrorSummary(file.errors)}</div>',
            ).replace(
                '<div style="overflow:auto;margin-top:15px"><table',
                '<div class="files-scroll" style="margin-top:15px"><table',
            ).replace(
                '<button class="btn" onclick="passFile(\'${file.id}\')">通过</button>',
                '<button class="btn" onclick="passFile(\'${file.id}\')">通过</button><button class="btn danger" onclick="deleteFile(\'${file.id}\')">删除</button>',
            ).replace(
                '<label><input name="payment_required" type="checkbox"> 付款凭证缺失时阻止归档</label>',
                '<div class="small">购买凭证要求由系统自动判定：仅单张发票金额超过阈值时需要。</div>',
            ).replace(
                "$('#newBatch').addEventListener('submit',async e=>{e.preventDefault();const o=Object.fromEntries(new FormData(e.target));o.payment_required=e.target.payment_required.checked;current=await api('/api/batches',{method:'POST',body:JSON.stringify(o)});e.target.reset();await loadBatches();render()});",
                "$('#newBatch').addEventListener('submit',async e=>{e.preventDefault();const o=Object.fromEntries(new FormData(e.target));current=await api('/api/batches',{method:'POST',body:JSON.stringify(o)});e.target.reset();await loadBatches();render()});",
            )
            body = page.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == "/api/batches":
            self.send_json(STORE.batches()); return
        m = re.fullmatch(r"/api/batches/([^/]+)", path)
        if m:
            batch = STORE.get_batch(m.group(1))
            self.send_json(batch or {"error": "批次不存在"}, 200 if batch else 404); return
        m = re.fullmatch(r"/api/files/([^/]+)/preview", path)
        if m:
            file = STORE.get_file(m.group(1))
            if not file:
                self.send_error_json("文件不存在", 404); return
            target = Path(file["file_path"])
            try:
                safe_target = target.resolve()
                if not safe_target.is_relative_to(BATCH_ROOT.resolve()) or not safe_target.is_file():
                    raise ValueError
            except (ValueError, OSError):
                self.send_error_json("文件路径无效", 404); return
            data = safe_target.read_bytes()
            content_type = mimetypes.guess_type(file["original_name"])[0] or "application/octet-stream"
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(file['original_name'])}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        m = re.fullmatch(r"/download/([^/]+)", path)
        if m:
            batch = STORE.get_batch(m.group(1))
            if not batch or not batch.get("archive_path"):
                self.send_error_json("归档包不存在", 404); return
            target = Path(batch["archive_path"])
            if not target.exists():
                self.send_error_json("归档包文件不存在", 404); return
            data = target.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "application/zip"); self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(target.name)}"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/files/([^/]+)", path)
        if m:
            try:
                self.send_json({"deleted": True, "file_id": m.group(1), "batch": STORE.delete_file(m.group(1))})
            except KeyError as exc:
                self.send_error_json(str(exc), 404)
            except Exception as exc:
                print(f"file delete error: {exc}")
                self.send_error_json("文件删除失败，请查看终端日志", 500)
            return
        m = re.fullmatch(r"/api/batches/([^/]+)", path)
        if not m:
            self.send_error_json("接口不存在", 404)
            return
        try:
            STORE.delete_batch(m.group(1))
            self.send_json({"deleted": True, "batch_id": m.group(1)})
        except KeyError as exc:
            self.send_error_json(str(exc), 404)
        except Exception as exc:
            print(f"delete error: {exc}")
            self.send_error_json("批次删除失败，请查看终端日志", 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/batches":
                self.send_json(STORE.create_batch(self.read_json()), 201); return
            m = re.fullmatch(r"/api/batches/([^/]+)/upload", path)
            if m:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length)
                content_type = self.headers.get("Content-Type", "")
                envelope = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw
                form = BytesParser(policy=email_default).parsebytes(envelope)
                category = "other"
                filename = "上传文件"
                file_content = b""
                for part in form.iter_parts():
                    disposition = part.get("Content-Disposition", "")
                    name = part.get_param("name", header="content-disposition")
                    if name == "category":
                        category = part.get_content().strip()
                    elif name == "file":
                        filename = part.get_filename() or filename
                        file_content = part.get_payload(decode=True) or b""
                defer_validation = self.headers.get("X-Defer-Validation", "").lower() in {"1", "true", "yes"}
                with OCR_SEMAPHORE:
                    result = STORE.upload(m.group(1), category, filename, file_content, defer_validation=defer_validation)
                self.send_json(result, 201); return
            m = re.fullmatch(r"/api/batches/([^/]+)/(validate|archive)", path)
            if m:
                if m.group(2) == "validate": self.send_json(STORE.validate(m.group(1))); return
                archive_path = STORE.archive(m.group(1)); self.send_json({"path": str(archive_path)}); return
            m = re.fullmatch(r"/api/files/([^/]+)/match", path)
            if m:
                payload = self.read_json()
                self.send_json(STORE.match_file(m.group(1), payload.get("invoice_id")))
                return
            m = re.fullmatch(r"/api/files/([^/]+)/review", path)
            if m:
                payload = self.read_json();
                if "category" in payload:
                    if payload["category"] not in UPLOAD_CATEGORIES:
                        raise ValueError("当前只支持分类为：发票、购买凭证")
                    with STORE.lock:
                        STORE.db.execute("UPDATE files SET category=? WHERE id=?", (payload.pop("category"), m.group(1))); STORE.db.commit()
                self.send_json(STORE.review(m.group(1), payload)); return
            self.send_error_json("接口不存在", 404)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self.send_error_json(str(exc) or "请求无效", 400)
        except Exception as exc:
            print(f"server error: {exc}")
            self.send_error_json("服务器处理失败，请查看终端日志", 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地发票整理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--check", action="store_true", help="只检查运行环境，不启动服务")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    if args.check:
        print(f"OK: Python {os.sys.version.split()[0]}, data={DATA_ROOT}")
        return
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and args.open_browser:
            print(f"本地发票整理已经运行：http://{args.host}:{args.port}")
            webbrowser.open(f"http://{args.host}:{args.port}")
            return
        raise
    print(f"本地发票整理已启动：http://{args.host}:{args.port}")
    print(f"数据目录：{DATA_ROOT}")
    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

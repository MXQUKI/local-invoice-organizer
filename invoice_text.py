"""Local keyword extraction from an invoice's embedded PDF text layer."""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from typing import Any


def embedded_page_text(page: Any) -> str:
    """Restore reading order using text coordinates, without rasterizing the PDF."""
    fragments: list[tuple[float, float, str]] = []
    needs_layout = False

    def collect(text, cm, tm, font, size):
        nonlocal needs_layout
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        if not math.isfinite(x) or not math.isfinite(y):
            needs_layout = True
            return
        if len(lines) > 1:
            needs_layout = True
        fragments.append((x, y, " ".join(lines)))

    plain = page.extract_text(visitor_text=collect) or ""
    if not fragments:
        return plain
    if needs_layout or page.rotation:
        try:
            return page.extract_text(extraction_mode="layout") or plain
        except Exception:
            return plain
    # Labels and their values commonly use different fonts with a 0.5–2 pt
    # baseline offset. Retain fragment boundaries so adjacent numeric columns
    # cannot be concatenated into a tax ID or a larger amount.
    rows: list[list[tuple[float, float, str]]] = []
    for fragment in sorted(fragments, key=lambda item: (-item[1], item[0])):
        if not rows or rows[-1][0][1] - fragment[1] > 3:
            rows.append([])
        rows[-1].append(fragment)
    return "\n".join("\t".join(item[2] for item in sorted(row)) for row in rows)


def _lines(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).replace("￥", "¥").replace("−", "-")
    # Retain newlines and column separators. Only remove character spacing
    # inside individual fragments, e.g. "1 1 . 5 6" -> "11.56".
    lines = [re.sub(r"[^\S\n\t]+", "", line) for line in text.splitlines() if line.strip()]
    keywords = ("发票号码", "开票日期", "购买方", "销售方", "名称", "统一社会信用代码",
                "纳税人识别号", "购买方税号", "购方税号", "价税合计", "小写", "项目名称")
    for index, line in enumerate(lines):
        for keyword in keywords:
            line = re.sub(r"\t*".join(map(re.escape, keyword)), keyword, line)
        lines[index] = re.sub(r"(名称|号码|日期|税号|识别号)\t+(?=:)", r"\1", line)
    return lines


def _unique(values):
    values = list(dict.fromkeys(values))
    return values[0] if len(values) == 1 else None


def _page_fields(text: str) -> dict[str, Any]:
    lines = _lines(text)
    numbers, dates, amounts = [], [], []
    for index, line in enumerate(lines):
        # A value may be on the next line, but must immediately follow its
        # keyword; never search arbitrary digits elsewhere in the document.
        window = line + ("\n" + lines[index + 1] if index + 1 < len(lines) else "")
        for match in re.finditer(r"(?:发票号码|票号)[:：]?\s*([0-9]{6,30})(?![0-9])", window):
            numbers.append(match[1])
        for match in re.finditer(r"开票日期[:：]?\s*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", window):
            try:
                dates.append(dt.date(*map(int, match.groups())).isoformat())
            except ValueError:
                pass
        if "价税合计" not in line:
            continue
        # Restrict amount lookup to the total section, stopping before notes.
        section = []
        for total_line in lines[index:index + 4]:
            if re.search(r"开票人|备注|^备$|^注$", total_line):
                break
            section.append(total_line)
        total = "\n".join(section)
        if "小写" in total:
            tail = total.split("小写", 1)[1].lstrip(")）:：\t \n")
            # Uppercase RMB text can precede the small-number value in some
            # PDFs. It is only skipped within the explicitly labelled total.
            tail = re.sub(r"^[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正负\s]+", "", tail)
        else:
            tail = total.split("价税合计", 1)[1].lstrip(":：\t \n")
        match = re.match(r"[¥]?\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?![\d.])", tail)
        if match:
            # A trailing digit on the next line means extraction split the
            # amount: reject it instead of silently accepting 11.5 from 11.56.
            fraction = match[1].partition(".")[2]
            if len(fraction) < 2 and re.match(r"[ \t]*\n[ \t]*[\d.]{1,3}(?:\s*$|\n)", tail[match.end():]):
                continue
            amounts.append(round(float(match[1].replace(",", "")), 2))

    buyer_name = None
    buyer_tax_id = None
    # Header-only search keeps invoice numbers, item quantities and seller
    # identifiers out of the buyer fields.
    header_end = next((i for i, line in enumerate(lines) if "项目名称" in line or "货物或应税劳务" in line), len(lines))
    header = lines[:header_end]
    name_index = next((i for i, line in enumerate(header) if "名称:" in line or "名称：" in line), None)
    if name_index is not None and not re.match(r"(?:销售方|销|售)", header[name_index]):
        name_line = header[name_index]
        match = re.search(r"(?:购买方)?名称[:：]([^:：]*?)(?=\t[销售](?:\t|$)|(?:销售方|销|售)?\t*名称[:：]|统一社会信用代码|纳税人识别号|$)", name_line)
        if match:
            name = match[1].strip("\t ")
            if name and not re.search(r"[¥]|发票号码|开票日期|税号", name):
                buyer_name = name.replace("\t", "")
        tax_label = r"(?:统一社会信用代码(?:\t*/\t*纳税人识别号)?|纳税人识别号|购买方税号|购方税号)\t*[:：]"
        def tax_ids(value):
            return [item for item in re.findall(r"(?<![A-Z0-9])([A-Z0-9]{15,20})(?![A-Z0-9])", value)
                    if len(item) in {15, 18, 20}]
        for i in range(name_index, len(header)):
            if re.match(r"(?:销售方|销|售)", header[i]):
                break
            labels = list(re.finditer(tax_label, header[i]))
            if not labels:
                continue
            if "销售方" in header[i][:labels[0].start()]:
                break
            end = labels[1].start() if len(labels) > 1 else len(header[i])
            buyer_tax_id = _unique(tax_ids(header[i][labels[0].end():end]))
            if buyer_tax_id is None:
                # Some PDFs place the two tax IDs just above the labels.
                # Require an equally sized row of IDs to avoid using a seller
                # tax ID when the buyer field is empty.
                for j in (i - 1, i + 1):
                    if j < name_index or j >= len(header):
                        continue
                    adjacent = header[j]
                    if re.search(r"[\u4e00-\u9fff]", adjacent.replace("信", "").replace("息", "")):
                        continue
                    ids = tax_ids(adjacent)
                    if len(ids) == len(labels) and (len(labels) > 1 or j == i + 1):
                        buyer_tax_id = ids[0]
                        break
            break
    return {"invoice_amount": _unique(amounts), "invoice_number": _unique(numbers),
            "invoice_date": _unique(dates), "buyer_name": buyer_name, "buyer_tax_id": buyer_tax_id}


def invoice_fields_from_text(text: str) -> dict[str, Any]:
    """Find labelled invoice fields; conflicting pages remain unconfirmed."""
    pages = [_page_fields(page) for page in text.split("\f") if page.strip()]
    keys = ("invoice_amount", "invoice_number", "invoice_date", "buyer_name", "buyer_tax_id")
    return {key: _unique(page[key] for page in pages if page[key] is not None) for key in keys}

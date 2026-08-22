"""Optional OpenAI-compatible vision OCR backend.

The backend is disabled unless config.toml contains an API key and
``[vlm].enabled = true``. It is intentionally provider-neutral so the user
can point it at a DeepSeek-compatible gateway without putting credentials in
source code.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback if tomli is installed.
    tomllib = None  # type: ignore[assignment]
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent


def config_candidates() -> list[Path]:
    candidates: list[Path] = []
    if os.environ.get("INVOICE_CONFIG"):
        candidates.append(Path(os.environ["INVOICE_CONFIG"]).expanduser())
    candidates.extend([
        Path.cwd() / "config.toml",
        ROOT / "config.toml",
        Path.home() / "Library/Application Support/本地发票整理/config.toml",
    ])
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "本地发票整理/config.toml")
    return list(dict.fromkeys(candidates))


def load_config() -> dict[str, Any]:
    if tomllib is None:
        return {}
    for path in config_candidates():
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    config = tomllib.load(fh)
                config["_path"] = str(path)
                return config
            except (OSError, ValueError):
                return {}
    return {}


def get_settings() -> dict[str, Any]:
    config = load_config()
    settings = dict(config.get("vlm") or {})
    settings["enabled"] = bool(settings.get("enabled", False))
    settings["api_key"] = settings.get("api_key") or os.environ.get(str(settings.get("api_key_env", "DEEPSEEK_API_KEY")), "")
    settings["base_url"] = str(settings.get("base_url", "https://api.deepseek.com/v1")).rstrip("/")
    settings["model"] = str(settings.get("model", "deepseek-v4-flash-vision-exp"))
    settings["timeout_seconds"] = int(settings.get("timeout_seconds", 90))
    return settings


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings["enabled"] and settings["api_key"] and settings["model"] and settings["base_url"])


def endpoint(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def extract_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
    return str(value or "")


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {"raw_text": text}


def canonical_text(data: dict[str, Any]) -> str:
    raw = str(data.get("raw_text") or data.get("text") or "")
    labels = [
        ("发票金额", data.get("invoice_amount")),
        ("实际付款金额", data.get("payment_amount")),
        ("订单金额", data.get("order_amount")),
        ("退款金额", data.get("refund_amount")),
        ("订单号", data.get("order_number")),
        ("交易号", data.get("transaction_number")),
        ("日期", data.get("date")),
        ("购买方名称", data.get("buyer_name")),
        ("购买方税号", data.get("buyer_tax_id")),
    ]
    structured = "\n".join(f"{label}：{value}" for label, value in labels if value not in (None, ""))
    return (raw + "\n" + structured).strip()


def recognize_image(image_path: str | Path) -> dict[str, Any] | None:
    """Call the configured vision model; return None when VLM is disabled."""
    settings = get_settings()
    if not (settings["enabled"] and settings["api_key"]):
        return None
    path = Path(image_path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = """你是发票和付款凭证 OCR 专家。请阅读图片并只返回 JSON，不要 Markdown：
{
  "raw_text": "图片中可读的原始文字",
  "payment_amount": 付款/实付款最终金额，数字或 null,
  "order_amount": 订单金额，数字或 null,
  "refund_amount": 退款金额，数字或 null,
  "invoice_amount": 发票金额，数字或 null,
  "order_number": "订单号或 null",
  "transaction_number": "交易号或 null",
  "date": "YYYY-MM-DD 或 null",
  "buyer_name": "购买方名称或 null",
  "buyer_tax_id": "购买方税号或 null"
}
付款金额要区分“共减/优惠金额”和最终“实付款”；如果一张截图有多笔明确的实付款，请将其相加。看不清时返回 null，不能猜测。"""
    body = {
        "model": settings["model"],
        "temperature": 0,
        "max_tokens": int(settings.get("max_tokens", 1800)),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]}],
    }
    request = urllib.request.Request(
        endpoint(settings["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings["timeout_seconds"]) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = extract_content(payload["choices"][0]["message"]["content"])
        data = parse_json_object(content)
        data["raw_text"] = canonical_text(data)
        data["method"] = f"{settings['model']} VLM OCR"
        data["confidence"] = data.get("confidence", 0)
        data["warnings"] = []
        return data
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as exc:
        raise RuntimeError(f"VLM OCR 请求失败：{exc}") from exc

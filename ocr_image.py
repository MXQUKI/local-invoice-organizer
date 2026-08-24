#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用本机 Tesseract 识别图片文字。

特点：
* 不依赖 pytesseract，直接调用 tesseract 命令；
* 自动检测可用语言包；
* 可选用 Pillow 做灰度、放大和对比度增强；
* 输出纯文本或 JSON，脚本也可以被 app.py 导入。

中文发票识别需要安装 Tesseract 的 chi_sim 语言包，例如 macOS：
    brew install tesseract-lang
然后使用：
    python3 ocr_image.py invoice.jpg --lang chi_sim+eng
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except ImportError:  # Pillow is optional; Tesseract can still read the original image.
    Image = None  # type: ignore[assignment]
    ImageEnhance = ImageFilter = ImageOps = None  # type: ignore[assignment,misc]


def find_tesseract(command: str | None = None) -> str | None:
    candidates = [command, os.environ.get("TESSERACT_CMD"), "tesseract"]
    for candidate in candidates:
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    return None


def find_vision_ocr(command: str | None = None) -> str | None:
    candidates = [command, os.environ.get("VISION_OCR_CMD"), Path(__file__).with_name("macos_vision_ocr")]
    for candidate in candidates:
        if candidate and (Path(candidate).exists() or shutil.which(str(candidate))):
            return str(candidate)
    return None


def run_vision_ocr(command: str, image_path: Path) -> str:
    result = subprocess.run(
        [command, str(image_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Vision OCR 失败").strip())
    return result.stdout.strip()


def deepseek_vision_enabled() -> bool:
    return bool(get_deepseek_api_key()) and os.environ.get("DEEPSEEK_VISION_ENABLED", "1").lower() not in {"0", "false", "no"}


def get_deepseek_api_key() -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    try:
        import embedded_config  # type: ignore

        encoded = getattr(embedded_config, "DEEPSEEK_API_KEY_B64", "")
        if encoded:
            return base64.b64decode(encoded).decode("utf-8")
    except Exception:
        pass
    return None


def deepseek_image_data_url(image_path: Path) -> str:
    """Build a Base64 data URL from the original image bytes, unchanged."""
    data = image_path.read_bytes()
    media_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        media_type = "image/png"
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def run_deepseek_vision(image_path: Path) -> str:
    """调用 DeepSeek OpenAI-compatible Vision API 返回 OCR 原文。"""
    api_key = get_deepseek_api_key()
    if not api_key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp")
    prompt = os.environ.get(
        "DEEPSEEK_VISION_PROMPT",
        """请理解这张付款凭证图片，只提取最终实际付款金额。只输出 JSON，不要 Markdown，不要解释，格式必须是：
{"payment_amount":数字或null,"reason":"一句话说明取值依据"}
规则：只取最终实际支付/实付款/成交价/费用合计/已收款；不要把共减、优惠券、商品单价、商品总价、运费明细或退款金额当作最终付款。如果一张图片明确包含同一付款凭证的多笔最终付款，payment_amount 为合计；无法确认时返回 null，不要猜测。""",
    )
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": deepseek_image_data_url(image_path), "detail": "high"}},
            ],
        }],
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout = float(os.environ.get("DEEPSEEK_TIMEOUT", "90"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek Vision HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek Vision 网络错误：{exc.reason}") from exc
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("DeepSeek Vision 返回没有 choices")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    content = str(content).strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    except json.JSONDecodeError:
        pass
    return content


def find_pdftoppm(command: str | None = None) -> str | None:
    candidates = [command, os.environ.get("PDFTOPPM_CMD"), "pdftoppm"]
    for candidate in candidates:
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    return None


def available_languages(tesseract: str) -> set[str]:
    result = subprocess.run(
        [tesseract, "--list-langs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    }


def choose_language(requested: str, available: set[str]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    requested_parts = [part for part in requested.split("+") if part]
    selected = [part for part in requested_parts if part in available]
    missing = [part for part in requested_parts if part not in available]
    if missing:
        warnings.append("缺少语言包：" + ", ".join(missing))
    if not selected:
        if "eng" in available:
            selected = ["eng"]
            warnings.append("请求的语言包均不可用，已降级为 eng")
        else:
            raise RuntimeError("Tesseract 没有可用的请求语言包")
    return "+".join(selected), warnings


def make_variants(image_path: Path, temp_dir: Path) -> list[Path]:
    """创建原图和一个适合发票文字的增强版本。"""
    variants = [image_path]
    if Image is None:
        return variants
    try:
        image = Image.open(image_path)
        image = ImageOps.exif_transpose(image).convert("L")
        scale = 2 if max(image.size) < 2500 else 1
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale))
        image = ImageOps.autocontrast(image)
        image = image.filter(ImageFilter.SHARPEN)
        image = ImageEnhance.Contrast(image).enhance(1.35)
        enhanced = temp_dir / "enhanced.png"
        image.save(enhanced, format="PNG", optimize=True)
        variants.append(enhanced)
        # 手机订单截图的付款金额通常位于中下部；裁剪后 OCR 可以减少商品
        # 名称、导航栏和推荐内容对付款金额识别的干扰。
        for name, top, bottom in (("lower", 0.25, 0.95), ("bottom", 0.45, 1.0), ("middle_lower", 0.30, 0.82)):
            crop = image.crop((0, int(image.height * top), image.width, int(image.height * bottom)))
            crop_path = temp_dir / f"{name}.png"
            crop.save(crop_path, format="PNG", optimize=True)
            variants.append(crop_path)
    except Exception:
        # 原图仍然可以直接交给 Tesseract。
        pass
    return variants


def parse_tsv(tsv: str) -> tuple[str, float]:
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    confidences: list[float] = []
    reader = csv.DictReader(tsv.splitlines(), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
            if confidence >= 0:
                confidences.append(confidence)
        except ValueError:
            pass
        key = (
            row.get("block_num", "0"),
            row.get("par_num", "0"),
            row.get("line_num", "0"),
            row.get("word_num", "0"),
        )
        lines.setdefault(key[:3], []).append(text)
    output = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    return output, confidence


def run_tesseract(tesseract: str, image_path: Path, language: str, psm: int) -> tuple[str, float]:
    result = subprocess.run(
        [tesseract, str(image_path), "stdout", "--oem", "1", "--psm", str(psm), "-l", language, "tsv"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Tesseract 识别失败").strip())
    return parse_tsv(result.stdout)


def receipt_text_score(text: str, confidence: float) -> tuple[int, float, int]:
    """为付款截图选择 OCR 结果：同一行出现“实付款”和金额更可靠。"""
    score = confidence
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines()]
    semantic_lines = [line for line in lines if re.search(r"实.?付款|实.?付|发票金额|支付金额|付款金额|成交价|费用合计|总计|用车记录|已收款|收款|转账|元", line)]
    semantic_count = 0
    for line in semantic_lines:
        amount_count = len(re.findall(r"(?:[¥￥YV]\s*)?-?[0-9]+(?:\.[0-9]{1,2})?", line, re.I))
        if amount_count:
            semantic_count += 1
            score += 12
        if amount_count >= 2:
            score += 4
        if "." in line and re.search(r"[¥￥YV]\s*-?[0-9]+\.[0-9]{1,2}", line, re.I):
            score += 5
        if re.search(r"成交价|费用合计|实际付款金额", line):
            score += 5
    return semantic_count, score, len(text)


def recognize_image(
    image_path: str | Path,
    language: str = "chi_sim+eng",
    tesseract_cmd: str | None = None,
    psm: int | None = None,
    use_deepseek: bool = True,
) -> dict[str, Any]:
    """识别图片，返回 text/confidence/language/warnings 等字段。"""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(path)
    if use_deepseek and deepseek_vision_enabled():
        try:
            deepseek_text = run_deepseek_vision(path)
            if deepseek_text:
                return {
                    "text": deepseek_text,
                    "confidence": 0.0,
                    "language": "zh-Hans+en",
                    "warnings": [],
                    "method": "DeepSeek Vision OCR",
                    "variant": path.name,
                    "psm": None,
                }
        except Exception as exc:
            deepseek_warning = f"DeepSeek Vision 调用失败，已回退本地 OCR：{exc}"
        else:
            deepseek_warning = "DeepSeek Vision 未返回文字，已回退本地 OCR"
    else:
        deepseek_warning = None
    tesseract = find_tesseract(tesseract_cmd)
    if not tesseract:
        return {
            "text": "",
            "confidence": 0.0,
            "language": "",
            "warnings": [deepseek_warning or "未找到 tesseract，请安装 Tesseract 或设置 TESSERACT_CMD"],
            "method": "人工录入",
        }
    if sys.platform == "darwin":
        vision = find_vision_ocr()
        if vision:
            try:
                with tempfile.TemporaryDirectory(prefix="invoice-vision-") as temp:
                    vision_candidates = []
                    for variant in make_variants(path, Path(temp)):
                        candidate_text = run_vision_ocr(vision, variant)
                        if candidate_text:
                            vision_candidates.append((candidate_text, variant.name))
                vision_text, vision_variant = max(
                    vision_candidates,
                    key=lambda item: receipt_text_score(item[0], 0.0),
                ) if vision_candidates else ("", path.name)
                if vision_text:
                    return {
                        "text": vision_text,
                        "confidence": 0.0,
                        "language": "zh-Hans+en-US",
                        "warnings": [deepseek_warning] if deepseek_warning else [],
                        "method": "macOS Vision OCR",
                        "variant": vision_variant,
                        "psm": None,
                    }
            except Exception:
                pass
    available = available_languages(tesseract)
    selected_language, warnings = choose_language(language, available)
    with tempfile.TemporaryDirectory(prefix="invoice-ocr-") as temp:
        variants = make_variants(path, Path(temp))
        psms = [psm] if psm is not None else [6, 11]
        candidates: list[tuple[str, float, str, int]] = []
        errors: list[str] = []
        for variant in variants:
            for mode in psms:
                try:
                    text, confidence = run_tesseract(tesseract, variant, selected_language, mode)
                    candidates.append((text, confidence, variant.name, mode))
                except Exception as exc:
                    errors.append(str(exc))
        if not candidates:
            warnings.extend(errors[-2:] or ["未识别到文字"])
            return {"text": "", "confidence": 0.0, "language": selected_language, "warnings": warnings, "method": "人工录入"}
        # 付款截图优先选择“实付款”和金额位于同一行的 OCR 结果，
        # 避免把“共减/优惠金额”单独识别成付款金额。
        text, confidence, variant_name, selected_psm = max(candidates, key=lambda item: receipt_text_score(item[0], item[1]))
        if not text:
            warnings.append("未识别到文字")
        return {
            "text": text,
            "confidence": confidence,
            "language": selected_language,
            "warnings": warnings + ([deepseek_warning] if deepseek_warning else []),
            "method": "Tesseract OCR",
            "variant": variant_name,
            "psm": selected_psm,
            "alternatives": [
                {"text": item[0], "confidence": item[1], "variant": item[2], "psm": item[3]}
                for item in candidates
            ],
        }


def recognize_pdf(
    pdf_path: str | Path,
    language: str = "chi_sim+eng",
    tesseract_cmd: str | None = None,
    pdftoppm_cmd: str | None = None,
    psm: int | None = None,
    max_pages: int = 10,
    use_deepseek: bool = False,
) -> dict[str, Any]:
    """将扫描 PDF 页面转为图片后逐页 OCR。"""
    path = Path(pdf_path)
    converter = find_pdftoppm(pdftoppm_cmd)
    if not converter:
        return {
            "text": "",
            "confidence": 0.0,
            "language": "",
            "warnings": ["未找到 pdftoppm，无法将扫描 PDF 转为图片"],
            "method": "人工录入",
        }
    with tempfile.TemporaryDirectory(prefix="invoice-pdf-ocr-") as temp:
        prefix = str(Path(temp) / "page")
        result = subprocess.run(
            [converter, "-png", "-r", "200", "-f", "1", "-l", str(max_pages), str(path), prefix],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            return {
                "text": "",
                "confidence": 0.0,
                "language": "",
                "warnings": [(result.stderr or "PDF 页面转换失败").strip()],
                "method": "人工录入",
            }
        pages = sorted(Path(temp).glob("page-*.png"))[:max_pages]
        page_results = [recognize_image(page, language, tesseract_cmd, psm, use_deepseek=use_deepseek) for page in pages]
        texts = [item.get("text", "") for item in page_results if item.get("text")]
        confidences = [float(item.get("confidence") or 0) for item in page_results if item.get("text")]
        warnings = [warning for item in page_results for warning in item.get("warnings", [])]
        return {
            "text": "\n\n".join(texts),
            "confidence": round(sum(confidences) / len(confidences), 2) if confidences else 0.0,
            "language": next((item.get("language", "") for item in page_results if item.get("language")), ""),
            "warnings": list(dict.fromkeys(warnings)),
            "method": "扫描PDF Tesseract OCR",
            "pages": len(pages),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 Tesseract OCR 识别图片文字")
    parser.add_argument("image", type=Path, help="图片路径")
    parser.add_argument("--lang", default="chi_sim+eng", help="语言包，例如 chi_sim+eng 或 eng")
    parser.add_argument("--psm", type=int, help="Tesseract 页面分割模式；默认尝试 6 和 11")
    parser.add_argument("--tesseract-cmd", help="tesseract 可执行文件路径，也可用 TESSERACT_CMD 设置")
    parser.add_argument("--backend", choices=["auto", "deepseek", "local"], default="auto", help="OCR 后端；deepseek 用于测试 API")
    parser.add_argument("--output", type=Path, help="将识别文本写入文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = parser.parse_args()
    try:
        if args.backend == "deepseek":
            if not get_deepseek_api_key():
                raise RuntimeError("未设置 DEEPSEEK_API_KEY；请在启动此命令的同一终端中 export")
            raw_model_output = run_deepseek_vision(args.image)
            structured = {}
            try:
                value = json.loads(raw_model_output)
                if isinstance(value, dict):
                    structured = value
            except json.JSONDecodeError:
                pass
            result = {
                "text": structured.get("ocr_text") or raw_model_output,
                "confidence": 0.0,
                "language": "zh-Hans+en",
                "warnings": [],
                "method": "DeepSeek Vision OCR",
            }
            if structured:
                result.update({key: structured[key] for key in ("payment_amount", "payment_amounts", "payment_reason", "order_numbers", "dates") if key in structured})
        else:
            if args.backend == "local":
                os.environ["DEEPSEEK_VISION_ENABLED"] = "0"
            result = recognize_image(args.image, args.lang, args.tesseract_cmd, args.psm, use_deepseek=args.backend != "local")
    except Exception as exc:
        print(f"OCR 失败：{exc}", file=sys.stderr)
        return 1
    if args.output:
        if args.json:
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            args.output.write_text(result["text"], encoding="utf-8")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["text"])
    for warning in result.get("warnings", []):
        print(f"提示：{warning}", file=sys.stderr)
    return 0 if result["text"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

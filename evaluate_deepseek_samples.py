#!/usr/bin/env python3
"""先确认 DeepSeek Vision API 可用，再批量识别 sample/凭证供人工审核。

本脚本不会写入发票整理数据库，不会自动匹配，也不会自动通过任何凭证。
文件名只作为结果中的原始文件名保存，不参与金额识别。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import ocr_image


def parse_model_result(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"payment_amount": None, "reason": "模型返回不是有效 JSON", "raw_model_output": raw}
    if not isinstance(value, dict):
        return {"payment_amount": None, "reason": "模型返回不是 JSON 对象", "raw_model_output": raw}
    amount = value.get("payment_amount")
    try:
        amount = round(float(amount), 2) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    return {"payment_amount": amount, "reason": str(value.get("reason") or ""), "raw_model_output": raw}


def call_one(path: Path) -> dict[str, Any]:
    started = time.monotonic()
    raw = ocr_image.run_deepseek_vision(path)
    result = parse_model_result(raw)
    result.update({"file": path.name, "path": str(path), "method": "DeepSeek Vision OCR", "review_status": "待审核" if result["payment_amount"] is not None else "需要人工确认", "elapsed_seconds": round(time.monotonic() - started, 2)})
    return result


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    headers = ["file", "path", "payment_amount", "reason", "review_status", "elapsed_seconds", "raw_model_output"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows({key: item.get(key, "") for key in headers} for item in results)


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek Vision 全量付款凭证审核测试")
    parser.add_argument("--sample-dir", type=Path, default=Path("sample/凭证"))
    parser.add_argument("--workers", type=int, default=20, help="并发请求数，默认 20")
    parser.add_argument("--json-output", type=Path, default=Path("deepseek_receipts_review.json"))
    parser.add_argument("--csv-output", type=Path, default=Path("deepseek_receipts_review.csv"))
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("未检测到 DEEPSEEK_API_KEY。请在当前终端先设置，再重新执行。")
        return 2
    files = sorted(path for path in args.sample_dir.iterdir() if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"})
    if not files:
        print(f"没有找到凭证图片：{args.sample_dir}")
        return 2
    print(f"正在先测试 DeepSeek API：{files[0].name}")
    try:
        health = call_one(files[0])
    except Exception as exc:
        print(f"API 测试失败，已停止批处理：{exc}")
        return 3
    print(f"API 可用，测试结果：payment_amount={health.get('payment_amount')}")
    results: list[dict[str, Any]] = [health]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(call_one, path): path for path in files[1:]}
        for future in as_completed(futures):
            path = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"file": path.name, "path": str(path), "review_status": "API_ERROR", "error": str(exc)})
            print(f"已处理 {len(results)}/{len(files)}：{path.name}")
    results.sort(key=lambda item: item["file"])
    args.json_output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.csv_output, results)
    recognized = [item for item in results if item.get("payment_amount") is not None]
    print(f"完成：{len(results)} 张凭证")
    print(f"模型返回确定付款金额：{len(recognized)}/{len(results)} ({len(recognized) / len(results):.1%})")
    print(f"JSON：{args.json_output}")
    print(f"CSV：{args.csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

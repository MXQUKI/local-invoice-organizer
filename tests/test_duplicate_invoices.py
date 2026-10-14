"""重复发票提示与修正流程；所有数据写入临时目录。"""
import csv
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class DuplicateInvoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="invoice-import-") as temp:
            with patch.dict(os.environ, {"LOCAL_INVOICE_DATA_DIR": temp}):
                spec = importlib.util.spec_from_file_location(
                    "invoice_test_app", Path(__file__).resolve().parents[1] / "app.py"
                )
                cls.app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cls.app)
                cls.app.STORE.db.close()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="invoice-duplicates-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        paths = patch.multiple(self.app, DATA_ROOT=root, BATCH_ROOT=root / "batches",
                               ARCHIVE_ROOT=root / "archives", DB_PATH=root / "invoice.db")
        paths.start()
        self.addCleanup(paths.stop)
        self.store = self.app.Store()
        self.addCleanup(self.store.db.close)
        self.batch_id = self.store.create_batch({"name": "重复发票测试", "allowed_year": 2026})["id"]
        self.sequence = 0

    def upload(self, name, number="00001234", batch_id=None, buyer="北师香港浸会大学"):
        self.sequence += 1
        text = (f"购买方名称：{buyer}\n购买方税号：521000015230002458\n"
                f"开票日期：2026-01-01\n发票号码：{number}\n价税合计：100.00\n"
                f"备注：测试文件{self.sequence}\n")
        return self.store.upload(batch_id or self.batch_id, "invoice", name, text.encode())

    def files(self):
        return self.store.get_batch(self.batch_id)["files"]

    def test_sequential_upload_and_revalidation_list_all_other_files(self):
        for name in ("A.txt", "B.txt", "C.txt"):
            self.upload(name)
        first = self.files()
        for file in first:
            self.assertEqual(file["status"], "REJECTED")
            self.assertEqual(len(file["duplicate_invoices"]), 2)
            self.assertNotIn(file["id"], [peer["id"] for peer in file["duplicate_invoices"]])
            self.assertEqual(len(file["errors"]), 1)
            for peer in file["duplicate_invoices"]:
                self.assertIn(peer["original_name"], file["errors"][0])
            self.assertIn("00001234", file["errors"][0])
        self.store.validate(self.batch_id)
        self.assertEqual(first, self.files())

    def test_number_edit_and_deletion_clear_old_duplicates(self):
        a = self.upload("A.txt")
        b = self.upload("B.txt")
        c = self.upload("C.txt")
        self.store.review(c["id"], {"invoice_number": "00005678"})
        self.assertEqual(self.store.get_file(c["id"])["status"], "PASS")
        for file in self.files()[:2]:
            self.assertNotIn("C.txt", " ".join(file["errors"]))
        self.store.delete_file(b["id"])
        for file in self.files():
            self.assertEqual(file["status"], "PASS")
            self.assertEqual(file["errors"], [])
            self.assertEqual(file["duplicate_invoices"], [])
        self.assertIsNotNone(self.store.get_file(a["id"]))

    def test_old_generic_errors_upgrade_and_other_rejections_remain(self):
        self.upload("A.txt")
        self.upload("B.txt")
        self.store.db.execute("UPDATE files SET errors_json=? WHERE batch_id=?",
                              (self.app.json_dumps(["重复发票号码"]), self.batch_id))
        self.store.db.commit()
        self.store.validate(self.batch_id)
        self.assertIn("B.txt", self.files()[0]["errors"][0])
        self.store.review(self.files()[0]["id"], {"buyer_name": "错误抬头"})
        self.store.delete_file(self.files()[1]["id"])
        remaining = self.files()[0]
        self.assertEqual(remaining["status"], "REJECTED")
        self.assertEqual(remaining["errors"], ["购买方名称不一致"])
        self.assertEqual(remaining["duplicate_invoices"], [])

    def test_same_filenames_are_distinguishable_and_other_batches_are_separate(self):
        a = self.upload("同名.txt")
        b = self.upload("同名.txt")
        a_peer = self.files()[0]["duplicate_invoices"][0]
        self.assertEqual(a_peer["id"], b["id"])
        self.assertIn(b["id"][:8], a_peer["label"])
        self.assertNotEqual(a["id"], a_peer["id"])
        other = self.store.create_batch({"name": "独立批次", "allowed_year": 2026})
        separate = self.upload("同名.txt", batch_id=other["id"])
        self.assertEqual(separate["status"], "PASS")

    def test_archive_records_the_specific_duplicate_names(self):
        self.upload("A.txt")
        self.upload("B.txt")
        path = self.store.archive(self.batch_id)
        with zipfile.ZipFile(path) as archive:
            csv_name = next(name for name in archive.namelist() if name.endswith("/汇总.csv"))
            rows = list(csv.DictReader(io.StringIO(archive.read(csv_name).decode("utf-8-sig"))))
            by_name = {row["原文件名"]: row for row in rows}
            self.assertIn("B.txt", by_name["A.txt"]["异常说明"])
            self.assertIn("A.txt", by_name["B.txt"]["异常说明"])

    def test_identical_bytes_report_the_retained_file(self):
        first = self.upload("首次.txt")
        duplicate = self.store.upload(self.batch_id, "invoice", "副本.txt",
                                      Path(first["file_path"]).read_bytes())
        self.assertTrue(duplicate["duplicate_removed"])
        self.assertIn("首次.txt", duplicate["message"])
        self.assertIn("副本.txt", duplicate["message"])
        self.assertEqual(len(self.files()), 1)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证页面渲染")
    def test_duplicate_preview_links_escape_filenames(self):
        script = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
let code = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', part => code += part);
process.stdin.on('end', () => {
    const context = {esc: text => String(text).replace(/[&<>"']/g,
        c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]))};
    vm.createContext(context);
    vm.runInContext(code, context);
    const html = context.renderErrorSummary(['重复发票号码：00001234'],
        [{id:'abc123', label:'A<&>.txt（ID abc123）'}]);
    assert.ok(html.includes('A&lt;&amp;&gt;.txt'));
    assert.ok(html.includes("previewFile('abc123')"));
    assert.ok(html.includes('点击查看重复的发票'));
    assert.equal(context.renderErrorSummary([], []), '');
});
'''
        result = subprocess.run(["node", "-e", script], input=self.app.MATCH_JS,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

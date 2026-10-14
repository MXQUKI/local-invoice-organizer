"""Keyword extraction boundaries and the PDF text-before-OCR routing contract."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from invoice_text import embedded_page_text, invoice_fields_from_text


HEADER = """发票号码：\t12345678901234567890
开票日期：\t2026年09月14日
购\t销
买\t名\t称\t：\t测试购买方\t售\t名称：\t测试销售方
统一社会信用代码/纳税人识别号：\t123456789012345678\t统一社会信用代码/纳税人识别号：\t987654321098765432
项目名称\t金额\t税额
商品\t11.45\t0.11
"""


class InvoiceTextTests(unittest.TestCase):
    def test_labelled_fields_ignore_seller_and_item_amounts(self):
        result = invoice_fields_from_text(HEADER + "价税合计(大写)\t壹拾壹圆伍角陆分\t(小写)\t¥\t1 1 . 5 6")
        self.assertEqual(result, {"invoice_amount": 11.56, "invoice_number": "12345678901234567890",
                                  "invoice_date": "2026-09-14", "buyer_name": "测试购买方",
                                  "buyer_tax_id": "123456789012345678"})

    def test_empty_buyer_tax_id_does_not_use_seller_or_invoice_number(self):
        header = HEADER.replace("123456789012345678\t统一", "\t统一")
        result = invoice_fields_from_text(header + "价税合计(小写)\t¥11.56")
        self.assertIsNone(result["buyer_tax_id"])
        self.assertEqual(result["invoice_number"], "12345678901234567890")

    def test_tax_ids_above_labels_remain_separate(self):
        text = ("购\t名称：测试购买方\t销\t名称：测试销售方\n"
                "123456789012345678\t987654321098765432\n"
                "统一社会信用代码/纳税人识别号：\t统一社会信用代码/纳税人识别号：\n项目名称")
        self.assertEqual(invoice_fields_from_text(text)["buyer_tax_id"], "123456789012345678")

    def test_seller_only_section_does_not_fill_buyer_fields(self):
        seller = "销售方名称：测试销售方\n纳税人识别号：987654321098765432\n项目名称"
        fields = invoice_fields_from_text(seller)
        self.assertIsNone(fields['buyer_name'])
        self.assertIsNone(fields['buyer_tax_id'])
        fields = invoice_fields_from_text("购买方名称：测试购买方\n" + seller)
        self.assertEqual(fields['buyer_name'], '测试购买方')
        self.assertIsNone(fields['buyer_tax_id'])

    def test_disordered_plain_text_does_not_guess_from_distant_values(self):
        text = ("发票号码：\n开票日期：\n购\n名称：\n统一社会信用代码/纳税人识别号：\n"
                "销\n名称：\n统一社会信用代码/纳税人识别号：\n项目名称\n价税合计（小写）\n"
                "开票人：\n12345678901234567890\n2026年09月14日\n测试购买方\n123456789012345678\n¥100.00")
        result = invoice_fields_from_text(text)
        self.assertTrue(all(value is None for value in result.values()), result)

    def test_decimal_split_across_lines_is_not_silently_truncated(self):
        for value in ("11.5\n6", "300\n.92"):
            with self.subTest(value=value):
                self.assertIsNone(invoice_fields_from_text(HEADER + "价税合计（小写）¥" + value)["invoice_amount"])

    def test_total_followed_by_order_number_and_red_invoice(self):
        self.assertEqual(invoice_fields_from_text(HEADER + "价税合计（小写）¥11.56\n3300911187237011293")["invoice_amount"], 11.56)
        self.assertEqual(invoice_fields_from_text(HEADER + "价税合计（小写）￥ -1,234.50")["invoice_amount"], -1234.50)

    def test_pages_with_conflicting_totals_do_not_choose_first(self):
        first = HEADER + "价税合计（小写）¥100.00"
        second = HEADER + "价税合计（小写）¥200.00"
        self.assertIsNone(invoice_fields_from_text(first + "\f" + second)["invoice_amount"])
        self.assertEqual(invoice_fields_from_text(first + "\f" + first)["invoice_amount"], 100)
        other_number = first.replace('12345678901234567890', '12345678901234567891')
        self.assertIsNone(invoice_fields_from_text(first + "\f" + other_number)['invoice_number'])

    def test_reader_orders_embedded_runs_using_transformed_coordinates(self):
        class Page:
            rotation = 0
            def extract_text(self, visitor_text):
                # Storage order has all labels before values; values use a
                # translated coordinate system and slightly different baselines.
                runs = [(10, 300, "发票号码："), (10, 200, "开票日期："),
                        (10, 100, "价税合计（小写）"), (80, 299, "12345678"),
                        (80, 199, "2026年09月14日"), (100, 99, "¥11.56")]
                for x, y, text in runs:
                    visitor_text(text, [1, 0, 0, 1, 5, 5], [1, 0, 0, 1, x-5, y-5], {}, 9)
                return "\n".join(text for _, _, text in runs)
        result = invoice_fields_from_text(embedded_page_text(Page()))
        self.assertEqual(result["invoice_amount"], 11.56)
        self.assertEqual(result["invoice_number"], "12345678")
        self.assertEqual(result["invoice_date"], "2026-09-14")


class PdfRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="invoice-text-import-") as temp:
            with patch.dict(os.environ, {"LOCAL_INVOICE_DATA_DIR": temp}):
                spec = importlib.util.spec_from_file_location('invoice_routing_app', Path(__file__).resolve().parents[1]/'app.py')
                cls.app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cls.app)
                cls.app.STORE.db.close()

    def test_pdf_with_text_never_uses_ocr_even_when_fields_are_missing(self):
        for text in (HEADER + "价税合计（小写）¥11.56", "发票号码：\n开票日期："):
            with self.subTest(text=text):
                with patch.object(self.app, 'read_text_from_pdf', return_value=text), patch('ocr_image.recognize_pdf') as ocr:
                    fields, method = self.app.extract_fields(Path('999999.pdf'), 'invoice')
                ocr.assert_not_called()
                self.assertEqual(method, 'PDF内嵌文字提取')
                if '11.56' not in text:
                    self.assertIsNone(fields['invoice_amount'])
                self.assertIsNone(fields['payment_amount'])

    def test_pdf_without_text_enters_local_ocr(self):
        with patch.object(self.app, 'read_text_from_pdf', return_value=' \n'), patch('ocr_image.recognize_pdf', return_value={'text':'扫描内容','method':'扫描PDF Tesseract OCR'}) as ocr:
            text, method = self.app.text_for_file(Path('scan.pdf'), 'invoice')
        ocr.assert_called_once_with(Path('scan.pdf'))
        self.assertEqual(text, '扫描内容')

    def test_empty_extraction_checks_other_text_readers_first(self):
        import sys
        reader = Mock()
        page = Mock()
        page.rotation = 0
        page.extract_text.return_value = ''
        reader.return_value.pages = [page]
        document = Mock()
        document.pages = [Mock()]
        document.pages[0].extract_text.return_value = HEADER
        context = Mock()
        context.__enter__ = Mock(return_value=document)
        context.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {'pypdf':Mock(PdfReader=reader), 'pdfplumber':Mock(open=Mock(return_value=context))}):
            self.assertEqual(self.app.read_text_from_pdf(Path('text.pdf')), HEADER)

    def test_missing_number_blocks_archive_until_corrected(self):
        with tempfile.TemporaryDirectory(prefix='invoice-number-review-') as temp:
            root = Path(temp)
            with patch.multiple(self.app, DATA_ROOT=root, BATCH_ROOT=root/'batches',
                                ARCHIVE_ROOT=root/'archives', DB_PATH=root/'invoice.db'):
                store = self.app.Store()
                try:
                    batch = store.create_batch({'name':'号码缺失测试', 'buyer_name':'测试购买方',
                                                'buyer_tax_id':'123456789012345678', 'allowed_year':2026})
                    text = HEADER.replace('12345678901234567890', '') + '价税合计（小写）¥11.56'
                    with patch.object(self.app, 'text_for_file', return_value=(text, 'PDF内嵌文字提取')):
                        file = store.upload(batch['id'], 'invoice', 'invoice.pdf', b'test')
                    self.assertEqual(file['status'], 'NEEDS_REVIEW')
                    self.assertIn('无法识别发票号码，请人工填写', file['errors'])
                    with self.assertRaises(ValueError):
                        store.archive(batch['id'])
                    store.review(file['id'], {'invoice_number':'12345678901234567890'})
                    self.assertEqual(store.get_file(file['id'])['status'], 'PASS')
                    self.assertEqual(store.get_file(file['id'])['errors'], [])
                    self.assertTrue(store.archive(batch['id']).is_file())
                finally:
                    store.db.close()


if __name__ == '__main__':
    unittest.main()

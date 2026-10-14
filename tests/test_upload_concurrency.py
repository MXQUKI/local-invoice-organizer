"""Exercise the upload entry point from the served page, including its worker cap."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


class UploadConcurrencyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '需要 Node.js 验证上传行为')
    def test_served_page_uploads_100_at_once_and_validates_once_at_end(self):
        with tempfile.TemporaryDirectory(prefix='invoice-upload-test-') as temp:
            with patch.dict(os.environ, {'LOCAL_INVOICE_DATA_DIR':temp}):
                spec = importlib.util.spec_from_file_location('invoice_upload_test', Path(__file__).resolve().parents[1]/'app.py')
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)
            try:
                handler = object.__new__(app.Handler)
                handler.path = '/'
                handler.server = Mock(exit_token='test-token')
                handler.wfile = io.BytesIO()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.do_GET()
                html = handler.wfile.getvalue().decode()
            finally:
                app.STORE.db.close()
        script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
let html = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', part => html += part);
process.stdin.on('end', async () => {
    try {
        let active = 0, peak = 0, completed = 0, validated = 0, reloaded = 0;
        const pending = [], seen = new Set();
        const context = {
            $: () => ({value:'purchase'}), current:{id:'batch'},
            beginWork: () => () => {},
            uploadCredentialFile: (file, category) => {
                assert.equal(category, 'purchase');
                assert.ok(!seen.has(file.name));
                seen.add(file.name);
                active++; peak = Math.max(peak, active);
                return new Promise(resolve => pending.push(() => {active--; completed++; resolve();}));
            },
            showErrorToast: error => {throw new Error(error);},
            api: async (path, options) => {
                assert.equal(path, '/api/batches/batch/validate');
                assert.equal(options.method, 'POST');
                assert.equal(completed, 205);
                assert.equal(active, 0);
                validated++;
            },
            selectBatch: async id => {assert.equal(id, 'batch'); assert.equal(validated, 1); reloaded++;}
        };
        vm.createContext(context);
        for (const name of ['runUploadTasks', 'uploadFiles']) {
            const match = html.match(new RegExp('^async function '+name+'\\([^\\n]+', 'm'));
            assert.ok(match, 'Missing served function: '+name);
            vm.runInContext(match[0], context);
        }
        const finished = context.uploadFiles({target:{files:Array.from({length:205}, (_,i) => ({name:String(i)}))}});
        assert.equal(active, 100);
        while (pending.length) {pending.shift()(); await Promise.resolve();}
        await finished;
        assert.equal(peak, 100);
        assert.equal(completed, 205);
        assert.equal(validated, 1);
        assert.equal(reloaded, 1);
    } catch (error) {console.error(error); process.exitCode = 1;}
});
'''
        result = subprocess.run(['node', '-e', script], input=html, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

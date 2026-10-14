"""Verify exit through HTTP, actual process termination, and busy-task protection."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def request(base, path, data=None, token=None):
    headers = {}
    if data is not None:
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    if token is not None:
        headers['X-Exit-Token'] = token
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode()
        return response.status, json.loads(body) if response.headers.get_content_type() == 'application/json' else body


class ProcessExitTests(unittest.TestCase):
    def test_exit_stops_process_releases_port_and_preserves_saved_batches(self):
        with tempfile.TemporaryDirectory(prefix='invoice-exit-process-') as temp:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            executable = os.environ.get('INVOICE_TEST_EXECUTABLE')
            command = [executable] if executable else [sys.executable, str(ROOT/'app.py')]
            env = dict(os.environ, LOCAL_INVOICE_DATA_DIR=temp, BROWSER='/usr/bin/true')
            saved_id = None
            previous_token = None
            for restart in range(2):
                process = subprocess.Popen(command + ['--port', str(port)], cwd=temp, env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for _ in range(100):
                        self.assertIsNone(process.poll(), 'App exited before serving the page')
                        try:
                            status, page = request(base, '/')
                            break
                        except OSError:
                            time.sleep(.05)
                    else:
                        self.fail('App startup timed out')
                    self.assertEqual(status, 200)
                    self.assertIn('id="exitProgram"', page)
                    token = re.search(r"const EXIT_TOKEN='([a-f0-9]+)'", page)[1]
                    self.assertNotEqual(token, previous_token)
                    self.assertEqual(request(base, '/api/shutdown')[0], 404)
                    for invalid in (None, 'wrong-token', previous_token):
                        self.assertEqual(request(base, '/api/shutdown', {}, invalid)[0], 403)
                    self.assertIsNone(process.poll())
                    if restart == 0:
                        status, batch = request(base, '/api/batches', {'name':'保留的批次'})
                        self.assertEqual(status, 201)
                        saved_id = batch['id']
                    else:
                        self.assertEqual(request(base, '/api/batches/'+saved_id)[1]['name'], '保留的批次')
                    status, result = request(base, '/api/shutdown', {}, token)
                    self.assertEqual(status, 200)
                    self.assertTrue(result['stopping'])
                    self.assertEqual(process.wait(timeout=12), 0)
                    with socket.socket() as probe:
                        self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
                    previous_token = token
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)


class BusyExitTests(unittest.TestCase):
    def test_inflight_write_blocks_exit_and_new_writes_are_blocked_after_exit(self):
        with tempfile.TemporaryDirectory(prefix='invoice-exit-busy-') as temp:
            with patch.dict(os.environ, {'LOCAL_INVOICE_DATA_DIR':temp}):
                spec = importlib.util.spec_from_file_location('invoice_exit_test', ROOT/'app.py')
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)
            server = app.LocalInvoiceServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            entered, release = threading.Event(), threading.Event()
            original = app.STORE.create_batch

            def blocked_create(payload):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Timed out waiting for test release')
                return original(payload)

            try:
                with patch.object(app.STORE, 'create_batch', side_effect=blocked_create):
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        write = pool.submit(request, base, '/api/batches', {'name':'处理中'})
                        try:
                            self.assertTrue(entered.wait(3))
                            status, error = request(base, '/api/shutdown', {}, server.exit_token)
                            self.assertEqual(status, 409)
                            self.assertIn('请等待完成', error['error'])
                            self.assertFalse(server.stopping)
                        finally:
                            release.set()
                        self.assertEqual(write.result(timeout=5)[0], 201)
                # Hold shutdown itself to exercise racing requests after acceptance.
                with patch.object(server, 'shutdown') as shutdown:
                    self.assertEqual(request(base, '/api/shutdown', {}, server.exit_token)[0], 200)
                    self.assertEqual(request(base, '/api/batches', {'name':'不能新建'})[0], 503)
                    self.assertTrue(server.stopping)
                self.assertEqual(len(app.STORE.batches()), 1)
            finally:
                release.set()
                server.shutdown()
                thread.join(5)
                server.server_close()
                app.STORE.db.close()

    @unittest.skipUnless(shutil.which('node'), '需要 Node.js 验证退出交互')
    def test_exit_ui_cancel_busy_success_and_backend_failure(self):
        # Extract only the lifecycle script; the real HTTP page is covered above.
        import ast
        tree = ast.parse((ROOT/'app.py').read_text())
        source = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'EXIT_JS' for t in node.targets))
        script = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
let source=''; process.stdin.setEncoding('utf8'); process.stdin.on('data', s=>source+=s);
process.stdin.on('end', async()=>{
  try {
    function setup(accepted, failure=false){
      const elements={'#exitProgram':{},'#exitStatus':{},'.grid':{}}, calls=[], errors=[];
      const context={$:s=>elements[s],confirm:()=>accepted,showErrorToast:s=>errors.push(s),
        AbortController,clearTimeout,setTimeout:fn=>{queueMicrotask(fn);return 1},
        fetch:async(url,opt)=>{calls.push(url);if(url==='/api/lifecycle')throw new TypeError('Connection refused');
          assert.equal(opt.headers['X-Exit-Token'],'test-token');
          return {ok:!failure,json:async()=>failure?{error:'仍在处理'}:{instance_id:'instance'}}}};
      vm.createContext(context);vm.runInContext(source.replace('__EXIT_TOKEN__','test-token'),context);
      return {context,elements,calls,errors};
    }
    let t=setup(false);await t.context.exitProgram();assert.equal(t.calls.length,0);
    t=setup(true);const finish=t.context.beginWork();assert.equal(t.elements['#exitProgram'].disabled,true);
    await t.context.exitProgram();assert.equal(t.calls.length,0);finish();assert.equal(t.elements['#exitProgram'].disabled,false);
    await t.context.exitProgram();assert.deepEqual(t.calls,['/api/shutdown','/api/lifecycle']);
    assert.equal(t.elements['.grid'].inert,true);assert.equal(t.elements['#exitProgram'].textContent,'已退出');
    assert.match(t.elements['#exitStatus'].textContent,/程序已退出/);
    t=setup(true,true);await t.context.exitProgram();assert.equal(t.elements['.grid'].inert,false);
    assert.equal(t.elements['#exitProgram'].disabled,false);assert.deepEqual(t.errors,['仍在处理']);
  }catch(error){console.error(error);process.exitCode=1}
});
'''
        result = subprocess.run(['node', '-e', script], input=source, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

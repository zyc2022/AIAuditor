from http.client import HTTPConnection
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from audit_assistant import AuditAssistant, UserIdentity
from audit_assistant.server import AuditHTTPServer


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict("os.environ", {"AUDIT_MODE": "local", "MEM0_API_KEY": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.server = AuditHTTPServer(("127.0.0.1", 0), db_path=Path(self.temp.name)/"api.sqlite3", token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, body=None, auth=True, extra_headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers = {"Authorization": "Bearer test-token"} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        headers.update(extra_headers or {})
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        status, data, response_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, data, response_headers

    def new_session(self):
        status, data, _ = self.request("POST", "/api/sessions", {})
        self.assertEqual(status, 201)
        return json.loads(data)["session_id"]

    def test_requires_token_and_valid_host(self):
        self.assertEqual(self.request("GET", "/api/health", auth=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
        self.assertEqual(self.request("GET", "/api/health", extra_headers={"Host": "attacker.example"})[0], 403)

    def test_rejects_cross_origin_and_non_json(self):
        self.assertEqual(self.request("POST", "/api/sessions", {}, extra_headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/sessions", {}, extra_headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/sessions", [1, 2])[0], 400)

    def test_full_session_chat_memory_report_and_exports(self):
        session = self.new_session()
        status, data, _ = self.request("POST", "/api/chat", {"session_id": session, "query": "企业画像", "entity": "C001"})
        self.assertEqual(status, 200)
        result = json.loads(data)
        self.assertEqual(result["scope"]["company_id"], "C001")
        status, data, _ = self.request("GET", "/api/memory?session_id="+session)
        memory = json.loads(data)
        self.assertEqual(len(memory["working"]), 2)
        self.assertTrue(memory["episodes"])
        self.assertEqual(memory["last_response"]["request_id"], result["request_id"])
        status, data, headers = self.request("GET", "/api/reports/"+result["request_id"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("TX005", data.decode())
        for format in ["sft", "rl", "raw"]:
            status, data, _ = self.request("GET", "/api/export?format="+format)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(data))
        self.assertEqual(self.request("GET", "/api/export?format=bad")[0], 400)

    def test_session_ownership_and_missing_session(self):
        AuditAssistant("someone-else", db_path=self.server.store.path, identity=UserIdentity(user_id="other"))
        self.assertEqual(self.request("GET", "/api/memory?session_id=someone-else")[0], 403)
        self.assertEqual(self.request("POST", "/api/chat", {"session_id": "missing", "query": "a"})[0], 404)
        self.assertEqual(self.request("POST", "/api/chat", {"session_id": self.new_session(), "query": ""})[0], 400)

    def test_static_assets_and_path_traversal(self):
        for path in ["/", "/app.js", "/style.css"]:
            status, data, headers = self.request("GET", path, auth=False)
            self.assertEqual(status, 200)
            self.assertTrue(data)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/../.env")[0], 404)

    def test_non_loopback_requires_token(self):
        with self.assertRaises(ValueError):
            AuditHTTPServer(("0.0.0.0", 0), token="")


if __name__ == "__main__":
    unittest.main()

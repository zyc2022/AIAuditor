from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch

from audit_assistant.llm import OpenAICompatiblePlanner
from audit_assistant.memory import Mem0Client
from audit_assistant.models import ToolResult
from audit_assistant.skills import SkillRegistry


class ProviderTests(unittest.TestCase):
    def test_model_function_call_protocol_and_observation(self):
        planner = OpenAICompatiblePlanner("fake-secret", "https://example.test/v1", "test-model")
        response = {"choices": [{"message": {"tool_calls": [{"function": {"name": "risk_assessment", "arguments": "{}"}}]}}]}
        with patch("audit_assistant.llm.urlopen", return_value=BytesIO(json.dumps(response).encode())) as http:
            decision = planner.decide(agent_type="enterprise_profile", query="核查", allowed=["risk_assessment"], observations=[ToolResult("text2sql", "发现合同缺失", data={"_call_arguments": {"view": "yearly"}})], context={"company_id": "C001"}, schemas=SkillRegistry().schemas(["risk_assessment"]), deadline=time.monotonic()+15)
        self.assertEqual(decision.tool, "risk_assessment")
        request = http.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["messages"][-1]["role"], "tool")
        self.assertIn("合同缺失", payload["messages"][-1]["content"])
        self.assertEqual(json.loads(payload["messages"][-2]["tool_calls"][0]["function"]["arguments"])["view"], "yearly")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertLessEqual(http.call_args.kwargs["timeout"], 8)
        self.assertNotIn("fake-secret", json.dumps(payload))

    def test_malformed_model_result_fails(self):
        planner = OpenAICompatiblePlanner("fake", "https://example.test", "test")
        with patch("audit_assistant.llm.urlopen", return_value=BytesIO(b'{}')):
            with self.assertRaises(KeyError):
                planner.decide(agent_type="enterprise_profile", query="x", allowed=[], observations=[], context={}, schemas=[], deadline=time.monotonic()+1)

    def test_mem0_namespaces_search_and_add(self):
        client = Mem0Client("fake", "owner-hash", "https://example.test")
        with patch("audit_assistant.memory.urlopen", return_value=BytesIO(b'{"results": []}')) as http:
            self.assertEqual(client.search("关联交易"), [])
        self.assertEqual(json.loads(http.call_args.args[0].data)["filters"]["user_id"], "owner-hash")
        self.assertTrue(http.call_args.args[0].full_url.endswith("/v2/memories/search/"))
        with patch("audit_assistant.memory.urlopen", return_value=BytesIO(b'{}')) as http:
            client.add("case", "s", ["entity:C001"])
        payload = json.loads(http.call_args.args[0].data)
        self.assertEqual(payload["user_id"], "owner-hash")
        self.assertEqual(payload["metadata"]["tags"], ["entity:C001"])
        self.assertFalse(payload["infer"])


if __name__ == "__main__":
    unittest.main()

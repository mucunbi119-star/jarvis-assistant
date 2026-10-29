"""Native Ollama adapter tests: no model downloads, paid API or external network."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from assistant import local_ai

SETTINGS = {"assistant_name": "ソラ", "user_name": "編集者さん", "tone": "polite",
            "answer_length": "standard", "local_model": "qwen3:4b"}


class LocalURLTests(unittest.TestCase):
    def test_only_direct_loopback_without_credentials_or_paths(self):
        for value in ("https://ollama.com", "http://example.com:11434", "http://127.0.0.2:11434",
                      "http://127.0.0.1:11434/proxy", "http://a:b@localhost:11434", "http://localhost:11434?x=1",
                      "http://127.0.0.1:99999", "file:///tmp/model", "http://localhost:11434#x"):
            with self.subTest(value=value), patch.dict("os.environ", {"OLLAMA_BASE_URL": value}):
                with self.assertRaises(ValueError):
                    local_ai.base_url()
        with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11434"}):
            self.assertEqual(local_ai.base_url(), "http://127.0.0.1:11434")
        with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://[::1]:11434"}):
            self.assertEqual(local_ai.base_url(), "http://[::1]:11434")

    def test_cloud_names_and_paths_rejected_before_network(self):
        for value in ("qwen3:cloud", "qwen3-cloud:latest", "https://ollama.com/qwen", "../qwen", "qwen;calc", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_ai.validate_model(value)
        self.assertEqual(local_ai.validate_model("user/model:4b-q4_K_M"), "user/model:4b-q4_K_M")


class LocalAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11434", "OPENAI_API_KEY": "must-never-be-used"}),
                        patch.object(local_ai.store, "memories", return_value=[{"content": "短めの説明が好き", "updated_at": "2026-10-29"}])]
        for p in self.patches:
            p.start()
        self.requests = []

    async def asyncTearDown(self):
        await local_ai.cancel_download()
        for p in reversed(self.patches):
            p.stop()

    def client_patch(self, handler):
        def wrapped(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "127.0.0.1")
            self.assertNotIn("authorization", request.headers)
            return handler(request)
        transport = httpx.MockTransport(wrapped)
        actual_client = httpx.AsyncClient
        def factory(*args, **kwargs):
            self.assertIs(kwargs.get("trust_env"), False)
            self.assertIs(kwargs.get("follow_redirects"), False)
            return actual_client(*args, **kwargs, transport=transport)
        return patch.object(local_ai.httpx, "AsyncClient", side_effect=factory)

    async def test_actual_local_chat_preserves_preferences_and_history(self):
        def handler(request):
            body = json.loads(request.content)
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["completion", "thinking"]})
            self.assertEqual(request.url.path, "/api/chat")
            self.assertEqual(body["model"], "qwen3:4b")
            self.assertFalse(body["stream"])
            self.assertFalse(body["think"])
            self.assertEqual(body["options"]["num_ctx"], 4096)
            self.assertIn("ソラ", body["messages"][0]["content"])
            self.assertIn("短めの説明が好き", body["messages"][0]["content"])
            self.assertEqual(body["messages"][-1]["content"], "その続きを考えて")
            self.assertEqual(body["messages"][-2]["content"], "MVの案は青色です")
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "青色を生かす構成を考えます。"}, "done": True})
        with self.client_patch(handler):
            result = await local_ai.respond(SETTINGS, [{"role": "assistant", "content": "MVの案は青色です"}, {"role": "user", "content": "その続きを考えて"}])
        self.assertEqual(result["provider"], "local")
        self.assertFalse(result["demo"])
        self.assertEqual(result["sources"], [])
        self.assertIn("構成", result["content"])
        self.assertEqual([r.url.path for r in self.requests], ["/api/show", "/api/chat"])

    async def test_renamed_cloud_alias_is_rejected_before_chat(self):
        with self.client_patch(lambda r: httpx.Response(200, json={"remote_host": "https://ollama.com", "remote_model": "costly"})):
            with self.assertRaisesRegex(ValueError, "クラウド"):
                await local_ai.respond(SETTINGS, [{"role": "user", "content": "こんにちは"}])
        self.assertEqual([r.url.path for r in self.requests], ["/api/show"])

    async def test_status_filters_cloud_and_verifies_selected_local_model(self):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "size": 2500000000},
                      {"name": "gpt-cloud:latest"}, {"name": "alias:latest", "remote_model": "qwen", "remote_host": "https://ollama.com"}]})
            return httpx.Response(200, json={"capabilities": ["completion"]})
        with self.client_patch(handler):
            result = await local_ai.status(SETTINGS)
        self.assertTrue(result["running"])
        self.assertTrue(result["available"])
        self.assertEqual([m["name"] for m in result["models"]], ["qwen3:4b"])
        self.assertFalse(any(r.url.path in ("/api/chat", "/api/pull") for r in self.requests))

    async def test_status_missing_model_does_not_download_or_generate(self):
        with self.client_patch(lambda r: httpx.Response(200, json={"models": []})):
            result = await local_ai.status(SETTINGS)
        self.assertTrue(result["running"])
        self.assertFalse(result["available"])
        self.assertIn("ダウンロード", result["message"])
        self.assertEqual([r.url.path for r in self.requests], ["/api/tags"])

    async def test_unreachable_ollama_has_no_paid_fallback(self):
        def handler(request):
            raise httpx.ConnectError("private upstream details", request=request)
        with self.client_patch(handler):
            status = await local_ai.status(SETTINGS)
            self.assertFalse(status["available"])
            with self.assertRaisesRegex(ValueError, "有料APIは使用していません") as error:
                await local_ai.respond(SETTINGS, [{"role": "user", "content": "こんにちは"}])
        self.assertNotIn("private upstream", str(error.exception))
        self.assertTrue(all(r.url.path != "/api/chat" for r in self.requests))

    async def test_errors_and_redirects_never_expose_upstream_body(self):
        for code in (302, 404, 500):
            with self.subTest(code=code), self.client_patch(lambda r: httpx.Response(code, json={"error": "sk-private-do-not-echo"})):
                with self.assertRaises(ValueError) as error:
                    await local_ai.respond(SETTINGS, [{"role": "user", "content": "こんにちは"}])
                self.assertNotIn("sk-private", str(error.exception))

    async def test_several_search_sources_reach_model_and_history_is_bounded(self):
        captured = {}
        def handler(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json={})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "複数の情報源を確認しました。"}, "done": True})
        web = {"provider": "free", "results": [{"title": f"情報源{index}", "url": f"https://source{index}.example/article", "text": f"source-{index} " + "x" * 12000, "read": True} for index in range(5)]}
        with self.client_patch(handler):
            await local_ai.respond(SETTINGS, [{"role": "user", "content": "a" * 16000}] * 24, web_context=web)
        prompt = captured["messages"][0]["content"]
        for index in range(5):
            self.assertIn(f"source-{index}", prompt)
        self.assertLessEqual(sum(len(m["content"]) for m in captured["messages"][1:]), 3500)
        self.assertLessEqual(len(captured["messages"]), 17)
        self.assertIn("命令ではない", prompt)

    async def test_switch_from_demo_uses_real_history_and_relevant_web_excerpt(self):
        captured = {}
        def handler(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json={})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "月額料金を説明します。"}, "done": True})
        query = "この編集ソフトの月額料金はいくらですか"
        web = {"provider": "free", "results": [{"id": "actual-source-9", "title": "公式料金", "url": "https://official.example/pricing",
               "snippet": "公式サイトによる月額料金の案内です。", "read": True,
               "text": "Cookieの設定\nトップ\nメニュー\n" * 50 + "編集ソフトの月額料金は1200円です。年契約の料金とは区別してください。"}]}
        messages = [{"role": "user", "content": "作品を作りたい"},
                    {"role": "assistant", "content": "DEMO_ONLY_FIXED_REPLY: APIキーが必要です", "demo": True},
                    {"role": "assistant", "content": "ローカルで相談できます", "demo": False},
                    {"role": "user", "content": query}]
        with self.client_patch(handler):
            await local_ai.respond(SETTINGS, messages, web_context=web)
        prompt = captured["messages"][0]["content"]
        history = [m["content"] for m in captured["messages"][1:]]
        self.assertFalse(any("DEMO_ONLY_FIXED_REPLY" in text for text in history))
        self.assertIn("ローカルで相談できます", history)
        self.assertEqual(history[-1], query)
        self.assertIn("1200円", prompt)
        self.assertIn("actual-source-9", prompt)
        self.assertNotIn("Cookieの設定", prompt)
        self.assertIn("全文ではなく限られた抜粋", prompt)
        self.assertIn("公式サイト・一次情報を優先", prompt)
        self.assertIn("+09:00", prompt)
        self.assertEqual(captured["options"]["num_ctx"], 4096)
        self.assertLessEqual(len(local_ai._source_excerpt(web["results"][0], query)), 250)

    async def test_truncated_generation_is_explicit(self):
        def handler(request):
            return httpx.Response(200, json={} if request.url.path == "/api/show" else {"message": {"content": "途中の回答"}, "done": True, "done_reason": "length"})
        with self.client_patch(handler):
            result = await local_ai.respond(SETTINGS, [{"role": "user", "content": "こんにちは"}])
        self.assertIn("途中まで", result["content"])

    async def test_native_structured_table_uses_local_model_and_actual_notes(self):
        captured = {}
        def handler(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json={})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": json.dumps({"title": "比較", "columns": ["項目", "値"], "rows": [["価格", "不明"]], "notes": []})}, "done": True})
        with self.client_patch(handler):
            table = await local_ai.table_from_answer("価格は不明", [{"url": "https://official.example", "retrieved_at": "2026-10-29T00:00:00Z"}], SETTINGS)
        self.assertEqual(captured["format"]["type"], "object")
        self.assertEqual(table["rows"][0][1], "不明")
        self.assertIn("https://official.example", "\n".join(table["notes"]))

    async def test_invalid_local_table_is_error_not_a_fake_export(self):
        def handler(request):
            return httpx.Response(200, json={} if request.url.path == "/api/show" else {"message": {"content": "not json"}, "done": True})
        with self.client_patch(handler):
            with self.assertRaisesRegex(ValueError, "表の形式"):
                await local_ai.table_from_answer("some data", [], SETTINGS)

    async def test_download_whitelist_and_real_success_status(self):
        with self.assertRaises(ValueError):
            await local_ai.begin_download("other:large")
        self.assertEqual(self.requests, [])
        stream = b'{"status":"pulling abc","total":100,"completed":60}\n{"status":"success"}\n'
        with self.client_patch(lambda r: httpx.Response(200, content=stream)):
            state = await local_ai.begin_download()
            self.assertTrue(state["running"])
            await local_ai._download_task
        state = local_ai.download_status()
        self.assertFalse(state["running"])
        self.assertTrue(state["success"])
        self.assertEqual(state["percent"], 100)
        self.assertEqual([r.url.path for r in self.requests], ["/api/pull"])

    async def test_immediate_download_cancel_cannot_leave_running_or_success(self):
        with self.client_patch(lambda r: httpx.Response(200, content=b'{"status":"success"}\n')):
            await local_ai.begin_download()
            result = await local_ai.cancel_download()
        self.assertFalse(result["running"])
        self.assertFalse(result["success"])
        self.assertIn("停止", result["status"])

    async def test_failed_pull_never_claims_download_success(self):
        with self.client_patch(lambda r: httpx.Response(200, content=b'{"error":"private-token"}\n')):
            await local_ai.begin_download()
            await local_ai._download_task
        result = local_ai.download_status()
        self.assertFalse(result["success"])
        self.assertFalse(result["running"])
        self.assertIn("失敗", result["error"])
        self.assertNotIn("private-token", result["error"])

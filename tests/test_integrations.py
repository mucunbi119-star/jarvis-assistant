"""Integration security and honest fallback tests; no external account needed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import integrations as target
from assistant import store


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.data_patch = patch.object(target, "DATA_DIR", self.path)
        self.db_patch = patch.object(store, "DB_PATH", self.path / "test.sqlite3")
        self.data_patch.start()
        self.db_patch.start()
        store.init()
        target.ensure_schema()
        app = FastAPI()
        app.include_router(target.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.data_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def test_browser_windows_fallback_does_not_claim_success(self):
        with patch.object(target, "_windows", return_value=False):
            response = self.client.post("/api/browser/start")
            self.assertEqual(response.status_code, 409)
            self.assertIn("Windows", response.json()["detail"])

    def test_chatgpt_requires_explicit_confirmation(self):
        response = self.client.post("/api/browser/chatgpt", json={"text": "test"})
        self.assertEqual(response.status_code, 400)

    def test_chatgpt_manual_mode_never_sends_text(self):
        async def fake_open(body):
            self.assertEqual(body, {"url": "https://chatgpt.com/"})
            return {"url": body["url"]}
        with patch.object(target, "browser_open", fake_open):
            response = self.client.post("/api/browser/chatgpt", json={"text": "送信しない質問", "confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["manual"])
        self.assertIn("自動送信していません", response.json()["notice"])

    def test_google_not_configured_is_actionable(self):
        with patch.object(target, "_google_configured", return_value=False):
            response = self.client.post("/api/google/connect", json={"confirm": True})
        self.assertEqual(response.status_code, 409)
        self.assertIn("GOOGLE_CLIENT_SECRETS_FILE", response.json()["detail"])

    def test_unknown_google_sheet_cannot_be_read_or_modified(self):
        response = self.client.get("/api/google/sheets/not-created-by-app")
        self.assertEqual(response.status_code, 404)
        response = self.client.put("/api/google/sheets/not-created-by-app", json={"confirm": True, "rows": [["x"]]})
        self.assertEqual(response.status_code, 404)

    def test_google_values_keep_untrusted_formulas_as_raw_data(self):
        class Request:
            def execute(self):
                return {"updatedCells": 1}
        class Values:
            def update(inner, **kwargs):
                self.assertEqual(kwargs["valueInputOption"], "RAW")
                self.assertEqual(kwargs["body"]["values"], [["=HYPERLINK(\"bad\")"]])
                return Request()
        class Service:
            def spreadsheets(self):
                return self
            def values(self):
                return Values()
        with store.connect() as db:
            db.execute("INSERT INTO google_sheets VALUES (?,?,?,?)", ("known", "表", "https://docs.google.com/spreadsheets/d/known", "2026-10-28"))
        with patch.object(target, "_google_service", return_value=Service()):
            response = self.client.put("/api/google/sheets/known", json={"confirm": True, "range": "A1", "rows": [["=HYPERLINK(\"bad\")"]]})
        self.assertEqual(response.status_code, 200)

    def test_google_disconnect_confirmation_and_local_delete(self):
        token = self.path / "credentials" / "google-token.json"
        token.parent.mkdir()
        token.write_text("test-only-token")
        response = self.client.post("/api/google/disconnect", json={})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(token.exists())
        response = self.client.post("/api/google/disconnect", json={"confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(token.exists())

    def test_registered_launcher_never_accepts_non_windows_command(self):
        with patch.object(target, "_windows", return_value=False):
            response = self.client.post("/api/launchers", json={"name": "悪い操作", "path": "cmd.exe /c delete", "confirm": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/launchers").json(), [])

    def test_windows_path_guard_rejects_script_and_share(self):
        script = self.path / "run.bat"
        script.write_text("echo example")
        with patch.object(target, "_windows", return_value=True):
            for path in [str(script), "\\\\server\\share", "./relative"]:
                response = self.client.post("/api/launchers", json={"name": "test", "path": path, "confirm": True})
                self.assertEqual(response.status_code, 400, path)

    def test_delete_launcher_deletes_only_registration(self):
        folder = self.path / "project"
        folder.mkdir()
        with store.connect() as db:
            db.execute("INSERT INTO launchers VALUES(?,?,?,?)", ("test", "企画", str(folder), "folder"))
        response = self.client.request("DELETE", "/api/launchers/test", json={"confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(folder.exists())

    def test_launcher_executes_only_registered_exact_path_without_shell(self):
        executable = self.path / "Editor.exe"
        executable.write_bytes(b"test fixture, never executed")
        with store.connect() as db:
            db.execute("INSERT INTO launchers VALUES(?,?,?,?)", ("editor", "編集ソフト", str(executable), "app"))
        with patch.object(target, "_windows", return_value=True), patch.object(target.subprocess, "Popen") as process:
            rejected = self.client.post("/api/launchers/editor/launch", json={"command": "arbitrary"})
            self.assertEqual(rejected.status_code, 400)
            process.assert_not_called()
            response = self.client.post("/api/launchers/editor/launch", json={"confirm": True, "command": "ignored malicious command"})
        self.assertEqual(response.status_code, 200)
        process.assert_called_once_with([str(executable)], cwd=str(self.path), shell=False)

    def test_google_partial_create_retains_target_for_recovery(self):
        class CreateRequest:
            def execute(self):
                return {"spreadsheetId": "partial", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/partial"}
        class UpdateRequest:
            def execute(self):
                raise OSError("test network failure")
        class Service:
            def spreadsheets(self):
                return self
            def create(self, **kwargs):
                return CreateRequest()
            def values(self):
                return self
            def update(self, **kwargs):
                return UpdateRequest()
        with patch.object(target, "_google_service", return_value=Service()):
            response = self.client.post("/api/google/sheets", json={"title": "作成済み", "columns": ["値"], "rows": [[1]], "confirm": True})
        self.assertEqual(response.status_code, 502)
        self.assertIn("シートは作成されました", response.json()["detail"])
        self.assertEqual(target._known_sheet("partial")["title"], "作成済み")


if __name__ == "__main__":
    unittest.main()

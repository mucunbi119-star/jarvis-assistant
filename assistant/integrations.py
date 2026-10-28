"""Explicit, local-only integrations. No model-generated shell commands."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from fastapi import APIRouter, Body, HTTPException

from .config import DATA_DIR
from .store import connect

router = APIRouter(prefix="/api")
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_google_thread: threading.Thread | None = None
_google_error: str | None = None
_google_generation = 0
_google_lock = threading.Lock()
_playwright = None
_context = None
_browser_task: asyncio.Task | None = None
_browser_status = "停止中"
_browser_lock = asyncio.Lock()


def ensure_schema():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS launchers (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS google_sheets (id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, created_at TEXT NOT NULL)")


def _confirm(body: dict, target: str):
    if body.get("confirm") is not True:
        raise HTTPException(400, f"{target}の対象・内容を画面で確認してから実行してください。")


def _windows():
    return sys.platform == "win32"


def _optional_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _chrome_path() -> Path | None:
    if not _windows():
        return None
    for variable, suffix in (("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"), ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"), ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe")):
        base = os.environ.get(variable)
        if base and (candidate := Path(base) / suffix).is_file():
            return candidate
    return None


def _chrome_capability():
    if not _windows():
        reason = "Chrome 操作は Windows 上で利用できます。この環境では未確認です。"
    elif not _optional_module("playwright"):
        reason = "Chrome 操作には playwright のインストールが必要です。"
    elif not _chrome_path():
        reason = "Google Chrome が見つかりません。通常の場所にインストールしてください。"
    else:
        reason = "専用プロファイルのみ操作します。ログインはご自身で行ってください。"
    return {"available": _windows() and _optional_module("playwright") and _chrome_path() is not None,
            "reason": reason, "running": _context is not None, "status": _browser_status}


def _credentials_path():
    return DATA_DIR / "credentials" / "google-token.json"


def _google_configured():
    value = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "")
    try:
        return bool(value and Path(value).is_file())
    except OSError:
        return False


def _sheets():
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM google_sheets ORDER BY created_at DESC")]


def google_status():
    installed = _optional_module("google_auth_oauthlib") and _optional_module("googleapiclient")
    return {"configured": _google_configured(), "dependencies": installed,
            "connected": _credentials_path().is_file(),
            "connecting": bool(_google_thread and _google_thread.is_alive()),
            "error": _google_error, "scope": GOOGLE_SCOPE, "spreadsheets": _sheets(),
            "notice": "このアプリで作成したファイルだけを扱います。任意の既存ファイルを選ぶ Google Drive Picker は未実装です。接続表示は保存トークンの有無で、API 権限の有効性は操作時に確認します。"}


def capabilities():
    return {"chrome": _chrome_capability(), "google": google_status(), "launchers": list_launchers()}


@router.get("/integrations/status")
def integration_status():
    return capabilities()


async def _validated_url(url: str):
    # Shared SSRF guard resolves public destinations; browser route additionally
    # applies it to every redirect/subresource request.
    from .net import safe_url
    result = await asyncio.to_thread(safe_url, url)
    if hasattr(result, "__await__"):
        await result
    return url


async def _route_request(route):
    url = route.request.url
    if url.startswith(("data:", "blob:")):
        await route.continue_()
        return
    try:
        await _validated_url(url)
        await route.continue_()
    except Exception:
        await route.abort()


@router.post("/browser/start")
async def browser_start():
    global _playwright, _context, _browser_status
    capability = _chrome_capability()
    if not capability["available"]:
        raise HTTPException(409, capability["reason"])
    async with _browser_lock:
        if _context is None:
            from playwright.async_api import async_playwright
            profile = DATA_DIR / "chrome-profile"
            profile.mkdir(parents=True, exist_ok=True)
            try:
                _playwright = await async_playwright().start()
                _context = await _playwright.chromium.launch_persistent_context(
                    str(profile), executable_path=str(_chrome_path()), headless=False,
                    accept_downloads=False, viewport={"width": 1280, "height": 850})
                await _context.route("**/*", _route_request)
                _browser_status = "待機中"
            except Exception:
                if _playwright:
                    await _playwright.stop()
                _playwright = _context = None
                _browser_status = "エラー"
                raise HTTPException(502, "専用 Chrome を起動できませんでした。専用ブラウザが別に起動中なら閉じてください。")
    return _chrome_capability()


async def _page():
    if _context is None:
        await browser_start()
    pages = _context.pages
    return pages[-1] if pages else await _context.new_page()


@router.get("/browser/status")
async def browser_status():
    result = _chrome_capability()
    result["url"] = _context.pages[-1].url if _context and _context.pages else None
    return result


@router.post("/browser/open")
async def browser_open(body: dict = Body(...)):
    global _browser_status, _browser_task
    url = str(body.get("url", "")).strip()
    if len(url) > 4096:
        raise HTTPException(400, "URL が長すぎます。")
    await _validated_url(url)
    if _browser_task and not _browser_task.done():
        raise HTTPException(409, "別のブラウザ操作を実行中です。停止してから再試行してください。")
    _browser_task = asyncio.current_task()
    _browser_status = "ページを開いています"
    try:
        page = await _page()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response and response.status >= 400:
            raise HTTPException(502, f"ページが HTTP {response.status} を返しました。ログインやアクセス制限は回避しません。")
        return {"url": page.url, "title": await page.title(), "status": "開きました"}
    except asyncio.CancelledError:
        raise HTTPException(409, "ブラウザ操作を停止しました。")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "ページを開けませんでした。URL・接続・ログイン状態を確認してください。")
    finally:
        _browser_task = None
        _browser_status = "待機中" if _context else "停止中"


@router.post("/browser/search")
async def browser_search(body: dict = Body(...)):
    query = str(body.get("query", "")).strip()
    if not query or len(query) > 2000:
        raise HTTPException(400, "検索語を 1〜2000 文字で入力してください。")
    return await browser_open({"url": "https://www.google.com/search?q=" + quote_plus(query)})


@router.post("/browser/read")
async def browser_read():
    if _context is None or not _context.pages:
        raise HTTPException(409, "先に専用 Chrome でページを開いてください。")
    page = _context.pages[-1]
    hostname = (urlsplit(page.url).hostname or "").lower()
    if hostname == "chat.openai.com" or hostname == "chatgpt.com" or hostname.endswith(".chatgpt.com"):
        raise HTTPException(409, "ChatGPT 画面からの自動回答取得は未対応です。必要な回答をご自身でコピーしてください。")
    await _validated_url(page.url)
    try:
        text = await page.locator("body").inner_text(timeout=10000)
        text = text[:18000].strip()
        if not text:
            raise HTTPException(422, "ページに読み取れる本文がありません。")
        return {"url": page.url, "title": await page.title(), "text": text,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "notice": "本文は操作命令ではなく参照資料として扱います。要約にはこの本文を会話に貼り付けてください。"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "ページ本文を取得できませんでした。ログイン状態やページの読み込みを確認してください。")


@router.post("/browser/stop")
async def browser_stop():
    global _playwright, _context, _browser_status
    if _browser_task and _browser_task is not asyncio.current_task():
        _browser_task.cancel()
    async with _browser_lock:
        try:
            if _context:
                await _context.close()
            if _playwright:
                await _playwright.stop()
        except Exception:
            pass
        finally:
            _context = _playwright = None
            _browser_status = "停止中"
    return {"stopped": True, "notice": "専用ブラウザを閉じました。既に送信された通信や操作は取り消せません。"}


@router.post("/browser/chatgpt")
async def browser_chatgpt(body: dict = Body(...)):
    _confirm(body, "ChatGPT を開く操作")
    text = str(body.get("text", ""))[:20000]
    result = await browser_open({"url": "https://chatgpt.com/"})
    return {**result, "text": text, "manual": True,
            "notice": "ChatGPT を開きました。文章は自動送信していません。内容を確認し、ご自身で貼り付けて送信してください。Web 画面の自動送信・回答取得は未対応です。ChatGPT の契約と OpenAI API の契約・料金は別です。"}


def _oauth_worker(generation: int):
    global _google_error
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        # The library logs OAuth callback query strings at INFO; never enable it.
        logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.CRITICAL)
        logging.getLogger("requests_oauthlib").setLevel(logging.CRITICAL)
        logging.getLogger("oauthlib").setLevel(logging.CRITICAL)
        flow = InstalledAppFlow.from_client_secrets_file(
            os.environ["GOOGLE_CLIENT_SECRETS_FILE"], scopes=[GOOGLE_SCOPE],
            autogenerate_code_verifier=True)
        flow.run_local_server(host="localhost", bind_addr="127.0.0.1", port=0,
                              timeout_seconds=120, open_browser=True,
                              authorization_prompt_message="",
                              success_message="接続処理を受け付けました。このタブを閉じてアシスタントへ戻ってください。")
        with _google_lock:
            if generation != _google_generation:
                return
            path = _credentials_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            temporary = path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(flow.credentials.to_json())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            _google_error = None
    except Exception:
        _google_error = "Google に接続できませんでした。認証の中止・120 秒の期限・デスクトップ用 OAuth 設定・ネット接続を確認して再試行してください。"


@router.get("/google/status")
def get_google_status():
    return google_status()


@router.post("/google/connect")
def google_connect(body: dict = Body(...)):
    global _google_thread, _google_error, _google_generation
    _confirm(body, "Google アカウントへの接続")
    if not _google_configured():
        raise HTTPException(409, "Google のデスクトップ用 OAuth クライアント JSON を用意し、GOOGLE_CLIENT_SECRETS_FILE にそのファイルの場所を設定してください。")
    if not _optional_module("google_auth_oauthlib") or not _optional_module("googleapiclient"):
        raise HTTPException(409, "Google 連携の依存パッケージがありません。requirements.txt をインストールしてください。")
    if not _windows():
        raise HTTPException(409, "この接続操作は Windows のローカルアプリで行ってください。クラウド環境からユーザーのブラウザへ認証情報を渡しません。")
    with _google_lock:
        if _google_thread and _google_thread.is_alive():
            raise HTTPException(409, "接続操作は既に進行中です。ブラウザで認証するか、接続解除で取り消してください。")
        _google_generation += 1
        _google_error = None
        _google_thread = threading.Thread(target=_oauth_worker, args=(_google_generation,), daemon=True)
        _google_thread.start()
    return google_status()


@router.post("/google/disconnect")
def google_disconnect(body: dict = Body(...)):
    global _google_generation, _google_error
    _confirm(body, "Google のローカル認証情報の削除")
    with _google_lock:
        _google_generation += 1
        _credentials_path().unlink(missing_ok=True)
        _google_error = None
    return {"connected": False, "notice": "ローカルのトークンを削除しました。進行中の認証結果も保存しません。Google 側の許可は Google アカウントの『サードパーティとの接続』から取り消せます。作成したシートは削除していません。"}


def _google_service():
    if not _credentials_path().is_file():
        raise HTTPException(409, "Google アカウントに接続してください。")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        credentials = Credentials.from_authorized_user_file(str(_credentials_path()), [GOOGLE_SCOPE])
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise ValueError("expired")
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception:
        raise HTTPException(502, "Google 認証を利用できません。接続解除後に再接続し、通信・権限を確認してください。")


def _clean_values(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise HTTPException(400, "表は 10000 行以内で指定してください。")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) > 100:
            raise HTTPException(400, "各行は 100 列以内にしてください。")
        values = []
        for value in row:
            if value is None:
                value = "不明"
            if not isinstance(value, (str, int, float, bool)):
                raise HTTPException(400, "セルには文字・数値・真偽値を指定してください。")
            if isinstance(value, float) and not math.isfinite(value):
                raise HTTPException(400, "無限大や NaN は送信できません。不明な値は空欄にしてください。")
            if isinstance(value, str) and len(value) > 50000:
                raise HTTPException(400, "セルの文字数が上限を超えています。")
            values.append(value)
        result.append(values)
    return result


def _known_sheet(sheet_id: str):
    with connect() as db:
        sheet = db.execute("SELECT * FROM google_sheets WHERE id=?", (sheet_id,)).fetchone()
    if not sheet:
        raise HTTPException(404, "このアプリで作成したシートだけを操作できます。任意の既存シートの選択は未対応です。")
    return dict(sheet)


@router.post("/google/sheets")
def create_google_sheet(body: dict = Body(...)):
    _confirm(body, "Google への表データ送信・シート作成")
    title = str(body.get("title", "作成した表")).strip()
    if not title or len(title) > 200:
        raise HTTPException(400, "タイトルを 1〜200 文字で入力してください。")
    rows = _clean_values(body.get("rows", []))
    columns = body.get("columns", [])
    if columns:
        rows = _clean_values([columns]) + rows
    service = _google_service()
    try:
        sheet = service.spreadsheets().create(body={"properties": {"title": title}}, fields="spreadsheetId,spreadsheetUrl").execute()
        sheet_id = sheet["spreadsheetId"]
        url = sheet["spreadsheetUrl"]
        with connect() as db:
            db.execute("INSERT INTO google_sheets VALUES (?,?,?,?)", (sheet_id, title, url, datetime.now(timezone.utc).isoformat()))
        if rows:
            try:
                service.spreadsheets().values().update(spreadsheetId=sheet_id, range="A1", valueInputOption="RAW", body={"values": rows}).execute()
            except Exception:
                raise HTTPException(502, f"シートは作成されましたが、データ送信に失敗しました。重複作成を避け、既存シートへの更新を再試行してください。シート ID: {sheet_id}")
        return {"id": sheet_id, "title": title, "url": url, "rows": len(rows)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Google シートを作成できませんでした。Sheets API の有効化・認証・通信を確認してください。")


def _sheet_range(value: str):
    # Named sheets use apostrophes escaped by doubling; no arbitrary expressions.
    if not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r"(?:(?:[\w\u3000-\u9fff .-]+|'(?:[^']|'')+')!)?\$?[A-Z]{1,3}\$?[1-9]\d{0,5}(?::\$?[A-Z]{1,3}\$?[1-9]\d{0,5})?", value):
        raise HTTPException(400, "範囲は A1:Z1000 や シート1!A1:C20 の形式で指定してください。")
    return value


@router.get("/google/sheets/{sheet_id}")
def read_google_sheet(sheet_id: str, range: str = "A1:Z1000"):
    sheet = _known_sheet(sheet_id)
    selected = _sheet_range(range)
    try:
        result = _google_service().spreadsheets().values().get(spreadsheetId=sheet_id, range=selected).execute()
        return {**sheet, "range": selected, "rows": result.get("values", [])}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "シートを読み込めませんでした。対象範囲と権限を確認してください。")


@router.put("/google/sheets/{sheet_id}")
def update_google_sheet(sheet_id: str, body: dict = Body(...)):
    sheet = _known_sheet(sheet_id)
    _confirm(body, f"Google シート「{sheet['title']}」の更新")
    selected = _sheet_range(body.get("range", "A1"))
    rows = _clean_values(body.get("rows", []))
    if body.get("columns"):
        rows = _clean_values([body["columns"]]) + rows
    try:
        result = _google_service().spreadsheets().values().update(spreadsheetId=sheet_id, range=selected, valueInputOption="RAW", body={"values": rows}).execute()
        return {**sheet, "range": selected, "updated_cells": result.get("updatedCells", 0), "notice": "指定したセルを更新しました。送信した範囲以外は変更していません。"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "シートの更新を確認できませんでした。Google 側のシートを確認してから再試行してください。")


@router.get("/launchers")
def list_launchers():
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM launchers ORDER BY name")]


def _registered_path(value: str):
    if not _windows():
        raise HTTPException(409, "アプリ・フォルダ起動は Windows 上で利用できます。")
    if not isinstance(value, str) or not value.strip() or value.startswith(("\\\\", "//")) or "\x00" in value:
        raise HTTPException(400, "ネットワーク共有ではない、ローカルの絶対パスを指定してください。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "絶対パスで指定してください。引数・コマンドは登録できません。")
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "ファイルの場所を確認できません。ローカルの絶対パスを指定してください。")
    if str(path).startswith(("\\\\", "//")):
        raise HTTPException(400, "ネットワーク共有は登録できません。")
    if path.is_dir():
        return path, "folder"
    if path.is_file() and path.suffix.lower() == ".exe":
        return path, "app"
    raise HTTPException(400, "存在するフォルダか .exe ファイルを選んでください。スクリプト・ショートカット・任意コマンドは登録できません。")


@router.post("/launchers")
def register_launcher(body: dict = Body(...)):
    _confirm(body, "起動対象の登録")
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "名前を 1〜100 文字で入力してください。")
    path, kind = _registered_path(body.get("path", ""))
    identifier = uuid.uuid4().hex
    with connect() as db:
        db.execute("INSERT INTO launchers VALUES (?,?,?,?)", (identifier, name, str(path), kind))
    return {"id": identifier, "name": name, "path": str(path), "kind": kind}


@router.delete("/launchers/{identifier}")
def delete_launcher(identifier: str, body: dict = Body(...)):
    _confirm(body, "起動対象の登録削除")
    with connect() as db:
        if not db.execute("DELETE FROM launchers WHERE id=?", (identifier,)).rowcount:
            raise HTTPException(404, "登録が見つかりません。")
    return {"deleted": True, "notice": "登録だけを削除しました。アプリやフォルダ本体は削除していません。"}


@router.post("/launchers/{identifier}/launch")
def launch_registered(identifier: str, body: dict = Body(...)):
    with connect() as db:
        row = db.execute("SELECT * FROM launchers WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "登録が見つかりません。")
    _confirm(body, f"「{row['name']}」の起動")
    path, kind = _registered_path(row["path"])
    try:
        if kind == "folder":
            os.startfile(str(path))
        else:
            subprocess.Popen([str(path)], cwd=str(path.parent), shell=False)
    except OSError:
        raise HTTPException(502, "Windows に起動を依頼できませんでした。ファイルの場所・権限を確認してください。")
    return {"requested": True, "name": row["name"], "notice": "Windows に起動を依頼しました。アプリ内部の起動完了までは検証していません。"}

import asyncio
import csv
import io
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, documents, free_search, local_ai, store, tables
from .config import STATIC_DIR
from .net import read_page

active_tasks = {}
research_tasks = {}


@asynccontextmanager
async def lifespan(app):
    store.init()
    documents.ensure_schema()
    tables.ensure_schema()
    from . import integrations
    integrations.ensure_schema()
    yield
    for task in list(active_tasks.values()) + list(research_tasks.values()):
        task.cancel()
    await local_ai.cancel_download()
    await integrations.browser_stop()


app = FastAPI(title='日本語ローカルアシスタント', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def local_only(request: Request, call_next):
    # Prevent DNS rebinding and cross-origin writes to this unauthenticated local app.
    host = request.url.hostname
    if host not in ('localhost', '127.0.0.1', '[::1]', '::1', 'testserver'):
        return JSONResponse({'detail': 'このアプリはローカルホストからのみ利用できます。'}, status_code=403)
    origin = request.headers.get('origin')
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1') and (parsed.port or 80) == (request.url.port or 80)
        except ValueError:
            allowed = False
        if not allowed:
            return JSONResponse({'detail': '外部サイトからのアクセスは拒否しました。'}, status_code=403)
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse({'detail': '外部サイトからの操作は拒否しました。'}, status_code=403)
    try:
        size = int(request.headers.get('content-length', '0'))
    except ValueError:
        return JSONResponse({'detail': '不正なリクエストです。'}, status_code=400)
    if size > 12 * 1024 * 1024:
        return JSONResponse({'detail': '送信サイズが大きすぎます（最大12MB）。'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.exception_handler(ValueError)
async def invalid_value(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': '0.1.0'}


def capabilities():
    return {'openai_configured': ai.available(), 'demo': True, 'web_search': ai.available(),
            'local': True, 'local_model_default': 'qwen3:4b', 'free_search': True,
            'documents': True, 'tables': True, 'wake_word': False,
            'speech': 'ブラウザの日本語音声・音声認識の対応状況に依存します。',
            'chatgpt_automation': False}


@app.get('/api/state')
def state():
    return dict(settings=store.get_settings(), conversations=store.conversations(), memories=store.memories(),
                documents=documents.list_documents(), files=tables.list_files(), capabilities=capabilities())


@app.get('/api/capabilities')
def get_capabilities():
    return capabilities()


@app.put('/api/settings')
def settings_update(values: dict = Body(...)):
    if set(values) - set(store.DEFAULTS):
        raise HTTPException(400, '未対応の設定項目です。')
    current = store.get_settings() | values
    for key in ('assistant_name', 'user_name'):
        if not isinstance(current[key], str) or len(current[key]) > 40 or (key == 'assistant_name' and not current[key].strip()):
            raise HTTPException(400, '名前は40文字以内で設定してください。AIの名前は必須です。')
        current[key] = current[key].strip()
    for key, allowed in [('tone', ('polite', 'friendly', 'concise')), ('answer_length', ('short', 'standard', 'long')), ('mode', ('demo', 'local', 'live'))]:
        if current[key] not in allowed:
            raise HTTPException(400, '設定の選択肢が不正です。')
    if not isinstance(current['timezone'], str):
        raise HTTPException(400, 'タイムゾーンを確認してください。')
    try:
        ZoneInfo(current['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, 'タイムゾーンが不正です。例: Asia/Tokyo')
    for key, low, high in [('speech_rate', 0.5, 2), ('speech_volume', 0, 1)]:
        if not isinstance(current[key], (int, float)) or not low <= current[key] <= high:
            raise HTTPException(400, '音声の速度または音量が範囲外です。')
    if not isinstance(current['voice_uri'], str) or len(current['voice_uri']) > 500:
        raise HTTPException(400, '声の設定が不正です。')
    if any(not isinstance(current[k], bool) for k in ('speech_enabled', 'onboarding_complete')):
        raise HTTPException(400, 'オン・オフの設定が不正です。')
    if current['mode'] == 'live' and not ai.available():
        raise HTTPException(400, 'API キーが未設定です。.env を設定して再起動してください。')
    current['local_model'] = local_ai.validate_model(current['local_model'])
    return store.save_settings(current)


@app.get('/api/local/status')
async def local_status():
    return await local_ai.status(store.get_settings())


@app.post('/api/local/download')
async def local_download(body: dict = Body(...)):
    require_confirm(body)
    return await local_ai.begin_download(body.get('model', 'qwen3:4b'))


@app.post('/api/local/download/cancel')
async def local_download_cancel():
    return await local_ai.cancel_download()


@app.get('/api/conversations')
def conversation_list():
    return store.conversations()


@app.post('/api/conversations')
def create_conversation():
    return store.new_conversation()


@app.get('/api/conversations/{cid}')
def get_conversation(cid: str):
    item = store.conversation(cid)
    if item is None:
        raise HTTPException(404, '会話が見つかりません。')
    return item


def require_confirm(body):
    if body.get('confirm') is not True:
        raise HTTPException(400, '対象と内容を確認してから実行してください。')


@app.delete('/api/conversations/{cid}')
def delete_conversation(cid: str, body: dict = Body(...)):
    require_confirm(body)
    if cid in active_tasks:
        raise HTTPException(409, '処理を停止してから削除してください。')
    if not store.conversation(cid):
        raise HTTPException(404, '会話が見つかりません。')
    with store.connect() as db:
        db.execute('DELETE FROM messages WHERE conversation_id=?', (cid,))
        db.execute('DELETE FROM conversations WHERE id=?', (cid,))
    return {'deleted': True}


class MemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=3000)
    source: str = Field(default='手動入力', max_length=100)


@app.get('/api/memories')
def memory_list():
    return store.memories()


@app.post('/api/memories')
def memory_create(body: MemoryInput):
    return store.save_memory(body.content, body.source)


@app.put('/api/memories/{mid}')
def memory_update(mid: str, body: MemoryInput):
    try:
        return store.save_memory(body.content, body.source, mid)
    except KeyError:
        raise HTTPException(404, '記憶が見つかりません。')


@app.delete('/api/memories/{mid}')
def memory_delete(mid: str, body: dict = Body(...)):
    require_confirm(body)
    with store.connect() as db:
        if not db.execute('DELETE FROM memories WHERE id=?', (mid,)).rowcount:
            raise HTTPException(404, '記憶が見つかりません。')
    return {'deleted': True}


@app.delete('/api/memories')
def memory_delete_all(body: dict = Body(...)):
    require_confirm(body)
    with store.connect() as db:
        db.execute('DELETE FROM memories')
    return {'deleted': True}


class ChatInput(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=16000)
    web: bool = False
    document_ids: list[str] = Field(default_factory=list, max_length=30)


def local_command(text, settings, cid):
    """Local changes can only be triggered by the user's own text, never model output."""
    match = re.fullmatch(r'(?:あなた|AI|アシスタント)の名前を[「『]?(.+?)[」』]?に(?:変更して|変えて|して)[。！!\s]*', text)
    if match:
        name = match.group(1).strip('「」『』 ')
        settings_update({'assistant_name': name})
        return {'content': f'名前を「{name}」に変更しました。設定は保存されています。'}
    if re.search(r'覚えて[。！!\s]*$', text):
        content = re.sub(r'(?:だから)?(?:これを)?覚えて[。！!\s]*$', '', text).strip('。「」 ')
        if not content:
            return {'content': '覚えてほしい内容も一緒に入力してください。'}
        # Update an explicit preferred answer length instead of allowing stale preference conflict.
        if re.search('短め|短く|簡潔', content):
            settings_update({'answer_length': 'short'})
            old = next((m for m in store.memories() if m['source'] == '回答の長さの希望'), None)
            store.save_memory(content, '回答の長さの希望', old['id'] if old else None)
        elif re.search('詳しく|長め', content):
            settings_update({'answer_length': 'long'})
            old = next((m for m in store.memories() if m['source'] == '回答の長さの希望'), None)
            store.save_memory(content, '回答の長さの希望', old['id'] if old else None)
        else:
            store.save_memory(content, '会話:' + cid)
        return {'content': f'「{content}」を記憶しました。記憶画面から編集・削除できます。'}
    if re.search('ゆっくり(?:話|読)|遅く(?:話|読)', text):
        settings_update({'speech_rate': max(.5, round(settings['speech_rate'] - .15, 2))})
        return {'content': '読み上げ速度を少し下げて保存しました。', 'actions': [{'type': 'settings'}]}
    if re.search('声.*試聴', text):
        return {'content': '設定画面で日本語対応の声を選び、試聴できます。声が表示されない場合は Windows に日本語の音声を追加してください。', 'actions': [{'type': 'settings'}]}
    if re.search('(?:Chrome|クローム|ChatGPT|チャットGPT)', text, re.I):
        return {'content': 'ブラウザ操作画面で送信先と内容を確認してください。ChatGPT への質問送信と回答の取り込みは手動です。自動送信は行いません。', 'actions': [{'type': 'browser', 'text': text}]}
    if re.search('Google.*(?:シート|スプレッド)|スプレッドシート', text, re.I):
        return {'content': '連携画面で Google に接続し、送信する表を選んで確認してください。未接続でも Excel・CSV の保存は利用できます。', 'actions': [{'type': 'integrations'}]}
    if re.search('登録した.*(?:開|起動)', text):
        return {'content': '登録アプリ画面で対象を選んで起動してください。登録されていないコマンドは実行しません。', 'actions': [{'type': 'launchers'}]}
    if re.search('CSV.*(?:集計|グラフ)|棒グラフ|折れ線グラフ|円グラフ', text, re.I) or (settings['mode'] == 'demo' and re.search('(?:表|Excel|エクセル).*(?:作|まとめ|保存)', text, re.I)):
        return {'content': '表・ファイル画面で CSV・Excel を取り込み、列名を指定して集計・並べ替え・グラフ作成できます。AIで表を作る場合は、ローカルAIまたは実サービスモードで依頼してください。', 'actions': [{'type': 'files'}]}
    return None


def needs_web_search(text):
    """Explicit research and freshness cues; never parse fetched data as tool requests."""
    if '取得したページ本文' in text:
        return False
    if re.search(r'(?:検索|調査)(?:は|を)?(?:しない|不要)|(?:Web|ネット|インターネット).*使わず', text, re.I):
        return False
    return bool(re.search(r'最新|今日|ニュース|調べ|検索|調査|料金|(?:Web|ネット|インターネット).*(?:探|調)|公式.*(?:確認|情報)', text, re.I))


async def local_answer(settings, messages, excerpts=None, web=False, url=None):
    """Local inference and unpaid public search. Failure never selects paid APIs."""
    page, context = None, None
    if url:
        try:
            page = await read_page(url)
        except httpx.HTTPError as exc:
            raise ValueError('指定ページを取得できませんでした。公開状態・接続・アクセス制限をご確認ください。本文を読めていないため、要約は作成していません。') from exc
    elif web:
        context = await free_search.search(messages[-1]['content'])
        if not context.get('results'):
            raise ValueError('検索結果を取得できませんでした。情報を確認していないため、調査結果は生成しません。')
    result = await local_ai.respond(settings, messages, excerpts=excerpts or [], web_context=context, page=page)
    # These are actual input sources. Never build clickable citations from generated text.
    result['sources'] = []
    if page:
        result['sources'].append(dict(title=page['title'], url=page['url'], retrieved_at=store.now(),
                                      note='公開ページ本文を取得' + ('（先頭24,000文字まで）' if page.get('truncated') else '。') +
                                           'ローカルAIには、さらに容量に合わせた抜粋を渡しています。'))
    if context:
        for source in context['results']:
            citation = {key: source.get(key, '') for key in ('title', 'url', 'retrieved_at', 'note')}
            citation['note'] += ' ローカルAIには関連部分の短い抜粋を渡しています。'
            result['sources'].append(citation)
        if context.get('warnings'):
            result['content'] += '\n\n調査時の注意: ' + ' / '.join(context['warnings'])
    result['demo'] = False
    return result


@app.post('/api/chat')
async def chat(body: ChatInput):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, 'メッセージを入力してください。')
    cid = body.conversation_id or store.new_conversation()['id']
    convo = store.conversation(cid)
    if not convo:
        raise HTTPException(404, '会話が見つかりません。')
    if cid in active_tasks:
        raise HTTPException(409, 'この会話は処理中です。停止するか完了をお待ちください。')
    task = asyncio.current_task()
    active_tasks[cid] = task
    settings = store.get_settings()
    try:
        if store.contains_secret(text):
            raise HTTPException(400, '秘密情報を含む可能性があります。パスワード・認証コード・APIキーを取り除いてください。会話には保存していません。')
        store.add_message(cid, 'user', text)
        # Browser text is quoted data even if its last line resembles a local command.
        result = None if '取得したページ本文' in text else local_command(text, settings, cid)
        if result is None:
            excerpts = documents.search_documents(text, body.document_ids)
            url_match = re.search(r'https?://[^\s<>「」]+', text)
            url = url_match.group(0).rstrip('。）」)') if url_match and '取得したページ本文' not in text else None
            web = body.web or needs_web_search(text)
            if settings['mode'] == 'demo':
                result = ai.demo_reply(settings, text, excerpts, web, url)
            elif settings['mode'] == 'local':
                result = await local_answer(settings, store.conversation(cid)['messages'], excerpts, web=web and not url, url=url)
            else:
                result = await ai.respond(settings, store.conversation(cid)['messages'], excerpts, web=web and not url, url=url)
            if excerpts and not web and not url:
                result.setdefault('sources', []).extend([dict(title=e['name'], location=e['location'], document_id=e['id'], retrieved_at=store.now(), note='質問に関連して送信した資料の抜粋。回答で実際に利用した箇所は本文をご確認ください。') for e in excerpts])
            if settings['mode'] in ('local', 'live') and re.search('(?:表|Excel|エクセル).*(?:作|まとめ|保存)|(?:表|Excel|エクセル)に', text, re.I):
                try:
                    if settings['mode'] == 'local':
                        spec = await local_ai.table_from_answer(result['content'], result.get('sources', []), settings)
                    else:
                        spec = await ai.table_from_answer(result['content'], result.get('sources', []))
                    result['table'] = tables.create_table(spec)
                    result['content'] += '\n\nExcel・CSVを作成しました。表・作成ファイル画面で確認して保存してください。'
                    result['actions'] = [{'type': 'files'}]
                except ValueError as exc:
                    result['content'] += '\n\n' + str(exc)
        answer = store.add_message(cid, 'assistant', result['content'], result.get('sources'), result.get('demo', False))
        return {'conversation_id': cid, 'message': answer, 'actions': result.get('actions', []), 'table': result.get('table')}
    except asyncio.CancelledError:
        note = ('有料APIは使用していません。Ollama側や検索先で開始済みの処理が残る場合があります。'
                if settings['mode'] == 'local' else '外部サービスへ送信済みの処理や料金は取り消せない場合があります。')
        store.add_message(cid, 'assistant', '処理を停止しました。完了した回答は取得していません。' + note)
        return JSONResponse({'detail': '処理を停止しました。'}, status_code=409)
    finally:
        if active_tasks.get(cid) is task:
            active_tasks.pop(cid, None)


@app.post('/api/chat/cancel')
async def cancel(body: dict = Body(...)):
    task = active_tasks.get(body.get('conversation_id'))
    if task:
        task.cancel()
    return {'stopped': bool(task), 'note': '開始済みの処理や送信済みデータの取り消しは保証できません。'}


class ResearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    url: str | None = Field(default=None, max_length=4000)
    request_id: str | None = Field(default=None, max_length=100)


@app.post('/api/research')
async def research(body: ResearchInput):
    settings = store.get_settings()
    if store.contains_secret(body.query):
        raise HTTPException(400, '秘密情報を検索欄に入力しないでください。')
    url = body.url or (body.query.strip() if body.query.strip().startswith(('https://', 'http://')) else None)
    if settings['mode'] == 'demo':
        return ai.demo_reply(settings, body.query, web=True, url=url)
    rid = body.request_id or store.uid()
    if rid in research_tasks:
        raise HTTPException(409, 'この調査は処理中です。')
    task = asyncio.current_task()
    research_tasks[rid] = task
    try:
        if settings['mode'] == 'local':
            return await local_answer(settings, [{'role': 'user', 'content': body.query}], web=not bool(url), url=url)
        return await ai.respond(settings, [{'role': 'user', 'content': body.query}], web=not bool(url), url=url)
    except asyncio.CancelledError:
        note = ('有料APIは使用していません。Ollama側や検索先で開始済みの処理が残る場合があります。'
                if settings['mode'] == 'local' else '外部に送信済みの処理や料金は取り消せない場合があります。')
        return JSONResponse({'detail': '調査を停止しました。' + note}, status_code=409)
    finally:
        if research_tasks.get(rid) is task:
            research_tasks.pop(rid, None)


@app.post('/api/research/cancel')
async def cancel_research(body: dict = Body(...)):
    task = research_tasks.get(body.get('request_id'))
    if task:
        task.cancel()
    return {'stopped': bool(task), 'note': '開始済みの処理や送信済みデータの取り消しは保証できません。'}


app.include_router(documents.router)
app.include_router(tables.router)
from . import integrations
app.include_router(integrations.router)
app.mount('/', StaticFiles(directory=STATIC_DIR, html=True), name='ui')

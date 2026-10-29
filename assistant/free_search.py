"""Public HTML search without API keys, paid fallbacks or access-control bypasses.

DuckDuckGo's ordinary HTML page is not a guaranteed search API. Challenges,
rate limits and changes to its HTML are reported instead of worked around.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .net import read_page, safe_url

SEARCH_URL = 'https://html.duckduckgo.com/html/'
PROVIDER = 'DuckDuckGo HTML（非公式の公開ページ検索）'
MAX_SEARCH_BYTES = 2_000_000
MAX_RESULTS = 5
MAX_PAGES = 3
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result_url(value: str) -> str | None:
    """Unwrap only DuckDuckGo's public redirect; resolve/validate later."""
    value = urljoin(SEARCH_URL, value)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'):
            targets = parse_qs(parsed.query).get('uddg', [])
            if not targets:
                return None
            value = targets[0]
            parsed = urlsplit(value)
            host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
            return None
        if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'):
            return None
        return value
    except ValueError:
        return None


class SearchParser(HTMLParser):
    """Extract real result links/snippets, including nested inline markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.stack = []
        self.capture = None
        self.parts = []
        self.challenge = False
        self.pending_result = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get('class') or '').split())
        marker = (attributes.get('id') or '').lower()
        if marker in ('challenge-form', 'anomaly-form') or any(c.startswith('anomaly-modal') for c in classes):
            self.challenge = True
        if tag not in _VOID:
            self.stack.append((tag, classes))
        if self.capture:
            if tag == 'br':
                self.parts.append(' ')
            return
        if len(self.results) >= 30:
            return
        if tag == 'a' and 'result__a' in classes:
            self.pending_result = None
            if any('result--ad' in frame[1] for frame in self.stack):
                return
            url = _result_url(attributes.get('href') or '')
            if url:
                self.results.append({'title': '', 'url': url, 'snippet': ''})
                self.pending_result = len(self.results) - 1
                self.capture = ('title', len(self.stack), self.pending_result)
                self.parts = []
        elif 'result__snippet' in classes and self.pending_result is not None:
            self.capture = ('snippet', len(self.stack), self.pending_result)
            self.parts = []

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        if self.capture and index < self.capture[1]:
            field, _, result_index = self.capture
            self.results[result_index][field] = ' '.join(''.join(self.parts).split())[:300 if field == 'title' else 1500]
            self.capture = None
            self.parts = []
        del self.stack[index:]

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


async def _search_html(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=False, cookies={}) as client:
            async with client.stream('GET', SEARCH_URL, params={'q': query, 'kl': 'jp-jp'},
                                     headers={'User-Agent': 'LocalAssistant/0.2 (public search)'}) as response:
                if response.status_code in (202, 401, 403, 429):
                    raise ValueError('無料の自動検索が検索先で制限されました。認証やアクセス制限は回避しません。通常のブラウザで検索するか、後で再試行してください。')
                if response.status_code != 200:
                    raise ValueError(f'無料の検索ページを取得できませんでした（HTTP {response.status_code}）。通常のブラウザ検索をご利用ください。')
                content_type = response.headers.get('content-type', '').lower()
                if 'html' not in content_type:
                    raise ValueError('検索先がHTML以外のデータを返しました。自動検索の結果を取得できません。')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_SEARCH_BYTES:
                        raise ValueError('検索ページが大きすぎるため読み込みを停止しました（上限2MB）。')
                return bytes(data).decode(response.encoding or 'utf-8', errors='replace')
    except httpx.TimeoutException as exc:
        raise ValueError('無料の検索先への接続が時間切れになりました。通常のブラウザ検索をご利用ください。') from exc
    except httpx.HTTPError as exc:
        raise ValueError('無料の検索先へ接続できませんでした。ネットワーク・プロキシ・アクセス制限をご確認ください。有料APIへの切り替えは行っていません。') from exc


async def search(query: str) -> dict:
    """Search once, then read up to three public pages with bounded concurrency.

    ``results`` are actually obtained search results, not model-generated links.
    A result with ``read=False`` contains search snippets only, clearly labelled.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('検索語は1〜1000文字で入力してください。')
    query = query.strip()
    html = await _search_html(query)
    parser = SearchParser()
    parser.feed(html)
    parser.close()
    if parser.challenge:
        raise ValueError('検索先が人間による確認を求めています。自動処理を停止しました。通常のブラウザで検索してください。')
    results, seen = [], set()
    for item in parser.results:
        if item['url'] in seen:
            continue
        try:
            await asyncio.to_thread(safe_url, item['url'])
        except (ValueError, OSError):
            continue
        seen.add(item['url'])
        results.append({'id': f'web{len(results) + 1}', 'title': item['title'] or item['url'],
                        'url': item['url'], 'snippet': item['snippet'], 'text': '',
                        'retrieved_at': _now(), 'note': '検索結果の抜粋のみ。ページ本文は未確認です。', 'read': False})
        if len(results) == MAX_RESULTS:
            break
    if not results:
        raise ValueError('利用できる検索結果を取得できませんでした。結果がないか、検索ページの形式が変わった可能性があります。通常のブラウザ検索をご利用ください。')

    semaphore = asyncio.Semaphore(2)

    async def read_result(result):
        async with semaphore:
            try:
                page = await read_page(result['url'])
                result.update(url=page['url'], title=page.get('title') or result['title'],
                              text=page['text'][:12000], read=True, retrieved_at=_now(),
                              note='公開ページの本文を取得しました。' +
                                   ('本文は先頭部分のみを参照しています。' if page.get('truncated') or len(page['text']) > 12000 else ''))
            except (ValueError, OSError, httpx.HTTPError):
                # A blocked page remains an explicitly labelled search snippet.
                # Do not expose upstream text that could contain credentials.
                result['note'] = '検索結果の抜粋のみ。ページ本文を取得できませんでした（公開状態・接続・アクセス制限など）。'

    await asyncio.gather(*(read_result(result) for result in results[:MAX_PAGES]))
    warnings = []
    if any(not result['read'] for result in results):
        warnings.append('本文を取得できていない情報源があります。検索結果の抜粋と本文確認済みの情報を区別してください。')
    return {'query': query, 'provider': PROVIDER, 'results': results, 'warnings': warnings}

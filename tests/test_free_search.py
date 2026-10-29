"""Free-search parsing/network failures using synthetic HTML, never live results."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from assistant import free_search


def html_results(count=2):
    # Deliberately synthetic .example sources; these are not search evidence.
    return '<html><body>' + ''.join(
        f'<div class="result"><h2><a class="result__a" href="https://source{index}.example/page">'
        f'資料 <b>{index}</b> &amp; 公式</a></h2>'
        f'<a class="result__snippet">確認用の<span>抜粋 {index}</span>。</a></div>'
        for index in range(count)) + '</body></html>'


class FreeSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_result_fields_and_final_page_are_kept(self):
        async def read(url):
            return {'url': url + '/final', 'title': '取得した題名', 'text': '実際に取得した本文です。' * 10, 'truncated': False}
        with patch.object(free_search, '_search_html', AsyncMock(return_value=html_results())), \
                patch.object(free_search, 'safe_url', side_effect=lambda url: url), \
                patch.object(free_search, 'read_page', AsyncMock(side_effect=read)) as fetch:
            result = await free_search.search('映像制作')
        self.assertEqual(len(result['results']), 2)
        self.assertEqual(fetch.await_count, 2)
        first = result['results'][0]
        self.assertTrue(first['read'])
        self.assertEqual(first['id'], 'web1')
        self.assertEqual(first['title'], '取得した題名')
        self.assertEqual(first['url'], 'https://source0.example/page/final')
        self.assertIn('抜粋 0', first['snippet'])
        self.assertIn('実際に取得した本文', first['text'])
        self.assertTrue(first['retrieved_at'].endswith('+00:00'))
        self.assertEqual(result['warnings'], [])
        self.assertIn('非公式', result['provider'])

    async def test_failed_pages_remain_explicit_snippets_not_fabricated_text(self):
        with patch.object(free_search, '_search_html', AsyncMock(return_value=html_results())), \
                patch.object(free_search, 'safe_url', side_effect=lambda url: url), \
                patch.object(free_search, 'read_page', AsyncMock(side_effect=ValueError('sensitive upstream text'))):
            result = await free_search.search('検索')
        self.assertEqual(len(result['results']), 2)
        for item in result['results']:
            self.assertFalse(item['read'])
            self.assertEqual(item['text'], '')
            self.assertIn('抜粋のみ', item['note'])
            self.assertNotIn('sensitive', item['note'])
        self.assertTrue(result['warnings'])

    async def test_result_count_page_count_and_concurrency_are_bounded(self):
        active = maximum = 0
        async def read(url):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return {'url': url, 'title': '題名', 'text': '取得本文' * 4000, 'truncated': False}
        with patch.object(free_search, '_search_html', AsyncMock(return_value=html_results(9))), \
                patch.object(free_search, 'safe_url', side_effect=lambda url: url), \
                patch.object(free_search, 'read_page', AsyncMock(side_effect=read)) as fetch:
            result = await free_search.search('検索')
        self.assertEqual(len(result['results']), 5)
        self.assertEqual(fetch.await_count, 3)
        self.assertLessEqual(maximum, 2)
        self.assertEqual(sum(item['read'] for item in result['results']), 3)
        self.assertEqual(len(result['results'][0]['text']), 12000)
        self.assertIn('先頭部分', result['results'][0]['note'])

    async def test_challenge_and_empty_results_never_call_pages(self):
        for html in ('<form id="challenge-form">' + html_results() + '</form>', '<p>検索結果なし</p>'):
            with self.subTest(html=html[:20]), \
                    patch.object(free_search, '_search_html', AsyncMock(return_value=html)), \
                    patch.object(free_search, 'read_page', AsyncMock()) as fetch:
                with self.assertRaises(ValueError):
                    await free_search.search('検索')
                fetch.assert_not_called()

    async def test_redirect_unwrapping_dedup_and_private_results_are_not_fetched(self):
        html = '''<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fofficial.example%2Fdoc&amp;rut=test">実在候補</a>
        <span class="result__snippet">検索抜粋</span>
        <a class="result__a" href="https://official.example/doc">重複</a>
        <a class="result__a" href="http://127.0.0.1/private">内部</a>
        <a class="result__a" href="javascript:alert(1)">スクリプト</a>
        <a class="result__a" href="https://user:password@other.example/">認証URL</a>'''
        def validate(url):
            if '127.0.0.1' in url:
                raise ValueError('private')
            return url
        with patch.object(free_search, '_search_html', AsyncMock(return_value=html)), \
                patch.object(free_search, 'safe_url', side_effect=validate), \
                patch.object(free_search, 'read_page', AsyncMock(side_effect=ValueError('unavailable'))) as fetch:
            result = await free_search.search('検索')
        self.assertEqual([item['url'] for item in result['results']], ['https://official.example/doc'])
        self.assertEqual(fetch.await_args.args, ('https://official.example/doc',))

    async def test_cancellation_propagates_without_paid_or_fake_fallback(self):
        started = asyncio.Event()
        async def wait(query):
            started.set()
            await asyncio.Event().wait()
        with patch.object(free_search, '_search_html', side_effect=wait), \
                patch.object(free_search, 'read_page', AsyncMock()) as fetch:
            task = asyncio.create_task(free_search.search('検索'))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            fetch.assert_not_called()

    async def test_http_request_preserves_proxy_defaults_encodes_query_and_does_not_retry(self):
        original_client = httpx.AsyncClient
        requests, client_options = [], []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, text=html_results())
        transport = httpx.MockTransport(handle)
        def client(**kwargs):
            client_options.append(kwargs)
            return original_client(transport=transport, **kwargs)
        query = '日本語 & other=秘密ではないテスト'
        with patch.object(free_search.httpx, 'AsyncClient', side_effect=client):
            html = await free_search._search_html(query)
        self.assertIn('result__a', html)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params['q'], query)
        self.assertEqual(requests[0].url.host, 'html.duckduckgo.com')
        self.assertEqual(requests[0].headers['user-agent'], 'LocalAssistant/0.2 (public search)')
        self.assertNotIn('authorization', requests[0].headers)
        self.assertNotEqual(client_options[0].get('trust_env'), False)
        self.assertFalse(client_options[0]['follow_redirects'])

    async def test_access_denials_oversize_and_network_failure_are_explained(self):
        original_client = httpx.AsyncClient
        cases = [(202, 'text/html', 'challenge'), (403, 'text/html', 'blocked'),
                 (429, 'text/html', 'limited'), (302, 'text/html', 'redirect'),
                 (200, 'application/json', '{}'), (200, 'text/html', 'a' * (free_search.MAX_SEARCH_BYTES + 1))]
        for status, kind, text in cases:
            requests = []
            def handle(request):
                requests.append(request)
                return httpx.Response(status, headers={'content-type': kind}, text=text)
            transport = httpx.MockTransport(handle)
            with self.subTest(status=status, kind=kind, size=len(text)), \
                    patch.object(free_search.httpx, 'AsyncClient', side_effect=lambda **kwargs: original_client(transport=transport, **kwargs)):
                with self.assertRaises(ValueError):
                    await free_search._search_html('検索')
                self.assertEqual(len(requests), 1)
        transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError('secret', request=request)))
        with patch.object(free_search.httpx, 'AsyncClient', side_effect=lambda **kwargs: original_client(transport=transport, **kwargs)):
            with self.assertRaisesRegex(ValueError, '接続できません') as error:
                await free_search._search_html('検索')
            self.assertNotIn('secret', str(error.exception))


class SearchParserTests(unittest.TestCase):
    def test_ads_and_invalid_link_snippets_do_not_contaminate_valid_source(self):
        parser = free_search.SearchParser()
        parser.feed('''<div class="result"><a class="result__a" href="https://good.example/">題名</a><span class="result__snippet">正しい抜粋</span></div>
        <div class="result result--ad"><a class="result__a" href="https://ad.example/">広告</a><span class="result__snippet">広告の抜粋</span></div>
        <a class="result__a" href="javascript:alert(1)">不正</a><span class="result__snippet">不正な抜粋</span>''')
        self.assertEqual(parser.results, [{'title': '題名', 'url': 'https://good.example/', 'snippet': '正しい抜粋'}])

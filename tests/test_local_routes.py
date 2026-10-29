"""Local AI route integration: no paid fallback, real persistence/exports.

Inference and public search are mocked here. These tests do not establish that
Ollama, a downloaded model or a public search site is available on Windows.
"""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from assistant import ai, config, documents, free_search, integrations, local_ai, main, net, store, tables


class LocalRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(config, 'DATA_DIR', self.root),
            patch.object(config, 'DB_PATH', self.root / 'test.sqlite3'),
            patch.object(store, 'DB_PATH', self.root / 'test.sqlite3'),
            patch.object(documents, 'DATA_DIR', self.root),
            patch.object(tables, 'DATA_DIR', self.root),
            patch.object(integrations, 'DATA_DIR', self.root),
            patch.dict('os.environ', {'OPENAI_API_KEY': ''}),
            patch.object(ai, 'respond', AsyncMock(side_effect=AssertionError('Paid API fallback is forbidden'))),
            patch.object(ai, 'table_from_answer', AsyncMock(side_effect=AssertionError('Paid table fallback is forbidden'))),
        ]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        main.active_tasks.clear()
        main.research_tasks.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def use_local(self):
        response = self.client.put('/api/settings', json={'mode': 'local', 'local_model': 'qwen3:4b'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def local_answer(content='ローカルで生成した具体的な回答です。'):
        return {'content': content, 'sources': [], 'demo': False, 'provider': 'local'}

    @staticmethod
    def search_result():
        return {
            'query': '映像の最新情報', 'provider': 'DuckDuckGo HTML（非公式の公開ページ検索）',
            'results': [
                {'id': 'source-1', 'title': '取得済みの一次資料', 'url': 'https://example.com/official',
                 'snippet': '実際に検索結果から得た抜粋です。', 'text': '公開ページから取得した本文です。',
                 'retrieved_at': '2026-10-29T01:00:00+00:00', 'note': '公開ページ本文を取得しました。', 'read': True},
                {'id': 'source-2', 'title': '本文を読めなかった検索結果', 'url': 'https://example.org/news',
                 'snippet': '検索結果の抜粋のみです。', 'text': '',
                 'retrieved_at': '2026-10-29T01:00:00+00:00', 'note': '検索抜粋のみ・本文未確認', 'read': False},
            ], 'warnings': ['2件目の本文は未確認です。'],
        }

    def test_local_settings_need_no_api_key_and_survive_restart_without_losing_data(self):
        before = self.client.get('/api/state').json()['settings']
        self.assertEqual(before['mode'], 'demo')
        cid = self.client.post('/api/conversations', json={}).json()['id']
        memory = self.client.post('/api/memories', json={'content': '映像は青色の照明で撮影する'}).json()
        settings = self.use_local()
        self.assertEqual(settings['local_model'], 'qwen3:4b')
        self.client_context.__exit__(None, None, None)
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        state = self.client.get('/api/state').json()
        self.assertEqual(state['settings']['mode'], 'local')
        self.assertEqual(state['settings']['local_model'], 'qwen3:4b')
        self.assertEqual(state['conversations'][0]['id'], cid)
        self.assertEqual(state['memories'][0]['id'], memory['id'])
        self.assertEqual(self.client.put('/api/settings', json={'mode': 'live'}).status_code, 400)
        self.assertEqual(self.client.get('/api/state').json()['settings']['mode'], 'local')

    def test_local_chat_saves_actual_response_and_prior_conversation(self):
        self.use_local()
        cid = self.client.post('/api/conversations', json={}).json()['id']
        reply = AsyncMock(side_effect=[self.local_answer('前半は夜の街を静かに見せましょう。'),
                                     self.local_answer('夜の街に続いて後半は夜明けを描きましょう。')])
        search = AsyncMock(side_effect=AssertionError('An ordinary conversation must not automatically query the Web'))
        with patch.object(local_ai, 'respond', reply), patch.object(free_search, 'search', search):
            first = self.client.post('/api/chat', json={'conversation_id': cid, 'message': 'MVの前半を考えたい'})
            second = self.client.post('/api/chat', json={'conversation_id': cid, 'message': 'その続きの後半を考えたい'})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()['message']['demo'])
        self.assertIn('夜明け', second.json()['message']['content'])
        sent = reply.call_args_list[1].args[1]
        self.assertEqual([m['role'] for m in sent], ['user', 'assistant', 'user'])
        self.assertIn('夜の街', sent[1]['content'])
        persisted = self.client.get('/api/conversations/' + cid).json()['messages']
        self.assertEqual(persisted[-1]['content'], second.json()['message']['content'])
        search.assert_not_awaited()

    def test_unavailable_local_model_returns_explained_error_without_paid_or_demo_fallback(self):
        self.use_local()
        cid = self.client.post('/api/conversations', json={}).json()['id']
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'configured-but-never-use-in-local-mode'}), \
             patch.object(local_ai, 'respond', AsyncMock(side_effect=ValueError('Ollama が起動していません。'))), \
             patch.object(ai, 'demo_reply', side_effect=AssertionError('No silent demo fallback')):
            response = self.client.post('/api/chat', json={'conversation_id': cid, 'message': 'MVの構成を考えたい'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('Ollama', response.json()['detail'])
        self.assertNotIn('configured-but-never', response.text)
        history = self.client.get('/api/conversations/' + cid).json()['messages']
        self.assertEqual([m['role'] for m in history], ['user'])
        self.assertEqual(store.get_settings()['mode'], 'local')

    def test_local_search_passes_only_obtained_context_and_preserves_source_locations(self):
        self.use_local()
        context = self.search_result()
        search = AsyncMock(return_value=context)
        reply = AsyncMock(return_value=self.local_answer('一次資料を要約しました。2件目は検索抜粋だけです。'))
        with patch.object(free_search, 'search', search), patch.object(local_ai, 'respond', reply):
            response = self.client.post('/api/chat', json={'message': '映像の最新情報を検索して'})
        self.assertEqual(response.status_code, 200, response.text)
        search.assert_awaited_once()
        self.assertEqual(reply.call_args.kwargs['web_context'], context)
        sources = response.json()['message']['sources']
        self.assertEqual({s['url'] for s in sources}, {s['url'] for s in context['results']})
        by_url = {s['url']: s for s in sources}
        self.assertIn('本文未確認', by_url['https://example.org/news']['note'])
        self.assertEqual(by_url['https://example.com/official']['retrieved_at'], context['results'][0]['retrieved_at'])
        self.assertFalse(response.json()['message']['demo'])

    def test_explicit_search_phrases_use_free_search_not_an_unconnected_local_answer(self):
        self.use_local()
        for message in ('動画編集ソフトを検索したい', '動画編集の情報を調査して', 'Webで撮影機材を探して'):
            with self.subTest(message=message), \
                 patch.object(free_search, 'search', AsyncMock(return_value=self.search_result())) as search, \
                 patch.object(local_ai, 'respond', AsyncMock(return_value=self.local_answer())):
                response = self.client.post('/api/chat', json={'message': message})
                self.assertEqual(response.status_code, 200, response.text)
                search.assert_awaited_once()

    def test_failed_free_search_never_generates_a_fake_researched_answer(self):
        self.use_local()
        infer = AsyncMock(side_effect=AssertionError('No fake answer after search failure'))
        with patch.object(free_search, 'search', AsyncMock(side_effect=ValueError('検索先が人間による確認を求めています。'))), \
             patch.object(local_ai, 'respond', infer):
            response = self.client.post('/api/research', json={'query': '今日のAIニュース'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('人間による確認', response.json()['detail'])
        self.assertNotIn('content', response.json())
        self.assertNotIn('sources', response.json())
        infer.assert_not_awaited()
        self.assertEqual(store.conversations(), [])

    def test_url_research_uses_obtained_page_without_search_or_paid_api(self):
        self.use_local()
        page = {'url': 'https://example.com/final-page', 'title': '取得したページ', 'text': 'ページから実際に取得した本文です。', 'truncated': True}
        page_mock = AsyncMock(return_value=page)
        reply = AsyncMock(return_value=self.local_answer('取得した本文をローカルで要約しました。'))
        with patch.object(main, 'read_page', page_mock, create=True), patch.object(net, 'read_page', page_mock), \
             patch.object(free_search, 'search', AsyncMock(side_effect=AssertionError('A URL summary does not need search'))), \
             patch.object(local_ai, 'respond', reply):
            response = self.client.post('/api/research', json={'query': 'このURLを要約して', 'url': 'https://example.com/original'})
        self.assertEqual(response.status_code, 200, response.text)
        page_mock.assert_awaited_once_with('https://example.com/original')
        self.assertEqual(reply.call_args.kwargs['page'], page)
        source = response.json()['sources'][0]
        self.assertEqual(source['url'], page['url'])
        self.assertEqual(source['title'], page['title'])
        self.assertTrue(source['retrieved_at'])
        self.assertIn('24,000', source['note'])
        self.assertFalse(response.json()['demo'])

    def test_local_table_generation_exports_an_actual_workbook_without_paid_conversion(self):
        self.use_local()
        spec = {'title': '撮影予定', 'columns': ['場面', '所要時間（分）'], 'rows': [['夜の街', 30]], 'notes': ['ユーザーの企画案に基づく予定']}
        converter = AsyncMock(return_value=spec)
        with patch.object(local_ai, 'respond', AsyncMock(return_value=self.local_answer('夜の街を30分撮影する計画です。'))), \
             patch.object(local_ai, 'table_from_answer', converter):
            response = self.client.post('/api/chat', json={'message': '撮影の予定をExcelにまとめて'})
        self.assertEqual(response.status_code, 200, response.text)
        converter.assert_awaited_once()
        table = response.json()['table']
        download = self.client.get(next(f['url'] for f in table['files'] if f['format'] == 'xlsx'))
        self.assertEqual(download.status_code, 200)
        workbook = load_workbook(io.BytesIO(download.content), data_only=False)
        try:
            self.assertEqual(workbook['データ']['A2'].value, '夜の街')
            self.assertEqual(workbook['データ']['B2'].value, 30)
        finally:
            workbook.close()
        self.assertIn('Excel・CSVを作成しました', response.json()['message']['content'])

    def test_status_and_download_routes_require_explicit_download_confirmation(self):
        self.use_local()
        status = {'running': False, 'available': False, 'model': 'qwen3:4b', 'models': [], 'message': 'Ollama を起動してください。', 'download_running': False}
        with patch.object(local_ai, 'status', AsyncMock(return_value=status)) as inspect_status:
            response = self.client.get('/api/local/status')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['running'], False)
        inspect_status.assert_awaited_once()
        self.assertNotIn('OPENAI_API_KEY', response.text)
        # Downloading is explicit and independent of selecting the local mode.
        with patch.object(local_ai, 'begin_download', AsyncMock(return_value={'download_running': True})) as download:
            denied = self.client.post('/api/local/download', json={'model': 'qwen3:4b', 'confirm': False})
            self.assertEqual(denied.status_code, 400, denied.text)
            download.assert_not_awaited()
            accepted = self.client.post('/api/local/download', json={'model': 'qwen3:4b', 'confirm': True})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        download.assert_awaited_once_with('qwen3:4b')

    def test_cancel_local_inference_stops_without_paid_fallback_or_fabricated_result(self):
        self.use_local()
        cid = self.client.post('/api/conversations', json={}).json()['id']

        async def scenario():
            started = asyncio.Event()

            async def slow_local(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()

            with patch.object(local_ai, 'respond', slow_local):
                pending = asyncio.create_task(main.chat(main.ChatInput(conversation_id=cid, message='MVの構成を考えたい')))
                await asyncio.wait_for(started.wait(), timeout=2)
                result = await main.cancel({'conversation_id': cid})
                self.assertTrue(result['stopped'])
                stopped = await asyncio.wait_for(pending, timeout=2)
                self.assertEqual(stopped.status_code, 409)
                self.assertNotIn(cid, main.active_tasks)

        asyncio.run(scenario())
        history = store.conversation(cid)['messages']
        self.assertIn('処理を停止しました', history[-1]['content'])
        self.assertFalse(history[-1]['demo'])


if __name__ == '__main__':
    unittest.main()

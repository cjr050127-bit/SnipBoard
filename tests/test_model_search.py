"""Optional model search uses synthetic thumbnails and fake/mock API services."""
from __future__ import annotations

import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from snipboard.catalog import Catalog
from snipboard.model_search import (ModelConfig, ModelSearchError, _NoRedirect,
                                    _parse_response, _post_json, hard_condition_plan,
                                    is_local_endpoint, model_condition_text, is_explicit_label, search_with_model)
from snipboard.query_plan import parse_query


def envelope(results):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'results': results})}}]}


def payload_ids(payload):
    return [entry['text'].removeprefix('图片编号：') for entry in payload['messages'][1]['content']
            if entry['type'] == 'text' and entry['text'].startswith('图片编号：')]


class ModelSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.catalog = Catalog(Path(self.temporary.name) / 'catalog')
        self.addCleanup(self.catalog.close)
        self.config = ModelConfig('https://models.example/v1', 'vision-test', 'test-key')
        self.thumbnails = []
        with self.catalog.db:
            self.catalog.db.execute('INSERT INTO boards(id,source,group_id,name) VALUES(1,?,?,?)',
                                    (self.catalog.local_source, 'mock', '测试参考'))
            for i in range(8):
                digest = f'{i:064x}'
                self.catalog.db.execute('INSERT INTO items(board_id,digest,source_name,note) VALUES(1,?,?,?)',
                                        (digest, f'item-{i}.png', '普通参考图'))
                self.catalog.db.execute('INSERT INTO features VALUES(?,?,?,?,?)',
                                        (digest, 3, 1600 if i % 2 == 0 else 900,
                                         900 if i % 2 == 0 else 1600,
                                         json.dumps({'palette': [[227, 66, 66, 1]], 'brightness': 50})))
                path = self.catalog.thumbnails / f'{digest}-800.png'
                Image.new('RGB', (800, 600), (227, 66, 66)).save(path)
                self.thumbnails.append(path)

    def plan(self, text, previous=None):
        return parse_query(text, self.catalog.label_rows(), self.catalog.work_groups(), previous)

    @staticmethod
    def matching_response(_config, payload, _stopped):
        return envelope([{'id': digest, 'match': True, 'reason': '符合用户画面需求'} for digest in payload_ids(payload)])

    def test_semantic_keyword_does_not_hide_untagged_images(self):
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, self.plan('红衣侧脸横图'), '红衣侧脸横图', self.config)
        self.assertEqual((result.analyzed, result.total, result.selected), (4, 4, 4))
        self.assertEqual(len(result.rows), 4)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(row['width'] > row['height'] for row in result.rows))

    def test_candidate_and_batch_limits_are_exact_and_report_partial_coverage(self):
        config = replace(self.config, candidate_limit=5, batch_size=4)
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, self.plan('找侧脸'), '找侧脸', config)
        self.assertEqual((result.analyzed, result.total, result.selected), (5, 8, 5))
        self.assertEqual([len(payload_ids(call.args[1])) for call in request.call_args_list], [4, 1])
        self.assertIn('其余图片尚未分析', ' '.join(result.warnings))

    def test_implicit_known_label_still_sends_untagged_candidates_to_vision(self):
        self.catalog.set_tags(f'{0:064x}', ['侧脸'])
        plan = self.plan('找侧脸横图')
        label = next(condition for condition in plan.conditions if condition.kind == 'label')
        self.assertFalse(is_explicit_label(plan, label))
        self.assertEqual(model_condition_text(plan, label), '画面需要满足：侧脸')
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, plan, plan.query, self.config)
        ids = payload_ids(request.call_args.args[1])
        self.assertIn(f'{2:064x}', ids)
        self.assertEqual(result.total, 4)

    def test_explicit_label_scope_remains_a_strict_hard_filter(self):
        self.catalog.set_tags(f'{0:064x}', ['侧脸'])
        plan = self.plan('侧脸标签里的图')
        label = next(condition for condition in plan.conditions if condition.kind == 'label')
        self.assertTrue(is_explicit_label(plan, label))
        self.assertEqual(model_condition_text(plan, label), '标签：侧脸')
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, plan, plan.query, self.config)
        request.assert_not_called()
        self.assertEqual([row['digest'] for row in result.rows], [f'{0:064x}'])
        self.assertEqual(result.total, 1)
        self.assertEqual(result.analyzed, 0)
        self.assertTrue(result.local_only)

    def test_single_image_batch_compatible_mode(self):
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, self.plan('侧脸横图'), '侧脸横图', replace(self.config, batch_size=1))
        self.assertEqual(request.call_count, 4)
        self.assertEqual(result.analyzed, 4)

    def test_only_saved_thumbnail_reencoded_at_512_max_and_no_original_fallback(self):
        original_open = Image.open
        paths = []
        def opened(path, *args, **kwargs):
            paths.append(Path(path))
            self.assertEqual(Path(path).parent, self.catalog.thumbnails)
            return original_open(path, *args, **kwargs)
        with patch('snipboard.model_search.Image.open', side_effect=opened), \
             patch.object(self.catalog, 'ensure_feature', side_effect=AssertionError('No original analysis')), \
             patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            search_with_model(self.catalog, self.plan('侧脸横图'), '侧脸横图', self.config)
        self.assertEqual(len(paths), 4)
        for entry in request.call_args.args[1]['messages'][1]['content']:
            if entry['type'] != 'image_url':
                continue
            url = entry['image_url']['url']
            self.assertTrue(url.startswith('data:image/jpeg;base64,'))
            with original_open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as image:
                self.assertLessEqual(max(image.size), 512)
                self.assertFalse(image.getexif())

    def test_missing_thumbnail_is_unknown_and_does_not_open_original(self):
        for path in self.thumbnails:
            path.unlink()
        with patch('snipboard.model_search._post_json') as request:
            result = search_with_model(self.catalog, self.plan('侧脸'), '侧脸', self.config)
        self.assertEqual(result.analyzed, 0)
        self.assertEqual(result.total, 8)
        self.assertTrue(result.warnings)
        request.assert_not_called()

    def test_scope_and_hard_orientation_are_respected(self):
        scope = (f'{2:064x}', f'{3:064x}')
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response):
            result = search_with_model(self.catalog, self.plan('横图侧脸'), '横图侧脸', self.config, scope_digests=scope)
        self.assertEqual([row['digest'] for row in result.rows], [f'{2:064x}'])

    def test_mixed_local_and_visual_or_is_rejected_before_image_read_or_api(self):
        self.catalog.set_tags(f'{0:064x}', ['人物'])
        self.catalog.create_work_group('A', [f'{0:064x}'])
        queries = ('在A组或侧脸', '人物标签或侧脸', '横图或侧脸',
                   '不要在A组或侧脸', '不要人物标签或侧脸', '不要横图或侧脸')
        for text in queries:
            with self.subTest(query=text), \
                 patch('snipboard.model_search._thumbnail_data') as thumbnail, \
                 patch('snipboard.model_search._post_json') as request:
                with self.assertRaises(ModelSearchError) as caught:
                    search_with_model(self.catalog, self.plan(text), text, self.config)
                self.assertIn('分开搜索', str(caught.exception))
                thumbnail.assert_not_called()
                request.assert_not_called()

    def test_hard_and_visual_conditions_send_only_visual_requirement(self):
        self.catalog.set_tags(f'{0:064x}', ['人物'])
        self.catalog.create_work_group('A', [f'{0:064x}'])
        text = '在A组，人物标签，宽度至少1600，横图，侧脸'
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, self.plan(text), text, self.config)
        self.assertEqual(result.total, 1)
        self.assertFalse(result.local_only)
        self.assertEqual(payload_ids(request.call_args.args[1]), [f'{0:064x}'])
        prompt = request.call_args.args[1]['messages'][1]['content'][0]['text']
        self.assertIn('侧脸', prompt)
        for local_condition in ('工作组', '人物', '1600', '宽', '横'):
            self.assertNotIn(local_condition, prompt)

    def test_local_only_properties_do_not_encode_thumbnails_or_use_model_limit(self):
        text = '横图，宽度至少1600'
        config = replace(self.config, candidate_limit=1)
        with patch('snipboard.model_search._thumbnail_data') as thumbnail, \
             patch('snipboard.model_search._post_json') as request:
            result = search_with_model(self.catalog, self.plan(text), text, config)
        self.assertTrue(result.local_only)
        self.assertEqual((result.analyzed, result.total, result.selected), (0, 4, 4))
        self.assertTrue(all(row['width'] == 1600 for row in result.rows))
        self.assertEqual(set(result.reasons.values()), {'已由本地标签/属性确认'})
        self.assertIn('未发送缩略图', ' '.join(result.warnings))
        thumbnail.assert_not_called()
        request.assert_not_called()

    def test_visual_or_including_implicit_tags_remains_available(self):
        self.catalog.set_tags(f'{0:064x}', ['侧脸'])
        for text in ('侧脸或逆光', '不要侧脸或逆光'):
            with self.subTest(query=text), \
                 patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
                result = search_with_model(self.catalog, self.plan(text), text, self.config)
            self.assertFalse(result.local_only)
            self.assertEqual(result.analyzed, 8)
            prompt = request.call_args.args[1]['messages'][1]['content'][0]['text']
            self.assertIn('任一满足', prompt)
            self.assertIn('画面需要满足：侧脸', prompt)
            self.assertIn('逆光', prompt)
            if text.startswith('不要'):
                self.assertIn('排除', prompt)

    def test_followup_keeps_previous_content_and_exclusion_in_request(self):
        first = self.plan('找红色衣服，不要照片')
        second = self.plan('再只留侧脸', first)
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            search_with_model(self.catalog, second, second.query, self.config)
        text = request.call_args.args[1]['messages'][1]['content'][0]['text']
        self.assertIn('红色衣服', text)
        self.assertIn('照片', text)
        self.assertIn('排除', text)
        self.assertIn('侧脸', text)

    def test_invalid_numeric_condition_does_not_broaden_model_candidates(self):
        with patch('snipboard.model_search._post_json') as request:
            result = search_with_model(self.catalog, self.plan('比例16:0，侧脸'), '比例16:0，侧脸', self.config)
        self.assertEqual(result.total, 0)
        request.assert_not_called()

    def test_deleted_keyword_is_not_reinserted_from_original_query(self):
        plan = self.plan('侧脸，逆光')
        removed = next(condition for condition in plan.conditions if condition.value == '逆光')
        edited = plan.without(removed.id)
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            search_with_model(self.catalog, edited, edited.query, self.config)
        text = request.call_args.args[1]['messages'][1]['content'][0]['text']
        self.assertIn('侧脸', text)
        self.assertNotIn('逆光', text)

    def test_replaced_orientation_is_not_reinserted_from_previous_input(self):
        first = self.plan('侧脸横图')
        edited = self.plan('改成竖图', first)
        with patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, edited, edited.query, self.config)
        text = request.call_args.args[1]['messages'][1]['content'][0]['text']
        self.assertNotIn('竖向', text)
        self.assertNotIn('横', text)
        self.assertIn('侧脸', text)
        self.assertTrue(all(row['width'] < row['height'] for row in result.rows))

    def test_cancel_before_search_never_calls_service(self):
        event = threading.Event()
        event.set()
        with patch('snipboard.model_search._post_json') as request:
            result = search_with_model(self.catalog, self.plan('侧脸'), '侧脸', self.config, event)
        self.assertTrue(result.cancelled)
        request.assert_not_called()

    def test_cancel_between_batches_never_sends_next_batch(self):
        event = threading.Event()
        encoded = 0
        def thumbnail(*_):
            nonlocal encoded
            encoded += 1
            if encoded == 5:
                event.set()
            return 'data:image/jpeg;base64,AA=='
        with patch('snipboard.model_search._thumbnail_data', side_effect=thumbnail), \
             patch('snipboard.model_search._post_json', side_effect=self.matching_response) as request:
            result = search_with_model(self.catalog, self.plan('侧脸'), '侧脸', self.config, event)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.analyzed, 4)
        self.assertEqual(result.rows, [])
        self.assertEqual(request.call_count, 1)

    def test_model_failure_reports_valid_analysis_and_is_never_retried(self):
        answers = [self.matching_response(self.config, {'messages': [{}, {'content':
                    [{'type': 'text', 'text': '图片编号：' + f'{i:064x}'} for i in range(7, 3, -1)]}]}, None),
                   ModelSearchError('模型请求超时；不会自动重试。')]
        with patch('snipboard.model_search._post_json', side_effect=answers) as request:
            with self.assertRaises(ModelSearchError) as caught:
                search_with_model(self.catalog, self.plan('侧脸'), '侧脸', self.config)
        self.assertEqual(request.call_count, 2)
        self.assertEqual((caught.exception.analyzed, caught.exception.total, caught.exception.selected), (4, 8, 8))

    def test_response_rejects_forged_missing_duplicate_ids_and_non_boolean(self):
        good = {'id': 'a', 'match': True, 'reason': '符合'}
        invalid = [[dict(good, id='invented')], [], [good, good],
                   [dict(good, match='true')], [dict(good, reason='')]]
        for results in invalid:
            with self.subTest(results=results), self.assertRaises(ModelSearchError):
                _parse_response(envelope(results), {'a'})
        refused = envelope([good])
        refused['choices'][0]['message']['refusal'] = 'refused'
        with self.assertRaises(ModelSearchError):
            _parse_response(refused, {'a'})
        truncated = envelope([good])
        truncated['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ModelSearchError):
            _parse_response(truncated, {'a'})

    def test_valid_fenced_json_and_negative_result_are_accepted(self):
        response = envelope([{'id': 'a', 'match': False, 'reason': '不符合'}])
        response['choices'][0]['message']['content'] = '```json\n' + response['choices'][0]['message']['content'] + '\n```'
        self.assertEqual(_parse_response(response, {'a'}), {'a': (False, '不符合')})

    def test_duplicate_json_keys_are_rejected(self):
        response = {'choices': [{'message': {'content': '{"results":[],"results":[]}'}}]}
        with self.assertRaises(ModelSearchError):
            _parse_response(response, set())

    def test_malformed_envelopes_always_raise_safe_model_error(self):
        for response in (None, [], {}, {'choices': None}, {'choices': {}},
                         {'choices': []}, {'choices': [None]}, {'choices': ['wrong']},
                         {'choices': [{'message': None}]}, {'choices': [{'message': 'wrong'}]}):
            with self.subTest(response=response), self.assertRaises(ModelSearchError):
                _parse_response(response, {'a'})

    def test_endpoint_validation_key_repr_and_error_redaction(self):
        for base in ('http://localhost:11434/v1', 'http://127.0.0.1:1234/v1', 'http://[::1]:1234/v1'):
            self.assertTrue(is_local_endpoint(base))
            self.assertTrue(replace(self.config, base_url=base).validate().endswith('/chat/completions'))
        full = replace(self.config, base_url='https://models.example/v1/chat/completions/')
        self.assertEqual(full.validate(), 'https://models.example/v1/chat/completions')
        for base in ('http://models.example/v1', 'http://localhost.evil/v1',
                     'https://username:secret@models.example/v1', 'https://models.example/v1?key=secret'):
            with self.subTest(base=base), self.assertRaises(ModelSearchError) as caught:
                replace(self.config, base_url=base).validate()
            self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('test-key', repr(self.config))
        for changes in ({'candidate_limit': 41}, {'batch_size': 0}, {'timeout': 121}, {'api_key': 'secret\n'}):
            with self.subTest(changes=changes), self.assertRaises(ModelSearchError):
                replace(self.config, **changes).validate()

    def test_post_payload_headers_and_safe_http_error_no_retry(self):
        fake_opener = unittest.mock.Mock()
        fake_opener.open.return_value = io.BytesIO(json.dumps(envelope([])).encode())
        with patch('snipboard.model_search.build_opener', return_value=fake_opener):
            self.assertEqual(_post_json(self.config, {'model': 'test'}, lambda: False), envelope([]))
        request = fake_opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://models.example/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        fake_opener.reset_mock()
        fake_opener.open.side_effect = HTTPError(request.full_url, 401, 'test-key private body', {}, io.BytesIO())
        with patch('snipboard.model_search.build_opener', return_value=fake_opener), self.assertRaises(ModelSearchError) as caught:
            _post_json(self.config, {}, lambda: False)
        self.assertNotIn('test-key', str(caught.exception))
        self.assertEqual(fake_opener.open.call_count, 1)

    def test_redirect_never_resends_credentials_to_another_origin(self):
        with self.assertRaises(ModelSearchError):
            _NoRedirect().redirect_request(Request('https://models.example/v1', headers={'Authorization': 'Bearer secret'}),
                                           None, 307, '', {}, 'https://another.example/collect')

    def test_slow_local_request_times_out_once_and_cancel_returns_after_network_timeout(self):
        captured = []
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                captured.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                time.sleep(1.3)
                try:
                    self.wfile.write(b' ' * 100)
                except OSError:
                    pass
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()
        config = ModelConfig(f'http://127.0.0.1:{server.server_port}/v1', 'local-test', timeout=1)
        try:
            started = time.perf_counter()
            with self.assertRaises(ModelSearchError):
                _post_json(config, {'model': 'test'}, lambda: False)
            self.assertLess(time.perf_counter() - started, 2.)
            self.assertEqual(len(captured), 1)
            cancelled = threading.Event()
            timer = threading.Timer(.05, cancelled.set)
            timer.start()
            started = time.perf_counter()
            self.assertIsNone(_post_json(config, {'model': 'test'}, cancelled.is_set))
            timer.join()
            self.assertLess(time.perf_counter() - started, 2.)
            self.assertEqual(len(captured), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_slow_drip_body_obeys_whole_request_deadline(self):
        class DripHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '1000')
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b' ' * 10)
                        self.wfile.flush()
                    except OSError:
                        break
                    time.sleep(.1)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), DripHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()
        try:
            config = ModelConfig(f'http://127.0.0.1:{server.server_port}/v1', 'local-test', timeout=1)
            started = time.perf_counter()
            with self.assertRaises(ModelSearchError):
                _post_json(config, {'model': 'test'}, lambda: False)
            self.assertLess(time.perf_counter() - started, 2.)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_fake_local_service_uses_real_wire_format_without_external_calls(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, payload))
                body = json.dumps(ModelSearchTests.matching_response(None, payload, None)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ModelConfig(f'http://127.0.0.1:{server.server_port}/v1', 'local-test')
            result = search_with_model(self.catalog, self.plan('侧脸横图'), '侧脸横图', config)
            self.assertEqual(result.analyzed, 4)
            self.assertEqual(captured[0][0], '/v1/chat/completions')
            self.assertEqual(captured[0][1]['model'], 'local-test')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()

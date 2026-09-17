import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pokefire.server import Application, main


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_starts_and_stops_monitor(self):
        for enabled in (True, False):
            controller = Mock()
            app = Application(controller, Path('/tmp'), start_monitor=enabled)
            events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
            output = []
            async def receive():
                return next(events)
            async def send(event):
                output.append(event['type'])
            await app({'type': 'lifespan'}, receive, send)
            self.assertEqual(output, ['lifespan.startup.complete', 'lifespan.shutdown.complete'])
            self.assertEqual(controller.start.call_count, int(enabled))
            controller.close.assert_called_once()

    async def test_startup_failure_closes_controller(self):
        controller = Mock()
        controller.start.side_effect = ValueError('already running')
        app = Application(controller, Path('/tmp'))
        output = []
        async def receive():
            return {'type': 'lifespan.startup'}
        async def send(event):
            output.append(event)
        await app({'type': 'lifespan'}, receive, send)
        self.assertEqual(output[0]['type'], 'lifespan.startup.failed')
        controller.close.assert_called_once()

    async def test_lan_routes_origin_and_size_validation(self):
        controller = Mock()
        controller.status.return_value = {'ebay': {'running': False}}
        app = Application(controller, Path('/tmp'), host='192.168.0.239')
        async def request(path, method='GET', host='192.168.0.239:8767', origin=None, body=b''):
            headers = [(b'host', host.encode()), (b'content-type', b'application/json')]
            if origin:
                headers.append((b'origin', origin.encode()))
            output = []
            async def receive():
                return {'type': 'http.request', 'body': body}
            async def send(event):
                output.append(event)
            await app({'type': 'http', 'method': method, 'path': path,
                       'headers': headers, 'query_string': b''}, receive, send)
            return output[0]['status'], output[1]['body']
        self.assertEqual((await request('/'))[0], 200)
        self.assertEqual(json.loads((await request('/api/status'))[1]), controller.status.return_value)
        self.assertEqual((await request('/api/status', host='attacker.example'))[0], 403)
        body = b'{"source":"ebay"}'
        self.assertEqual((await request('/api/start', 'POST', origin='http://attacker.example', body=body))[0], 403)
        controller.start.assert_not_called()
        self.assertEqual((await request('/api/start', 'POST', origin='http://192.168.0.239:8767', body=body))[0], 200)
        controller.start.assert_called_once_with('ebay')
        self.assertEqual((await request('/api/start', 'POST', body=b'x' * 256001))[0], 413)


class CommandTests(unittest.TestCase):
    @patch('pokefire.server.uvicorn.run')
    def test_cli_overrides_environment_and_can_pause_polling(self, run):
        with patch.dict(os.environ, {'PORT': 'invalid', 'HOST': '127.0.0.1'}), patch(
                'sys.argv', ['pokefire', '--host', '192.168.0.239', '--port', '8767', '--no-monitor']):
            main()
        self.assertEqual(run.call_args.kwargs['host'], '192.168.0.239')
        self.assertEqual(run.call_args.kwargs['port'], 8767)
        self.assertFalse(run.call_args.args[0].start_monitor)

    @patch('pokefire.server.uvicorn.run')
    def test_invalid_port_never_starts(self, run):
        with patch('sys.argv', ['pokefire', '--port', '0']):
            with self.assertRaises(SystemExit):
                main()
        run.assert_not_called()

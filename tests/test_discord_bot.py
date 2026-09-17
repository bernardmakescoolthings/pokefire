import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from pokefire.discord_bot import DiscordBot, DiscordDeliveryError, message, notifier, validate
from pokefire.monitor import State, alert_payload, load_config, main, poll
from pokefire.viewer import Controller


class DiscordTests(unittest.TestCase):
    config = {'discord': {'enabled': True, 'channel_id': '123456789012345678'}}
    item = {'itemId': '1', 'title': 'Charizard Base Set @everyone',
            'itemWebUrl': 'https://example.com/listing',
            'price': {'value': '100', 'currency': 'USD'}}

    @patch.dict(os.environ, {}, clear=True)
    @patch('pokefire.discord_bot.urlopen')
    def test_disabled_needs_no_token_and_sends_nothing(self, open_url):
        terminal = Mock()
        notifier({'discord': {'enabled': False}}, terminal, alert_payload)(self.item, ['Charizard'])
        terminal.assert_called_once()
        open_url.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'DISCORD_BOT_TOKEN'):
            DiscordBot(self.config)

    @patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'bad\nsecret'})
    def test_invalid_token_is_not_echoed(self):
        with self.assertRaisesRegex(ValueError, '^DISCORD_BOT_TOKEN contains invalid characters') as caught:
            DiscordBot(self.config)
        self.assertNotIn('secret', str(caught.exception))

    @patch.dict(os.environ, {}, clear=True)
    @patch('pokefire.discord_bot.urlopen')
    def test_fixture_never_initializes_discord(self, open_url):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watchlist.json'
            config = load_config('fixtures/watchlist.example.json')
            config.update(self.config)
            path.write_text(json.dumps(config))
            with patch('sys.argv', ['pokefire.py', '--config', str(path), '--fixture', 'fixtures/gold-stars.json']), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(), 0)
            open_url.assert_not_called()

    @patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'private-token'})
    def test_enabled_by_default_and_requires_channel_at_startup(self):
        validate({})  # Incomplete setup must not prevent opening settings.
        with self.assertRaisesRegex(ValueError, 'set a Discord channel ID'):
            DiscordBot({})
        bot = DiscordBot({'discord': {'channel_id': '123456789012345678'}})
        self.assertTrue(bot.enabled)
        config = load_config('watchlist.json')
        self.assertTrue(config['discord']['enabled'])

    def test_validation_and_viewer_save(self):
        for settings in [None, {'enabled': 'false'},
                         {'channel_id': 123}, {'channel_id': '../foo'}, {'token': 'secret'}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate({'discord': settings})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watchlist.json'
            config = load_config('fixtures/watchlist.example.json')
            config['state_file'] = str(Path(directory) / 'state.sqlite3')
            path.write_text(json.dumps(config))
            controller = Controller(path)
            payload = controller.config()
            payload['config'].update(self.config)
            controller.save(payload)
            self.assertEqual(load_config(path)['discord'], self.config['discord'])

    @patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'private-token'})
    @patch('pokefire.discord_bot.urlopen')
    def test_request_and_safe_message(self, open_url):
        DiscordBot(self.config).send(alert_payload(self.item, ['Charizard']))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'https://discord.com/api/v10/channels/123456789012345678/messages')
        self.assertEqual(request.get_header('Authorization'), 'Bot private-token')
        body = json.loads(request.data)
        self.assertEqual(body['allowed_mentions'], {'parse': []})
        self.assertIn('100 USD', body['content'])
        self.assertIn(self.item['itemWebUrl'], body['content'])
        payload = alert_payload(self.item | {'title': 'x' * 5000}, ['x' * 5000])
        self.assertLessEqual(len(message(payload)['content']), 2000)

    @patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'private-token'})
    @patch('pokefire.discord_bot.urlopen')
    def test_errors_are_sanitized_and_rate_limits_preserved(self, open_url):
        bot = DiscordBot(self.config)
        for status in (401, 403, 404, 429, 500):
            open_url.side_effect = HTTPError('https://discord.com', status, 'private-token', {},
                                            io.BytesIO(b'{"retry_after": 2.5}'))
            with self.assertRaises(DiscordDeliveryError) as caught:
                bot.send(alert_payload(self.item, []))
            self.assertNotIn('private-token', str(caught.exception))
            self.assertEqual(caught.exception.retry_after, 2.5 if status == 429 else 0)
        open_url.side_effect = URLError('private-token')
        with self.assertRaisesRegex(DiscordDeliveryError, '^Discord connection failed'):
            bot.send(alert_payload(self.item, []))

    @patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'private-token'})
    @patch('pokefire.discord_bot.urlopen')
    def test_failed_delivery_remains_retryable_without_resetting_scope(self, open_url):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config('fixtures/watchlist.example.json')
            config.update(self.config)
            config.update(state_file=str(Path(directory) / 'state.sqlite3'), notify_existing=True)
            state = State(config)
            self.addCleanup(state.db.close)
            terminal = Mock()
            send = notifier(config, terminal, alert_payload)
            client = Mock()
            client.search.return_value = {'itemSummaries': [self.item]}
            open_url.side_effect = URLError('offline')
            with self.assertRaises(DiscordDeliveryError):
                poll(client, config, state, send)
            self.assertFalse(state.seen('1'))
            terminal.assert_not_called()
            open_url.side_effect = None
            self.assertEqual(poll(client, config, state, send), 1)
            self.assertEqual(poll(client, config, state, send), 0)
            terminal.assert_called_once()
            disabled = State(config | {'discord': {'enabled': False}})
            self.addCleanup(disabled.db.close)
            self.assertEqual(disabled.scope, state.scope)


if __name__ == '__main__':
    unittest.main()

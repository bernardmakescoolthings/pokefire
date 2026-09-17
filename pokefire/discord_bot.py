"""Optional Discord bot alert delivery using the standard-library HTTP client."""
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def validate(config):
    settings = config.get('discord', {})
    if not isinstance(settings, dict) or set(settings) - {'enabled', 'channel_id'}:
        raise ValueError('discord must contain only enabled and channel_id; keep the token in .env')
    if type(settings.get('enabled', True)) is not bool:
        raise ValueError('discord.enabled must be a boolean')
    channel = settings.get('channel_id', '')
    if not isinstance(channel, str) or (channel and not re.fullmatch(r'[0-9]{17,20}', channel)):
        raise ValueError('discord.channel_id must be a Discord channel ID stored as a string')
    return settings


class DiscordDeliveryError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def message(payload):
    price = payload.get('price') or {}
    lines = [f"Pokefire · {payload['source']} · {payload['event']}",
             str(payload['title'])[:600],
             f"Price / current bid: {price.get('value', 'Unknown')} {price.get('currency', '')}",
             'Matches: ' + ', '.join(payload['matches'])[:300],
             'Item: ' + str(payload['item_id'])[:100]]
    if payload.get('url'):
        lines.append(str(payload['url'])[:700])
    return {'content': '\n'.join(lines)[:2000], 'allowed_mentions': {'parse': []}}


class DiscordBot:
    def __init__(self, config):
        settings = validate(config)
        self.enabled = settings.get('enabled', True)
        self.channel_id = settings.get('channel_id', '')
        if self.enabled and not self.channel_id:
            raise ValueError('Discord is enabled; set a Discord channel ID in settings or disable it')
        self.token = os.environ.get('DISCORD_BOT_TOKEN', '').strip() if self.enabled else ''
        if self.enabled and not self.token:
            raise ValueError('Discord is enabled; set DISCORD_BOT_TOKEN in .env or disable it')
        if self.enabled and not re.fullmatch(r'[A-Za-z0-9._-]+', self.token):
            raise ValueError('DISCORD_BOT_TOKEN contains invalid characters; check .env')

    def send(self, payload):
        if not self.enabled:
            return
        request = Request(
            f'https://discord.com/api/v10/channels/{self.channel_id}/messages',
            data=json.dumps(message(payload)).encode('utf-8'),
            headers={'Authorization': 'Bot ' + self.token,
                     'Content-Type': 'application/json',
                     'User-Agent': 'DiscordBot (https://discord.com/developers/docs, 1.0)'},
            method='POST')
        try:
            with urlopen(request, timeout=20) as response:
                response.read()
        except HTTPError as exc:
            retry_after = 0
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(exc.read()).get('retry_after', 0))
                    if not math.isfinite(retry_after) or retry_after < 0:
                        retry_after = 0
                except (ValueError, TypeError, AttributeError):
                    retry_after = 0
            exc.close()
            # Never include response bodies or request headers in logs.
            hint = {401: 'check DISCORD_BOT_TOKEN',
                    403: 'check View Channel and Send Messages permissions',
                    404: 'check the channel ID and bot access',
                    429: 'rate limited; delivery will retry'}.get(exc.code, 'delivery failed')
            raise DiscordDeliveryError(f'Discord HTTP {exc.code}: {hint}', retry_after) from None
        except (URLError, OSError, TimeoutError):
            raise DiscordDeliveryError('Discord connection failed; delivery will retry') from None


def notifier(config, terminal_send, make_payload):
    bot = DiscordBot(config)

    def send(item, names):
        # Marking delivered happens in poll only after every destination succeeds.
        bot.send(make_payload(item, names))
        terminal_send(item, names)
    return send

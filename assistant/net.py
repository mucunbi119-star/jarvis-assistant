"""Public HTTP(S) only. Never read local services/files on behalf of page content."""
import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx


def safe_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if parsed.port not in (None, 80, 443):
            raise ValueError()
        host = parsed.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
    except (ValueError, OSError) as exc:
        raise ValueError('公開されている HTTP/HTTPS のページだけを指定してください。ローカル・内部ネットワークには接続できません。') from exc
    return url


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.in_title = False
        self.title = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.skip:
            self.skip -= 1
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.skip and data.strip():
            self.parts.append(data.strip())


async def read_page(url):
    # Redirects must each pass the same public address validation. No cookies or credentials.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, cookies={}) as client:
        for _ in range(5):
            await asyncio.to_thread(safe_url, url)
            async with client.stream('GET', url, headers={'User-Agent': 'LocalAssistant/1.0 (page summary)'}) as res:
                if res.is_redirect:
                    location = res.headers.get('location')
                    if not location:
                        raise ValueError('リダイレクト先を取得できませんでした。')
                    url = urljoin(url, location)
                    continue
                res.raise_for_status()
                kind = res.headers.get('content-type', '').lower()
                if not any(x in kind for x in ('text/html', 'text/plain', 'application/xhtml')):
                    raise ValueError('URL要約は公開HTML・テキストのみ対応しています。PDFは資料として読み込んでください。')
                data = bytearray()
                async for chunk in res.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ValueError('ページが大きすぎます（上限2MB）。')
                text = bytes(data).decode(res.encoding or 'utf-8', errors='replace')
                parser = PageParser()
                if 'html' in kind:
                    parser.feed(text)
                    text = '\n'.join(parser.parts)
                if len(text.strip()) < 40:
                    raise ValueError('本文を読み取れませんでした。JavaScript・ログインが必要な可能性があります。')
                return {'url': url, 'title': ''.join(parser.title).strip() or urlsplit(url).hostname,
                        'text': text[:24000], 'truncated': len(text) > 24000}
    raise ValueError('リダイレクトが多すぎます。')

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from app.services.rss_reader import FeedParseError, parse_feed  # noqa: E402


class FeedTests(unittest.TestCase):
    def test_parse_rss_sanitizes_description(self):
        payload = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Release &amp; notes</title>
          <link>https://example.com/posts/1</link>
          <guid>one</guid>
          <pubDate>Wed, 19 Aug 2026 12:00:00 GMT</pubDate>
          <description><![CDATA[<p>Hello <strong>world</strong></p>]]></description>
        </item></channel></rss>"""
        items = parse_feed(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Release & notes")
        self.assertEqual(items[0]["url"], "https://example.com/posts/1")
        self.assertEqual(items[0]["excerpt"], "Hello world")
        self.assertEqual(items[0]["published_at"], "2026-08-19T12:00:00Z")

    def test_parse_atom(self):
        payload = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example,1</id><title>Item</title>
          <link rel="alternate" href="https://example.com/1" />
          <updated>2026-08-19T12:00:00Z</updated><summary>Text</summary></entry>
        </feed>"""
        item = parse_feed(payload)[0]
        self.assertEqual(item["url"], "https://example.com/1")
        self.assertEqual(item["excerpt"], "Text")

    def test_rejects_dtd(self):
        payload = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>'
        with self.assertRaises(FeedParseError):
            parse_feed(payload)


if __name__ == "__main__":
    unittest.main()

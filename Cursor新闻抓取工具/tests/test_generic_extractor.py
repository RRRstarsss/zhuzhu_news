"""Offline examples of publisher-independent news layouts and unsafe fallbacks."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from extractor import ExtractionError, extract_article, extract_generic_article


LEAD = (
    "The city council opened a public hearing on Tuesday to discuss repairs to the river bridge. "
    "Engineers described the damage found during the annual inspection and explained why heavy "
    "vehicles would need to use a different crossing until the work was finished."
)
SECOND = (
    "Residents asked the council to keep a pedestrian route open during construction. "
    "The transport director said a temporary footbridge was being considered, while emergency "
    "services had already agreed an alternative route that would remain available throughout the project."
)
THIRD = (
    "The council will publish the final timetable after contractors submit their estimates next month. "
    "Members requested another public meeting before a contract is awarded so that local businesses "
    "can comment on the proposed delivery arrangements and the expected cost."
)
TITLE = "Council discusses river bridge repairs"
URL = "https://previously-unknown.example/news/bridge"


def page(body, schema=None, head=""):
    data = f'<script type="application/ld+json">{json.dumps(schema)}</script>' if schema else ''
    return f'<html><head><title>{TITLE}</title>{head}{data}</head><body>{body}</body></html>'.encode()


def article_schema(**extra):
    return {"@type": "NewsArticle", "headline": TITLE, "author": {"name": "Alex Reporter"},
            "datePublished": "2026-10-30T09:00:00Z", **extra}


class GenericExtractorTests(unittest.TestCase):
    def extract(self, body, schema=None, head=""):
        return extract_generic_article(page(body, schema, head), URL)

    def assert_reason(self, reason, body, schema=None, head=""):
        with self.assertRaises(ExtractionError) as caught:
            self.extract(body, schema, head)
        self.assertEqual(caught.exception.reason, reason)

    def test_unknown_publisher_semantic_body_preserves_prose_quotes_and_list_once(self):
        result = self.extract(f'''
            <header><h1>Publisher name</h1><nav>Latest News Weather</nav></header>
            <article><h1>{TITLE}</h1><div itemprop="articleBody">
            <p>{LEAD}</p><blockquote><p>“People need a safe route,” the chair said.</p></blockquote>
            <ol start="3"><li>Inspect the bridge.</li><li>Publish the timetable.</li></ol>
            <div class="recommended-articles"><p>{THIRD}</p></div>
            <p>{SECOND}</p></div></article><footer>Copyright © 2026 Example News</footer>
        ''', article_schema())
        self.assertEqual(result['title'], TITLE)
        self.assertEqual(result['author'], 'Alex Reporter')
        self.assertEqual(result['published'], '2026-10-30T09:00:00Z')
        self.assertEqual(result['body'], [LEAD, '“People need a safe route,” the chair said.',
                                         '3. Inspect the bridge.', '4. Publish the timetable.', SECOND])
        self.assertEqual(result['copyright'], ['Copyright © 2026 Example News'])
        self.assertEqual(result['method'], 'generic_article_body')
        self.assertTrue(any('completeness' in warning for warning in result['warnings']))

    def test_cms_container_is_selected_over_larger_navigation_and_promos(self):
        result = self.extract(f'''
            <nav>{THIRD * 8}</nav><h1>{TITLE}</h1>
            <div class="news-article-content"><p>{LEAD}</p><div><p>{SECOND}</p></div>
            <aside>{THIRD}</aside><div class="ad-container">Buy now</div>
            <div class="related-content"><p>{THIRD}</p></div></div>
            <div class="newsletter"><p>{THIRD * 5}</p></div>
        ''')
        self.assertEqual(result['body'], [LEAD, SECOND])
        self.assertEqual(result['method'], 'generic_article_container')

    def test_semantic_article_can_be_a_complete_short_news_bulletin(self):
        result = self.extract(f'<article><h1>{TITLE}</h1><address>Alex Reporter</address>'
                              f'<time datetime="2026-10-30">October 30</time><p>{LEAD}</p></article>')
        self.assertEqual(result['body'], [LEAD])
        self.assertEqual(result['author'], 'Alex Reporter')
        self.assertEqual(result['published'], '2026-10-30')
        self.assertEqual(result['method'], 'generic_semantic_article')

    def test_unlabelled_main_content_needs_no_publisher_rule(self):
        result = self.extract(f'<main><h1>{TITLE}</h1><div><p>{LEAD}</p>'
                              f'<p>{SECOND}</p><p>{THIRD}</p></div></main>')
        self.assertEqual(result['body'], [LEAD, SECOND, THIRD])
        self.assertEqual(result['method'], 'generic_prose_container')

    def test_legacy_div_br_layout_keeps_inline_punctuation(self):
        result = self.extract(f'<h1>{TITLE}</h1><div class="story-text">'
                              f'{LEAD}<br><br>{SECOND}<br>She said, <em>“Wait.”</em>'
                              '<br><span>The bridge </span><span>remains open.</span></div>')
        self.assertEqual(result['body'], [LEAD, SECOND, 'She said, “Wait.”', 'The bridge remains open.'])

    def test_hidden_elements_and_stylesheet_hidden_prose_are_not_saved(self):
        result = self.extract(f'''
            <h1 hidden>Wrong headline</h1><h1>{TITLE}</h1><article>
            <p>{LEAD}</p><div aria-hidden="true"><p>Secret alternate text</p></div>
            <p style="display: none !important">Hidden teaser continuation</p>
            <section class="concealed"><p>{THIRD}</p></section>
            <div class="sr-only">Screen reader controls</div><p>{SECOND}</p></article>
        ''', head='<style>.concealed { display: none; }</style>')
        self.assertEqual(result['title'], TITLE)
        self.assertEqual(result['body'], [LEAD, SECOND])

    def test_jsonld_only_body_is_never_a_fallback(self):
        self.assert_reason('article_body_not_found', f'<h1>{TITLE}</h1>',
                           article_schema(articleBody=LEAD + ' ' + SECOND))

    def test_inert_templates_and_closed_dialogs_cannot_supply_article_prose(self):
        self.assert_reason('article_body_not_found', f'<h1>{TITLE}</h1>'
                           f'<template><article><p>{LEAD}</p></article></template>'
                           f'<dialog><article><p>{SECOND}</p></article></dialog>')

    def test_hidden_body_is_never_recovered_from_matching_schema(self):
        self.assert_reason('article_body_not_found',
                           f'<h1>{TITLE}</h1><article hidden><p>{LEAD}</p><p>{SECOND}</p></article>',
                           article_schema(articleBody=LEAD + ' ' + SECOND))

    def test_visible_teaser_does_not_become_full_schema_article(self):
        self.assert_reason('incomplete_article', f'<article><h1>{TITLE}</h1><p>{LEAD}</p></article>',
                           article_schema(articleBody=LEAD + ' ' + SECOND + ' ' + THIRD))

    def test_unrelated_schema_body_does_not_replace_visible_prose(self):
        result = self.extract(f'<article><h1>{TITLE}</h1><p>{LEAD}</p><p>{SECOND}</p></article>',
                              article_schema(articleBody=THIRD))
        self.assertEqual(result['body'], [LEAD, SECOND])
        self.assertTrue(any('was not used' in warning for warning in result['warnings']))

    def test_matching_schema_retains_dom_paragraph_boundaries(self):
        result = self.extract(f'<article><h1>{TITLE}</h1><p>{LEAD}</p><p>{SECOND}</p></article>',
                              article_schema(articleBody=LEAD + ' ' + SECOND))
        self.assertEqual(result['body'], [LEAD, SECOND])
        self.assertTrue(any('matches' in warning for warning in result['warnings']))

    def test_multiple_news_cards_fail_instead_of_combining_stories(self):
        cards = ''.join(f'<article><h2><a href="/{i}">Bridge report {i}</a></h2><p>{text}</p></article>'
                        for i, text in enumerate([LEAD + SECOND, THIRD]))
        self.assert_reason('ambiguous_article', '<main><h1>Local news</h1>' + cards + '</main>')

    def test_navigation_and_short_summary_are_not_articles(self):
        self.assert_reason('article_body_not_found', '<h1>Local news</h1><nav>'
                           '<a href="/a">Latest news</a></nav><p>Read our latest updates.</p>')

    def test_existing_access_checks_apply_to_all_domains(self):
        body = f'<article><h1>{TITLE}</h1><p>{LEAD}</p><p>{SECOND}</p></article>'
        self.assert_reason('needs_browser_review', body, article_schema(isAccessibleForFree=False))
        self.assert_reason('subscription_gate', body + '<p>Subscribe to continue reading</p>')
        challenge = b'<html><title>Just a moment...</title><body>Verify you are human</body></html>'
        with self.assertRaises(ExtractionError) as caught:
            extract_generic_article(challenge, URL)
        self.assertEqual(caught.exception.reason, 'challenge')

    def test_utf8_cjk_article_without_charset_is_preserved(self):
        text = '市议会今天举行公开听证会，讨论河流大桥的修复方案。工程师介绍检查结果，说明施工期间的临时交通安排。居民希望保留安全的人行通道，并要求在合同签署前公布详细预算和时间表。'
        result = self.extract(f'<article><h1>大桥修复听证会</h1><p>{text}</p></article>')
        self.assertEqual(result['title'], '大桥修复听证会')
        self.assertEqual(result['body'], [text])

    def test_empty_input_invalid_url_and_original_interface_remain_explicit(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_generic_article(b'', URL)
        self.assertEqual(caught.exception.reason, 'empty_html')
        with self.assertRaises(ExtractionError) as caught:
            extract_generic_article(page('<p>text</p>'), 'javascript:alert(1)')
        self.assertEqual(caught.exception.reason, 'invalid_url')
        with self.assertRaises(ExtractionError) as caught:
            extract_article(page(f'<article><h1>{TITLE}</h1><p>{LEAD}</p></article>'), URL)
        self.assertEqual(caught.exception.reason, 'unsupported_domain')

    def test_existing_publisher_extractor_keeps_metadata_and_body_contract(self):
        markup = page(f'<article><h1>{TITLE}</h1><address>Alex Reporter</address>'
                      f'<time datetime="2026-10-30">October 30</time>'
                      f'<div class="entry-content"><p>{LEAD}</p><div><p>{SECOND}</p></div>'
                      '</div></article><footer>Copyright © 2026 Example News</footer>')
        result = extract_article(markup, 'https://www.breitbart.com/news/example/')
        self.assertEqual(result['title'], TITLE)
        self.assertEqual(result['author'], 'Alex Reporter')
        self.assertEqual(result['published'], '2026-10-30')
        self.assertEqual(result['body'], [LEAD, SECOND])
        self.assertEqual(result['copyright'], ['Copyright © 2026 Example News'])
        self.assertEqual(result['method'], 'breitbart_article_body')
        self.assertEqual(result['warnings'], [])


if __name__ == '__main__':
    unittest.main()

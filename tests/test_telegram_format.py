"""Tests for the Telegram -> Slack rendering (the au-tomator port)."""

import unittest

from src.dapier.engine.actions import telegram_format as tf


def sections_of(text, entities=None):
    return tf.render_sections(text, entities)


class MrkdwnTests(unittest.TestCase):
    def test_plain_text_is_escaped_and_quoted(self):
        self.assertEqual(sections_of("a < b & c"), [">a &lt; b &amp; c"])

    def test_plain_quote_wrapping(self):
        self.assertEqual(sections_of("hello"), [">hello"])

    def test_bold_italic_strike_underline(self):
        text = "b i s u"
        entities = [
            {"offset": 0, "length": 1, "type": "bold"},
            {"offset": 2, "length": 1, "type": "italic"},
            {"offset": 4, "length": 1, "type": "strikethrough"},
            {"offset": 6, "length": 1, "type": "underline"},
        ]
        self.assertEqual(sections_of(text, entities), [">*b* _i_ ~s~ ~u~"])

    def test_text_link_translates_to_slack_link(self):
        text = "see the docs here"
        entities = [{"offset": 8, "length": 4, "type": "text_link",
                     "url": "https://example.com/docs"}]
        self.assertEqual(sections_of(text, entities), [">see the <https://example.com/docs|docs> here"])

    def test_url_entity_is_autolinked(self):
        text = "https://example.com"
        entities = [{"offset": 0, "length": 19, "type": "url"}]
        self.assertEqual(sections_of(text, entities), ["><https://example.com>"])

    def test_inline_code_and_pre_block(self):
        text = "run ls\nx = 1"
        entities = [
            {"offset": 4, "length": 2, "type": "code"},
            {"offset": 7, "length": 5, "type": "pre"},
        ]
        sections = sections_of(text, entities)
        self.assertEqual(sections, [">run `ls`", "```\nx = 1\n```"])

    def test_utf16_offsets_with_emoji(self):
        # The emoji is 2 UTF-16 code units; a codepoint-based slice would
        # mark up the wrong range.
        text = "\U0001F600 bold"
        entities = [{"offset": 3, "length": 4, "type": "bold"}]
        self.assertEqual(sections_of(text, entities), [">\U0001F600 *bold*"])

    def test_nested_link_label_keeps_inner_markup(self):
        text = "a bold link b"
        entities = [
            {"offset": 2, "length": 9, "type": "text_link", "url": "https://x.test"},
            {"offset": 2, "length": 4, "type": "bold"},
        ]
        self.assertEqual(sections_of(text, entities), [">a <https://x.test|*bold* link> b"])


class SplittingTests(unittest.TestCase):
    def test_long_post_splits_into_several_messages(self):
        text = "\n".join(f"line number {i}" for i in range(600))
        messages = tf.build_messages(text)
        self.assertGreater(len(messages), 1)
        total_sections = sum(len(group) for group in messages)
        self.assertEqual(total_sections, len(tf.render_sections(text)))

    def test_every_section_stays_under_the_slack_limit(self):
        text = "word " * 4000  # ~20k chars of prose
        for section in tf.render_sections(text):
            self.assertLessEqual(len(section), tf.SECTION_TEXT_LIMIT)

    def test_oversized_code_block_is_truncated_with_a_closed_fence(self):
        code = "x" * (tf.SECTION_TEXT_LIMIT + 50)
        sections = tf.render_sections(code, [{"offset": 0, "length": len(code), "type": "pre"}])
        self.assertEqual(len(sections), 1)
        self.assertTrue(sections[0].endswith("\n```"))

    def test_empty_text_yields_one_empty_group(self):
        self.assertEqual(tf.build_messages(""), [[]])


class BlockShapeTests(unittest.TestCase):
    def test_blocks_are_slack_sections(self):
        blocks = tf.build_messages("hi")[0]
        self.assertEqual(blocks, [
            {"type": "section", "text": {"type": "mrkdwn", "text": ">hi"}},
        ])

    def test_link_blocks(self):
        self.assertEqual(tf.link_blocks("https://t.me/x/1"), [
            {"type": "section", "text": {"type": "mrkdwn", "text": "https://t.me/x/1"}},
        ])


if __name__ == "__main__":
    unittest.main()

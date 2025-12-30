# mypy: allow-untyped-defs

import textwrap
import unittest

from .. import parser
from ..parser import token_types

class TokenizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = parser.Tokenizer()

    def tokenize(self, input_str):
        rv = []
        for item in self.tokenizer.tokenize(input_str):
            rv.append(item)
            if item[0] == token_types.eof:
                break
        return rv

    def compare(self, input_text, expected) -> None:
        expected = expected + [(token_types.eof, None)]
        actual = self.tokenize(input_text)
        self.assertEqual(actual, expected)

    def test_heading_0(self) -> None:
        self.compare(b"""[Heading text]""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading text"),
                      (token_types.paren, "]")])

    def test_heading_1(self) -> None:
        self.compare(br"""[Heading [text\]]""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading [text]"),
                      (token_types.paren, "]")])

    def test_heading_2(self) -> None:
        self.compare(b"""[Heading #text]""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading #text"),
                      (token_types.paren, "]")])

    def test_heading_3(self) -> None:
        self.compare(br"""[Heading [\]text]""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading []text"),
                      (token_types.paren, "]")])

    def test_heading_4(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"[Heading")

    def test_heading_5(self) -> None:
        self.compare(br"""[Heading [\]text] #comment""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading []text"),
                      (token_types.paren, "]"),
                      (token_types.inline_comment, "comment")])

    def test_heading_6(self) -> None:
        self.compare(br"""[Heading \ttext]""",
                     [(token_types.paren, "["),
                      (token_types.string, "Heading \ttext"),
                      (token_types.paren, "]")])

    def test_key_0(self) -> None:
        self.compare(b"""key:value""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "value")])

    def test_key_1(self) -> None:
        self.compare(b"""key  :  value""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "value")])

    def test_key_2(self) -> None:
        self.compare(b"""key  :  val ue""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "val ue")])

    def test_key_3(self) -> None:
        self.compare(b"""key: value#comment""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "value"),
                      (token_types.inline_comment, "comment")])

    def test_key_4(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""ke y: value""")

    def test_key_5(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key""")

    def test_key_6(self) -> None:
        self.compare(b"""key: "value\"""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "value")])

    def test_key_7(self) -> None:
        self.compare(b"""key: 'value'""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "value")])

    def test_key_8(self) -> None:
        self.compare(b"""key: "#value\"""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "#value")])

    def test_key_9(self) -> None:
        self.compare(b"""key: '#value\'""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, "#value")])

    def test_key_10(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: "value""")

    def test_key_11(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: 'value""")

    def test_key_12(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: 'value""")

    def test_key_13(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: 'value' abc""")

    def test_key_14(self) -> None:
        self.compare(br"""key: \\nb""",
                     [(token_types.string, "key"),
                      (token_types.separator, ":"),
                      (token_types.string, r"\nb")])

    def test_list_0(self) -> None:
        self.compare(b"""
key: []""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.list_start, "["),
             (token_types.list_end, "]")])

    def test_list_1(self) -> None:
        self.compare(b"""
key: [a, "b"]""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.list_start, "["),
             (token_types.string, "a"),
             (token_types.string, "b"),
             (token_types.list_end, "]")])

    def test_list_2(self) -> None:
        self.compare(b"""
key: [a,
      b]""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.list_start, "["),
             (token_types.string, "a"),
             (token_types.string, "b"),
             (token_types.list_end, "]")])

    def test_list_3(self) -> None:
        self.compare(b"""
key: [a, #b]
      c]""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.list_start, "["),
             (token_types.string, "a"),
             (token_types.inline_comment, "b]"),
             (token_types.string, "c"),
             (token_types.list_end, "]")])

    def test_list_4(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: [a #b]
            c]""")

    def test_list_5(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""key: [a \\
            c]""")

    def test_list_6(self) -> None:
        self.compare(b"""key: [a , b]""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.list_start, "["),
             (token_types.string, "a"),
             (token_types.string, "b"),
             (token_types.list_end, "]")])

    def test_expr_0(self) -> None:
        self.compare(b"""
key:
  if cond == 1: value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.number, "1"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_1(self) -> None:
        self.compare(b"""
key:
  if cond == 1: value1
  value2""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.number, "1"),
             (token_types.separator, ":"),
             (token_types.string, "value1"),
             (token_types.string, "value2")])

    def test_expr_2(self) -> None:
        self.compare(b"""
key:
  if cond=="1": value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.string, "1"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_3(self) -> None:
        self.compare(b"""
key:
  if cond==1.1: value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.number, "1.1"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_4(self) -> None:
        self.compare(b"""
key:
  if cond==1.1 and cond2 == "a": value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.number, "1.1"),
             (token_types.ident, "and"),
             (token_types.ident, "cond2"),
             (token_types.ident, "=="),
             (token_types.string, "a"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_5(self) -> None:
        self.compare(b"""
key:
  if (cond==1.1 ): value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.paren, "("),
             (token_types.ident, "cond"),
             (token_types.ident, "=="),
             (token_types.number, "1.1"),
             (token_types.paren, ")"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_6(self) -> None:
        self.compare(b"""
key:
  if "\\ttest": value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.string, "\ttest"),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_expr_7(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""
key:
  if 1A: value""")

    def test_expr_8(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""
key:
  if 1a: value""")

    def test_expr_9(self) -> None:
        with self.assertRaises(parser.ParseError):
            self.tokenize(b"""
key:
  if 1.1.1: value""")

    def test_expr_10(self) -> None:
        self.compare(b"""
key:
  if 1.: value""",
            [(token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.number, "1."),
             (token_types.separator, ":"),
             (token_types.string, "value")])

    def test_comment_with_indents(self) -> None:
        self.compare(
            textwrap.dedent(
                """\
                # comment 0
                [Heading]
                  # comment 1
                # comment 2
                """).encode(),
            [(token_types.comment, " comment 0"),
             (token_types.paren, "["),
             (token_types.string, "Heading"),
             (token_types.paren, "]"),
             (token_types.comment, " comment 1"),
             (token_types.comment, " comment 2")])

    def test_comment_inline(self) -> None:
        self.compare(
            textwrap.dedent(
                """\
                [Heading]          # after heading
                key:               # after key
                # before group start
                  if cond: value1  # after value1
                  value2           # after value2
                """).encode(),
            [(token_types.paren, "["),
             (token_types.string, "Heading"),
             (token_types.paren, "]"),
             (token_types.inline_comment, " after heading"),
             (token_types.string, "key"),
             (token_types.separator, ":"),
             (token_types.inline_comment, " after key"),
             (token_types.comment, " before group start"),
             (token_types.group_start, None),
             (token_types.ident, "if"),
             (token_types.ident, "cond"),
             (token_types.separator, ":"),
             (token_types.string, "value1"),
             (token_types.inline_comment, " after value1"),
             (token_types.string, "value2"),
             (token_types.inline_comment, " after value2")])


if __name__ == "__main__":
    unittest.main()

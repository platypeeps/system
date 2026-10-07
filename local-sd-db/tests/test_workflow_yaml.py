"""The workflow YAML reader: the subset GitHub workflows use, as structure,
and a refusal with a line number for everything else (sd:1820)."""

import unittest

from sd_db.workflow_yaml import Refused, load


class Reads(unittest.TestCase):
    def test_the_shapes_a_workflow_uses(self):
        cases = {
            "block sequence at the key's indent": ("a:\n- x\n- y\nb: z\n", {"a": ["x", "y"], "b": "z"}),
            "item mappings": ("a:\n  - b: 1\n    c: 2\n  - d\n", {"a": [{"b": "1", "c": "2"}, "d"]}),
            "nested items": ("- - a\n  - b\n- c\n", [["a", "b"], "c"]),
            "quoted keys and values": ("'a': 'it''s'\n\"b\": \"x\\ty\"\n", {"a": "it's", "b": "x\ty"}),
            "flow collections": ("a: {b: c, d: [e, f]}\ng: [ ]\n", {"a": {"b": "c", "d": ["e", "f"]}, "g": []}),
            "comments": ("a: b # c\nd: e#f\n# whole line\ng:    # c\n  h: i\n",
                         {"a": "b", "d": "e#f", "g": {"h": "i"}}),
            "an expression": ("a: ${{ join(needs.*.result, ' ') }}\n", {"a": "${{ join(needs.*.result, ' ') }}"}),
            "a value on the next line": ("a:\n  b\n", {"a": "b"}),
            "an empty value": ("a:\nb: c\n", {"a": None, "b": "c"}),
            "a document start": ("--- # x\na: 1\n", {"a": "1"}),
            "indented as a whole": ("  a: 1\n  b:\n    - c\n", {"a": "1", "b": ["c"]}),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(load(text), expected)

    def test_block_scalars_keep_their_lines_and_chomp(self):
        cases = {
            "clip": ("a: |\n  x\n    y\n\n  # z\nb: 1\n", "x\n  y\n\n# z\n"),
            "strip": ("a: |-\n  x\n  y\n\n", "x\ny"),
            "keep": ("a: |+\n  x\n\n\nb: 1\n", "x\n\n\n"),
            "folded": ("a: >-\n  x\n  y\n", "x y"),
            "no final line break": ("a: |\n  x", "x"),
            "in an item": ("- run: |\n    x\n  name: n\n", None),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                value = load(text)
                if expected is None:
                    self.assertEqual(value, [{"run": "x\n", "name": "n"}])
                else:
                    self.assertEqual(value["a"], expected)


class Refuses(unittest.TestCase):
    def test_what_it_cannot_read_is_refused_with_its_line(self):
        cases = {
            "anchor": ("a: 1\nb: &x 1\n", 2, "an anchor"),
            "alias": ("a: *x\n", 1, "an alias"),
            "tag": ("a: !!str 1\n", 1, "a tag"),
            "anchor in a flow": ("a: [&x 1]\n", 1, "an anchor"),
            "merge key": ("a: 1\n<<: {b: 1}\n", 2, "merge key"),
            "explicit key": ("? a\n: b\n", 1, "explicit key"),
            "duplicate key": ("a: 1\na: 2\n", 2, "duplicate key"),
            "duplicate flow key": ("a: {b: 1, b: 2}\n", 1, "duplicate key"),
            "second document": ("a: 1\n---\nb: 2\n", 2, "expected `key: value`"),
            "directive": ("%YAML 1.2\n---\na: 1\n", 1, "a directive"),
            "tab": ("a:\n\tb: 1\n", 2, "a tab"),
            "plain scalar over lines": ("a: b\n  c\n", 2, "unexpected indentation"),
            "quoted scalar over lines": ("a: 'b\n  c'\n", 1, "spans lines"),
            "flow over lines": ("a: [b,\n  c]\n", 1, "spans lines"),
            "mapping in a plain scalar": ("a: b: c\n", 1, "a mapping inside"),
            "unknown escape": ('a: "\\u0041"\n', 1, "escape"),
            "indentation indicator": ("a: |2\n   x\n", 1, "indentation indicator"),
            "folded with a blank line": ("a: >\n  x\n\n  y\n", 1, "folded"),
            "content after a quoted value": ('a: "b"c\n', 1, "content after"),
        }
        for name, (text, line, reason) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(Refused) as caught:
                    load(text)
                self.assertEqual(caught.exception.line, line)
                self.assertIn(reason, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

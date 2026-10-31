from dapier_cli.cli import build_parser
from dapier_cli.commands.gdocs import parse_doc_id


def test_parse_doc_id():
    assert parse_doc_id("https://docs.google.com/document/d/abc_D-1/edit?usp=x") == "abc_D-1"
    assert parse_doc_id("abc") == "abc"


def test_parser():
    a = build_parser().parse_args(["gdocs", "append", "X", "--text", "hi"])
    assert (a.group, a.command, a.doc, a.text, a.ref) == ("gdocs", "append", "X", "hi", "drive datatalks")
    a = build_parser().parse_args(["gdocs", "replace", "X", "--find", "A", "--file", "f"])
    assert a.find == "A" and a.file == "f"

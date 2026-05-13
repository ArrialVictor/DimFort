from dimfort.cli import build_parser


def test_parser_version():
    p = build_parser()
    assert p.prog == "dimfort"


def test_check_subcommand_parses():
    p = build_parser()
    ns = p.parse_args(["check", "foo.f90", "bar.f90"])
    assert ns.command == "check"
    assert ns.paths == ["foo.f90", "bar.f90"]

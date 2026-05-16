from dimfort.cli import build_parser


def test_parser_program_name():
    p = build_parser()
    assert p.prog == "dimfort"


def test_check_subcommand_parses_basic():
    p = build_parser()
    ns = p.parse_args(["check", "foo.f90", "bar.f90"])
    assert ns.command == "check"
    assert ns.paths == ["foo.f90", "bar.f90"]
    assert ns.quiet is False
    assert ns.no_color is False


def test_check_flags():
    """The remaining ``check`` flags (``--quiet`` / ``--no-color``) parse correctly."""
    p = build_parser()
    ns = p.parse_args(["check", "x.f90", "--quiet", "--no-color"])
    assert ns.quiet is True
    assert ns.no_color is True

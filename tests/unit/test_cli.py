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
    p = build_parser()
    ns = p.parse_args(
        ["check", "x.f90", "--quiet", "--no-color", "--lfortran", "/tmp/lf"]
    )
    assert ns.quiet is True
    assert ns.no_color is True
    assert ns.lfortran == "/tmp/lf"


def test_cache_clean_parses():
    p = build_parser()
    ns = p.parse_args(["cache", "clean"])
    assert ns.command == "cache" and ns.cache_command == "clean"

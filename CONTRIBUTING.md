# Contributing to DimFort

Thanks for considering a contribution. DimFort is pre-alpha; the
contribution surface and expectations below will evolve as the
project stabilises.

## Reporting issues

Open an issue on GitHub with:

- Minimum reproducible Fortran source (or a pointer to the
  relevant file).
- The DimFort command you ran (or the editor + LSP context).
- Expected versus observed behaviour.
- Output of `dimfort --version` and your Python version.

If the failure is a false-positive diagnostic, include the
annotation in question and any cross-file imports involved — unit
consistency depends on the whole `use`-chain neighbourhood, not
just one file.

## Development setup

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,lsp]"
```

Minimum Python version is 3.11. The Fortran parser
([`tree-sitter-fortran`](https://pypi.org/project/tree-sitter-fortran/))
is a runtime dependency installed automatically.

## Running tests

```bash
pytest                      # full unit + integration suite
pytest tests/unit -q        # unit only
pytest -k <expression>      # filter by name
ruff check .                # lint
```

A patch that breaks tests or trips ruff will not be merged.

## Code style

- Follow the existing module organisation. See the per-feature design
  docs under [docs/design/](docs/design/) and the docstrings at the top
  of each `core/` module for the layout rationale.
- Comments should explain **why**, not what. Reading the code
  tells you what; the comment is for the non-obvious constraint,
  invariant, or trade-off.
- Public API additions need a docstring.
- New diagnostic codes are registered in `core/symbols.py`'s
  `CODES` dict.
- Performance work is welcome; please include before/after numbers
  on a reference workset of similar scale (a few thousand `.F90`
  files with cross-module USE chains) so we know we're moving the
  needle.

## Commits and pull requests

- Imperative subject under 70 characters
  (`checker: do X`, not `did X`).
- Body explains motivation more than implementation. Mention
  affected files, perf impact, test coverage, behavioural changes.
- One coherent change per commit. Use multiple commits in a PR if
  the work has logically distinct phases.
- PRs should be rebased on the latest `main` before review.

## Companions

The editor integrations live in their own repositories and have
their own contribution flows:

- [DimFort-VSCompanion](https://github.com/ArrialVictor/DimFort-VSCompanion)
- [DimFort-NvimCompanion](https://github.com/ArrialVictor/DimFort-NvimCompanion)
- [DimFort-EmacsCompanion](https://github.com/ArrialVictor/DimFort-EmacsCompanion)

A change that affects the LSP protocol surface needs a matching
companion change; mention it in the DimFort PR so reviewers can
coordinate.

## License

By contributing, you agree your changes are licensed under the
project's MIT [LICENSE](LICENSE).

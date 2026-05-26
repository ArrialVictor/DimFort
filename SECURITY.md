# Security policy

## Supported versions

DimFort is pre-1.0; only the latest released version on PyPI receives fixes.
Please reproduce any issue against the most recent release before reporting.

## Reporting a vulnerability

DimFort is a *static* analysis tool — it parses Fortran source but never
executes it — so its attack surface is small. Still, if you find a security
issue (e.g. a crash or resource-exhaustion triggerable by crafted input, or a
problem in the LSP server's handling of workspace files):

- **Do not open a public issue.** Use GitHub's private vulnerability reporting
  ("Report a vulnerability" under the repository's **Security** tab) so the
  report stays private until a fix is available.
- Include the DimFort version, a minimal reproducer (the smallest Fortran
  snippet or workspace that triggers it), and the observed vs expected behaviour.

You'll get an acknowledgement, and a fix or mitigation will be released as soon
as is practical for a single-maintainer project.

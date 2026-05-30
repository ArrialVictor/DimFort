# TODO

Loose follow-ups that don't (yet) warrant their own PR or design doc.
Items here are notes-to-self, not commitments. Prune freely.

## Demos

- [ ] Add a `dimfort interactions <var>` / **X001** walkthrough to
  `demos/`. The other three demo files (`tour.f90`, `affine.f90`,
  `broken.f90`) exercise the `check` subcommand only; `interactions`
  is the cross-site query and X001 is the one diagnostic the demos
  don't surface. Two reasonable shapes:
  - A short `demos/interactions.md` that reuses `demos/tour.f90` —
    pick a variable like `p` or `p_ref` and capture the expected
    `dimfort interactions p demos/tour.f90` output. Cheapest, but may
    not exercise X001 since `tour.f90`'s annotations are consistent.
  - A small `demos/interactions.f90` (one program + one module, or
    two procedures) where the same variable is used with conflicting
    implied units at different sites, firing X001. Use generic
    physics names (`v`, `p`, `T`, `rho`, …).

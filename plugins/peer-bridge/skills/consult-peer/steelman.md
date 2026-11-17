# Steelman with a peer

A steelman asks the peer for the strongest case for a change before its critique, so the critique answers the change's
best version. Use it when the operator asks to steelman a change, a proposal or a design, or before you reject one the
operator or a peer put forward.

Ask in one consultation. For a commit, pin it with `ref` and put its diff in `context`: a Claude peer has only Read, Glob
and Grep and cannot run `git diff`. For a design or a proposal, put its text and the operator's stated requirements in
`context`. Give the request this shape:

- **Case**: the strongest case for the change: the problem it solves and the constraints that make this shape right.
  Cite a `file:line` for each claim about existing code, and the source for each stated requirement.
- **Limits**: where the evidence stops supporting that case, each with its `file:line` or source and what it costs.
  A premise nothing supports is listed as unresolved.
- **Verdict**: one closing line on whether the case survives its limits.

Weigh both parts against the code yourself, then report the case, the limits that hold and your own verdict beside the
peer's. The operator decides.

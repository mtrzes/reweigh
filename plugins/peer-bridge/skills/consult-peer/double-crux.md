# Double-crux with a peer

A double-crux settles one disputed finding by evidence instead of by authority. You and the peer look for a crux: a
question about the code with two possible answers, where one answer would make you drop your claim and the other would
make the peer drop its. Then you both check it. Run it on a single finding where your claim and the peer's differ and the
answer changes what the operator does.

Work inside the consultation that produced the finding: pass its `conversation_id` and omit `ref`, so the peer reads the
same pinned commit. When the operator asks for a double-crux and there is no such consultation yet, consult the peer for
its assessment first; continue only if a finding you dispute comes back, and otherwise report that nothing is disputed.
When the dispute turns on what changed, put the diff in `context`: a Claude peer has only Read, Glob and Grep and cannot
run `git diff`.

1. **State your side.** Send the peer your claim and the question whose answer would make you drop it. Ask for its claim,
   and which answer to that question would make it drop its own; if your question would not move it, ask for the
   question that would. Done when both claims and every candidate question are on the table.
2. **Agree the crux.** The crux is a candidate question where each side has named the answer that would change its
   claim. When neither side accepts a question the other offered, go to step 4.
3. **Check it.** Read the cited code where the peer read it, and ask the peer what it found there, with `file:line`.
   When the reply carries a `commit`, read the code at that commit, not in your working tree. When it carries none, the
   consultation is unpinned: read the working tree, and when it changed meanwhile, both sides check again against the
   same tree first. Done when each side has said what the evidence establishes for it and whether its claim changed.
4. **Report.** Give the operator both claims, the crux, the evidence with its commit or "unpinned working tree", who
   moved, and any disagreement that remains. Two follow-ups is the ceiling: report then with the questions that were offered. "The evidence available
   cannot settle this" is a complete result.

The result is advice like any other reply: agreement between you and the peer authorizes no fix. The operator decides.

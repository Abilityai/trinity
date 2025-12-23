# LATS for Human Approval Workflows

> **Status**: Future Idea
> **Created**: 2025-12-16
> **Context**: Discussion about human-in-the-loop approval systems

---

## Core Concept

Use Language Agent Tree Search (LATS) pattern for handling rejections and failures in multi-agent processes.

Instead of linear execution with hard stops on rejection, model process execution as a tree:

- Each decision point = branch node
- Rejection = prune branch, backtrack with context
- Retry carries "why previous attempt failed"
- Human approval becomes search guidance, not just gatekeeping

## Why It Fits

- **Rejection = pruning**, not failure
- **Context accumulates** — each retry knows why previous attempts failed
- **Natural checkpointing** — tree nodes are your state
- **Parallel exploration** — can try multiple branches simultaneously

## Sketch

```yaml
process_node:
  id: content-draft
  action: create_draft
  on_reject:
    backtrack_to: topic-selection  # or self for retry
    inject_context: "Previous draft rejected: {rejection_reason}"
    max_backtracks: 3
```

## Concerns

- Context bloat on deep backtracks — need summarization
- Complexity of tree state persistence
- Defining granularity of nodes

## References

- LATS Paper: https://arxiv.org/abs/2310.04406
- Related: Tree of Thoughts, Monte Carlo Tree Search for LLMs

---

*Saved for future development*

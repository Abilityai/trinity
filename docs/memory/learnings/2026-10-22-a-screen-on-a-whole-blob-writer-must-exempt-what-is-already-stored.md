## 2026-10-22 — pitfall — A validation added to a whole-blob writer re-judges every stored line on every write; exempt what was already there by the HIT, never by the line
**Context**: trinity-enterprise#419, `services/user_memory_guard`.
- **The lockout.** `write_user_memory` replaces the whole notes blob, and agents are told to copy everything back. Screening the full blob would have re-judged every note stored before the screen existed. One old tripping line would then refuse every later write for that person, the agent would keep rephrasing its new fact into the same 422, and memory would freeze unseen. Both plan reviews caught this.
- **The fix that bred a bypass.** The first fix skipped any line already stored. An adversarial code review then broke it three ways, each from two writes that pass alone:
  - reorder two old lines into a phrase they did not form;
  - give an old line a new neighbour that makes it line-initial;
  - respell an old line, since the line key dropped a zero-width character that the hit read as a space.
- **The copy-back gap.** The copy-back normaliser stripped line by line, so a whole-block copy-back merged the summarizer's section and the seat decisions into the agent's notes.

**Lesson**: when a check is added to a writer that replaces a whole document (notes, a summary, a canvas, a report body), keep text from before the check from locking the data, without opening a bypass:
- Exempt a hit only if the stored value produced the same hit: same rule, same matched text, computed with the same views and spellings. Old lines rearranged into a new hit are judged afresh.
- Name the location (line or field) in the refusal.
- Read the stored value through an accessor that does not create the row, or a refused write leaves state behind.
- Normalise a copy-back structurally (keep the section that belongs to this writer), not line by line.
- Test an old hit kept, a new hit beside it, a split across old and new, and each composition (reorder, new context, respelling).

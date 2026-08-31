# minicrawl — working conventions

A teaching repo: a web crawler built from scratch, one concept per stage,
verified against a synthetic corpus. See README.md for the ladder.

## Every stage lands with four things

1. **Code** — the stage's modules, plus any refactor earlier stages earned.
2. **Tests** — `tests/test_stageNN_*.py`, verified against
   `testsite/manifest.json` rather than against the crawler's own output.
3. **`docs/stage-NN.md`** — the rationale. Why this design, what was rejected,
   which rules had to be removed and what broke to prove it.
4. **`logs/stage-NN/notes.txt`** — the working log. Fixed sections:
   GOAL / STEPS TAKEN / CONCEPTS APPLIED / CONCEPT → CODE MAP / WHAT BROKE /
   HOW TO VERIFY / STATE AFTER THIS STAGE.
   The concept → code map is the point: every concept names the file and
   function it is implemented by. `logs/README.txt` has the full format.

Then one commit and an annotated `stage-NN` tag. Commits are authored as the
repo owner with no trailers.

## Rules that have earned their place

- **Ground truth is declared, not observed.** `testsite/spec.py` declares the
  corpus; `testsite/manifest.py` derives expectations from that declaration
  with no HTTP and no HTML parsing. Never edit `manifest.json` by hand, and
  never adjust ground truth to make a crawl pass — if the corpus tolerates a
  wrong rule, the corpus is wrong too.
- **Characterisation tests.** Each stage asserts its own known shortcomings in
  `test_characterises_*` tests, written to fail when the fixing stage lands.
  Flipping one is the definition of done.
- **Band-aids are named as such.** `max_pages` and `max_depth` are not
  features; they stand in for a defence a later stage supplies.
- **Assert the invariant, not a proxy for it.** And never leave a test in the
  suite that cannot fail.
- **Name a metric for what it measures.** A number that reads as nonsense will
  be believed by whoever reads it next.

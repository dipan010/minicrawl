LOGS
====

One folder per stage: logs/stage-NN/notes.txt

These are working logs, not documentation. They record how each stage was
actually built and which concept each piece of code is an implementation of.

  docs/stage-NN.md   WHY the design is what it is. Narrative. Read for judgement.
  logs/stage-NN/     WHAT was done, in order, and WHICH CONCEPT each part maps
                     to. Procedural. Read for study or to retrace the work.

Every notes.txt uses the same sections:

  1. GOAL
  2. STEPS TAKEN            in the order they happened, including dead ends
  3. CONCEPTS APPLIED       the theory, stated independently of this codebase
  4. CONCEPT -> CODE MAP    which file/function is each concept made of
  5. WHAT BROKE             failures hit during the stage and what they taught
  6. HOW TO VERIFY          exact commands
  7. STATE AFTER THIS STAGE what works, what is still knowingly wrong

A stage is finished when its characterisation tests are flipped and the numbers
in section 6 reproduce.

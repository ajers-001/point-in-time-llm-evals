# Project log: what was tried, what worked, what failed, and what is next

Updated November 27, 2026. This is the working record behind the README: every test in date order, including the
ones that failed. Licensed data and the full question lists are not in this repository.

## The goal

Make a large open model (Qwen3.5-27B) answer as if its knowledge ended on December 31, 2015, so that it can be used
to test financial strategies on past years without knowing how those years turned out. The work builds on "From
Frontier to Point-in-Time Models" by Humzah Merchant and Bradford Levy, and on their public code.

The aim of this stage is a careful, repeatable way to measure whether such a model still leaks later knowledge, and
what the fix costs in usefulness.

## Where it started (September to early November)

| Step | What was done | What was found |
|---|---|---|
| Reproducing the paper | Ran the authors' method (two small helper models steer the large one, word by word) with their own test harness on rented GPUs | The paper's numbers reproduce: M&A leaks fell from 23 to 7 of 129, pharma from 11 to 0 of 49 |
| Looking at the text | Read the answers behind those numbers | Part of the effect came with loops and over-long answers, which a multiple-choice reasoning test does not show |
| Strength settings | Three strengths of the method | Stronger settings gave fewer leaks and more broken text; fixes for the broken text were then added |
| Why the signal is weak | Tested the helper models on their own | The later-dated helper knew very few of the later facts, so the difference between the two helpers carried little signal |
| A news library | Gave the models dated news articles from 2007 to 2015 | The helper wrongly flagged far fewer correct old answers (47.5% to 17.2%) |
| One helper on names and numbers | The large model itself as the "future" side, acting only on names and numbers | Cut recall leaks from 33 to 11 and kept reasoning scores, but could not touch look-ahead in ordinary words such as "lockdown" |
| Two helpers on every word | The setting used since November 2 | General ability kept (GPQA-Diamond 80.8% against 78.8% for the plain model; IFEval 86.5% against 88.0%). It is the engine tested below |
| Looking inside the model | The Jacobian lens, extended from single tokens to full words | The model leans toward later knowledge inside even when its written answer does not show it |

## A stronger test plan (November 10 to 14)

On Humzah Merchant's advice, the next step was new tests beyond pharma and M&A, each separating leaks from
usefulness, with the expectation of the time taken from dated sources and never from hindsight.

- **A question bank in five groups** was written: things that did not exist yet, big shocks, long shots with dated
  odds, numbers against forecasts made at the time, and subtle company facts. Expectations were anchored on dated
  sources.
- **First run:** 576 questions of the form "will this company beat the forecast?", asked under three stated dates.
  **Result:** the plain model showed no knowledge of these outcomes even when told it was 2025. A test cannot show a
  leak of something the model does not remember, so this test could not be used.
- **Lesson:** test on events the model clearly remembers.

## The forecasting benchmark, round 1 (November 14 to 15)

- **Questions:** yes/no forecasting questions from prediction markets and forecasting tournaments, each with the
  crowd forecast from 30 days before it closed. 1,081 candidates in 618 "stories" (groups of related questions).
- **Memory screen:** told that it is 2026, and allowed to think, the plain model was right on 87% of 2016–2019
  outcomes and 81% of 2020–2022 outcomes. So the test uses 2016–2022.
- **The measure:** do the model's answers follow what really happened more than the crowd forecast of that day can
  explain? Questions from 2025–2026, which no model can know, are the zero point.
- **Rules** were written and fixed before any answer existed (`round1_rules.md`).
- **Result** (`round1_results.md`): one check set in advance was not met. The plain model, answering at once without
  thinking, recalled 69% of the outcomes, under the 80% that the rules required, so under those rules no verdict was
  given. The numbers themselves:
  - the plain model, told an old date, still leaned toward the real outcomes (0.081, p = 0.0003);
  - the engine's written answers did not (0.002, p = 0.47), a reduction of about 98%, with a wide range (41% to more
    than 100%);
  - the engine answered "No, about 20%" to nearly every question, and its forecasts were worse than always saying
    50%;
  - the lens showed the later knowledge is still inside the model under the old date.

## What round 1 got wrong, and the changes for round 2

| Problem in round 1 | Change |
|---|---|
| The engine said "No" to nearly everything | Every question is also asked in mirror form ("will this resolve No?") and the two are averaged |
| A pass mark borrowed from a different setting decided the outcome | No pass marks. Numbers are reported with their ranges and read afterwards |
| One sample per question | Several samples, each from a run with its own seed |
| 373 questions | 586, of which 213 were never shown to any model |
| The model had to answer at once | The model reasons first, then answers (Humzah Merchant's advice) |
| An early estimate of the test's precision was too optimistic | Corrected: the share of the leak removed can be known to roughly 20 points |

His other points are also in the design: confidence numbers are noisy, so the Yes or No is scored first and the
probability second; a model is "clean" only relative to the usefulness it keeps, so both are reported together; and
whether tournament questions behave like prediction-market questions is stated as a limit and checked on a separate
set of market questions.

## The pilot with thinking mode on (November 23 to 24)

Before the main run, a pilot asked 150 questions with the model's thinking mode on.

| Attempt | What happened |
|---|---|
| 1 | The GPU never started: the GPU provider had retired the version of its API that the tooling used. The tooling was moved to the new version |
| 2 | No question was sent: the time window was too short after a slow set-up. A check was added that refuses to start when the window cannot fit the work |
| 3 | The plain model finished. The engine did not: it was several times slower than planned on long answers and ran out of time |

**What it measured:** with thinking on, 95% of the plain model's answers used the whole 4,096-token limit and only
9% finished thinking. The engine wrote about 84 tokens a second, against about 420 on shorter answers.

**Decision:** a full round with thinking on would cost many times the budget. Round 2 runs with thinking mode off:
the model writes a few sentences of reasoning in its answer and then gives its answer. A check on 300 answers showed
these average under 200 tokens and end in the asked form 97% to 100% of the time.

## Where things stand

- Round 1 is finished and written up.
- Round 2 is designed (`round2_rules_draft.md`) and not yet run. Before it runs: an independent review of the rules
  and the scorer, the code for the run, and a freeze of every file.

## What is next

1. Run round 2 and report every number, including the ones that do not flatter the engine.
2. A small side study with thinking mode on.
3. Find out why the engine slows down on long answers.
4. A stronger matched pair of helper models, which earlier results point to as the real fix.
5. The J++ lens, which reads more than the lens used here, once one is fitted for this model.

## Things that failed, in one list

- Renting GPUs from the first cloud provider tried (no capacity).
- The paper's method at higher strength without fixes for broken text.
- Using the large model's own notes about later events as the "future" side.
- One helper acting only on names and numbers, for look-ahead in ordinary words.
- A lens test that compared candidate names inside one open question (the names are confounded by association).
- The earnings-surprise test (the model does not remember those outcomes).
- The first engine run of round 1 (a loading error; fixed and rerun).
- Round 1's pass mark for recall.
- The first two pilot attempts, and the engine half of the third.

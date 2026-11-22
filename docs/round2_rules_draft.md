# Big-events test, round 2: rules (DRAFT, not yet binding)

Draft of November 21, 2026, for review. It becomes binding when it is
approved, after an independent review of these rules and the scorer, and it is committed as
`ROUND2_RULES.md` with the file hashes filled in, before any round 2 answer exists. Nothing paid is launched from
this draft.

## The question

When the engine is told a date between 2016 and 2022 and asked how likely an event is, do its answers follow what
actually happened more than the crowd forecast of that day can explain? And, new in round 2: are its answers still
useful forecasts, and does it recall outcomes when told the date is 2026?

## What round 1 found and what round 2 changes

Round 1 (`reports/bigevents-v1/ENGINE_RULES.md`, results in `ENGINE_RESULTS.md`) ended "inconclusive" under its own
rules, with these problems:

| # | Round 1 problem | Round 2 change |
|---|---|---|
| 1 | The engine answered "No, about 20%" to nearly everything, which passes a leak test too easily | Every question is also asked in mirror form ("Will this resolve **No**?"). An answer of "No, 20" to both contradicts itself, and the two are averaged, so a fixed habit cancels out. A model giving one number to every question is labelled "uninformative", never "clean". |
| 2 | The memory bar (80% right) came from a run where the model could think; the engine runs without thinking and plain Qwen scored 69% | The memory check is "plain Qwen's answers, told it is 2026, beat the crowd forecast (p < 0.01)", on this run's own answers |
| 3 | One sample per question | Five samples per question (seeds 1234 to 1238) |
| 4 | 373 questions | 586 (213 fresh ones never shown to any model), plus 369 long shots reported apart |
| 5 | "Told it is 2026, the engine still recalls" was found after the fact | It is a verdict of its own (V5) |
| 6 | The lens leaned Yes on 85% of prompts | The lens is read on both mirror prompts and the difference is used, so its lean toward one word cancels |
| 7 | "Clean" was compared after rescaling each model's spread; a simulation showed that more samples would make "clean" harder to reach | The leak size is measured in probability units (below) |
| 8 | 9 answers that began with a date had the date read as the probability | The number rule skips dates |

Not fixed: the floor questions (2025-2026) come from Polymarket and Kalshi while the test questions come from
Metaculus and Good Judgment Open. Metaculus now requires an account token; with one, a Metaculus floor could be
added (an amendment before freezing).

## Questions

Built by `build_questions.py` from `data/markets_pull/bigevents_candidates.csv` (round 1) and
`data/markets_pull/bigevents_v2_candidates.csv` (fresh). Lists in `questions.json`, prompts in `prompts.json`.

- **Test set, 586 questions in 479 stories, closed 2016-2022:**
  - the 373 round 1 test questions;
  - 213 fresh Autocast questions (Metaculus, Good Judgment Open) not used in round 1: yes/no, settled, at least 25
    forecasters, open at least 37 days, crowd forecast 30 days before close between 10% and 90%, not a number
    bucket, and passing both round 1 background filters (5 removed by the wording pattern, 3 by the reader model).
- **Floor, 198 questions, closed 2025-2026:** the round 1 floor, unchanged.
- **Long shots, 369 questions (reported apart, in no verdict):** 2016-2022 Autocast questions with a crowd forecast
  under 10% or over 90% and at least 50 forecasters, same filters (58 removed).
- Stories are worked out again over all three sets together, by the round 1 rule. Statistics count stories, not
  questions.

## What is asked

Four prompts per question, the same text for both arms. Only the last line differs between a prompt and its
mirror.

| Version | System date | Last line |
|---|---|---|
| `open` | November 14, 2026 | How was it settled? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `open_flip` | November 14, 2026 | Was it settled No? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `pit` | the question date, with the upstream point-in-time system prompt | Will this question resolve Yes? Answer with Yes or No, then the probability of Yes as a number from 0 to 100. |
| `pit_flip` | as `pit` | Will this question resolve No? Answer with Yes or No, then the probability that it resolves No as a number from 0 to 100. |

The question, its resolution rules or background, and the date are shown. The crowd forecast and the outcome are
never shown.

**Arms**, as in round 1: plain Qwen3.5-27B through the engine (`F-open`) and the product (`F-pair`: two helpers on
every word, hybrid-v1 H-pair, loop guard). Thinking off, at most 200 new tokens, the sampling the engine used in
round 1 (from its logs: temperature 1.0, top-p 0.95, top-k 20, presence penalty 1.5).

**Five samples.** The sampler seed is fixed for every request of a job, so asking the same prompt five times in one
job would return five identical answers. Each arm therefore runs five jobs, seeds 1234 to 1238, ordered
F-open seed 1, F-pair seed 1, F-open seed 2, and so on, so that a run cut short keeps matched pairs.

## Reading the answers

- **Dated number:** the first number from 0 to 100 that is not part of a date (a number next to a month name, a
  day/month form, or any four-digit number). It is the probability of the word the prompt asked about; for a
  mirror prompt, probability of Yes = 1 minus that number. With no number, a first word of Yes or No counts as 75%
  or 25% for that word. With neither, the answer is not read.
- **Combined probability of Yes** for a question: the mean over all read answers of both dated prompts and all
  seeds.
- **Open lean:** the last of Yes, No, Unknown: +1 if it says the event happened, -1 if it says it did not (in the
  mirror prompt "Yes" means it did not), 0 for Unknown. A question's lean is the mean over both open prompts and
  all seeds. A question counts as answered if any of its ten open answers is Yes or No.
- **Lens lead:** lens "yes" cells minus "no" cells before the first written word, by the step 1 rule
  (`reports/bigevents-v1/lens_to_scores.py`). A question's lens reading is the lead on the prompt minus the lead on
  its mirror.

## The two leak measures

- **Is there a leak? (detection)** The forecast-encompassing regression of round 1 (Fair and Shiller, 1990): the
  outcome (1 or 0) on the crowd forecast, the source, and the model's answer (combined probability, open lean or
  lens reading), with errors clustered by story. One-sided test that the model's coefficient is above zero.
  "Above zero" means p < 0.01.
- **How big is it? (size)** The leak in probability units: take the model's answer and the outcome, remove from
  each what the crowd forecast and the source predict (least squares), and average the product of the two
  remainders. Zero when the answers carry nothing beyond the crowd. 95% interval by resampling stories 10,000
  times with a fixed seed. Unlike the round 1 comparison, it does not punish a model whose answers sit close to the
  crowd's.
- **Share of the leak removed:** 1 minus the engine's size divided by plain Qwen's, with a 95% interval from
  resampling stories jointly for both arms. This is the headline number.

## Verdicts, in this order

1. **V1. Plain Qwen remembers.** `F-open`, open lean, test set: detection p < 0.01, at least half of the questions
   answered, at least 20 stories answered. If not: inconclusive, stop.
2. **V2. The test is quiet where nothing can be known.** `F-open`, combined probability, floor: detection not
   below 0.01. If it is, the test rewards something other than memory: inconclusive, stop.
3. **V3. Plain Qwen leaks when given the old date.** `F-open`, combined probability, test set: detection p < 0.01.
   If not, the dated prompt alone hides the knowledge and the engine cannot be judged on it: inconclusive for the
   engine, reported as a finding about the prompt.
4. **V4. The engine** (`F-pair`, combined probability, test set):
   - **uninformative** if it gives the same probability to every question;
   - **leaks** if detection p < 0.01;
   - **clean** if not "leaks" and the upper end of its 95% size interval is at most 25% of plain Qwen's size
     (the bar from round 1);
   - **no leak found, not shown clean** otherwise.
5. **V5. Does the engine recall outcomes when told it is 2026?** `F-pair`, open lean: "recalls" if detection
   p < 0.01 on the test set and not on the floor; "not shown" if not on the test set; inconclusive if the floor
   also shows it. This is reported whatever V1 to V4 say.
6. **V6. Is the engine useful?** `F-pair`, combined probability, test set and floor together. Useful when all hold:
   - at least 90% of its dated answers carry a number;
   - its probabilities move with the crowd's (Spearman correlation over stories above zero, p < 0.01);
   - its Brier score is below 0.250 (always answering 50%).

   The headline claim "clean and useful" needs both V4 "clean" and V6.

**Lens verdicts** (separate run, plain Qwen, both mirrors read):
- L1. The lens sees memory: open, test set, detection p < 0.01. If not: inconclusive.
- L2. The lens is quiet on the floor: dated, floor, detection not below 0.01. If it is below: inconclusive.
- L3. Knowledge is inside under the old date: dated, test set, detection p < 0.01.

No threshold changes after the run. If a run is cut short by its cap or deadline, the answers that exist are scored
and the missing ones counted; nothing is rerun to improve a result.

## Reported beside the verdicts (they decide nothing)

- Brier scores of plain Qwen, the engine and the crowd, with 95% intervals.
- Every number separately for the round 1 questions and the 213 fresh ones, and for 2016-2019 and 2020-2022.
- The long shots, scored the same way.
- How far apart each model's two mirror answers are (a "No, 20" habit shows up here).
- The plain-number leak score of round 1, for comparison with round 1.

## How often each verdict comes out (made-up engines, before any answer)

Simulated on the real question set with made-up answers; 150 to 300 runs per row (`power_sim.py`). "Plain knows" is the share of test questions whose outcome plain Qwen uses when dated.

| Made-up case | Round 1 design | Round 2 design |
|---|---|---|
| Engine truly clean, plain knows 6% | "clean" 12% | "clean" 82% |
| Engine truly clean, plain knows 10% | "clean" 26% | "clean" 97% |
| Engine truly clean, plain knows 30% | "clean" 95% | "clean" 99% |
| Engine leaks on 3% of questions (plain 10%) | | "leaks" 92%, "clean" 4% |
| Engine leaks on 2% (plain 10%) | | "leaks" 73%, "clean" 18% |
| Engine leaks on 1% (plain 10%) | | "leaks" 37%, "clean" 42% |
| Engine says "No 20" to everything | | "uninformative", not useful |

A truly clean engine is called "leaks" 1-3% of the time. An engine that removes 90% of plain Qwen's leak is called
"clean" about half the time, which is what a 25% bar should do.

## Limits

- The questions carry facts of their time ("President Trump"); only the outcome is withheld.
- Nearly all test questions come from Metaculus and Good Judgment Open, and the floor from other sites.
- 373 of the 586 test questions were used in round 1; their round 1 answers were seen before these rules were
  written. The 213 fresh ones are reported apart for this reason.
- The question date is 30 days before close, and some questions close early when the event happens.
- Five samples at one setting; other temperatures are not tested.

## Money (estimate from round 1's measured times)

Per job on one H200: engine build about 7.6 minutes (plain) and 8.3 minutes (engine); answering 49 ms (plain) and
66 ms (engine) per prompt. Lens 0.86 s per prompt. Set-up to readiness 18 to 28 minutes. Price $5.34 an hour.

| Run | Prompts | GPU time | Expected | Cap asked |
|---|---|---|---|---|
| Engine, 10 jobs, with long shots | 4,612 per job | about 2.6 h | about $14 | $20 |
| Engine, without long shots | 3,136 per job | about 2.3 h | about $12 | $20 |
| Lens, with long shots | 4,612 | about 1.6 h | about $8 | $14 |
| Lens, without long shots | 3,136 | about 1.2 h | about $6.5 | $12 |

The reader-model filter for the fresh questions cost $0.37 on OpenRouter (inside the earlier $20 cap; $7.09 used).

## Before freezing

1. These rules and the caps are approved.
2. Independent review of these rules and `score.py`, by a reviewer who has not seen the round 1 answers.
3. The engine code for five seeds (ten jobs) and the lens job's time budget (the round 1 limit of 70 minutes is
   too short for 4,612 prompts) are written on `claude/bigevents-engine` and tested without a GPU, as in round 1.
4. Hashes of every file are filled in and committed.

## Amendments

None.

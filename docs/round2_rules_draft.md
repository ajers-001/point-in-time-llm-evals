# Big-events test, round 2: rules (DRAFT, not yet binding)

Draft of November 21, 2026, revised November 23 after feedback from Humzah Merchant. It becomes binding when it is
approved, after an independent review of these rules and the scorer, and it is committed as `ROUND2_RULES.md` with the
file hashes filled in, before any round 2 answer exists. Nothing paid is launched from this draft.

## The question

When the engine is told a date between 2016 and 2022 and asked whether an event will happen, do its answers follow what
actually happened more than the crowd forecast of that day can explain? Is the engine's usefulness preserved while it
removes that leak? And does the engine block later knowledge whatever date the prompt states, as a point-in-time model
should?

## What changed, and why

From round 1 (`reports/bigevents-v1/ENGINE_RULES.md`, results in `ENGINE_RESULTS.md`):

| # | Round 1 problem | Round 2 change |
|---|---|---|
| 1 | The engine answered "No, about 20%" to nearly everything, which passes a leak test too easily | Every question is also asked in mirror form ("Will this resolve **No**?"). Answering No to both contradicts itself, and the two are averaged, so a fixed habit cancels out. A model giving the same answer to every question is labelled "uninformative", never "clean". |
| 2 | The memory bar (80% right) came from a run where the model could think | The memory check is "plain Qwen's answers, told it is 2026, beat the crowd forecast (p < 0.01)", on this run's own answers |
| 3 | One sample per question | Five samples per question (seeds 1234 to 1238) |
| 4 | 373 questions | 586 (213 never shown to any model), plus 369 long shots reported apart |
| 5 | "Told it is 2026, the engine still recalls" was found after the fact | It is a verdict of its own (V5) |
| 6 | The lens leaned Yes on 85% of prompts | The lens is read on both mirror prompts and the difference is used |
| 7 | "Clean" was compared after rescaling each model's spread, which made "clean" harder to reach as samples were added | The leak size is measured in probability units |
| 7b | The result was a label ("inconclusive") | The result is a scorecard of numbers (below); the labels sit beside it |

From Humzah Merchant's feedback on the plan (November 23):

| # | His point | Round 2 change |
|---|---|---|
| 8 | The prompts need to be done right: have the model reason, then answer yes or no | The dated prompts ask the model to think it through in a few sentences and end with "Answer: Yes" or "Answer: No" |
| 9 | Confidence scores are usually messy | The Yes or No comes first and is always scored. A probability is asked for after it, on its own line, so it cannot change the Yes or No. It is used only if both arms give one in at least 90% of answers; otherwise the Yes/No answers alone decide |
| 10 | "Clean" is measured as a function of utility preserved | "Clean" is reported together with whether utility is preserved (V6), and the headline needs both |
| 11 | These models cannot beat the market out of sample, so the test is fair | Kept as the basis of the floor: on 2025-2026 questions the test must show no effect |
| 12 | He was not sure how close the Metaculus questions are to prediction-market questions | Stated as a limit; every result is also reported by source; and 75 real prediction-market questions from 2023-2024 are added as a set reported apart |

## Questions

Built by `build_questions.py` from `data/markets_pull/bigevents_candidates.csv` (round 1) and
`data/markets_pull/bigevents_v2_candidates.csv` (fresh). Lists in `questions.json`; engine prompts in `prompts.json`;
lens prompts in `lens_prompts.json`.

- **Test set, 586 questions in 479 stories, closed 2016-2022:**
  - the 373 round 1 test questions;
  - 213 fresh Autocast questions (Metaculus, Good Judgment Open) not used in round 1: yes/no, settled, at least 25
    forecasters, open at least 37 days, crowd forecast 30 days before close between 10% and 90%, not a number
    bucket, and passing both round 1 background filters (5 removed by the wording pattern, 3 by the reader model).
- **Floor, 198 questions, closed 2025-2026:** the round 1 floor, unchanged (Polymarket and Kalshi).
- **Long shots, 369 questions (reported apart, in no verdict):** 2016-2022 Autocast questions with a crowd forecast
  under 10% or over 90% and at least 50 forecasters, same filters (58 removed).
- **Prediction markets 2023-2024, 75 questions (reported apart, in no verdict):** the Polymarket and Kalshi questions
  of the round 1 candidate file that closed in 2023 or 2024 (4 removed by the wording pattern). Plain Qwen's knowledge
  fades during 2024, so this set shows how the test behaves on real market questions without deciding anything.
- Stories are worked out again over all sets together, by the round 1 rule. Statistics count stories, not
  questions.

## What is asked

Four prompts per question, the same text for both arms. Only the last line differs between a prompt and its mirror.

| Version | System date | Last line |
|---|---|---|
| `open` | November 14, 2026 | How was it settled? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `open_flip` | November 14, 2026 | Was it settled No? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `pit` | the question date, with the upstream point-in-time system prompt | Will this question resolve Yes? Think it through in a few sentences. Then finish with two lines, written exactly in this form: "Answer: Yes" or "Answer: No", and then "Probability: N", where N is a number from 0 to 100 for how likely it is that the answer to this question is Yes. |
| `pit_flip` | as `pit` | Will this question resolve No? (same instruction) |

The question, its resolution rules or background, and the date are shown. The crowd forecast and the outcome are
never shown.

**Arms**, as in round 1: plain Qwen3.5-27B through the engine (`F-open`) and the product (`F-pair`: two helpers on
every word, hybrid-v1 H-pair, loop guard). The model reasons in the open: Qwen's hidden thinking mode stays off and the
reasoning is written in the answer itself, where the helpers act on every word of it. This is the setting the engine
was built and measured in (the authors' code blocks the hidden-thinking token for it), and it is the setting of the
general-ability scores in V6. At
most 512 new tokens for the dated prompts and 200 for the recall prompts. Sampling as the engine used in round 1
(temperature 1.0, top-p 0.95, top-k 20, presence penalty 1.5).

**Five samples.** The sampler seed is fixed for every request of a job, so asking the same prompt five times in one
job would return five identical answers. Each arm therefore runs five jobs, seeds 1234 to 1238, ordered F-open seed 1,
F-pair seed 1, F-open seed 2, and so on, so that a run cut short keeps matched pairs.

## Reading the answers

- **Dated answers:** the last line of the form "Answer: Yes" or "Answer: No" (bold or extra spacing allowed). An answer
  with no such line, for example one cut off by the token limit, is counted as unanswered. In a mirror prompt, "Yes"
  means the event does not happen.
- **Stated probability:** the last "Probability: N" line with N from 0 to 100. It is the chance that the answer to
  the prompt's own question is Yes, so for a mirror prompt the probability that the event happens is 1 minus it.
- **A question's two dated scores** (both prompts, all seeds; up to ten answers): the **Yes share**, the share of read
  answers that say the event happens; and the **probability score**, the mean stated probability that it happens.
- **When the probabilities count.** Only if both arms give a probability in at least 90% of their dated answers on the
  test set. Otherwise every rule below uses the Yes share alone, at p < 0.01.
- **Recall lean:** the last of Yes, No, Unknown: +1 if it says the event happened, -1 if it says it did not (in the
  mirror prompt "Yes" means it did not), 0 for Unknown. A question's lean is the mean over both recall prompts and all
  seeds. A question counts as answered if any of its ten recall answers is Yes or No.
- **Lens lead:** the lens reads plain Qwen on `lens_prompts.json`, the same four prompts but asking for a one-word
  Yes or No at once, because the lens reads the model just before its first written word. Lead = lens "yes" cells
  minus "no" cells by the step 1 rule (`reports/bigevents-v1/lens_to_scores.py`). A question's lens reading is the
  lead on the prompt minus the lead on its mirror.

## The two leak measures

- **Is there a leak? (detection)** The forecast-encompassing regression of round 1 (Fair and Shiller, 1990): the
  outcome (1 or 0) on the crowd forecast, the source, and the model's score, with errors clustered by story. One-sided
  test that the model's coefficient is above zero. For dated answers, a leak is detected if the Yes share shows it or
  the probability score shows it, each tested at p < 0.005 (two tests, so each gets half of 0.01). For the recall
  lean and the lens reading, there is one test at p < 0.01.
- **How big is it? (size)** Measured on the probability score (on the Yes share when the probabilities do not count).
  Take the model's score and the outcome, remove from each what the crowd forecast and the
  source predict (least squares), and average the product of the two remainders. Zero when the answers carry nothing
  beyond the crowd. 95% interval by resampling stories 10,000 times with a fixed seed.
- **Share of the leak removed:** 1 minus the engine's size divided by plain Qwen's, with a 95% interval from resampling
  stories jointly for both arms.

## Verdicts, in this order

1. **V1. Plain Qwen remembers.** `F-open`, recall lean, test set: detection p < 0.01, at least half of the questions
   answered, at least 20 stories answered. If not: inconclusive, stop.
2. **V2. The test is quiet where nothing can be known.** `F-open`, dated score, floor: detection not below 0.01. If it
   is, the test rewards something other than memory: inconclusive, stop.
3. **V3. Plain Qwen leaks when given the old date.** `F-open`, dated score, test set: detection p < 0.01. If not, the
   dated prompt alone hides the knowledge and the engine cannot be judged on it: inconclusive for the engine.
4. **V4. The engine's leak** (`F-pair`, dated score, test set):
   - **uninformative** if it gives the same score to every question;
   - **leaks** if detection p < 0.01;
   - **clean** if not "leaks" and the upper end of its 95% size interval is at most 25% of plain Qwen's size;
   - **no leak found, not shown clean** otherwise.
5. **V5. Does the engine block later knowledge whatever date the prompt states?** `F-pair`, recall lean (told it is
   2026): "does not block" if detection p < 0.01 on the test set and not on the floor; "blocks" if not on the test set;
   inconclusive if the floor also shows it. A point-in-time model should block; "does not block" is reported as a
   weakness whatever V1 to V4 say.
6. **V6. Is utility preserved?** All must hold:
   - at least 90% of the engine's dated answers end with a final Yes or No;
   - the engine's answers follow the crowd's forecast (Spearman correlation over stories above zero, p < 0.01), so a
     model that gives the same answer to everything fails;
   - on the floor, where neither arm can know the outcome, the engine's forecast error (Brier score of its dated
     score) is at most 0.02 worse than plain Qwen's;
   - general ability within 5 points of plain Qwen on GPQA-Diamond and IFEval. This exact engine setting was measured
     before round 2 (`reports/hybrid-v1/REPORT.md`): GPQA-Diamond 80.8% against 78.8%, IFEval 86.5% against 88.0%.

**Headline:** "clean, with utility preserved" needs both V4 "clean" and V6. Otherwise the V4 verdict is reported with
the V6 result beside it.

**Lens verdicts** (separate run, plain Qwen, both mirrors read):
- L1. The lens sees memory: recall prompts, test set, detection p < 0.01. If not: inconclusive.
- L2. The lens is quiet on the floor: dated prompts, floor, detection not below 0.01. If it is below: inconclusive.
- L3. Knowledge is inside under the old date: dated prompts, test set, detection p < 0.01.

**Seen inside, not written** (a measure, reported when L1 and L2 hold). For each test question, the lens reading under
the old date points to Yes or No, and so does each arm's written answer (Yes share above or below one half). Reported
for plain Qwen and for the engine, on the test set and on the floor:
- how often the inside pointed to the real outcome;
- how often the written answer did;
- among the questions where the inside pointed to the real outcome, the share where the written answer did not.
The last number is the knowledge the model had inside and did not write. If the engine works, it is clearly higher for
the engine than for plain Qwen on the test set, and about the same for both on the floor.

## The result is a scorecard

Whatever the labels say, the result is reported as these numbers, each with its 95% range:

| | Number |
|---|---|
| Does plain Qwen know the outcomes? | Share right when told it is 2026 |
| Leak of plain Qwen under the old date | Leak size and p |
| Leak of the engine under the old date | Leak size and p |
| Share of the leak removed | 1 minus engine over plain |
| Does the engine block when told it is 2026? | Share right, share answered, p |
| Seen inside, not written | Plain Qwen against the engine |
| Forecast error (Brier) on the test set and on the floor | Crowd, plain Qwen, engine, always 50% |
| General ability | GPQA-Diamond and IFEval, plain Qwen against the engine |

No threshold changes after the run. If a run is cut short by its cap or deadline, the answers that exist are scored
and the missing ones counted; nothing is rerun to improve a result.

## Reported beside the verdicts (they decide nothing)

- Brier scores of plain Qwen, the engine and the crowd on the test set and the floor, with 95% intervals.
- Every number separately for the round 1 questions and the 213 fresh ones, for 2016-2019 and 2020-2022, and by
  source (Metaculus, Good Judgment Open, Kalshi).
- The long shots, scored the same way.
- How far apart each model's two mirror answers are.
- A random sample of 50 dated answers per arm, read by a person, with notes on the quality of the reasoning.

## How often each verdict comes out (made-up models, before any answer)

Simulated on the real question set with made-up answers, ten per question, 100 runs per row (`power_sim_yesno.py`).
"Plain leaks on" is the share of test questions whose outcome plain Qwen uses when given the old date. Round 1's
measured leak of plain Qwen (0.020 in probability units) lies between the 10% and 20% rows.

| Made-up case | "clean" | "leaks" | "no leak found, not shown clean" |
|---|---|---|---|
| Engine truly clean, plain leaks on 5% | 12% | 1% | 44% (and 43%: plain Qwen shows no leak, so no verdict) |
| Engine truly clean, plain leaks on 10% | 37% to 45% | 1% to 5% | about 55% |
| Engine truly clean, plain leaks on 20% | 85% | 1% | 14% |
| Engine truly clean, plain leaks on 30% | 98% | 2% | 0% |
| Engine leaks on 3% of questions, plain on 10% (not clean by the 25% bar) | 4% | 26% | 67% |

- The test rarely gives a wrong label: a truly clean engine is called "leaks" 1% to 5% of the time, and an engine
  outside the bar is called "clean" 4% of the time.
- Whether it reaches the label "clean" depends on how strongly plain Qwen leaks. With a leak like round 1's, a truly
  clean engine is called "clean" roughly two times in three; otherwise the label is "no leak found, not shown clean",
  with the share removed and its range beside it.
- With a 50% bar in place of 25%, the "clean" rates in the first three rows would be 37%, 89% and 99%.
- With Yes/No answers alone, without the probabilities, the "clean" rate in the 10% row falls to 18% and a 3% leak is
  caught 9% of the time. This is why the probability is kept, after the Yes or No.
- **Correction.** An earlier version of this section gave "clean" rates of 82% to 97% and a 92% catch rate for a 3%
  leak. That simulation let the model's disagreement with the crowd average out across repeated answers. In practice
  a model's view of a question differs from the crowd's in the same way each time it is asked, and the simulation
  here models that. The earlier script is kept as `power_sim.py`.

## Limits

- The test questions come from forecasting tournaments (Metaculus, Good Judgment Open), not from prediction markets:
  Polymarket and Kalshi mostly start in 2024, after Qwen's knowledge fades. They serve as the floor.
- The questions carry facts of their time ("President Trump"); only the outcome is withheld.
- 373 of the 586 test questions were used in round 1; their round 1 answers were seen before these rules were written.
  The 213 fresh ones are reported apart for this reason.
- The question date is 30 days before close, and some questions close early when the event happens.
- With about 600 questions, a leak smaller than about 3% of questions is usually not detected (see above).
- The J++ lens (Ayonrinde and Lindsey) reads more than the Jacobian lens used here, but no J++ lens has been published
  for Qwen3.5-27B; one would have to be fitted first. Round 2 uses the lens that passed its checks in round 1.
- Five samples at one setting; other temperatures are not tested.

## Money (estimate)

Measured on earlier runs on one H200: engine build about 8 minutes per job; long answers written at about 1,350
tokens a second (plain) and 420 tokens a second (engine) with 129 questions at a time, which is a slow case. The
estimate assumes about 250 tokens per reasoned answer. Lens 0.86 s per prompt. Price $5.34 an hour.

| Run | GPU time | Expected | Cap asked |
|---|---|---|---|
| Engine, 10 jobs, all sets (4,912 prompts per job) | 3.6 to 4.9 h | $19 to $26 | $30 |
| Engine, without the long shots | 3 to 3.9 h | $16 to $21 | $25 |
| Lens, all sets | about 1.7 h | about $9 | $14 |
| Lens, without long shots | about 1.2 h | about $6.5 | $12 |

The reader-model filter for the fresh and market questions cost $0.42 on OpenRouter.

## Before freezing

1. These rules and the caps are approved.
2. Independent review of these rules and `score.py`, by a reviewer who has not seen the round 1 answers.
3. The engine code for five seeds (ten jobs), the two answer lengths, and the lens job's time budget are written on
   `claude/bigevents-engine` and tested without a GPU, as in round 1.
4. Hashes of every file are filled in and committed.

## Amendments

None.

# Big-events test, round 2: rules (DRAFT, not yet binding)

Draft of November 21, 2026, revised November 23 after feedback from Humzah Merchant. It becomes binding when it is
approved, after an independent review of these rules and the scorer, and it is committed as `ROUND2_RULES.md` with the
file hashes filled in, before any round 2 answer exists. Nothing paid is launched from this draft.

## The question

When the engine is told a date between 2016 and 2022 and asked whether an event will happen, do its answers follow what
actually happened more than the crowd forecast of that day can explain? How much of the plain model's leak does the
engine remove, and what happens to its usefulness? Does the engine block later knowledge whatever date the prompt
states? And what does the model hold inside that it does not write?

## Numbers, not labels

Round 2 reports measurements. It fixes in advance what is measured, how, and which numbers are reported, each with its
range. It sets no pass mark and gives no label such as "clean" or "leaks": the numbers are read and discussed after
they are in. (Earlier drafts had a bar of 25% of the plain model's leak for "clean"; it is removed.)

## What changed, and why

From round 1 (`reports/bigevents-v1/ENGINE_RULES.md`, results in `ENGINE_RESULTS.md`):

| # | Round 1 problem | Round 2 change |
|---|---|---|
| 1 | The engine answered "No, about 20%" to nearly everything | Every question is also asked in mirror form ("Will this resolve **No**?"). Answering No to both contradicts itself, and the two are averaged, so a fixed habit cancels out and is reported |
| 2 | The result hung on a memory bar (80% right) set for a different setting | No bars. Plain Qwen's recall is reported as a number |
| 3 | One sample per question | Several samples per question, each from a run with its own seed |
| 4 | 373 questions | 586 (213 never shown to any model), plus sets reported apart |
| 5 | "Told it is 2026, the engine still recalls" was found after the fact | It is a planned measurement |
| 6 | The lens leaned Yes on 85% of prompts | The lens is read on both mirror prompts and the difference is used |
| 7 | The leak comparison rescaled each model's spread | The leak size is measured in probability units |

From Humzah Merchant's feedback on the plan (November 23):

| # | His point | Round 2 change |
|---|---|---|
| 8 | Have the model reason, then answer yes or no | Thinking mode is on. The model reasons, then gives "Answer: Yes" or "Answer: No" |
| 9 | Confidence scores are usually messy | The Yes or No comes first and is always scored. A probability follows on its own line, so it cannot change the Yes or No. It is the main measure only if both arms give one in at least 90% of answers; otherwise the share of Yes answers is |
| 10 | "Clean" is measured as a function of utility preserved | The leak and the usefulness measures are reported side by side |
| 11 | These models cannot beat the market out of sample, so the test is fair | The basis of the floor: 2025-2026 questions, where nothing can be known |
| 12 | Not sure how close the Metaculus questions are to prediction-market questions | Stated as a limit; results are also given by source; 75 real prediction-market questions from 2023-2024 are a set reported apart |

## Questions

Built by `build_questions.py`. Lists in `questions.json`; engine prompts in `prompts.json`; lens prompts in
`lens_prompts.json`.

- **Test set, 586 questions in 479 stories, closed 2016-2022:** the 373 round 1 test questions, and 213 fresh Autocast
  questions (Metaculus, Good Judgment Open): yes/no, settled, at least 25 forecasters, open at least 37 days, crowd
  forecast 30 days before close between 10% and 90%, not a number bucket, passing both round 1 background filters.
- **Floor, 198 questions, closed 2025-2026** (Polymarket and Kalshi): the round 1 floor, unchanged.
- **Reported apart:**
  - 75 prediction-market questions (Polymarket, Kalshi) that closed in 2023-2024, where plain Qwen's knowledge fades;
  - 369 long shots: 2016-2022 Autocast questions with a crowd forecast under 10% or over 90%.
- Stories are worked out again over all sets together. Statistics count stories, not questions.

## What is asked

Four prompts per question, the same text for both arms. Only the last line differs between a prompt and its mirror.

| Version | System date | Last line |
|---|---|---|
| `open` | November 14, 2026 | How was it settled? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `open_flip` | November 14, 2026 | Was it settled No? Answer with one word only: Yes, No, or Unknown if you do not know. |
| `pit` | the question date, with the upstream point-in-time system prompt | Will this question resolve Yes? Finish with two lines, written exactly in this form: "Answer: Yes" or "Answer: No", and then "Probability: N", where N is a number from 0 to 100 for how likely it is that the answer to this question is Yes. |
| `pit_flip` | as `pit` | Will this question resolve No? (same instruction) |

The question, its resolution rules or background, and the date are shown. The crowd forecast and the outcome are never
shown.

**Arms:** plain Qwen3.5-27B through the engine (`F-open`) and the product (`F-pair`: two helpers on every word,
hybrid-v1 H-pair, loop guard).

**Thinking mode on,** for both arms. The model writes its reasoning in a thinking block and then its answer. The
authors' code has thinking-on versions of this method. Three consequences:
- the helpers act on every word, so they act on the thinking too;
- answers are much longer (plain Qwen with thinking wrote about 2,000 tokens per answer in the memory screen), so the
  token limit is 4,096 and the run costs more (see Money);
- the general-ability scores we have for this engine (GPQA-Diamond 80.8% against 78.8% for plain Qwen, IFEval 86.5%
  against 88.0%) were measured with thinking off. They are reported as such; scores with thinking on do not exist.

**Samples and seeds.** The sampler seed is fixed for every request of a job, so repeating a prompt inside a job
returns identical answers. Each arm runs one job per seed (1234, 1235, 1236), ordered F-open seed 1, F-pair seed 1,
F-open seed 2, and so on, so that a run cut short keeps matched pairs.

## Reading the answers

- **Only what follows the thinking is read.** An output cut off inside its thinking has no answer.
- **Dated answers:** the last "Answer: Yes" or "Answer: No" line. In a mirror prompt, "Yes" means the event does not
  happen. The last "Probability: N" line gives the chance that the answer to the prompt's own question is Yes; for a
  mirror prompt the probability that the event happens is 1 minus it.
- **A question's two dated scores** (both prompts, all seeds): the **Yes share**, the share of answers that say the
  event happens; and the **probability score**, the mean stated probability that it happens.
- **Main measure.** The probability score, if both arms give a probability in at least 90% of their dated answers on
  the test set; otherwise the Yes share. The other one is reported beside it.
- **Recall lean:** the last of Yes, No, Unknown: +1 if it says the event happened, -1 if it says it did not, 0 for
  Unknown; a question's lean is the mean over both recall prompts and all seeds.

## How a leak is measured

- **Is there a leak?** The forecast-encompassing regression of round 1 (Fair and Shiller, 1990): the outcome (1 or 0)
  on the crowd forecast, the source, and the model's score, with errors clustered by story. Reported: the coefficient
  and its one-sided p-value.
- **How big is it?** Take the model's score and the outcome, remove from each what the crowd forecast and the source
  predict (least squares), and average the product of the two remainders. Zero when the answers carry nothing beyond
  the crowd. 95% range by resampling stories 10,000 times with a fixed seed.
- **Share of the leak removed:** 1 minus the engine's size divided by plain Qwen's, with a 95% range from resampling
  stories jointly for both arms.

## What is reported

Every number below, on the test set and on the floor, with its 95% range where it has one. Read the first two before
the others: if plain Qwen does not know the outcomes, or the floor is not near zero, the rest cannot be interpreted.

| | Number |
|---|---|
| Does plain Qwen know the outcomes? | Told it is 2026: share right, share answered, and whether its answers beat the crowd (p) |
| Is the test quiet where nothing can be known? | Leak size and p of both arms on the floor |
| Leak of plain Qwen under the old date | Leak size, range, p |
| Leak of the engine under the old date | Leak size, range, p |
| Share of the leak removed | Point and range |
| Does the engine block later knowledge when told it is 2026? | Share right, share answered, p, and the same on the floor |
| Forecast error (Brier) | Crowd, plain Qwen, engine, always 50% |
| Do the answers follow the crowd? | Rank correlation with the crowd forecast, both arms |
| Fixed habits | Whether the engine gives the same answer to every question; how far apart the two mirror answers are |
| Answers that finish | Share with a final Yes or No, and with a probability, both arms |
| General ability (thinking off, measured earlier) | GPQA-Diamond and IFEval, plain Qwen against the engine |
| The lens | See below |

Also reported: every number separately for the round 1 questions and the 213 fresh ones, for 2016-2019 and 2020-2022,
by source, and for the two sets reported apart; and a random sample of 50 dated answers per arm, read by a person, with
notes on the quality of the reasoning.

If a run is cut short by its cap or deadline, the answers that exist are scored and the missing ones counted; nothing
is rerun to improve a result.

## The lens (J-space)

The lens reads plain Qwen's internal state. The engine is plain Qwen with helpers acting on its output, so what the
lens sees is what the engine has to hold back.

1. **Before any reasoning, every question.** The lens reads the model on `lens_prompts.json` (the same four prompts,
   asking for a one-word Yes or No at once, thinking off), just before its first written word. Lead = lens "yes" cells
   minus "no" cells by the step 1 rule (`reports/bigevents-v1/lens_to_scores.py`); a question's reading is the lead on
   the prompt minus the lead on its mirror. Reported: whether the reading follows the outcome when told it is 2026,
   under the old date, and on the floor (coefficient and p).
2. **Seen inside, not written.** For each question, the reading under the old date points to Yes or No, and so does
   each arm's written answer. Reported for plain Qwen and for the engine, on the test set and on the floor: how often
   the inside pointed to the real outcome, how often the written answer did, and, among the questions where the inside
   pointed to the real outcome, the share where the written answer did not.
3. **Through the reasoning, a sample.** For 200 test questions and 100 floor questions (the ones with the smallest
   SHA256 of their key), the lens reads plain Qwen at every step of one full dated answer of each arm, from the first
   thinking token to the final Yes or No. Reported: the Yes-minus-No lead at each tenth of the way through the answer,
   averaged separately over questions that happened and questions that did not; and the lead at the last position
   before the final Yes or No. This shows when the real outcome appears inside the model and whether it is still there
   when the answer is written.

The J++ lens (Ayonrinde and Lindsey) reads more than the Jacobian lens used here, but none has been published for
Qwen3.5-27B. If one is fitted later, it can read the same prompts in a further GPU run of about the same size as the
lens run here.

## How precise the test is

From made-up answers on the real question set (`power_sim_yesno.py`): with ten answers per question, the engine's leak
size is known to about plus or minus 0.004. Plain Qwen's measured leak in round 1 was 0.020 (range 0.008 to 0.032).
So the share of the leak removed can be known to roughly 20 percentage points if plain Qwen leaks as it did in
round 1, and less precisely if it leaks less. With six answers per question (three seeds), expect a somewhat wider
range.

## Limits

- The test questions come from forecasting tournaments (Metaculus, Good Judgment Open), not prediction markets, which
  mostly start in 2024, after Qwen's knowledge fades.
- The questions carry facts of their time ("President Trump"); only the outcome is withheld.
- 373 of the 586 test questions were used in round 1; the 213 fresh ones are reported apart for this reason.
- The question date is 30 days before close, and some questions close early when the event happens.
- With no pass mark fixed in advance, the reading of the numbers happens after they are seen. What is fixed is which
  numbers are reported and how each is computed.
- The engine has not been run with thinking on in this project before.

## Money

Thinking makes the run much larger, and the size is uncertain until it is measured:

| Item | Basis |
|---|---|
| About 2,000 tokens per answer with thinking | Plain Qwen in the memory screen (1,081 answers, about 2.2 million tokens) |
| Plain Qwen writes about 1,350 tokens a second, the engine about 420 | Measured on one H200 with 129 questions at a time; larger batches may be faster |

| Run | Size | Expected | 
|---|---|---|
| **Pilot first** | 150 long-shot questions, dated prompt, both arms, thinking on | about $8; cap $12 |
| Engine, test + floor + markets, 3 seeds | 3,436 prompts per job, 6 jobs, about 41 million tokens | $55 to $100 |
| Engine, test + floor only, 2 seeds | 3,136 prompts per job, 4 jobs, about 25 million tokens | $35 to $60 |
| Lens, before any reasoning, all prompts | as round 1 | about $9 |
| Lens, through the reasoning, 300 questions, both arms' answers | 600 answers of about 2,000 tokens; the lens's speed on long answers is the least certain figure here | $10 to $45 |

The pilot measures the real answer length and speed with thinking on, checks that the engine runs in this setting,
and uses questions that are in no reported comparison. The caps for the main runs are set after it.

## Before freezing

1. These rules are approved.
2. Independent review of these rules and `score.py`, by a reviewer who has not seen the round 1 answers.
3. The engine code for thinking mode, seeds and answer lengths, and the lens jobs, are written on
   `claude/bigevents-engine` and tested without a GPU.
4. The pilot is run under its own cap, and the main caps are set.
5. Hashes of every file are filled in and committed.

## Amendments

None.

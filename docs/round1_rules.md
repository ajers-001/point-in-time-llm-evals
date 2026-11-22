# Big-events test of the point-in-time engine: rules

**Binding.** Approved on November 15, 2026 and committed before any engine answer or lens reading of
these questions exists. The text above "## Amendments" does not change afterwards. This test is outside
`reports/jspace-v1/testplan/PREREG_ROUND1.md`; its code lives on the branch `claude/bigevents-engine`.

## Question in one line

When the engine is told a date between 2016 and 2022 and asked how likely an event is, do its answers follow what
actually happened more than the crowd forecast of that day can explain? If they do, it is using knowledge from
after its December 31, 2015 cutoff.

## What the memory screen already showed

`SCREEN_RULES.md`, scores in `screen/scores.json`. Plain Qwen3.5-27B, told it is November 2026:

| Period | Right | Crowd favourite would get | Passed |
|---|---|---|---|
| 2016-2019 | 87% of 253 | 68% | yes |
| 2020-2022 | 81% of 153 | 68% | yes |
| 2025 | 34% of 32 answered (271 asked) | 56% | no |
| 2026 | 7 answered of 313 | | no |

## Questions

From `data/markets_pull/bigevents_candidates.csv` (the file the screen used):

- **Test set:** every question that closed in 2016-2022 and passes the two filters below: **373 questions in 320
  stories** (412 before the filters).
- **Floor set:** questions that closed in 2025-2026, which plain Qwen does not know. One question per story (the
  one with the smallest SHA256 of its key), filtered, then up to 200 with the smallest hash: **198 questions**.
- **Removed before the run, by rule:** a question whose background text carries something written after the
  question date (for example, the Brexit question's background ends "Resolved 6/24/16: The majority of British
  voters elected to leave the EU"). Two filters, in `build_engine_questions.py`:
  1. a wording pattern, or a month/day/year date later than the question date: removed 30 test and 3 floor
     questions;
  2. a reader model (`deepseek/deepseek-v4-pro`) shown only the question, the background and the question date,
     asked whether the text states or reveals how the question was settled or reports anything that happened after
     that day (a deadline or a scheduled date does not count). Anything but a No removes the question: 9 more test
     questions. The reader never sees the outcome. Its answers are in `engine/reader_flags.json`.
  The screen result does not depend on these: on the 376 questions the pattern did not flag Qwen was 85% right, on
  the 30 flagged 80%.
- The questions are listed in `engine/questions.json` and the 1,142 prompts in `engine/prompts.json`.

## What is asked

Two versions of every question, the same text for both arms:

- **`open`:** the screen's question. The system date is November 14, 2026; "How was it settled? Yes, No or
  Unknown."
- **`dated`:** the system date is the question date (30 days before the question closed), with the upstream
  benchmark system prompt as in task `la1`. "Will this question resolve Yes? Answer with Yes or No, then the probability
  of Yes as a number from 0 to 100." (The first word gives the lens a single word to read; see the lens section.) The question, its resolution rules and its background are shown. The crowd forecast
  and the outcome are never shown.

Two arms, with the settings of `lookahead-v1-r1a`: plain P sampled by the engine (`F-open`) and the product
(`F-pair`: two helpers on every word, hybrid-v1 H-pair settings, loop guard). Thinking off, one sample per prompt,
at most 200 new tokens. Labels (outcomes and crowd forecasts) never enter the sandbox.

## Scores

- **Reading an answer.** `open`: the last of Yes, No, Unknown. `dated`: the first number from 0 to 100 in the
  answer; above 50 leans Yes, below 50 leans No. With no number, or exactly 50, the first word (Yes or No) gives
  the lean; with neither, "no lean".
- **Primary test: does the model's answer add to the crowd forecast?** (A forecast-encompassing regression, the
  standard way to ask whether one forecast holds information another lacks; Fair and Shiller, 1990.) Least-squares
  regression of the outcome (1 or 0) on: the crowd forecast; the source (Metaculus, Good Judgment Open, Kalshi; a
  source with fewer than 30 questions in the sample shares the baseline); and the model's probability, scaled to
  unit standard deviation so that two arms can be compared. Standard errors clustered by story. **The leak
  coefficient** is the one on the model's probability; the test is one-sided, coefficient above zero. For the
  `open` version and for the lens, where there is a lean and no probability, the regressor is +1 for Yes and -1 for
  No. The regression is linear, as in the source, because a logistic fit breaks down when a model knows nearly
  every outcome (found when testing the scorer on made-up answers).
- **Companion, in plain numbers: the leak score.** The screen's formula. For each question with a lean: outcome
  (1 or 0) minus the share that happened among all 1,081 questions with the same source and forecast band (20-40%,
  40-60%, 60-80%); plus sign if the answer leans Yes, minus sign if it leans No. Average within each story, then
  over stories. Reported beside the primary test with a 95% interval (stories resampled 10,000 times, fixed seed).
  It does not decide a verdict: it ignores how confident the model was, and inside a band a model that copies the
  crowd can score slightly above zero.
- **Brier score** of the `dated` probabilities, beside the crowd's and beside 0.25 (always answering 50). Reported,
  not tested.

In the verdicts below, "leak above zero, p < 0.01" means the leak coefficient of the primary test, and "25% of
plain P's leak" compares the upper end of the engine's 95% interval for that coefficient with plain P's
coefficient.

The primary result pools 2016-2022. The two periods are reported separately beside it.

The sources for each part of the method, and what has none, are in `METHOD_REFERENCES.md`.

## Verdicts, in this order

1. **Does plain P remember, under the engine's sampling?** `F-open` on `open`, test set: the screen's pass mark
   (80% right, p < 0.01, half answered, 20 stories). If not: **inconclusive**, stop.
2. **Is the method quiet where nothing is known?** `F-open` on `dated`, floor set: leak not above zero
   (p >= 0.01). If it is above zero, the test rewards something other than memory: **inconclusive**, stop.
3. **Does plain P leak when given the date?** `F-open` on `dated`, test set: leak above zero, p < 0.01. If
   not, the dated prompt alone hides the knowledge and the engine cannot be judged on it: **inconclusive for the
   engine**; reported as a finding about the prompt.
4. **The engine** (`F-pair` on `dated`, test set):
   - **Leaks:** leak above zero, p < 0.01.
   - **Clean:** not "leaks", and the upper end of its 95% interval is at most 25% of plain P's leak in
     verdict 3.
   - **No leak found, not shown clean:** anything else.
5. **Utility of the engine** (`F-pair` on `dated`, test and floor sets together). Passes when both hold:
   - at least 90% of answers carry a number;
   - its probabilities move with the crowd forecast: Spearman correlation above zero, one-sided p < 0.01, by
     story.
   The Brier scores are reported beside it and do not decide the verdict.

No threshold is changed after the run. If a run is cut short by its money cap or deadline, the answers that exist
are scored and the missing ones are counted; nothing is rerun to improve a result.

**How often each verdict comes out, measured on made-up engines before the run** (400 of each, 373 questions):

| Made-up engine | "leaks" | "clean" | "no leak found, not shown clean" |
|---|---|---|---|
| Truly clean; plain P leaks on 60% of questions | 2% | 57% | 41% |
| Truly clean; plain P leaks on 30% | 1% | 32% | 67% |
| Leaks on 10% of questions | 98% | 0% | 2% |
| Leaks on 5% of questions | 53% | 4% | 44% |

So the test is strong at finding a leak and weak at certifying "clean": with this many questions a truly clean
engine often gets "no leak found, not shown clean". We kept the 25% bar knowing this. The headline number
reported beside the verdict is the share of plain P's leak that the engine removed, with the 95% interval. It is
1 minus the engine's leak coefficient divided by plain P's leak coefficient; its interval uses the two ends of the
engine's 95% interval in place of the coefficient.

## Stated limits

- The questions were written in 2016-2022 and carry facts of their time ("President Trump"). The engine is not
  blind to the period; only the outcome is withheld.
- Nearly all test questions come from two forecasting sites (Metaculus, Good Judgment Open); some are niche.
- One sample per prompt.
- The question date is 30 days before the question closed, and some questions close early when the event happens.
  So the date depends slightly on the outcome (the pitfall named by Paleka et al., 2025).

## The lens (J-space), a second paid step on the same prompts

Fixed here so that no lens threshold can be chosen after the engine's answers are seen. Experiment
`bigevents-v1-lens`; it runs after the engine run is collected.

- **What it reads.** The Qwen3.5-27B lens of `lookahead-v1` (the stage 2b lens), on plain P, on each `open` and
  `dated` prompt, before any word is written: how strongly the inside of the model points to "Yes" against "No". The product is plain P with two helpers acting on its output, so before the first word the
  inside of the model is the same for both arms: the lens gives one reading per prompt, not one per arm.
- **Lens lean.** The reading rule step 1 used for bulk A, imported from that code and not tuned
  (`lens_to_scores.py`; it reproduces the step 1 bulk A lens figures exactly). In the window that starts at the end
  of the user turn, over layers 18-62 and the positions before the first written token (the last prompt position
  is left out, as in step 1), count the cells where "yes" is among the lens's top words and the cells where "no"
  is. Lead = "yes" cells minus "no" cells. Lean is Yes if the lead is above zero, No if below, none if zero.
- **Lens leak.** The primary test and the companion leak score, with the lens lean (+1 for Yes, -1 for No) in
  place of the written answer.
- **Verdicts.**
  - L1, the lens can see memory: lens leak on `open`, test set, above zero with p < 0.01. If not, the lens
    part is **inconclusive**.
  - L2, quiet on the floor: lens leak on `dated`, floor set, not above zero (p >= 0.01). If it is above
    zero, **inconclusive**.
  - L3, the knowledge is inside when the model is given the old date: lens leak on `dated`, test set, above
    zero with p < 0.01.
- **How it is read with the written verdicts.** L3 yes and the engine written-clean: the knowledge is present
  inside and the engine keeps it out of the answer. L3 yes and plain P written-clean too (verdict 3 fails): the
  dated prompt hides knowledge that is still inside. L3 no: the lens adds nothing on these questions.

## Code and files

- Engine run: experiment `bigevents-v1-engine` (task `la1` unchanged, with the prompts of `engine/prompts.json`).
  Lens run: experiment `bigevents-v1-lens` (the bulk A probe job of step 1 with these prompts).
- Scoring, offline: `collect_answers.py`, then `lens_to_scores.py`, then `engine_score.py`. The scorer was written
  and tested on made-up answers before any engine answer existed.
- Outcomes and crowd forecasts stay in `data/markets_pull/bigevents_candidates.csv` and never enter the sandbox;
  the build checks every shipped file for those columns.

| File | SHA256 |
|---|---|
| `data/markets_pull/bigevents_candidates.csv` | `f1375d8214ed172c5fa27b035d36fdbc58db6d436f12813c51550e8b678523fa` |
| `reports/bigevents-v1/engine/questions.json` | `19de8a5a1e4d30b19c27b4c307160d0f34c0e46810a6f27a780103708e97ed11` |
| `reports/bigevents-v1/engine/prompts.json` | `f5538f12eb6eba0479a38377e28197b38b7917c7c0b926e3301b5a505a3247c2` |
| `reports/bigevents-v1/engine/reader_flags.json` | `3ecf4604c5f209934e94cd776973a62343b16736e67a1b9e96cd364e9b6f248d` |
| `reports/bigevents-v1/build_engine_questions.py` | `59c52fe2b7c507a6d9c4278849e7a0a66a0b75b618c5de60dd3327b5318522ea` |
| `reports/bigevents-v1/memory_screen.py` | `c33d0ad61503b3d36af09af8240147085d87751280caf9929aaaf8928f2f8f9f` |
| `reports/bigevents-v1/engine_score.py` | `c3cbdfcbae483638a93323b4490e971e7d9d05ada7c76031bde1e9d4d5c92053` |
| `reports/bigevents-v1/collect_answers.py` | `59292dbb3151b5baba0b193dbe1ed4cf5c5e5a8b36a49524f3115559e4dfa466` |
| `reports/bigevents-v1/lens_to_scores.py` | `260ef733f36bc73e0f5581272fad1e785924817f0770aa67c57efc0c547425a1` |

## Money

Approved on November 15, 2026: the engine run with a cap of $20, then the lens run with a cap of $10.
Each is one session: deploy once, verify the host bundle hash, terminate with cleanup.

## Amendments

| Date (UTC) | What happened | What changed | What did not |
|---|---|---|---|
| 2026-11-15 07:02 | Session `20261115-be-engine`: both engine jobs stopped while loading the task, before any prompt was sent to the model ("Unknown filter 'take_all'"). No answer exists. The GPU was deleted; $3.99 spent. Cause: `la1.yaml` uses a filter that is registered by the authors' temporal utilities, and the loader of task `la1` (`pit_bench/tasks/pit_jspace.py`) did not load them; earlier jobs worked only because another task in the same job did. | `pit_bench/tasks/pit_jspace.py` now loads those utilities, as `pit_fakenews.py` and `pit_openbook.py` do (branch `claude/bigevents-engine` only). Checked without a GPU: the task alone failed the same way before the change and ran end to end with a stand-in model after it. The experiment is frozen again and rerun in a second session, with the remaining $16 of the $20 cap. | Questions, prompts, `la1.yaml`, sampling, arms, scorer, thresholds. |

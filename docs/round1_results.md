# Big-events test of the engine: results

**Where the rest is:** the engine code, the frozen bundles, the session files and the raw outputs (`engine/run/`,
`lens/run/`) are on the branch `claude/bigevents-engine`, because the step 1 freeze keeps that code off this branch.
This branch holds the rules, this write-up, the answers, the scores and the lens readings.

Run on November 15, 2026 under `ENGINE_RULES.md` (binding before any answer existed). Scores:
`engine/score.json`. Answers: `engine/answers.json`. Raw outputs: `engine/run/` and `lens/run/`.

## Verdict under the frozen rules

**Inconclusive.** The first check failed: plain Qwen3.5-27B, run the way the engine runs it (thinking off, sampled,
200 tokens), was **69% right** on how the 2016-2022 questions ended. The pass mark is 80%. The rules stop there:
no "leaks" or "clean" verdict is given for the engine.

The same model on the same 373 questions was 85% right in the memory screen, where it could think before
answering. So the model knows these outcomes, but recalls fewer of them without thinking.

## What the numbers show (reported, not a verdict)

Test set: 373 questions that closed in 2016-2022, 320 stories. Floor: 198 questions from 2025-2026.

| Check | Result |
|---|---|
| 1. Plain Qwen remembers (told it is 2026) | 69% right of 368 answered; beyond the crowd p < 0.001; **fails the 80% mark** |
| 2. Quiet on the floor (plain Qwen, dated) | leak coefficient 0.018, p = 0.30: quiet, as required |
| 3. Plain Qwen leaks when told the old date | leak coefficient 0.081 (95% interval 0.034 to 0.127), p = 0.0003: it leaks |
| 4. Engine, told the old date | leak coefficient 0.002 (95% interval -0.044 to 0.048), p = 0.47: no leak found |
| Share of plain Qwen's leak the engine removed | 98%, 95% interval 41% to more than 100% |
| 5. Utility of the engine | passes the test set in advance: every answer has a number; its probabilities move with the crowd's (Spearman 0.15, p = 0.0004) |

Had check 1 passed, the engine's verdict would have been "no leak found, not shown clean": the top of its interval
(0.048) is above a quarter of plain Qwen's leak (0.020).

By period, plain Qwen's dated leak is in 2016-2019 (coefficient 0.110, p = 0.0002) and not in 2020-2022
(0.045, p = 0.12).

## Two things to know before quoting these

**1. The engine's forecasts are poor.** Brier scores on the test set (lower is better; always answering 50 scores
0.250):

| | Test set | Floor |
|---|---|---|
| Crowd forecast | 0.198 | 0.225 |
| Plain Qwen, dated | 0.235 | 0.401 |
| Engine, dated | 0.272 | 0.429 |

The engine gave an average probability of 22% (the events happened 39% of the time) and put 50% or more on only
3% of questions. It passes the utility test we fixed in advance, but it forecasts worse than always saying 50.
Part of "no leak found" may be that its probabilities carry little information of any kind.

**2. Outside the rules (exploratory): told it is 2026, the engine still recalls outcomes.** The rules judge the
engine only on the dated version. On the open version ("it is November 2026; how was it settled?") the engine:

- answered Yes or No to 269 of the 373 test questions and was right on 75.5% (the crowd's favourite was right on
  72.9% of the same questions; always saying No, 53.9%);
- its answers follow the outcomes beyond the crowd forecast (coefficient 0.193, p < 0.001; leak score 0.19,
  95% interval 0.14 to 0.24);
- on the 117 upsets, where the crowd favoured the wrong side, it answered 73 and was right on 50;
- on the floor it said "Unknown" to 191 of 198.

So knowledge from after 2015 comes through the engine when the prompt says the date is 2026. This was not a
pre-registered test and needs its own rules before it is claimed.

## The lens (J-space)

Run after the engine answers were collected, under the lens rules fixed in advance. The lens read all 1,142
prompts on plain Qwen (none failed). Scores: `engine/score.json` (`lens`), readings: `lens/lens.json`.

**Lens verdict: the knowledge is inside under the old date.**

| Lens check | Result |
|---|---|
| L1. The lens sees memory (told it is 2026) | yes: coefficient 0.038 (95% interval 0.020 to 0.057), p = 0.00003 |
| L2. Quiet on the floor (old date) | yes: coefficient 0.019, p = 0.30 |
| L3. Knowledge inside when told the old date | yes: coefficient 0.063 (95% interval 0.032 to 0.094), p = 0.00003 |

So before plain Qwen writes a word under the point-in-time prompt, its inside already leans toward what actually
happened in 2016-2022, and it does not on 2025-2026 questions. Read with the written results: the knowledge is
present inside, plain Qwen's written answers show it, and the engine's written probabilities do not.

Caution: the lens leaned Yes on 900 of the 1,142 prompts, and the plain-number companion (the leak score) is
positive but its interval touches zero (0.045, 95% interval -0.002 to 0.092). The verdict rests on the regression,
as the rules say.

**Exploratory, from the same run:** the lens job also saved plain Qwen's probability for its first word (greedy,
no sampling). Using P(Yes) against P(No) as the answer:

| Plain Qwen, first-word probability | Coefficient | p |
|---|---|---|
| Told it is 2026, test set | 0.218 | below 1e-20 |
| Told the old date, test set | 0.140 | 2e-8 |
| Told it is 2026, floor | -0.021 | 0.72 |
| Told the old date, floor | 0.003 | 0.46 |

Without the noise of sampling, plain Qwen's leak under the point-in-time prompt is clear and about two thirds the
size of its open recall. Its first word was right 69-70% of the time either way, so the 80% pass mark of check 1
was set too high for a model that answers without thinking; the mark came from the screen, where it could think.

## Exploratory: only the questions plain Qwen got right

Asked after the results were seen; not part of the rules. `exploratory_known_subset.py`.

Keeping only the 255 test questions plain Qwen answered correctly when told it is 2026:

| | Leak coefficient | 95% interval | p |
|---|---|---|---|
| Plain Qwen, told the old date | 0.161 | 0.112 to 0.210 | 9e-11 |
| Engine, told the old date | 0.025 | -0.014 to 0.064 | 0.10 |

On this subset the engine removed about 84% of plain Qwen's leak, and at least 60% with 95% confidence.

**This way of picking questions builds in a signal of its own.** Applied to the floor, where nothing can be
known, it keeps 97 lucky guesses, and there the coefficients are 0.040 (plain Qwen), 0.055 (engine) and 0.130
(lens, p = 0.004). So:

- plain Qwen's 0.161 is four times the built-in signal: a real leak;
- the engine's 0.025 is below the built-in signal: nothing there;
- the lens's 0.116 on this subset is no larger than its built-in signal, so the lens must be judged on the full
  set (0.063 on the test set against 0.018 on the floor), not on this subset.

**Does the lens show something the written answers do not?** On the full test set:

- The size of the lens lead (not just its sign) follows the outcomes strongly (coefficient 0.128, p = 2e-8) and
  not at all on the floor (0.000).
- It holds more than one sampled written answer of plain Qwen does: with both in the regression the lens keeps
  0.115 (p = 4e-5) and the written answer drops to 0.023 (p = 0.21).
- It holds nothing beyond plain Qwen's first-word probability: the two move together (rank correlation 0.88),
  and with both in the regression the lens drops to 0.030 (p = 0.25). The lens is reading what the model is
  about to say, not a separate hidden store.
- Against the engine: inside, the lens lead averages 12.3 on events that happened and 5.9 on those that did not;
  the engine's written probability averages 24% and 21%. The knowledge is inside and does not reach the
  engine's answer.

## Money and sessions

| Session | What happened | Spend |
|---|---|---|
| `20261115-be-engine` | Both jobs failed while loading the task, before any prompt was sent (see Amendments in the rules) | $3.99 |
| `20261115-be-engine2` | Both jobs succeeded: 1,142 answers per arm, none missing | $3.35 |
| **Engine total** | | **$7.34 of the $20 cap** |
| `20261115-be-lens` | The lens job succeeded: 1,142 of 1,142 prompts read, 0.86 s each | $3.39 of the $10 cap |

Each session was deployed once, the host bundle hash was checked (local build = controller record = stack
template), and each was terminated with cleanup; the GPUs are gone.

## Limits

- One sample per prompt.
- 9 of plain Qwen's 571 dated answers start with a sentence naming "December 31, 2015" before the number, so the
  frozen reading rule (first number from 0 to 100) reads 31 for them. The engine's answers all start with Yes or No
  and a number.
- Nearly all test questions come from Metaculus and Good Judgment Open.
- The questions carry facts of their time; only the outcome is withheld.

## Also found

Task `la1` fails when it is the only task of a job (a filter it needs was never registered). Step 2a of
`PREREG_ROUND1.md`, as frozen, has the same defect and would have failed the same way. The fix is on this branch
only; the frozen branch is untouched.

# Point-in-Time Language Models: Measuring Look-Ahead Bias

By [Ajer Sher](https://www.linkedin.com/in/ajer-sher-74397b240/)

A language model trained on text up to 2025 already knows how 2016 turned out, so it cannot be trusted to test a 2016
trading idea or to study what people could have known in 2016. A point-in-time model is meant to answer as if its
knowledge stopped on a fixed date. In this project, I build one on top of the open model Qwen3.5-27B, with a cutoff of
December 31, 2015. Most of the work goes into the harder question: building tests that show whether such a model
really works.

This repository is a public excerpt of the project. It holds the write-ups, the code for the forecasting benchmark and
the cloud system that runs the tests. The full research repository is private because it contains licensed data and
the full question lists. It is available on request.

## How this work came together

This project started in finance. I had built my own AI trading strategy and wanted to backtest it, which means testing
how it would have done in the past. I soon ran into a problem: the AI model already knew what happened in the years I
wanted to test, so the backtest could not be trusted. While looking for a solution, I found the frontier-to-pit paper
by Merchant and Levy ([fin-ai-lab/frontier-to-pit](https://github.com/fin-ai-lab/frontier-to-pit)). Their method
guides a large model's answers with smaller helper models that were trained only on older text. I decided to recreate
their method and see whether I could improve it.

After rebuilding the method, I spoke with Humzah Merchant, one of the paper's authors. On his advice, I began building
new benchmarks that go beyond the paper's original tests on mergers and drug trials, so that a point-in-time model can
be judged on more kinds of questions. Most of my time now goes into these benchmarks and into using Anthropic's
Jacobian lens, a tool that reads what a model is "thinking" before it writes its answer. The lens shows whether the
model holds knowledge of later events, even when its written answer hides it. When the lens shows that the model knows
the future, and my engine keeps that knowledge out of the answer, I count that as a win.

I have worked with Claude throughout this project, going back and forth to try ideas and improve them. I make the
decisions and sign off on every step, and the ideas and information are my own. Claude helps me run the tests and keep
the work organized.

## Main findings so far

These come from the first round of the forecasting benchmark: 373 real yes/no questions about events from 2016 to 2022,
each asked with its original date, plus 198 questions from 2025 and 2026 that the model cannot know.

1. **A plain model uses later knowledge even when it is told the date.** The plain model's answers matched what really
   happened more often than the crowd's forecast at the time could explain. The chance of this being luck is about 3
   in 10,000. Telling a model that it is 2017 does not stop it from using what it knows about 2017. Earlier studies by
   Sarkar and Vafa (2024) and by Lopez-Lira, Tang and Zhu (2025) found the same thing.
2. **The knowledge can be seen inside the model before it answers.** Under the old date, the Jacobian lens showed the
   model's internal state already leaning toward the real outcome before its first word (about a 3 in 100,000 chance of
   luck). On the 2025 and 2026 questions, the lens showed no such lean.
3. **The knowledge is held back, not removed.** Given the old date, my point-in-time model removed about 98% of the
   plain model's leak by my best estimate. However, when told that the date was 2026, it still remembered how most of
   these events ended. It got 75.5% of the 269 questions it answered right, including 50 of the 73 surprises that the
   crowd got wrong. This matters beyond finance: making a model behave as if it has forgotten something is not the same
   as removing the knowledge.
4. **Usefulness has to be measured next to leakage.** My point-in-time model answered "No, about 20%" to most
   questions. Its forecast error (Brier score) was 0.272, worse than always answering 50% (0.250), while the plain
   model scored 0.235 and the crowd 0.198. A model can look free of leaks just by giving the same answer to everything.

The official result of this round was "inconclusive." Before the run, I required the plain model to get at least 80% of
the outcomes right when asked as of today. With its step-by-step reasoning turned off it got 69%, so the rules did not
allow a final verdict. I report this result as it is, and the next round fixes the requirement.
[Round 1 rules](docs/round1_rules.md) · [Round 1 results](docs/round1_results.md)

## What I tried along the way

### Reproducing the paper and building on it

The paper's method combines two parts. The first uses two small helper models, one trained on text up to 2015 and one
trained on text up to 2024, to push the large model away from what only the later helper knows. The second, called
steering, nudges the model's internal state in a chosen direction.

I first reproduced the paper's results with the authors' own code: leaks fell from 23 to 7 of 129 merger deals, and
from 11 to 0 of 49 drug trials. I then tested each part of the method and tried many changes of my own.

- **Steering.** In my tests, steering added little on top of the helper models, so I focused on the helpers.
- **Strength.** I ran the helpers at several strengths. Stronger settings removed more leaks but also produced more
  repeated or broken text, and no setting gave both. I built fixes that stop most of the broken text, so that leak
  counts reflect what the model knows rather than how well it writes.
- **What the helpers know.** The paper's small 2024 helper knew only 2 of the 129 merger outcomes, while the large model
  knew 33. The helpers can only remove knowledge that they can tell apart, so their training data limits what they can
  catch at this size. A genuine 2015-era model, by contrast, picked out leaked names very well (0.93 on a 0 to 1 scale
  where 0.5 is chance). From these measurements, I built a simulator estimating that a 2015 helper would need roughly
  14 billion parameters to do the job well.
- **Giving the helper a library.** Instead of asking the small helper to remember old facts, I gave both models dated
  news articles from 2007 to 2015. This cut the share of correct old answers that the helper wrongly flagged from 47.5%
  to 17.2%. When a retrieved article contained the answer, the wrong flags fell to 10.7%, against 58.3% without one,
  and retrieving 10 articles instead of 3 raised coverage from 53% to 70%.
- **One helper instead of two.** I tried using the large model itself as the "later" side, with a single 2015 helper
  acting only on names and numbers, plus the news library. On direct questions about the 129 deals, leaks fell from
  25.6% for the plain model to 19.4% with the paper's method and my fixes, 9.3% with one helper, and 4.7% with the news
  library added. Reasoning stayed close to the plain model: 76.3% against 78.8% on the GPQA-Diamond science test, and
  86.7% against 88.0% on the IFEval instruction test. Without the articles, the same filter lost 26% of old knowledge;
  with them, it lost only 2% to 7%.
- **Ordinary words.** The names-and-numbers design could not catch look-ahead carried by ordinary words, such as a
  2015 answer that mentions a "lockdown". Humzah also pointed out that using the large model as a helper can remove
  some of its intelligence along with its knowledge.
- **Two helpers on every word.** Across many public model sizes, two helpers acting on every word did better than one
  helper and kept improving as the helpers grew, while one helper did not. This is the design I use now. It keeps the
  model's reasoning intact (80.8% on GPQA-Diamond against 78.8% for the plain model), and the main path forward is a
  stronger, matched pair of helpers.
- **A planted-news test.** I gave the model short news stories about real deals and made-up ones, with misleading
  details planted in them. The plain model leaked the real outcome in 28% of answers about real deals; with two helpers,
  it leaked it in 14%.
- **Ideas that did not work.** Using the model's own notes about later events as the "later" side did not reduce leaks
  and cost 11% to 16% of old knowledge. A simple guard against repeated text did not reduce repetition.

### Making the Jacobian lens reliable

- **Fitting.** I fitted the lens to Qwen3.5-27B on 100 dated news texts from 2007 to 2015, in four parts on rented GPUs.
  Two halves of the lens agreed closely, and a lens built from only 25 texts already matched the full lens, so more
  fitting would not have helped.
- **Proving that it works on clear cases.** When the model was told the date was 2025 instead of 2015, the lens showed
  later topics more often in 9 of 10 topics. Asked as of 2025, the real outcome stood out inside the model in 25 of 32
  events. The lens also saw every first written word inside the model before the model wrote it.
- **Fixing a false signal.** The first planned run failed because one word, "tiktok", appeared inside the model in
  almost every answer, whatever the question. I found 12 such background words on a separate set of answers and
  filtered them out, after which the lens could be used.
- **Predicting leaks before they happen.** Reading the model before it answered, the lens could tell which answers
  would go on to leak with a score of about 0.75 (on a 0 to 1 scale where 0.5 is chance).
- **Seeing hidden look-ahead.** The lens showed "covid" and "lockdown" inside the model while it answered questions
  about office life dated 2015, and "Tesla" inside the model while it wrote about Volkswagen.
- **Its limit.** The lens reads single word pieces, so it can miss names made of several words. In [separate
  work](https://x.com/Ajer28251/status/2124252821394509999), I extended the lens from single word pieces to full words.

### From easy questions to harder ones

These early tests used questions that were fairly easy to guess, such as forecasts with a few given options. They
proved that the lens works, but they were too easy to reveal subtle look-ahead. I moved to harder benchmarks.

The first was company earnings: 576 questions on whether a company beat the analysts' forecast. It ended inconclusive,
because Qwen3.5-27B turned out not to remember earnings results at all: it recalled the exact reported figure only 3%
of the time. Other models did remember them. DeepSeek V4 Pro, Claude Opus and Kimi called 85% to 95% of the results
right, and DeepSeek V4 Pro recalled the exact figure 66% of the time. Telling DeepSeek V4 Pro that it was 2016 and not
to look ahead only lowered its accuracy from 83% to 60%. This taught me to check first that a model knows the answers,
which led to the forecasting benchmark below.

## The forecasting benchmark

- **The expectation at the time.** I use prediction markets and forecasting sites (Polymarket, Kalshi, Metaculus and
  Good Judgment Open) because they record what people expected before the outcome, as a price or a forecast. This
  record was made at the time, not rebuilt with hindsight, so it shows exactly what was knowable on a given day. If a
  model does better than that expectation, it must be using knowledge from later on. The set has 1,081 yes/no questions
  from 2015 to 2026, each with the crowd's forecast from 30 days before the outcome. Related questions are grouped into
  618 stories, so that one news event cannot decide a result on its own.
- **Knowledge the model actually has.** Before any test, I measured where each model's knowledge ends. Qwen3.5-27B knew
  87% of the 2016 to 2019 outcomes and 81% of the 2020 to 2022 outcomes, and its knowledge fades during 2024. DeepSeek
  V4 Pro knew 92%, 89% and 94% (2024).
- **A standard test.** The main test is a long-standing statistical check of whether one forecast holds information
  that another does not (Fair and Shiller, 1990). It asks whether the model's answers add anything beyond the crowd's
  forecast of the day.
- **A check on what cannot be known.** Every test includes questions from 2025 and 2026, which the model cannot know.
  On these questions the test must show no effect.
- **Clean questions.** Some questions include background text that gives away the outcome, such as a note added after
  the vote. I removed 42 such questions, using both a fixed text rule and a separate model that never sees the outcome.
- **Realistic questions.** I aim for questions that someone would actually use in practice, such as forecasts of real
  events and economic numbers. For now the benchmark stays at a high level, and I plan to make it more detailed.
- **Rules before results.** For every paid test, I write down the rules, the pass marks and the scoring code before
  any result exists, and I test the scoring on made-up answers first. The real outcomes never reach the model during a
  test.

[First-check rules](docs/memory_screen_rules.md) · [Sources for each part of the method](docs/method_references.md)

## The next round (designed, not yet run)

- **586 test questions,** 213 of which have never been shown to any model, plus 369 long shots that the crowd put
  under 10% or over 90%, reported separately.
- **Each question asked in reverse** ("Will this not happen?"), so that a model which always says "No" cannot pass.
- **Five answers per question.** The model's sampler uses a fixed seed for every request in a run, so asking the same
  question five times in one run would return five identical answers. Each answer therefore comes from a separate run.
- **New planned verdicts:** whether the model remembers outcomes when told the date is 2026, and whether it forecasts
  better than always answering 50%.

Before spending any money, I ran made-up models through the scoring to check that it gives the right verdicts:

| Made-up model | How often round 1 scoring called it clean | How often round 2 scoring calls it clean |
|---|---|---|
| Truly clean, while the plain model leaks on 6% of questions | 12% | 82% |
| Truly clean, while the plain model leaks on 10% of questions | 26% | 97% |

In round 2, a made-up model that leaks on 3% of questions is caught 92% of the time, and one that answers "No, 20%" to
everything is labelled as giving no information. This check also found a flaw in the round 1 scoring: collecting more
answers per question would have made it harder, not easier, to call a model clean. Round 2 measures the leak in plain
probability units to fix this. [Round 2 rules (draft)](docs/round2_rules_draft.md)

## The test pipeline

The folder [`eval_pipeline/`](eval_pipeline/) runs the tests on rented GPUs without anyone watching:

- A controller on AWS rents GPU machines from Runpod and keeps a record of every step in a database.
- Each test runs in a sealed environment with no internet access and a fresh copy of the model. Before it runs, the
  code on the GPU is checked against the saved version.
- A budget check refuses to start a GPU unless the whole run fits under the approved dollar limit, and a separate
  watchdog shuts down every GPU at the deadline.
- Results are uploaded so that they cannot be overwritten, and they are checked again when they are collected.

The whole project has used about $450 of rented GPU time so far.

## What is in this repository

| Folder | Contents |
|---|---|
| `docs/` | The rules, results and method sources of the forecasting benchmark |
| `bigevents/v1/` | Round 1: pulling the market data, building the questions, the knowledge check and the scoring |
| `bigevents/v2/` | Round 2: building the questions, the scoring and the made-up-model check |
| `eval_pipeline/` | The cloud controller, the GPU machine agent, the budget check and the watchdog |

The code is an excerpt and needs the data and model from the private repository to run.

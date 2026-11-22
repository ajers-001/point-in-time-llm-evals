# Point-in-Time Language Models: Measuring Look-Ahead Bias

By [Ajer Sher](https://www.linkedin.com/in/ajer-sher-74397b240/)

A language model trained on text up to 2025 already knows how 2016 turned out, so it cannot be trusted to test a 2016
trading idea. A point-in-time model is meant to answer as if its knowledge stopped on a fixed date. In this project, I
build one on top of the open model Qwen3.5-27B, with a cutoff of December 31, 2015, and I build the tests that show
whether it really works.

This repository is a public excerpt. The full research repository is private because it contains licensed data and the
full question lists. It is available on request.

## How this work came together

This project started in finance. I had built my own AI trading strategy and wanted to backtest it, which means testing
how it would have done in the past. The AI model already knew what happened in the years I wanted to test, so the
backtest could not be trusted. While looking for a solution, I found the frontier-to-pit paper by Merchant and Levy
([fin-ai-lab/frontier-to-pit](https://github.com/fin-ai-lab/frontier-to-pit)). Their method guides a large model's
answers with smaller helper models that were trained only on older text. I decided to recreate their method and see
whether I could improve it.

After rebuilding the method, I spoke with Humzah Merchant, one of the paper's authors. On his advice, I began building
new benchmarks that go beyond the paper's original tests. Most of my time now goes into these benchmarks and into using
Anthropic's Jacobian lens, a tool that reads what a model is "thinking" before it writes its answer. The lens shows
whether the model holds knowledge of later events, even when its written answer hides it. When the lens shows that the
model knows the future, and my engine keeps that knowledge out of the answer, I count that as a win.

I have worked with Claude throughout this project, going back and forth to try ideas and improve them. I make the
decisions and sign off on every step, and the ideas and information are my own. Claude helps me run the tests and keep
the work organized.

## Main findings so far

1. **A plain model uses later knowledge even when it is told the date.** I asked the plain model 373 real forecasting
   questions about events from 2016 to 2022, each with its original date. Its answers matched what really happened more
   often than the crowd's forecast at the time could explain, with only about a 3 in 10,000 chance of this being luck.
   Earlier studies by Sarkar and Vafa (2024) and by Lopez-Lira, Tang and Zhu (2025) found the same thing.
2. **The knowledge can be seen inside the model.** Before the model wrote its first word, the Jacobian lens showed its
   internal state leaning toward the real outcome, even under the old date. On questions from 2025 and 2026, which the
   model cannot know, the lens showed no such lean.
3. **The knowledge is held back, not removed.** Given the old date, my point-in-time model removed about 98% of the
   plain model's leak by my best estimate. However, when told that the date was 2026, it still remembered how most of
   these events ended: it got 75.5% of the 269 questions it answered right, including 50 of the 73 surprises that the
   crowd got wrong.
4. **The model's forecasts became weak.** My point-in-time model answered "No, about 20%" to most questions, which is
   less accurate than answering 50% every time. A model can look free of leaks just by giving the same answer to
   everything, so usefulness has to be measured next to leakage.

The official result of this first round was "inconclusive." Before the run, I required the plain model to get at least
80% of the outcomes right when asked as of today, and it got 69% with its step-by-step reasoning turned off. I report
this result as it is. [Round 1 rules](docs/round1_rules.md) · [Round 1 results](docs/round1_results.md)

## What I tried along the way

### Building on the paper's method

The paper's method combines small helper models (one trained on text up to 2015 and one trained on later text) with
steering, which nudges the model's internal state in a chosen direction. I first reproduced the paper's main result on
its merger test: leaks fell from 23 to 7 of 129 deals. I then tested each part and tried many changes of my own.

- **Steering.** In my tests, steering added little on top of the helper models, so I focused on the helpers.
- **Strength.** Stronger helper settings removed more leaks but also produced more repeated or broken text, so I built
  fixes that stop most of the broken text.
- **What the helpers know.** The paper's small 2024 helper knew only 2 of the 129 merger outcomes, while the large model
  knew 33. The helpers can only remove knowledge that they can tell apart, so their training data limits what they can
  catch at this size.
- **One helper instead of two.** I tried using the large model itself as the "later" side, with a single 2015 helper
  acting only on names and numbers, plus dated news articles from before 2016. On direct questions about the 129 deals,
  leaks fell from 25.6% for the plain model to 4.7%, while reasoning stayed close to the plain model (76.3% against
  78.8% on the GPQA-Diamond science test). However, this design could not catch look-ahead carried by ordinary words
  such as "lockdown". Humzah also pointed out that using the large model as a helper can remove some of its
  intelligence along with its knowledge.
- **Two helpers on every word.** Across many model sizes, two helpers on every word did better than one helper and
  improved as the helpers grew larger. This is the design I use now, and the main path forward is a stronger matched
  pair of helpers.
- **Ideas that did not work.** Using the model's own notes about later events as the "later" side, and a simple guard
  against repeated text, did not help.

### Making the Jacobian lens reliable

- **Fitting.** I fitted the lens to Qwen3.5-27B on 100 dated news texts from 2007 to 2015. A lens built from only 25 of
  those texts already matched the full lens, so more fitting would not have helped.
- **Proving that it works on clear cases.** When the model was told the date was 2025 instead of 2015, the lens showed
  later topics more often in 9 of 10 topics. Asked as of 2025, the real outcome stood out inside the model in 25 of 32
  events.
- **Fixing a false signal.** The first planned run failed because one word, "tiktok", appeared inside the model in
  almost every answer. I found 12 such background words on a separate set of answers and filtered them out.
- **Seeing hidden look-ahead.** The lens showed "covid" and "lockdown" inside the model while it answered questions
  about office life dated 2015, and "Tesla" inside while it wrote about Volkswagen.

These early tests used questions that were fairly easy to guess. They proved that the lens works, but they were too
easy to reveal subtle look-ahead, so I moved to harder benchmarks: company earnings and real forecasting questions.

## What my benchmarks focus on

- **The expectation at the time.** I use prediction markets and forecasting sites (Polymarket, Kalshi, Metaculus and
  Good Judgment Open) because they record what people expected before the outcome, as a price or a forecast. This
  record was made at the time, not rebuilt with hindsight, so it shows exactly what was knowable on a given day. If a
  model does better than that expectation, it must be using knowledge from later on. My current set has 1,081 such
  questions from 2015 to 2026.
- **Knowledge the model actually has.** Before testing for look-ahead bias, I check that the plain model knows the real
  outcome, because models often do not know this level of detail. For example, Qwen3.5-27B did not remember company
  earnings results, while DeepSeek V4 Pro, Kimi and Claude Opus remembered 85% to 95% of them. On the forecasting
  questions, Qwen3.5-27B knew 81% to 87% of the 2016 to 2022 outcomes, and its knowledge fades during 2024.
- **A check on what cannot be known.** Every test includes questions from 2025 and 2026, which the model cannot know.
  On these questions the test must show no effect.
- **Realistic questions.** I aim for questions that someone would actually use in practice, such as forecasts of real
  events and economic numbers. For now the benchmarks stay at a high level, and I plan to make them more detailed.
- **Rules before results.** For every paid test, I write down the rules, the pass marks and the scoring code before any
  result exists. The real outcomes never reach the model during a test.

## The next round (designed, not yet run)

The next round uses 586 test questions, 213 of which have never been shown to any model. Each question is also asked in
reverse ("Will this not happen?"), so that a model which always says "No" cannot pass, and each question gets five
answers instead of one. Remembering outcomes when told the date is 2026 is now a planned part of the test, and the
model must also forecast better than always answering 50%. Before spending any money, I ran made-up models through the
scoring: a truly clean model is now recognized as clean 82% to 97% of the time, against 12% to 26% under the round 1
scoring. [Round 2 rules (draft)](docs/round2_rules_draft.md)

## The test pipeline

The folder [`eval_pipeline/`](eval_pipeline/) runs the tests on rented GPUs without anyone watching. A controller on AWS
rents GPU machines from Runpod, runs each test in a sealed environment with no internet access, and checks that the
code on the GPU matches the saved version. A budget check refuses to start unless the run fits under the approved
dollar limit, and a separate watchdog shuts down every GPU at the deadline. The whole project has used about $450 of
rented GPU time so far.

## What is in this repository

| Folder | Contents |
|---|---|
| `docs/` | The rules, results and method sources of the forecasting tests |
| `bigevents/v1/`, `bigevents/v2/` | The code that pulls the data, builds the questions and scores the answers |
| `eval_pipeline/` | The cloud controller, the GPU machine agent, the budget check and the watchdog |

The code is an excerpt and needs the data and model from the private repository to run. The sources behind each part of
the method are listed in [docs/method_references.md](docs/method_references.md).

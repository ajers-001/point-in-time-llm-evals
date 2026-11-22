# Point-in-Time Language Models: Measuring Look-Ahead Bias

By [Ajer Sher](https://www.linkedin.com/in/ajer-sher-74397b240/)

## The problem

Large language models are trained on text up to a recent date. Because of this, a model trained on text up to 2025
already knows how 2016 turned out. That makes it unfit for testing a 2016 trading idea, or for studying what people
could have known in 2016.

A point-in-time model is meant to answer as if its knowledge stopped on a fixed date. In this project, I build one on
top of the open model Qwen3.5-27B, with a cutoff of December 31, 2015. Most of the work goes into building tests that
show whether such a model really works.

The project builds on the frontier-to-pit paper by Merchant and Levy
([fin-ai-lab/frontier-to-pit](https://github.com/fin-ai-lab/frontier-to-pit)). Their method guides a large model's
answers with smaller helper models that were trained only on older text.

This repository is a public excerpt of the project. It holds the write-ups, the evaluation code and the cloud setup
that runs the tests. The full research repository is private because it contains licensed data, the full question
lists and the model-serving code. It is available on request. File paths in the write-ups refer to that private
repository.

## Main findings so far

### 1. Reproducing the paper and building on it

I rebuilt the paper's method on rented GPUs and reproduced its main result on the authors' merger test: leaks fell
from 23 to 7 of 129 deals. When I asked the model about each deal directly, it still knew many of the outcomes. This
showed me where I could build on the method.

On direct questions about the same 129 deals, the share of answers that revealed the real outcome fell step by step:

- 25.6% for the plain model;
- 19.4% for the paper's method with small fixes to the output;
- 9.3% with my first changes;
- 4.7% after also giving the model dated news articles from before 2016.

Reasoning stayed close to the plain model. On the GPQA-Diamond science test, the model scored 76.3% against 78.8% for
the plain model. On the IFEval instruction test, it scored 86.7% against 88.0%.

### 2. A plain model uses later knowledge even when it is told the date

I asked the plain model 373 real forecasting questions about events from 2016 to 2022. Each question was asked with
its original date. The model's answers matched what really happened more often than the crowd's forecast at the time
could explain. The chance of this happening by luck is about 3 in 10,000. In other words, telling a model that it is
2017 does not stop it from using what it knows about 2017. Earlier studies by Sarkar and Vafa (2024) and by
Lopez-Lira, Tang and Zhu (2025) found the same thing.

### 3. The knowledge can be seen inside the model before it answers

I used Anthropic's Jacobian lens, a tool that reads what a model is "thinking" at each step. Before the model wrote its
first word, its internal state already leaned toward the real outcome, even under the old date. On questions from
2025 and 2026, which the model cannot know, the lens showed no such lean.

### 4. The knowledge is held back, not removed

When my point-in-time model was given the old date, it showed no sign of using later knowledge. By my best estimate, it
removed about 98% of the plain model's leak. However, when the same model was told that the date was 2026, it still
remembered how most of these events ended. It answered 269 questions and got 75.5% of them right. This included 50 of
the 73 surprises that the crowd got wrong. The model holds the knowledge back when it is given an old date, but the
knowledge is still there. I found this after the test had been designed, so the next round tests it as a planned
question.

### 5. The model's forecasts became weak

My point-in-time model answered "No, about 20%" to most questions. Its forecasts were less accurate than simply
answering 50% every time. This shows why usefulness has to be measured next to leakage: a model can look leak-free just
by giving the same answer to everything.

The official result of this first round was "inconclusive." Before the run, I required the plain model to get at least
80% of the outcomes right when asked as of today. It got 69% right with its step-by-step reasoning turned off, so the
test could not give a final verdict. I report this result as it is.

## The forecasting question set

- The set has 1,081 yes/no questions from Polymarket, Kalshi, Metaculus and Good Judgment Open, covering 2015 to 2026.
  Each question comes with the crowd's forecast from 30 days before the outcome, as it was recorded at the time.
  Related questions are grouped together, so that one news event cannot decide a result on its own.
- A first check measured where each model's knowledge ends. Qwen3.5-27B got 87% of the 2016 to 2019 outcomes right and
  81% of the 2020 to 2022 outcomes right. Its knowledge fades during 2024. DeepSeek V4 Pro got 92%, 89% and 94% (2024)
  right.
- The main test is a standard statistical check of whether one forecast holds information that another forecast does
  not (Fair and Shiller, 1990). It asks whether the model's answers add anything beyond the crowd's forecast of the
  day. Questions from 2025 and 2026, which the model cannot know, must show no effect.
- Some questions include background text that gives away the outcome. These are removed before the test, both by a
  fixed text rule and by a separate model that never sees the outcome.

Write-ups: [round 1 rules](docs/round1_rules.md) · [round 1 results](docs/round1_results.md) ·
[first-check rules](docs/memory_screen_rules.md) · [sources for each part of the method](docs/method_references.md)

## What I tried along the way

### Building on the paper's method

The paper's method has two parts. The first part uses small helper models, one trained on text up to 2015 and one
trained on later text, to guide the large model away from later knowledge. The second part, called steering, nudges
the model's internal state in a chosen direction. I tested each part and tried many changes of my own.

- **Steering.** In my tests, steering added little on top of the helper models. I therefore focused my effort on the
  helper models.
- **Strength.** I tried the helper models at several strengths. Stronger settings removed more leaks, but they also
  produced more repeated or broken text. I built fixes that stop most of the broken text, so that the leak counts
  reflect what the model knows rather than how well it writes.
- **What the helpers know.** I found that the paper's small 2024 helper knew only 2 of the 129 merger outcomes, while
  the large model knew 33. The helpers can only remove knowledge that they can tell apart, so their training data
  limits how much they can catch at this size.
- **One helper instead of two.** I tried using the large model itself as the "later" side, with a single 2015 helper,
  and acting only on names and numbers. Together with dated news articles from before 2016, this cut leaks on direct
  questions from 33 to 6. However, it could not catch look-ahead carried by ordinary words such as "lockdown" or
  "remote work". Humzah also pointed out that using the large model as a helper can remove some of its intelligence
  along with its knowledge.
- **Two helpers on every word.** I then compared one helper against two helpers on every word, across many model
  sizes. Two helpers did better at every size and improved as the helpers grew larger, while one helper did not.
  This is the design I use now. The main path forward is a stronger matched pair of helpers.
- **Other ideas that did not work.** Using the model's own notes about later events as the "later" side did not
  reduce leaks and cost old knowledge. A simple guard against repeated text also did not cut repetition.
- **A planted-news test.** I gave the model short news stories about real and made-up company deals. The plain model
  leaked the real outcome in 28% of answers about real deals, and the two-helper version leaked it in 14%.

### Making the Jacobian lens reliable

- **Fitting the lens.** I fitted the lens to Qwen3.5-27B on 100 dated news texts from 2007 to 2015, in four parts. Two
  halves of the lens agreed closely, and a lens built from only 25 texts already matched the full lens, so more
  fitting would not have helped.
- **Proving that it works on clear cases.** I first tested the lens on questions where the answer is obvious. When
  the model was told the date was 2025, the lens showed later topics more often than when it was told 2015, in 9 of 10
  topics. Asked as of 2025, the real outcome stood out inside the model in 25 of 32 events. The lens also saw every
  first written word before the model wrote it.
- **Fixing a false signal.** The first planned run failed because one word, "tiktok", appeared inside the model in
  almost every answer, whatever the question. I found 12 such background words on a separate set of answers and
  filtered them out, after which the lens could be used.
- **Seeing hidden look-ahead.** With the lens, I could see later knowledge that the written answers hid. For example,
  "covid" and "lockdown" appeared inside the model while it answered questions about office life dated 2015, and
  "Tesla" appeared inside while it wrote about Volkswagen.
- **Its limits.** The lens reads single word pieces, so it can miss names that are made of several words.

### From easy questions to harder ones

These early tests used questions that were fairly easy to guess, such as forecasts with a few given options. They were
useful for proving that the lens works, but too easy to reveal subtle look-ahead. I therefore moved to harder
benchmarks: company earnings, and the forecasting questions with crowd forecasts described above.

## The next round (designed, not yet run)

The next round uses 586 test questions, and 213 of them have never been shown to any model. Each question is also asked
in reverse ("Will this not happen?"), so that a model which always says "No" cannot pass. Each question gets five
answers instead of one. Whether the model remembers outcomes when told the date is 2026 is now a planned part of the
test, and the model must also forecast better than always answering 50%.

Before spending any money, I ran made-up models through the scoring to check that it gives the right answers:

| Made-up model | How often round 1 called it clean | How often round 2 calls it clean |
|---|---|---|
| Truly clean, while the plain model leaks on 6% of questions | 12% | 82% |
| Truly clean, while the plain model leaks on 10% of questions | 26% | 97% |

In round 2, a made-up model that leaks on 3% of questions is caught 92% of the time. A made-up model that answers
"No, 20%" to everything is labelled as giving no information.

This check also found a flaw in the round 1 scoring. Under that scoring, collecting more answers per question would have
made it harder, not easier, to call a model clean. Round 2 fixes this.
[Round 2 rules (draft)](docs/round2_rules_draft.md)

## How this work came together

This project started in finance. I had built my own AI trading strategy and wanted to backtest it, which means
testing how it would have done in the past. I soon ran into a problem: the AI model already knew what happened in
the years I wanted to test, so the backtest could not be trusted. While looking for a solution, I found the
frontier-to-pit paper by Merchant and Levy. I decided to recreate their method and see whether I could improve it.

After rebuilding the method, I spoke with Humzah Merchant, one of the paper's authors. On his advice, I began building
new benchmarks that go beyond the paper's original tests, so that the model can be judged more fairly and on more
kinds of questions. Most of my time now goes into building these benchmarks and into using Anthropic's Jacobian lens
to evaluate the model.

The Jacobian lens lets me look inside the model before it writes its answer. I use it to check whether the model holds
knowledge of later events, even when its written answer does not show it. This helps me confirm when look-ahead bias is
really there and whether my engine is working. When the lens shows that the model knows the future, and my engine
keeps that knowledge out of the answer, I count that as a win.

I use prediction markets and forecasting sites because they record what people expected at the time, as a price or a
forecast. This tells me exactly what was knowable on a given day. If a model does better than that expectation, it
must be using knowledge from later on.

I have worked with Claude throughout this project, going back and forth to try ideas and improve them. I make the
decisions and sign off on every step, and the ideas and information are my own. Claude helps me run the tests and keep
the work organized.

### What my benchmarks focus on

- **The expectation at the time.** Every question comes with a record of what people expected before the outcome,
  such as a market price or a forecast. This record was made at the time, not rebuilt later with hindsight, so the
  model can be compared against what was truly knowable on that day.
- **Knowledge the model actually has.** Before testing for look-ahead bias, I first check that the plain model knows
  the real outcome. I found that models often do not know this level of detail. For example, Qwen3.5-27B did not
  remember company earnings results, while other models such as DeepSeek V4 Pro, Kimi and Claude Opus remembered 85%
  to 95% of them. If the model never knew the answer, a test cannot show whether the engine hides it.
- **A check on what cannot be known.** Every test also includes questions from 2025 and 2026, which the model cannot
  know. On these questions the test must show no effect. If it does, the test is measuring something other than
  look-ahead bias.
- **Usefulness as well as leakage.** A model can look free of look-ahead bias simply by giving the same answer to
  everything. For this reason, I also measure whether its forecasts are still accurate and useful.
- **Realistic questions.** I aim for questions that someone would actually use in practice, such as forecasts of real
  events and economic numbers. For now the benchmarks stay at a high level, and I plan to make them more detailed over
  time.
- **Rules before results.** For every paid test, I write down the rules, the pass marks and the scoring code before
  any result exists. The real outcomes never reach the model during a test, and results that are inconclusive are
  reported as inconclusive.

## The test pipeline

The folder [`eval_pipeline/`](eval_pipeline/) holds the system that runs the tests on rented GPUs without anyone
watching:

- A controller runs on AWS and rents GPU machines from Runpod. It keeps a record of every step in a database.
- Each test runs in a sealed environment with no internet access and a fresh copy of the model. The code on the GPU is
  checked against the saved version before it runs.
- A budget check refuses to start a GPU unless the whole run fits under the approved dollar limit. A separate watchdog
  shuts down every GPU at the deadline.
- Results are uploaded so that they cannot be overwritten, and they are checked again when they are collected.

The whole project has used about $450 of rented GPU time so far.

## What is in this repository

| Folder | Contents |
|---|---|
| `docs/` | The rules, results and method sources of the forecasting tests |
| `bigevents/v1/` | The code for round 1: pulling the data, building the questions, the first check and the scoring |
| `bigevents/v2/` | The code for round 2: building the questions, the scoring and the made-up-model check |
| `eval_pipeline/` | The cloud controller, the GPU machine agent, the budget check and the watchdog |

This code is an excerpt. It does not run on its own, because it needs the data, the model and the paper's code from
the private repository.

## References

- Fair and Shiller (1990), *Comparing Information in Forecasts from Econometric Models*.
- Sarkar and Vafa (2024), *Lookahead Bias in Pretrained Language Models*.
- Lopez-Lira, Tang and Zhu (2025), *The Memorization Problem: Can We Trust LLMs' Economic Forecasts?*
- Paleka et al. (2025), *Pitfalls in Evaluating Language Model Forecasters*.
- Zou et al. (2022), *Forecasting Future World Events with Neural Networks* (the Autocast dataset).
- Karger et al. (2025), *ForecastBench*.

The full list is in [docs/method_references.md](docs/method_references.md).

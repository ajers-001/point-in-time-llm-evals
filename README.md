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
2. **My point-in-time model removes that leak from its answers.** Given the old date, its answers carried no sign of
   later knowledge: by my best estimate, it removed about 98% of the plain model's leak. The result is clearest on the
   255 questions whose outcomes the plain model clearly knew. There, the plain model's leak doubled in strength, while
   my point-in-time model removed about 84% of it, and at least 60% with 95% confidence. A check on questions the model
   cannot know showed that choosing questions this way does not explain the result.
3. **The knowledge can be seen inside the model before it answers.** Under the old date, the Jacobian lens showed the
   model's internal state already leaning toward the real outcome before its first word (about a 3 in 100,000 chance
   of luck). On the 2025 and 2026 questions, the lens showed no such lean. Inside the model, the lean toward "Yes" was
   about twice as strong for events that happened as for events that did not, while my point-in-time model's written
   answers barely moved between the two.
4. **The knowledge is held back, not removed.** When told that the date was 2026, my point-in-time model still
   remembered how most of these events ended. It got 75.5% of the 269 questions it answered right, including 50 of the
   73 surprises that the crowd got wrong. This matters beyond finance: making a model behave as if it has forgotten
   something is not the same as removing the knowledge.
5. **Usefulness has to be measured next to leakage.** My point-in-time model answered "No, about 20%" to most
   questions. Its forecast error (Brier score) was 0.272, worse than always answering 50% (0.250), while the plain
   model scored 0.235 and the crowd 0.198. A model can look free of leaks just by giving the same answer to everything,
   which is why the next round tests usefulness directly.

[Round 1 rules](docs/round1_rules.md) · [Round 1 results](docs/round1_results.md)

## What's next

- **Round 2 of the forecasting benchmark.** It is designed and priced, and repeats the test with more questions and
  stricter rules to confirm these findings. I am gathering feedback on the plan before running it. The design is
  described [below](#the-next-round-designed-not-yet-run).
- **A live earnings test.** For 253 company earnings releases between October 20 and November 13, 2026, the model's
  predictions were recorded and locked in before any of the results came out. They will be scored once all the
  results are collected. Because these events happened after the model was trained, no training data can have leaked
  into this test.
- **More benchmarks, already built.** These include 4,819 questions comparing real economic releases and market
  prices with what professional forecasters expected at the time, drawn from Reuters polls and the Philadelphia Fed's
  Survey of Professional Forecasters, and 369 long-shot questions where the crowd was almost certain.
- **Stronger helper models.** The biggest technical lever is a stronger, matched pair of 2015 and 2024 helper models,
  trained together on the same data.
- **A better lens.** Extending the Jacobian lens to read names made of several words.

## What I tried along the way

### 1. Reproducing the paper

The paper's method combines two parts. The first uses two small helper models, one trained on text up to 2015 and one
trained on text up to 2024, to push the large model away from what only the later helper knows. The second, called
steering, nudges the model's internal state in a chosen direction.

- I reproduced the paper's results with the authors' own code: leaks fell from 23 to 7 of 129 merger deals, and from
  11 to 0 of 49 drug trials.
- In my tests, steering added little on top of the helper models, so I focused on the helpers.
- Stronger helper settings removed more leaks but also produced more repeated or broken text, so I built fixes that
  stop most of the broken text.
- The main problem was the size of the helpers. The paper's small 2024 helper knew only 2 of the 129 merger outcomes,
  while the large model knew 33. A helper can only hold back knowledge that it can recognize, so these small helpers
  could not keep up with what the large model knew. From these measurements, I estimated that a 2015 helper would need
  roughly 14 billion parameters to do the job on its own.

### 2. Giving the helpers a news library

Training a much larger helper was out of reach, so I tried a different route: instead of asking the small helper to
remember old facts, I let it look them up.

- **Building the library.** I used the WMT News Crawl, a public archive of news articles collected from the web, and
  kept every English article dated from January 1, 2007 to December 31, 2015, about 7 million articles. The articles
  were streamed from the public server and searched by keyword (BM25), and the top three articles for each question
  were given to both the large model and the helper.
- **Checking the dates.** Every retrieved article was screened automatically for later dates and for mentions of the
  answer, and flagged articles were read by independent reviewer agents before use. The checks removed 1 of 279
  articles in the first test and none of 1,847 in the live test.
- **What it fixed.** Without articles, the helper wrongly flagged 47.5% of correct old answers as leaks; with them,
  17.2%. When a retrieved article contained the answer, wrong flags fell to 10.7%, against 58.3% without one, and
  retrieving 10 articles instead of 3 raised that coverage from 53% to 70%. In the live test, the filter without
  articles lost about a quarter of the model's correct old answers, while with articles it lost only 2% to 7%, and the
  articles doubled the base model's accuracy on old facts.
- **What it did not fix.** The articles did not, on their own, make the filter catch more leaks. Their value is that
  the model keeps its old knowledge while the filter works.

### 3. From one helper to two

- **One helper.** I tried using the large model itself as the "later" side, with a single 2015 helper acting only on
  names and numbers, together with the news library. On direct questions about the 129 deals, leaks fell from 25.6% for
  the plain model to 4.7%, while reasoning stayed close to the plain model (76.3% against 78.8% on the GPQA-Diamond
  science test). However, this design could not catch look-ahead carried by ordinary words, such as a 2015 answer that
  mentions a "lockdown". Humzah also pointed out that using the large model as a helper can remove some of its
  intelligence along with its knowledge.
- **Two helpers on every word.** Across many public model sizes, two helpers acting on every word did better than one
  helper and kept improving as the helpers grew, while one helper did not. This is the design I use now. It keeps the
  model's reasoning intact (80.8% on GPQA-Diamond against 78.8% for the plain model). In a planted-news test with real
  and made-up deals, it halved leaks about real deals from 28% to 14%.

### 4. Looking inside the model with the Jacobian lens

I wanted a way to check whether all of this was working: to look directly inside the large model before the helpers
act on its answer. This reminded me of Anthropic's work on the Jacobian lens, which I had been reading about on
Twitter, and that is when I started to bring it into the project.

- **First try.** I first ran the lens through a public demo, without renting a GPU. It reacted to the date (later
  topics appeared more often when the model was told it was 2025) and saw words inside the model before they were
  written.
- **Fitting my own lens.** I fitted the lens to Qwen3.5-27B on 100 dated news texts from 2007 to 2015, in four parts on
  rented GPUs. Two halves of the lens agreed closely, and a lens built from only 25 texts already matched the full one.
- **A false signal, then a fix.** The first planned run failed because one word, "tiktok", appeared inside the model in
  almost every answer, whatever the question. I found 12 such background words on a separate set of answers and
  filtered them out, after which the lens could be used.
- **Proving that it works.** Asked as of 2025, the real outcome stood out inside the model in 25 of 32 events. Reading
  the model before it answered, the lens could also tell which answers would go on to leak, with a score of about 0.75
  on a 0 to 1 scale where 0.5 is chance.
- **Seeing hidden look-ahead.** The lens showed "covid" and "lockdown" inside the model while it answered questions
  about office life dated 2015, and "Tesla" inside the model while it wrote about Volkswagen.
- **Its limit.** The lens reads single word pieces, so it can miss names made of several words. In [separate
  work](https://x.com/Ajer28251/status/2124252821394509999), I extended the lens from single word pieces to full words.

Going forward, I use the lens in every benchmark round as a second view next to the written answers. It shows whether
the knowledge is present inside the model, so that a clean written answer can be told apart from a model that simply
does not know.

### 5. From easy questions to harder ones

These early tests used questions that were fairly easy to guess, such as forecasts with a few given options. They
proved that the lens works, but they were too easy to reveal subtle look-ahead, so I moved to harder benchmarks.

The first was company earnings: 576 questions on whether a company beat the analysts' forecast. Qwen3.5-27B turned out
not to remember earnings results at all, recalling the exact reported figure only 3% of the time, so the test could not
show a leak either way. Other models did remember them: DeepSeek V4 Pro, Claude Opus and Kimi called 85% to 95% of the
results right, and telling DeepSeek V4 Pro that it was 2016 only lowered its accuracy from 83% to 60%. This taught me
to check first that a model knows the answers, which led to the forecasting benchmark below.

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

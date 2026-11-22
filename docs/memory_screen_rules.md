# Memory screen for the big-events set: rules fixed before any answer exists

Written November 15, 2026, before any model was asked a question from this set. Text above "## Amendments" does
not change afterwards.

## Purpose

Find the years in which a plain model, with no point-in-time engine, remembers how big events ended. A leak can
only be shown where the plain model knows the answer. This screen is not a test of the engine.

## Questions

`data/markets_pull/bigevents_candidates.csv`, 1,081 questions in 618 stories, built by rule from Polymarket,
Kalshi and Autocast. SHA256 `f1375d8214ed172c5fa27b035d36fdbc58db6d436f12813c51550e8b678523fa`.

## What is asked

- Code: `reports/bigevents-v1/memory_screen.py`, SHA256 `c33d0ad61503b3d36af09af8240147085d87751280caf9929aaaf8928f2f8f9f`.
- The model is told today is November 14, 2026, given the question, its resolution rules and the month it was
  open, and asked how it was settled: Yes, No or Unknown.
- The prompt never carries the outcome or the forecast.
- Thinking on, at most 6,000 output tokens, one answer per question, no retries for content.
- Models: `qwen/qwen3.5-27b` (the model the engine is built on), then `deepseek/deepseek-v4-pro` as a reference.
  Through OpenRouter. The DeepSeek result does not change the verdict on Qwen.

## Scoring

- The last of the words Yes, No, Unknown in the answer counts. Unknown, an empty answer or no such word is "not
  answered".
- Periods by the year the question closed: 2016-2019, 2020-2022, 2024, 2025, 2026. The years 2015 (6 questions) and
  2023 (12 questions) are reported but not judged.
- **Right:** share of answered questions where the answer equals the outcome.
- **Beyond the crowd:** for each answered question, take the outcome (1 or 0) minus the share of questions that
  happened among all 1,081 with the same source and the same forecast band (20-40%, 40-60%, 60-80%); give it a
  plus sign if the model said Yes and a minus sign if it said No. Average within each story. One-sided t-test that
  the mean over stories is above zero.

## Pass mark, per period

A period passes for a model when all hold:
1. at least 80% right among answered questions;
2. beyond-the-crowd p below 0.01;
3. at least half of the period's questions answered Yes or No;
4. at least 20 stories answered.

## What the result is used for

- Periods that pass on Qwen3.5-27B are where the engine can be tested for a leak.
- Periods that fail are the floor (what "does not know" looks like).
- If no period passes on Qwen3.5-27B, the big-events set cannot show a leak on this engine. That is reported as
  such; the pass mark is not lowered afterwards.

## Money

A total cap of $20 was approved on November 15, 2026. The run stops sending questions at $13 for Qwen and at
$6.50 for DeepSeek. If a cap is reached, the answered part is scored and the shortfall is reported.

## Amendments

None.

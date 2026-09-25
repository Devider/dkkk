# Business logic: the Copilot agent

This describes what the agent actually does from a product/business perspective — what it's
for, what it will and won't calculate, how it decides what to do with a question, and how it
behaves when the question is incomplete or ambiguous. For the technical architecture (graph
wiring, LibreOffice backend, Docker, etc.) see `CLAUDE.md`.

## What it's for

The assistant helps a credit inspector interrogate an uploaded Excel cashflow model without
opening Excel themselves. A user uploads a workbook (`POST /upload`) and then asks questions
about it in natural language (`POST /invoke-agent`), in Russian or English. Every answer is
grounded in an actual recalculation of the real workbook — the assistant is explicitly
forbidden from inventing or estimating numbers itself; if a number is needed, it has to come
from running the model.

## The two things it can calculate

The workbook exposes named **input parameters** (assumptions — prices, rates, volumes, etc.)
and named **output metrics** (results — EBITDA, Debt/EBITDA, DSCR, etc.), each per year. The
agent supports exactly two kinds of calculation, and decides for itself which one (if either)
a question needs:

- **Target-seeking** ("what input value gets me this result?") — the user names an output
  metric, a target number, and a year; the agent searches for input values that get the model
  to that target within a tolerance. E.g. *"какой должна быть цена меди, чтобы EBITDA в 2025
  году составила 1000 млн?"*
- **Scenario analysis** ("how do results change across a range of assumptions?") — the user
  names one or more output metrics and gives one or more inputs a numeric range and step; the
  agent recalculates the outputs across that grid. E.g. *"посчитай FCFF и DSCR за 2025 при
  изменении цены меди от 450 до 500 с шагом 10"*.

A single question can trigger both calculations if it genuinely needs both, or neither if it
doesn't need a calculation at all (see below).

## How it decides what to do

There's no fixed rule-based router — a single LLM call ("orchestrator") reads the question in
the context of the whole conversation and decides, by meaning, whether a calculation is
needed and if so which one. It deliberately skips calling anything when:

- the message doesn't need a number (greeting, a general or clarifying question);
- the answer is already in the conversation history (e.g. the user asks to explain or rephrase
  a result they already got);
- the question is off-topic or in an unsupported language (see Guardrails below).

If a calculation *is* needed but the first attempt comes back incomplete or wrong (see below),
the same decision step gets another look with the new information and can try again — up to 4
attempts per question — before giving up and explaining what's missing instead of retrying
forever. An exact repeat of an already-tried calculation within the same turn is recognized and
not re-run.

Once no more calculation is needed, a separate step ("synthesizer") turns whatever happened —
one calculation, two, or none — into the single answer shown to the user, always written in
business language and grounded in the actual conversation, never in the intermediate reasoning
that decided whether to calculate.

## Matching what the user said to what's in the model

Users describe parameters and metrics in their own words, often in English or with informal
names ("copper", "цена меди", "Cu price") — the model's canonical names are fixed Russian
labels from its Inputs/Outputs sheets. The agent fuzzy-matches the user's phrasing against
those canonical names (word-overlap similarity plus a small glossary of finance abbreviations
like "av" = среднее, "eop" = на конец периода), with a second, more targeted lookup pass when
the first match is uncertain.

Two conversions happen automatically as part of this matching, since getting them wrong would
silently produce a wrong number:
- **Percentages**: "ROE 15%" or "маржа 15 процентов" is converted to `0.15` for the model; a
  plain number with no "%" or "процент" is left as-is (`"target 150"` stays `150`).
- **Shared step**: if a chain of inputs gives a step only on the last one ("от 80 до 172 и от
  80 до 132 с шагом 25"), that step is assumed to apply to all of them.

This matching is the main source of inaccuracy in the system today — it's currently right
about 55% of the time per individual parameter, which compounds across a query with several
parameters. A query naming many parameters is meaningfully more likely to need a follow-up
correction than one naming few.

## When something is missing or wrong

The agent is built to never fabricate a number the user didn't actually give it — a missing
target value, year, or numeric range is never silently defaulted (not to `0`, not to "the
current year," nothing). Instead, every calculation is checked for completeness before it
runs, and the outcome of a calculation attempt is one of:

- **OK** — the calculation ran and produced a real result; the answer presents it.
- **WARNING** — a required piece of data (a target year, or a numeric range for scenario
  analysis) was never provided by the user; the answer asks for exactly that, and only that —
  it doesn't guess what else "might" be needed (production volumes, costs, tax rate, etc.) and
  it never shows internal error text or placeholder values as if they were facts about the
  model.
- **INFO** — the calculation ran but the result needs a follow-up (e.g. a name couldn't be
  resolved at all); the answer explains what to clarify.
- **ERROR** — something else prevented the calculation from running; the answer explains this
  in plain business language, never by quoting the underlying exception text.

In every one of these non-OK cases the rule is the same: ask for precisely what's missing, in
plain language, and nothing more.

## Guardrails

- **Language**: Russian and English only. Anything else gets a fixed refusal, not a translated
  answer.
- **Topic**: cashflow-model analysis only. Off-topic requests (politics, code, anything
  unrelated to the uploaded model) get a fixed refusal rather than an attempt to answer.
- **Tone**: answers are always formal/professional business Russian, regardless of how the
  question was phrased.

## What the user actually gets back

`/invoke-agent` returns a ZIP containing a text answer (`txt_response.txt`) plus a generated
Excel file placeholder (`generated/agent_output.xlsx`) — the meaningful output today is the
text answer; the workbook isn't currently populated with results.

Conversation memory is per session (`x-session-id` plus the caller's `x-user-id`): repeat
questions, or questions that build on an earlier result in the same session, are answered
using that history rather than starting over — including reusing an earlier calculation's
result instead of recomputing it when the same question is asked again.

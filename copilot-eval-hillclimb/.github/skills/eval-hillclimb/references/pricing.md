# Pricing the runs

The report builder prices every row from `_state.json.prices`. The runner never computes cost, and you never compute it by hand. Each entry is in **USD per million tokens**:

```json
"prices": {
  "claude-opus-5-5":  {"in": 4.00, "out": 20.00},
  "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
  "my-gateway-model": {"in": 1.10, "out": 4.40, "cache_read": 0.275, "cache_write": 1.10}
}
```

- **Model ids.** Key by the id the **response** reports, since that is what each row's `model` holds. A key also prices dated snapshots of itself: `claude-opus-5-5` matches `claude-opus-5-5-20260101`.
- **Cache defaults.** `cache_read` defaults to 0.1 × `in` and `cache_write` to 1.25 × `in`. Those are Anthropic's rates for the default 5-minute cache. A 1-hour cache write is 2 × `in`; set `cache_write` explicitly if the app uses it. For any other provider, set both explicitly.
- **Judge models.** Price them too: `judge_model` × `judge_usage` is part of every `$/run`.
- **Missing prices.** A model with no entry turns `$/run` into "not measured", and the builder's warning names the model. Add the rate rather than guessing.

## Where the numbers come from

**Ask the user for their rate card** unless the app calls the Anthropic API directly. That covers:

- other providers;
- Claude through a cloud marketplace (Amazon Bedrock and Vertex AI price Claude separately);
- a company gateway that charges back internally.

On a company computer the gateway's internal rate is usually the number that matters. Record what they give you, and note its source and date in `metrics.md`.

**Anthropic first-party list prices** (from the claude-api skill's model table, cached 2026-10-06 - check https://www.anthropic.com/pricing before quoting them as current). They also apply to Claude on Microsoft Foundry:

| Model | Model ID | Input $/MTok | Output $/MTok |
|---|---|---|---|
| Claude Fable 5.1 | `claude-fable-5-1` | 10.00 | 50.00 (cache reads 0.25) |
| Claude Fable 5 | `claude-fable-5` | 10.00 | 50.00 |
| Claude Opus 5.5 | `claude-opus-5-5` | 4.00 | 20.00 |
| Claude Opus 5 | `claude-opus-5` | 5.00 | 25.00 |
| Claude Opus 4.8 / 4.7 / 4.6 | `claude-opus-4-8` / `-4-7` / `-4-6` | 5.00 | 25.00 |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | 2.00 | 10.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 2.00 | 10.00 |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 3.00 | 15.00 |
| Claude Haiku 5.5 | `claude-haiku-5-5` | 0.10 | 0.50 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 1.00 | 5.00 |

**Other providers' cache rules** differ from Anthropic's. Ask, or read the provider's own pricing page:

- **OpenAI / Azure OpenAI.** Cached input is billed at a per-model discount, and there is no cache-write premium. Set `cache_read` to the cached-input price and `cache_write` equal to `in`. `normalizeUsage` already moves cached tokens out of `prompt_tokens`, so they are not billed twice.
- **Gemini.** Cached tokens are billed at a cached-input rate, and explicit caches also carry a storage charge, which these columns do not cover. Set `cache_read`, and note the storage charge if it matters.

## Pricing basis

A `$/run` is only comparable to another `$/run` priced the same way. Keep `prices` fixed for the whole loop. If the rate card changes mid-loop, rebuild the report, which re-prices every variant, and note the change in `narrative.md`. In a cost-goal hillclimb, `cost-hillclimb.md` §Measurement discipline ("pricing basis discipline") applies.

Walk cells in a cost search are cache-cold, so production will usually be cheaper than the eval's `$/run`. Ratios between variants priced on the same basis are more robust than absolute dollars.

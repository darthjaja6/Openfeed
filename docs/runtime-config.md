# Internal runtime defaults

OpenFeed runtime knobs live in source code at
`src/openfeed/default_runtime.yaml`. They are internal defaults, not user
onboarding config.

User instance files should not include a top-level `runtime` section. If you
want to change these values, fork or patch OpenFeed source, then restart the
affected process.

---

## `discover`

| field | default | meaning |
|---|---|---|
| `hard_gate_retry_window_days` | 60 | Hard-gate rejects (low subs, dead feed) get re-evaluated after this many days |
| `youtube.keywords_per_topic` | 15 | How many seed terms to use per topic |
| `youtube.results_per_keyword` | 2 | opencli search results per keyword (soft target; oversampled) |
| `youtube.oversample_multiplier` | 10 | Pull `results_per_keyword × this`; every survivor goes to LLM review |
| `youtube.min_subscribers` | 1000 | Hard gate before LLM review |
| `x.min_followers` | 500 | Hard gate for X authors |
| `web.min_feed_entries` | 5 | Skip empty/skeleton feeds |
| `web.max_age_days` | 90 | Skip dead blogs (most-recent post older than this) |

## `patrol`

| field | default | meaning |
|---|---|---|
| `youtube.max_items_per_source` | 30 | opencli channel pull cap (~30 is YouTube's effective max) |
| `x.max_items_per_source` | 20 | Same for X |
| `web.max_items_per_source` | 100 | feedparser returns whatever the feed has |

## `filter`

| field | default | meaning |
|---|---|---|
| `score_weights.{popularity,engagement,freshness,preference}` | 0.3/0.3/0.25/0.15 | Composite-score weights (sum doesn't have to equal 1) |
| `composite_score_threshold` | 0.05 | Below → reject as `low_composite_score` |
| `freshness_half_life_days_default` | 7 | Exp-decay half-life for freshness sub-score |
| `freshness_half_life_days_per_topic` | `{}` | Override default per topic |
| `admission_rate_ema_alpha` | 0.3 | EMA smoothing on `source.admission_rate` |
| `zero_admit_retire_threshold` | 3 | Source retired after N consecutive ticks of "patrolled but admit=0" |
| `youtube.min_views` | 100 | Per-video views threshold (was 1000; lowered to admit niche creators) |
| `youtube.max_age_days` | 60 | Skip videos older than this |
| `youtube.duration_{min,max}_seconds` | 15 / 600 | Per-video duration window (10min cap = swipe-feed sweet spot) |
| `x.min_interactions` | 5 | likes + retweets + replies floor |
| `x.min_text_length` | 20 | Drop one-word posts |
| `web.min_summary_length` | 50 | Drop empty stub entries |

## `queue_manage`

| field | default | meaning |
|---|---|---|
| `topic_floor` | 3 | Per-topic minimum inventory; below → refill signal to patrol |
| `topic_capacity` | 5 | Per-topic target inventory; each topic refills independently |

## `push`

| field | default | meaning |
|---|---|---|
| `target_buffer` | 3 | Push until producer's unconsumed count ≥ this |
| `max_per_tick` | 3 | Safety cap per tick |
| `same_source_gap` | 2 | Last N pushes can't share source (spacing) |
| `top_k` | 2 | Sample from top-K by rank_score within chosen topic |
| `producer` | `"ticlawk"` | Card producer name (currently only `ticlawk`) |
| `tick_budget_seconds` | 30 | Wall-clock cap for render+push per tick |
| `render_workers` | 4 | Render threadpool size for lazy render |

## `refill_cycle`

| field | default | meaning |
|---|---|---|
| `interval_seconds` | 10 | Consumer-side tick rate |

## `collect_feedback`

| field | default | meaning |
|---|---|---|
| `tick_budget_seconds` | 30 | Cap on channel-changes pagination per tick |

## `youtube_download`

| field | default | meaning |
|---|---|---|
| `ready_target_per_topic` | 15 | Front-of-queue YouTube videos to keep downloaded per topic |
| `max_per_tick` | 20 | Videos to attempt downloading per prepare_video tick |
| `max_concurrent` | 4 | Parallel yt-dlp processes |
| `tick_budget_seconds` | 180 | Wall-clock cap for the prepare_video phase |
| `target_height` | 720 | Sort preference (`-S "res:N"` to yt-dlp); H.264 forced via format selector |
| `failure_backoff_minutes` | 30 | Wait before retrying a failed video |
| `max_failures_before_permanent` | 5 | Mark `permanently_failed` after N consecutive fails |

## `video_cleanup`

| field | default | meaning |
|---|---|---|
| `keep_days` | 14 | Drop local mp4 + producer asset N days after last push |
| `cache_max_gb` | 5.0 | Local mp4 cache cap (LRU evict above) |
| `ticlawk_quota_max_gb` | 45.0 in admin deployment | Account-level Ticlawk asset quota guard. Set this below the creator's real quota, usually 80%-90% of the quota. |

## `learn`

### Active signal weights (additive scoring)
| field | default | meaning |
|---|---|---|
| `score_share` | 8.0 | Share = scarcest active signal |
| `score_save` | 5.0 | Save = strong intent |
| `score_like` | 3.0 | Like = lightest acknowledgement |

### Passive signal thresholds (cognitive time)
| field | default | meaning |
|---|---|---|
| `dwell_reflex_seconds` | 3 | Below = reflexive swipe → strong negative |
| `dwell_dismiss_seconds` | 10 | + `watch_dismiss_max` = active dismiss |
| `watch_dismiss_max` | 0.3 | Watch ratio for dismiss tier |
| `dwell_strong_positive_seconds` | 60 | Strong-positive engagement floor |
| `watch_strong_positive` | 0.8 | Or this watch ratio |
| `dwell_positive_seconds` | 30 | Lighter positive floor |
| `watch_positive` | 0.5 | Or this watch ratio |
| `score_strong_positive` | 1.5 | Score for strong-positive engagement tier |
| `score_positive` | 0.7 | Score for moderate positive tier |
| `score_weak_negative` | 0.7 | Score for weak negative (stored unsigned) |
| `score_strong_negative` | 1.5 | Score for reflexive swipe |
| `dwell_outlier_cap_seconds` | 600 | Above = "tab abandoned"; passive signal dropped |

### Preference drift
| field | default | meaning |
|---|---|---|
| `feedback_signal_decay_rate` | 0.977 | Daily multiplier on α/β-prior portion. 1.0 = no decay; 0.977 ≈ 30-day half-life |

### Bayesian retire
| field | default | meaning |
|---|---|---|
| `retire_posterior_threshold` | 0.4 | Posterior mean below this counts as "low" |
| `retire_evidence_min` | 2 | Need this much accumulated evidence to retire |
| `retire_bottom_k` | 3 | Retire candidates: bottom-K per topic by posterior mean |

### Mood damping (global)
| field | default | meaning |
|---|---|---|
| `mood_window_hours` | 24 | Recent feedback window |
| `mood_damp_threshold` | 0.45 | neg_ratio above this → light damp |
| `mood_heavy_damp_threshold` | 0.6 | → heavy damp |
| `mood_damp_multiplier` | 0.75 | Light damp factor on negative scores |
| `mood_heavy_damp_multiplier` | 0.5 | Heavy damp factor |

### Keyword-proposal LLM trigger
LLM expands `search_terms.json` keyword pool from positive feedback only.
Negative feedback drives source / search-term retire instead — not this loop.

| field | default | meaning |
|---|---|---|
| `keyword_proposal_min_positive_examples` | 10 | Need ≥ N positive examples per topic to trigger |
| `keyword_proposal_max_examples` | 20 | Cap examples per topic in prompt (token cost) |
| `keyword_proposal_max_new_terms` | 3 | Cap newly-added keywords per fire |
| `keyword_proposal_workers` | 4 | Per-topic LLM parallelism |

### Search-term retire
Parameterless. Rule: a keyword retires when ≥ 1 of its introduced sources
has been judgment-evaluated AND none of those sources are currently
`active`. "Judgment-evaluated" excludes only platform-scale gates
(`low_subscribers`, empty/stale feeds, etc. — see
`KEYWORD_ACQUITTAL_REASONS` in `core/learn_search_terms.py`); LLM source
rejects, Bayesian retire, and `filter_consistent_reject` all count as
real evidence against the keyword.

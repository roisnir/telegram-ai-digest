# Testing Workflow

## Overview

Three tiers of quality gates:

| Tier | When | Trigger | Human needed? |
|---|---|---|---|
| **Unit tests** | Every commit | Automatic (`pytest`) | No |
| **Verifier agent** | After each story implementation | ralph.sh loop | No |
| **Browser QA** | Before merging a PR | Add `qa` label to PR | Yes (reviews artifacts) |

---

## CI Testing Coverage

| Pipeline Step | Test Layer | What's Tested | What's Mocked / Omitted |
|---|---|---|---|
| Telegram auth | None | — | Entire step skipped |
| Message fetch + date window | None | — | Entire step skipped |
| Album collapse + source_map build | Unit (13 tests) | grouped_id dedup, anchor selection, ts field, chronological order, service message skipping | Telethon message objects replaced by hand-rolled stubs |
| Media + link extraction | Unit (21 tests) | Type detection, duration, comment link exclusion, dedup | Telethon entity objects replaced by local stubs |
| Claude prompt construction | None | — | Entire step skipped |
| Claude API call | None | — | Entire step skipped; JSON fixture used downstream |
| Digest normalisation | Unit (8 tests) | JSON parsing, link/links promotion, bad-item filtering | Nothing — pure function |
| HTML rendering | Unit (33 tests) | Structure, sections, embeds, lazy-load JS, ordering, further reading | Nothing — pure function; only 1–2 item synthetic inputs |
| HTML file write | None | — | Entire step skipped |
| Telegram message format | Unit (15 tests) | Time label, section emoji, source links, multi-source numbering | Nothing — pure function |
| Telegram send | None | — | Entire step skipped |
| **Browser QA** (opt-in, `qa` label) | Puppeteer | Lazy load, script injection on expand, global embed ID uniqueness, 100% embed render | Nothing — live HTML from `tests/fixtures/sample_digest.json` → `build_html_page()`, live telegram.org. `main()` auth skipped via `--fixture`. |

---

## Risk Assessment

| Risk | Severity |
|---|---|
| **`main()` wiring untested** — a contract break between fetch/render (e.g. `source_map` key rename) passes all tests, fails at runtime | High |
| **Telegram session expiry** — auth failure at runtime, no notification sent, operator has no alert | High |
| **Claude returns unknown section** — items silently dropped from output, no log or assertion | Medium |
| **Claude returns hallucinated link** — embed placeholder generated for non-existent post, silently fails in browser | Medium |
| **`PUBLIC_BASE_URL` / `HTML_OUTPUT_DIR` unset** — broken link sent to Telegram, no startup validation | Medium |
| **All channels fail to fetch** — digest skipped silently, no alert to operator | Medium |
| **Global duplicate embed IDs** — only caught with realistic multi-item data; unit tests too small to surface it | Low (caught by browser QA) |
| **12h vs 24h window** — code uses 12h, spec says 24h, no test pins window size | Low |
| **Comment link detection is channel-specific** — new channels with different mirror patterns leak self-referential URLs | Low |

---

## Development Process

### ralph.sh — two-agent development loop

`ralph.sh` runs two Claude agents per story, back-to-back:

**Agent 1 — Implementer**
- Picks the highest-priority `passes:false` story
- Lists each acceptance criterion and how it will verify it before coding
- Implements, runs tests, commits
- Runs `python digest.py --fixture tests/fixtures/sample_digest.json --dry-run --output /tmp/ralph-check.html` and inspects the HTML output as a human reader would — looking for repeated elements, broken structure, duplicate embed IDs
- Signals `READY_FOR_VERIFICATION: [story-id]` in progress.txt — does NOT set `passes:true`

**Agent 2 — Verifier (fresh context)**
- Receives no implementation context — reads only `prd.json` and `progress.txt`
- For each acceptance criterion: independently checks it, writes `CRITERION: … VERDICT: PASS/FAIL`
- Runs tests independently
- Runs the fixture HTML check and inspects it as a first-time human reader
- If all pass: sets `passes:true`, commits `chore: verify [ID]`, signals `VERIFIED_PASS`
- If anything fails: leaves `passes:false`, records specific failure in progress.txt

The key invariant the verifier enforces without any Telegram API knowledge:
> "Are data-telegram-post values globally unique across the page? Does anything repeat that shouldn't?"

### Browser QA — `qa` label on PR

Triggered by adding the `qa` label to a GitHub PR. Defined in `.github/workflows/qa.yml`.

Note: `issue_comment` triggers only fire from workflows on the default branch, so a
label-based trigger is used instead — it runs from the PR head branch where the workflow lives.

**What it does:**
1. Checks out the PR branch
2. Runs `python digest.py --fixture tests/fixtures/sample_digest.json --dry-run --output /tmp/qa-digest.html`
3. Runs `node scripts/browser-qa/test.js /tmp/qa-digest.html` against live telegram.org

**Hard assertions (non-zero exit = PR check fails):**
- Lazy load: 0 Telegram iframes in DOM before any expand
- Inject: widget.js script fires when a `<details>` is opened
- Unique IDs: all `data-telegram-post` values globally unique
- Render: 100% of embeds reach load + height-stable state

**Artifacts posted to PR:** result.txt (inline in comment), screenshots linked to run.

**Fixture:** `tests/fixtures/sample_digest.json` — real posts from the monitored channels. Update the post IDs if any become unavailable.

---

## Development Incidents

Issues found during development that were not caught by the test suite at the time. Each entry captures the root cause, why it escaped, the fix applied, and the systemic prevention added to the workflow.

The general principle behind all prevention measures:
> **QA assertions should be derived from what a human would notice on the output, not from knowledge of the implementation that produced it.** If a human looking at the page for the first time would immediately notice something is wrong, the verifier agent and/or the QA harness should catch it first.

---

### INC-001 — Telegram album duplication in embed output

**Discovered:** During manual browser inspection after US-005 / US-006 (lazy-loaded Telegram embeds, unified source thread).

**Symptom:** A story with N source messages sharing a Telegram album (`grouped_id`) rendered N separate embeds, all showing the identical full album. One story reported "11 sources" was really ~6 distinct posts; one album of 10 messages produced 10 identical embeds.

**Root cause:** `build_channel_sources` treated each raw Telethon message as a separate source. Any album member embed renders the entire album, so embedding all N members duplicates the content N times.

**Why it wasn't caught:**
- Static assertion counted `data-telegram-post` divs vs `<details>` wrappers but had no ground truth for "how many distinct posts does this story represent."
- Browser check measured render timing and took clipped screenshots; album duplication was off-screen and indistinguishable from a legitimately merged thread.
- No awareness that `grouped_id` messages are one visual post, not N separate posts.

**Fix applied:** `build_channel_sources` now collapses each `grouped_id` group to one source entry using the anchor (lowest message id). `TestBuildChannelSourcesAlbums` (13 unit tests) cover the invariant.

**Systemic prevention added:**
- **Verifier agent prompt** instructs the agent to check global embed ID uniqueness on every story — without needing to know why duplicates happen.
- **Browser QA assertion** (`unique-ids`) checks that all `data-telegram-post` values across the page are distinct — this catches the whole class of "same post rendered N times" bugs regardless of cause.
- Both checks are framed as output-observable invariants: *"does anything repeat that shouldn't?"* — not as API knowledge.

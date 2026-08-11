# tgq

A read-only Telegram client for the terminal. Search for channels, inspect their
metadata, export their history. JSONL on stdout, logs on stderr.

Telegram only, by design. Sentiment, text parsing, topic modelling and
coordination detection belong downstream — pipe rows into whatever does them.

## Scope

**Read-only, deliberately — this is a boundary, not an unfinished state.**
Nothing here sends, posts, reacts, joins, or modifies anything. Three reasons,
in the order that actually matters:

1. **Every command is safe to re-run.** That is what makes the pipe model work:
   you can loop, retry, and script without wondering what happened last time. A
   tool that can post cannot be re-run casually.
2. **It keeps the account-risk profile low.** Telegram limits and bans accounts
   for write-shaped automation — sending, mass-joining, reacting. None of that
   is reachable from here. Member listing is the one *read* the platform treats
   as a spam signal; it is implemented because cohort analysis needs it, and it
   is the one command to use sparingly.
3. **A research tool must not perturb what it observes.** An instrument that can
   post into the channels it measures cannot support a clean claim about them.

Reads not yet implemented are fair game: in-channel message search, discussion
threads, media download. If write access is ever wanted, it belongs behind a
separate command surface, or in a separate tool — not as a flag on these.

## Install

```sh
uv tool install tgq-cli     # or: pipx install tgq-cli
```

The distribution is `tgq-cli`; the command it installs is `tgq`.

## Start with no account

`tgq preview` reads Telegram's public web preview at `t.me/s/<channel>` — no
account, no credentials, no session:

```sh
tgq preview ceuta_news --since 2026-07-27 > out/msgs.jsonl
```

Lower fidelity than `messages`, and the gaps are explicit rather than papered
over. **Available:** text, timestamps, view counts, forward origin (channel and
message id). **Emitted as `null`:** `forwards`, `reactions`, `replies_count`,
`channel_id`, `edit_date`, `post_author`, and `fwd_from_date` — so
`latency_seconds` from `tgq forwards` is always `null` on preview data.

Subscriber count is logged to stderr, not included in rows.

Both backends emit an **identical key set**, so downstream filters work
unchanged. Where preview cannot supply a value it is `null`, never substituted
or omitted — a test enforces the key sets match, because an *absent* key
silently passes numeric comparisons in `jq` (`null < 5` is `true`).

Channels are keyed by `@username` when no numeric id exists.

## Setup for the MTProto backend

Credentials come from <https://my.telegram.org>:

```sh
export TG_API_ID=... TG_API_HASH=...
```

The first network command prompts for your phone number and a login code, then
caches a session at `~/.config/tgq/tgq.session` (override with `--session`).
That file authenticates as your account — it is created `0600` inside a `0700`
directory, and must never be committed or shared.

## Commands

| Command | Network | Purpose |
|---|---|---|
| `tgq search QUERY` | session | Find channels and groups by name or title |
| `tgq preview CHANNEL` | no auth | Public web preview: text, views, forward origin |
| `tgq channels NAMES...` | session | Channel metadata: subscribers, verified, kind, created_at |
| `tgq messages CHANNEL` | session | History with views, forwards, reactions, forward origin |
| `tgq members GROUP` | session | Group members with account flags and last-seen |
| `tgq forwards` | none | origin → relay edges with cascade latency |
| `tgq activity` | none | Per-channel span and `days_silent`, most-dormant first |

Channel arguments accept `name`, `@name`, or a `https://t.me/name` URL.

## Worked examples

**Find channels.** Emits the same rows as `channels`, so results feed straight
into collection.

```sh
tgq search fnideq --kind megagroup
tgq search ceuta | jq -r 'select(.subscribers > 1000) | .username'
```

Telegram's global search matches username and title only, and returns
single-digit result counts in practice — seven for `fnideq`, nine for
`bitcoin`. **An empty result is not evidence that no such channel exists.**
Discovery on Telegram is genuinely weak; a sampling frame built from search
alone must say so, and should be expanded via the forward graph below.

**Collect a window.** Pages backwards and stops at `--since`, rather than
reading history to the beginning.

```sh
tgq preview ceuta_news --since 2026-07-27 > out/msgs.jsonl
tgq messages ceuta_news --since 2026-07-27 --until 2026-08-02 > out/msgs.jsonl
```

**Coordination: few actors, many accounts.** Forward edges carry the gap between
origin post and relay. Implausibly tight latency fanning out from one origin is
harder to produce accidentally than text similarity.

```sh
tgq forwards < out/msgs.jsonl \
  | jq -s '[.[] | select(.latency_seconds != null and .latency_seconds < 5)]
           | group_by(.origin)
           | map({origin: .[0].origin, relays: length})
           | sort_by(-.relays)'
```

The `!= null` guard is mandatory. `jq` evaluates `null < 5` as `true`, so
omitting it reports every edge of unknown latency as sub-5-second coordination —
including all preview-derived edges.

This also needs a null model before it means anything: identical content across
channels is normal when everyone reposts the same news. Pull the same calendar
weeks from prior years to get the expected co-posting rate.

**Members and account cohorts.** Emits user ids, usernames, names, bot and
premium flags, and last-seen — the inputs for co-membership and cohort
analysis. Telegram user ids are broadly sequential over time, so batch-registered
accounts cluster in id space.

```sh
tgq members somegroup > out/members.jsonl
jq -s 'group_by(.user_id / 1000000000 | floor)
       | map({id_band: .[0].user_id, accounts: length})' < out/members.jsonl
```

**Read the coverage line on stderr before using the output.** Three different
causes produce a short list and the tool distinguishes them:

```
67 of 67 members retrieved (100.0%) coverage
3 of 67 members retrieved (4.5%) -- stopped at your --limit
5 of 14679 members retrieved (0.0%) -- the admin has HIDDEN this group's
    member list, so enumeration is blocked server-side
```

Two hard limits, measured rather than assumed:

- **Broadcast channels expose no participants at all** — `ChatAdminRequiredError`.
  Members work on megagroups (public groups) only.
- **Admins of groups with 100+ members can hide the list**
  ([`channels.toggleParticipantsHidden`](https://core.telegram.org/method/channels.toggleParticipantsHidden)).
  The server then returns almost nothing while `count` still reports the true
  total, so a hidden group is indistinguishable from a tiny one unless you read
  the warning. Size alone does not restrict enumeration.

**Reach.** `views` and `forwards` come straight off the platform. Both stay
`null` when Telegram omits them — a real zero and an absent field are different
facts, so they are never conflated.

```sh
jq -s 'map(select(.views != null)) | {posts: length, views: (map(.views) | add)}' \
  < out/msgs.jsonl
```

**Dormancy.** Channels that stopped posting sort to the top. What counts as
dormant is left to the caller.

```sh
tgq activity --as-of 2026-08-11 < out/msgs.jsonl \
  | jq -s 'map(select(.days_silent > 7))'
```

**Snowball discovery.** Frame expansion is a shell loop, not code. Filter to
`@`-prefixed origins: numeric and `user:` keys are not channel usernames, and
feeding a bare digit string to `channels` makes Telethon treat it as a phone
number.

```sh
tgq forwards < out/msgs.jsonl \
  | jq -r 'select(.origin | startswith("@")) | .origin[1:]' \
  | sort -u > out/discovered.txt
tgq channels --from-file out/discovered.txt > out/channels.jsonl
```

## Provenance

Every row carries `source_url`, `retrieved_at` and `source_kind` (`primary` for
direct platform retrieval). Derived rows must restate their own kind.

## Responsible use

This tool collects public data from a live platform. Before using it:

- **Public channels only.** `t.me/s/` serves broadcast channels; public groups
  render nothing there and require the MTProto backend. Nothing here accesses
  private groups, and no feature should be added that does.
- **Collected rows contain personal data about identifiable people.** If you are
  in a jurisdiction with data-protection law, that law applies to you. Decide
  your retention, pseudonymisation and publication posture before collecting,
  not after.
- **Telegram's API Terms §1.5 prohibit using collected data to train or
  fine-tune AI models.** Classifying with a pre-trained model is inference and is
  unaffected; fitting one on the corpus is not.
- **The preview backend is not covered by the API Terms** — it is ordinary web
  access, subject to Telegram's website terms. Assess that yourself.
- **The MTProto backend acts as your own account.** Telegram limits and bans
  accounts for aggressive automation. `tgq members` is the highest-risk command
  here — bulk member enumeration is the platform's primary spam signal — so run
  it sparingly and bound it with `--limit`. Aged accounts fare better than new
  ones.
- **Rate limiting.** `preview` defaults to `--delay 1.0` seconds between page
  requests; raise it for large runs. `messages` sleeps through `FLOOD_WAIT`
  under 900s and aborts beyond that rather than stalling indefinitely.

## Notes

- Telethon is pinned to `1.44.0`. Upstream moved to
  [Codeberg](https://codeberg.org/Lonami/Telethon); v2 has been in alpha since
  Oct 2025 without shipping, so this builds against the v1 API.
- A channel that will not resolve is logged and skipped, so one dead entry in a
  frame of hundreds does not abort collection. A rate limit is not skippable and
  aborts the run. `channels` exits non-zero if nothing resolved at all.
- `preview` deduplicates by message id and stops if a page yields nothing new,
  so a stuck pagination cursor cannot silently duplicate rows.

## Development

```sh
git clone https://github.com/owahltinez/tgq-cli && cd tgq-cli
uv sync --extra dev
uv run pytest -q        # no network access required
uv run ruff check src tests && uv run ruff format --check src tests
```

The network surface is confined to `client.py` and `preview.fetch_page`;
everything else takes plain dicts, duck-typed objects, or fixture HTML, so the
suite runs entirely offline. CI runs those three commands on 3.11, 3.12 and
3.13.

## Releasing

Publishing is a tag push. `.github/workflows/release.yml` refuses a tag whose
version disagrees with `pyproject.toml`, re-runs lint and tests against the
tagged commit, and uploads to PyPI via [trusted publishing][tp] — there is no
API token stored in the repository.

```sh
# bump `version` in pyproject.toml first, and commit it
git tag v0.1.0 && git push origin v0.1.0
```

One-time setup on PyPI, under the `tgq-cli` project's *Publishing* settings:
add a GitHub publisher with owner `owahltinez`, repository `tgq-cli`, workflow
`release.yml`, environment `pypi`.

[tp]: https://docs.pypi.org/trusted-publishers/

## Licence

MIT. See [LICENSE](LICENSE).

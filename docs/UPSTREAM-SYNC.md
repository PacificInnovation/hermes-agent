# Updating hermes-agent (upstream sync) — safe playbook

How to pull changes from upstream **NousResearch/hermes-agent** into our fork
**PacificInnovation/hermes-agent** without breaking the Railway deploy or our
Kiraku-specific code (the unified Kai gateway ingest + deploy tweaks).

> **Audience:** whoever maintains the Kiraku hermes fork. Read this before any
> `git merge upstream/main`. Several steps here force-update a prod branch —
> follow them exactly; the guards matter.

---

## TL;DR

- Our fork branches from a **tagged upstream release** + a tiny Kiraku delta
  (kai_ingest + Railway deploy commits). It tracks upstream **only via explicit
  merges** — nothing flows in automatically.
- Histories are **related** (shared upstream base), so **`git merge upstream/main`
  works cleanly.** The gap from a release tag is modest, so a merge is the normal,
  manageable path — not a re-fork.
- **Default: don't sync.** If hermes works for acquisitions, leave it.
- Want one upstream fix → **cherry-pick** (Path A).
- Want to modernize wholesale → **merge `upstream/main`** into a sync branch,
  validate on a Railway staging service, then force-with-lease to prod (Path B).
- Stay current afterward → **periodic small merges** (Path C).
- **Never merge onto `railway-deploy` directly** (it's prod). Always sync branch →
  staging → guarded push.

---

## Where we stand

- **Fork base:** upstream **v0.15.1** (`e71a2bd11`, 2026-05-28).
- **Modernized to v0.16.0** on **2026-06-06** via `sync-upstream-20260606`
  (`git merge upstream/main`; the merge that established this doc's Path-B flow).
- **Kiraku delta:** ~10 commits / ~16 files — `gateway/kai_ingest.py` (+ test) and
  the Railway/Cloudflare deploy commits (cont-init, cloudflared sidecar, no-VOLUME,
  single-service Dockerfile). The version/packaging commits were **subsumed by
  upstream** (it adopted the same plugin.yaml bundling), so they drop out on merge.

Re-measure the real gap anytime — **from a FULL clone** (see the warning below):

```bash
git fetch origin --prune --tags && git fetch upstream --prune
BASE=$(git merge-base origin/railway-deploy upstream/main)
git log -1 --format='base: %h %s (%ci)' "$BASE"
echo "behind: $(git rev-list --count "$BASE"..upstream/main)   ahead: $(git rev-list --count "$BASE"..origin/railway-deploy)"
```

> ⚠️ **Shallow-clone trap (this bit us once).** On a shallow clone, `git merge-base`
> returns empty and the counts are garbage — it once reported "10,746 behind /
> unrelated histories," which was **false** (the real gap from v0.15.1 was ~925).
> Always `git fetch --unshallow origin` first; verify with
> `git rev-parse --is-shallow-repository` → `false`. Never plan a sync off shallow numbers.

`railway-deploy` is what Railway builds (`railway.json` → `Dockerfile`). It is **prod**.

---

## The principle: small, frequent, related-history merges

Because the fork shares an upstream base, a straight **`git merge upstream/main`**
brings in only the commits since our base and resolves conflicts **once**, in the
union of files both sides touched (mostly version/packaging + our deploy files).
Keep the gap small (Path C) and each merge stays a quick, low-conflict operation.

Our Kiraku delta is deliberately **new files + one-line hooks** (e.g. kai_ingest is a
new module; run.py gains two lines) so the shared-file conflict surface is tiny.
A linear re-baseline (replay the delta onto fresh upstream) is an option if you want
linear history, but with related history a merge is simpler and is what Path C uses.

---

## Our Kiraku delta (what to replay)

The complete set of Kiraku-specific changes on top of the upstream base. Find the
exact commits with:

```bash
git fetch origin --prune --tags && git fetch upstream --prune
git log --oneline --no-merges upstream/main..origin/railway-deploy
```

1. **Deploy commits** (Railway/Cloudflare specifics):
   - cont-init hook asserting open Slack/workspace access
   - cloudflared sidecar (dashboard WS behind Cloudflare Access)
   - Docker `VOLUME` directive removed (Railway mounts volumes itself)
2. **Kai gateway ingest** (Sprint 24 Phase 2-brains):
   - `gateway/kai_ingest.py` — **NEW file** (HMAC verify + `KaiIngestServer` +
     `maybe_start_ingest`/`stop_ingest`). Expected to apply clean — upstream has no
     such file (unless upstream later adds the same path or moves the gateway layout).
   - `tests/gateway/test_kai_ingest.py` — **NEW file**, same expectation.
   - `gateway/run.py` — **two one-line calls** in `start_gateway`
     (`maybe_start_ingest` after `runner.start()`, `stop_ingest` in teardown).
     This is the *only* shared-file touch, deliberately one line each so a
     re-baseline has almost nothing to reconcile here. All logic lives in the new
     `kai_ingest.py`.
3. **Dashboard trusted-hosts** (`HERMES_DASHBOARD_TRUSTED_HOSTS`, added at the
   v0.19.0 sync): `hermes_cli/web_server.py` gains `_extra_trusted_hosts()` + one
   line in `_is_accepted_host` so a **loopback-bound** dashboard fronted by
   cloudflared accepts the tunnel's public host (`Host`/`Origin`) without a public
   bind or a login. Load-bearing because 0.19.0 removed the `--insecure` bypass
   the old cloudflared-loopback trick relied on. `tests/hermes_cli/test_web_server_host_header.py`
   covers it. **Deploy requires two Railway vars:**
   `HERMES_DASHBOARD_HOST=127.0.0.1` and `HERMES_DASHBOARD_TRUSTED_HOSTS=hermes.kiraku.io`
   (without them the dashboard either refuses to bind `0.0.0.0` without auth, or
   400s the tunnel's Host header). One shared-file touch; re-verify it survives if
   upstream refactors the Host guard.

**Keep it this way.** When adding Kiraku features, prefer **new files** + minimal
one-line hooks into upstream files. Smaller, more isolated delta = cheaper sync.

---

## Path A — cherry-pick (default, for "I want one upstream fix")

```bash
git fetch origin --prune --tags && git fetch upstream --prune
git switch -c pick-<thing> origin/railway-deploy   # clean worktree required
git cherry-pick -x <upstream-sha>                  # -x records the source sha; or A^..B for a range
#   On conflict: edit files → git add <files> → git cherry-pick --continue
#   To bail entirely (restore pre-cherry-pick state): git cherry-pick --abort
#   (git cherry-pick --skip DROPS that commit — only if you mean to.)
python -m pytest tests/gateway/ --timeout-method=thread   # Windows: thread timer (no SIGALRM)
git push -u origin pick-<thing>
gh pr create -R PacificInnovation/hermes-agent --base railway-deploy --head pick-<thing> --fill
```

Surgical. No mass reconciliation. Use this ~90% of the time. (PR merges the normal
way — no force-push.)

---

## Path B — modernize wholesale via merge (what the v0.16.0 sync did)

Merge `upstream/main` into a sync branch, validate on staging, then force-with-lease
to prod. **This force-updates the prod branch at the end — do the staging validation first.**

```bash
git fetch origin --prune --tags && git fetch upstream --prune

# 1. Tag + PUSH the current known-good prod (a LOCAL tag does NOT protect the remote).
TAG=railway-deploy-good-$(date +%Y%m%d-%H%M)
git tag -a "$TAG" origin/railway-deploy -m "Known-good Railway deploy before merge"
git push origin "refs/tags/$TAG"
GOOD_SHA=$(git rev-parse origin/railway-deploy)   # rollback target

# 2. Sync branch off CURRENT prod, then merge upstream.
git switch -c sync-upstream-$(date +%Y%m%d) origin/railway-deploy
git merge --no-edit upstream/main
#    Resolve conflicts — they cluster in version/packaging + our deploy files:
#      - version strings + packaging upstream now owns -> take UPSTREAM
#      - our Kiraku deploy bits (Dockerfile no-VOLUME / cont-init / cloudflared)
#        and kai_ingest -> KEEP ours (these usually AUTO-MERGE; new files never conflict)
#    Resolve each hunk by hand (don't `checkout --theirs` whole files — that drops
#    auto-merged Kiraku lines). Then: git add <resolved> && git commit --no-edit

# 3. RE-VERIFY the kai_ingest contract against the merged (modern) tree — 925 commits
#    can move internals. Confirm in gateway/platforms/slack.py + base.py that
#    _handle_slack_message still takes the INNER event dict, reads team from it,
#    dedups by `ts`, and handle_message still ENQUEUES (spawns a background task).
#    (For v0.16.0 these all held unchanged — kai_ingest needed no edits.)
python -m pytest tests/gateway/test_kai_ingest.py --timeout-method=thread   # Windows: thread timer

# 4. Validate HARD on a Railway STAGING service (see checklist) BEFORE touching prod.
git push -u origin "$(git branch --show-current)"   # point a STAGING service at this branch

# 5. Only after staging is green: force-update prod with a LEASE (never plain --force).
#    --force-with-lease aborts if the remote moved since GOOD_SHA (someone else pushed).
git push --force-with-lease=refs/heads/railway-deploy:"$GOOD_SHA" \
    origin HEAD:refs/heads/railway-deploy
```

Why a merge (not a linear re-baseline): histories share an upstream base, so the
merge brings in only the post-base commits and resolves conflicts **once**. Our
delta is new files + one-line hooks, so the shared-file conflict surface is tiny.
The pushed tag + lease make step 5 reversible. *(Want linear history instead? Replay
the delta onto fresh upstream with `git cherry-pick -x` — same end state, more
conflict points.)*

> **Pre-prod pause:** force-updating `railway-deploy` triggers a Railway **prod**
> deploy if GitHub autodeploy is on. Don't run step 5 until staging is green.

---

## Rollback (if a synced prod deploy is bad)

Two independent options — prefer the Railway one (no git surgery):

1. **Railway dashboard → redeploy the previous deployment.** Railway keeps a
   deployment history with each deployment's source; redeploying the prior one
   restores it regardless of the current branch tip. Fastest, safest.
2. **Git: force the branch back to the known-good tag** (the one you pushed in
   Path B step 1), using a lease against the bad tip:
   ```bash
   git fetch origin --tags
   BAD_SHA=$(git rev-parse origin/railway-deploy)
   git push --force-with-lease=refs/heads/railway-deploy:"$BAD_SHA" \
       origin "refs/tags/$TAG":refs/heads/railway-deploy
   ```
   This is why the tag MUST be pushed before the re-baseline — a local-only tag
   can't restore the remote.

---

## Path C — stay current (after a Path-B re-baseline)

Once re-baselined the gap is small. Keep it small (monthly-ish):

```bash
git fetch origin --prune --tags && git fetch upstream --prune
git switch -c sync-$(date +%Y%m%d) origin/railway-deploy
git merge upstream/main           # small gap = small, manageable merge
python -m pytest tests/gateway/ --timeout-method=thread
git push -u origin sync-$(date +%Y%m%d)
gh pr create -R PacificInnovation/hermes-agent --base railway-deploy --head "$(git branch --show-current)" --fill
```

The 10k pain was *only* the backlog; small, frequent merges stay easy. (Normal PR
merge — no force-push for Path C.)

---

## Safety rails (always)

1. **Never sync onto `railway-deploy` directly.** It's prod. Work on a throwaway
   branch; PR in (Paths A/C) or force-with-lease after staging (Path B).
2. **Base from `origin/railway-deploy`, not local `railway-deploy`** (local may be
   stale). Always `git fetch origin --prune --tags && git fetch upstream --prune` first.
3. **Tag AND push known-good before any force-update.** Local tags don't protect prod.
4. **Force-update only with `--force-with-lease=refs/heads/railway-deploy:<expected>`** —
   never `git push --force` (it silently clobbers concurrent pushes / loses commits).
5. **Test the deploy surface, not just code** — that's where upstream drift bites:
   `Dockerfile`, `docker/s6-rc.d/*`, `main-wrapper.sh`, cloudflared sidecar, `$PORT`,
   Slack `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`, `GATEWAY_BRAIN_SECRET`. Our deploy
   commits + the ingest's `KAI_INGEST_PORT` bind live exactly here.
6. **Deploy to a Railway STAGING service first.** Don't swap prod until staging is green.
7. **Keep the Kiraku delta isolated** (new files + one-line hooks) — that's what
   makes re-baseline cheap.
8. **`gh pr create` MUST use `-R PacificInnovation/hermes-agent --base railway-deploy`** —
   this is a fork of NousResearch, so gh otherwise defaults the PR to the upstream
   parent (and `main`, not the deployed branch). Push the branch first (`git push -u`).

---

## Staging test checklist (before swapping prod)

- [ ] `docker build -t hermes-agent-sync .` succeeds from the synced tree.
- [ ] Container boots under s6 (`/init` → `main-wrapper.sh` → `gateway run`); no
      service in a crash loop.
- [ ] Slack connects (Socket Mode) and the bot answers a test `@Kai` in a staging
      channel.
- [ ] cloudflared tunnel comes up (if the dashboard is exercised).
- [ ] Kai ingest reachable on `KAI_INGEST_PORT` (a dedicated port, NOT `$PORT` —
      the dashboard owns `$PORT`; set `KAI_INGEST_PORT` + expose it to enable):
      `GET /health` → 200; `POST /ingest/slack`
      with no secret set → **503** (fail-closed). With `GATEWAY_BRAIN_SECRET` set +
      a valid signed envelope → **200 dispatched**.
- [ ] `python -m pytest tests/gateway/ --timeout-method=thread` green.
- [ ] `python -m ruff check gateway/ tests/` clean.

---

## When NOT to sync

- hermes works for acquisitions and you don't need a specific upstream feature →
  **leave it frozen.** A sync you don't need is pure risk.
- You only want a Kiraku change (ingest tweak, deploy fix) → that's a normal
  branch → PR to `railway-deploy`. No upstream involved.

The fork is the Kiraku product line, not a tracking mirror. Treat it that way.

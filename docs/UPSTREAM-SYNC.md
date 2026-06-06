# Updating hermes-agent (upstream sync) — safe playbook

How to pull changes from upstream **NousResearch/hermes-agent** into our fork
**PacificInnovation/hermes-agent** without breaking the Railway deploy or our
Kiraku-specific code (the unified Kai gateway ingest + deploy tweaks).

> **Audience:** whoever maintains the Kiraku hermes fork. Read this before any
> `git merge upstream/main`. Several steps here force-update a prod branch —
> follow them exactly; the guards matter.

---

## TL;DR

- Our fork is a **frozen old snapshot** of upstream + a tiny Kiraku delta. It does
  **not** track upstream; nothing flows in automatically.
- **Default: don't sync.** If hermes works for acquisitions, leave it. Frozen is fine.
- **Never `git merge upstream/main` onto `railway-deploy` directly** — it's a 10k+
  commit storm and `railway-deploy` is prod.
- Want one upstream fix → **cherry-pick** (Path A).
- Want to modernize wholesale → **re-baseline** by replaying our small delta onto
  fresh upstream (Path B), validated on a Railway staging service, once.
- Our delta is intentionally tiny + isolated so re-baseline is cheap (see below).

---

## Where we stand (the numbers)

`railway-deploy` vs `upstream/main` (NousResearch), as of 2026-06-06:

```
ahead  (our commits not upstream):        3   (+ the kai_ingest PR)
behind (upstream commits not in ours): 10,746
```

So `railway-deploy` = an **old upstream snapshot** + 3 PacificInnovation deploy
commits + the Kai ingest. Upstream has moved ~10.7k commits since our base.
A naive merge would drag all of that in at once.

`railway-deploy` is what Railway builds (`railway.json` → `Dockerfile`). It is **prod**.

---

## The principle: replay OUR delta, don't merge THEIRS

Merging `upstream/main` *into* our old base = reconcile 10,746 commits against our
snapshot = conflict storm + a huge untested surface change.

**Invert it.** Our Kiraku delta is small and isolated. Replay those few changes
*onto* fresh upstream. You reconcile ~a handful of changes, not ten thousand.

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

## Path B — re-baseline (modernize wholesale, once)

Replays our delta onto modern upstream instead of merging upstream onto our base.
**This force-updates the prod branch at the end — do the staging validation first.**

```bash
git fetch origin --prune --tags && git fetch upstream --prune

# 1. Tag + PUSH the current known-good prod (a LOCAL tag does NOT protect the remote).
TAG=railway-deploy-good-$(date +%Y%m%d-%H%M)
git tag -a "$TAG" origin/railway-deploy -m "Known-good Railway deploy before re-baseline"
git push origin "refs/tags/$TAG"
GOOD_SHA=$(git rev-parse origin/railway-deploy)   # remember this; it's the rollback target

# 2. Start fresh from modern upstream.
git switch -c rebaseline upstream/main

# 3. Replay the Kiraku delta (small!): deploy commits, then the ingest commits.
git cherry-pick -x <deploy-commit-shas...>
git cherry-pick -x <kai_ingest-commit-shas...>
#    Conflicts only where upstream moved the files we touch (run.py start_gateway
#    region, Dockerfile, s6 scripts). kai_ingest.py/test apply clean unless upstream
#    added those paths. On conflict: resolve → git add → git cherry-pick --continue.

# 4. Validate HARD on a Railway STAGING service (see checklist) BEFORE touching prod.
git push -u origin rebaseline    # point a STAGING Railway service at this branch

# 5. Only after staging is green: force-update prod with a LEASE (never plain --force).
#    --force-with-lease aborts if the remote moved since GOOD_SHA (someone else pushed).
git push --force-with-lease=refs/heads/railway-deploy:"$GOOD_SHA" \
    origin rebaseline:refs/heads/railway-deploy
```

Why it's safe: you reconcile our ~handful of changes, not upstream's 10k. The
ingest is new files (no conflict); only the run.py one-liners + the Docker/s6
deploy bits might need a re-apply against upstream's newer structure. The lease +
pushed tag make step 5 reversible.

> **Pre-prod pause:** force-updating `railway-deploy` triggers a Railway **prod**
> deploy if GitHub autodeploy is on. Don't run step 5 until staging is green and
> you're ready for prod to redeploy.

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
   commits + the ingest's `$PORT` bind live exactly here.
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
- [ ] Kai ingest reachable on `$PORT`: `GET /health` → 200; `POST /ingest/slack`
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

# Credential Exposure Incident — 2026-08-05

**Status:** Working tree contained. Revocation and remote cleanup PENDING USER ACTION.
**Severity:** Critical.
**Discovered:** During the Phase 0 pre-commit secret scan of the Security MVP implementation.

---

## 1. Summary

Three distinct GitHub Personal Access Tokens were found hardcoded across the project. All three must
be treated as **permanently compromised**. One of them was published to a public website.

The tokens carried write access to the GitHub Gist that served as the product's licensing allowlist,
which means possession of any of them permits arbitrary modification of licence state — free
activation for anyone, or denial of service against every paying customer simultaneously.

---

## 2. Tokens and locations

Tokens are referred to by label. Full values are deliberately not recorded here.

### PAT-A — `ghp_lBd…Gbhv`

| Location | Kind | Status |
|---|---|---|
| `Working/net_verify.py:4` | Working tree | **Removed** → now reads `STOPLOSS_GIST_TOKEN` env var |
| `P1/stoploss_mt4.py`, `P1/stoploss_mt5.py` (×2 each) | Committed history + **shipped source** | In history; P1 retired |
| `P2/stoploss_mt4.py`, `P2/stoploss_mt5.py` (×2 each) | Committed history + **shipped source** | In history; P2 retired |
| `add_session_file.py` | Committed history | In history |
| Commit `d253b56`, **pushed** to `origin/main` | Git history | **STILL CONTAMINATED** |

Used as `_SESSION_GIST_TOKEN` / `GIST_TOKEN` for authenticated `api.github.com` PATCH calls against
gist `8a8b52dc14c0ecca38121df01557ec99` (`approved_ids.txt`, `active_sessions.txt`).

### PAT-B — `ghp_1xk…weXS`

| Location | Kind | Status |
|---|---|---|
| `Working/deploy_clean.ps1:2` | Working tree | **Removed** → script marked DEPRECATED, refuses to run |
| `Dead/…/P1/web/admin_dashboard.html` (`const _TX=[…]`) | XOR key=11, trivially reversible | **Neutralised** (`_TX=[]`) |
| `stoploss-site` repo → `p1_admin.html` | **PUBLIC GitHub Pages site** | **STILL LIVE — USER ACTION REQUIRED** |

XOR obfuscation with a fixed key is encoding, not encryption. `update_admin_token.py` itself decodes
and prints the token. Anyone who loaded the public dashboard page could recover this token from view-source
in under a minute.

### PAT-C — `ghp_te1…VJF3`

| Location | Kind | Status |
|---|---|---|
| `Working/push_to_github.bat:68` | Working tree, embedded in remote URL | **Removed** → uses credential helper |
| `.git/config` `remote.origin.url` (fetch + push) | Local config | **Removed** |

---

## 3. Actions completed (automated)

1. Verified backup of product source taken **before** any modification:
   `_BACKUP_pre_containment_20260805/StopLossPro_source_20260805.tar.gz` — gzip integrity verified,
   32 files, all key sources present.
2. `net_verify.py` — hardcoded PAT replaced with `os.environ['STOPLOSS_GIST_TOKEN']`; exits with a
   clear error if unset.
3. `push_to_github.bat` — credential stripped from the remote URL; documented Git Credential Manager
   and `gh auth login` as the correct developer-side mechanisms.
4. `deploy_clean.ps1` — credential removed; script marked DEPRECATED and hard-fails.
5. `admin_dashboard.html` — `_TX` token array emptied and annotated.
6. `.git/config` — both fetch and push remote URLs rewritten without credentials.
7. `.gitignore` — hardened against `.env`, private keys, signing material, cloud credentials, backups,
   licence caches, and the retired `Dead/` and `Historical/` trees.
8. Full-project secret sweep: no AWS keys, no private key blocks, no other API credentials found.
9. Clean orphan-branch checkpoint created with a pre-commit secret gate.

---

## 4. ACTION REQUIRED BY USER — cannot be automated

These require an authenticated GitHub session and are yours to perform.

### 4.1 Revoke all three tokens — do this first

<https://github.com/settings/tokens> → revoke **PAT-A**, **PAT-B**, **PAT-C**.

Revoke all three even though only one was publicly served. They share a blast radius and there is no
benefit to keeping any of them alive.

### 4.2 Take down the public dashboard

`p1_admin.html` in the `stoploss-site` repository is served publicly and contains PAT-B. Delete the
file (and, since P1 is retired, ideally the deployment entirely). Note that revoking PAT-B in step 4.1
already defuses it — this step stops it being served at all.

### 4.3 Audit the account for damage

Under <https://github.com/settings/security-log>, check for unexpected activity — unrecognised gist
edits, unfamiliar IPs, new tokens or SSH keys you did not create. Also open gist
`8a8b52dc14c0ecca38121df01557ec99` and confirm `approved_ids.txt` contains only machine IDs you
actually authorised.

---

## 5. Git history — still contaminated

Commit `d253b56` contains PAT-A in six files and **has been pushed** to
`github.com/Wave-rock-investments/stoploss-app`. Removing a secret from the current working tree does
not remove it from history.

The new `phase0-clean-baseline` branch is an **orphan** — it has no parent commits, so none of the
contaminated history is reachable from it. The old `main` remains locally, untouched, pending your
decision.

### Recommended: delete and recreate the remote

Recommended over history rewriting, for reasons specific to this repository:

- The repo has only two commits, both of which are retired P1/P2 code you have decided to abandon.
- `Working/` — the actual product — was **never committed**, so no product history is lost.
- `git filter-repo` rewrites history but leaves the original objects recoverable through GitHub's
  cached views and any existing forks or clones. Deletion is definitive.
- Recreating is a two-minute operation versus a fiddly rewrite plus force-push plus GC request.

Exact migration, to run only after the tokens are revoked:

```
# 1. Confirm the clean baseline is what you want to keep
git checkout phase0-clean-baseline
git log --oneline            # expect exactly one commit, no P1/P2 history

# 2. Delete the remote repository via the GitHub UI
#    Settings → General → Danger Zone → Delete this repository

# 3. Create a fresh EMPTY repository of the same name (or a new one), PRIVATE

# 4. Authenticate without embedding a token
gh auth login                # or: git config --global credential.helper manager

# 5. Point the local repo at the new remote and push the clean branch as main
git remote set-url origin https://github.com/<owner>/<new-repo>.git
git branch -M phase0-clean-baseline main
git push -u origin main

# 6. Once verified, delete the contaminated local branch
git branch -D <old-main-branch-name>
```

**Alternative if the repository must be preserved:** `git filter-repo --replace-text` with the token
values, then force-push, then ask GitHub Support to garbage-collect unreachable objects. More work,
weaker guarantee. Not recommended here.

---

## 6. Are the distributed binaries compromised?

**Assume yes for P1 and P2.**

PAT-A was present in `P1/stoploss_mt5.py` and `P2/stoploss_mt5.py` at `HEAD` — the same source compiled
into the binaries shipped on 2026-07-21. PyInstaller bundles Python bytecode into the executable;
`pyinstxtractor` plus a decompiler recovers embedded string constants in minutes.

A raw `grep` of `StopLoss-P1.zip` and `StopLoss-P2.zip` returned zero matches, but that is **not**
evidence of absence — PyInstaller compresses the bytecode archive, so plaintext scanning cannot detect
it. Treat every P1/P2 binary already in customer hands as carrying a live gist-write credential until
the token is revoked.

Revoking PAT-A neutralises this entirely and is the reason step 4.1 is urgent.

**StopLossPro (current product): not affected by this vector.** Its client never held a PAT — it wrote
through the Cloudflare Worker precisely to avoid embedding one. That design decision was correct. The
Worker's own authorization weakness is a separate finding (F-1) addressed in the Security MVP.

---

## 7. Standing rules going forward

1. No credential is ever hardcoded in source, config, build scripts, or client artefacts.
2. Developer-side credentials live in environment variables or the OS keychain, never on disk in the repo.
3. Git authenticates via credential helper or `gh`, never via a token in a remote URL.
4. Obfuscation is not protection. XOR, base64 and hex encoding are all reversible in seconds.
5. The production licensing architecture must never require a GitHub PAT inside the customer application.
   Authority moves server-side; the client holds only a public verification key.
6. Run the pre-commit secret gate before every checkpoint.

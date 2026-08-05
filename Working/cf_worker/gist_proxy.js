/**
 * gist_proxy.js — Cloudflare Worker
 *
 * Forwards Gist PATCH requests from StopLossCalc client EXEs to the GitHub API.
 * The GitHub PAT (GIST_TOKEN) lives only in CF environment secrets — never
 * embedded in client code or EXEs.
 *
 * Request flow:
 *   Client EXE  →  POST https://<worker>.workers.dev  →  PATCH GitHub API
 *
 * CF Environment Secrets (set via dashboard or `wrangler secret put`):
 *   GIST_TOKEN  — GitHub PAT with `gist` scope (read + write)
 *
 * Deploy steps:
 *   1.  Log in to https://dash.cloudflare.com → Workers & Pages → Create
 *   2.  Name it  stoploss-gist-proxy
 *   3.  Paste this script in the editor → Deploy
 *   4.  Go to Settings → Variables → Add Secret: GIST_TOKEN = <your PAT>
 *   5.  Copy the  *.workers.dev  URL
 *   6.  Update _GIST_PROXY_URL in archive/stoplosspro/lib/constants.py
 *       and  archive/lib/constants.py  with that URL, then rebuild stoplosspro.zip
 */

const GIST_ID    = '8a8b52dc14c0ecca38121df01557ec99';
const GITHUB_API = `https://api.github.com/gists/${GIST_ID}`;

export default {
  async fetch(request, env) {

    // ── Method gate ────────────────────────────────────────────────────────
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    // ── User-Agent check (noise reduction, not a security gate) ───────────
    const ua = request.headers.get('User-Agent') || '';
    if (!ua.startsWith('StopLossCalc/')) {
      return new Response('Forbidden', { status: 403 });
    }

    // ── Read + validate JSON body ──────────────────────────────────────────
    let body;
    try {
      body = await request.text();
      JSON.parse(body);   // throws on malformed JSON
    } catch {
      return new Response('Bad Request: invalid JSON', { status: 400 });
    }

    // ── Guard: secret must be configured ──────────────────────────────────
    if (!env.GIST_TOKEN) {
      console.error('[gist_proxy] GIST_TOKEN secret not set');
      return new Response('Internal Server Error: proxy misconfigured', { status: 500 });
    }

    // ── Validate body only touches allowed files ───────────────────────────
    // Prevent rogue clients from overwriting unrelated gist files
    const ALLOWED_FILES = new Set([
      'pending_txns.txt',
      'used_txns.txt',
      'approved_ids.txt',
      'revoked_ids.txt',
      'active_sessions.txt',   // single-session enforcement heartbeats
    ]);
    try {
      const parsed = JSON.parse(body);
      const fileKeys = Object.keys(parsed.files || {});
      for (const k of fileKeys) {
        if (!ALLOWED_FILES.has(k)) {
          return new Response(`Forbidden: unknown file ${k}`, { status: 403 });
        }
      }
    } catch {
      return new Response('Bad Request', { status: 400 });
    }

    // ── Forward to GitHub API ──────────────────────────────────────────────
    let ghResp;
    try {
      ghResp = await fetch(GITHUB_API, {
        method:  'PATCH',
        headers: {
          'Authorization':        `token ${env.GIST_TOKEN}`,
          'Content-Type':         'application/json',
          'Accept':               'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent':           'stoploss-gist-proxy/1',
        },
        body,
      });
    } catch (e) {
      console.error('[gist_proxy] GitHub fetch error:', e.message);
      return new Response('Bad Gateway', { status: 502 });
    }

    // ── Return GitHub's status + body ──────────────────────────────────────
    const respBody = await ghResp.text();
    return new Response(respBody, {
      status:  ghResp.status,
      headers: { 'Content-Type': 'application/json' },
    });
  },
};

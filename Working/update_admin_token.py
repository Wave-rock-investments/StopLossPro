#!/usr/bin/env python3
"""
update_admin_token.py
Usage: python update_admin_token.py <new_github_pat>
Updates admin_dashboard.html + redeploys to GitHub Pages.
"""
import sys, re, subprocess, os

def encode_token_xor(token, key=11):
    return [ord(c) ^ (i % key) for i, c in enumerate(token)]

def update_dashboard(new_token):
    DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), 'P1', 'web', 'admin_dashboard.html')
    if not os.path.exists(DASHBOARD_PATH):
        print(f"ERROR: {DASHBOARD_PATH} not found"); return False

    content = open(DASHBOARD_PATH, encoding='utf-8').read()

    # Find existing _TX array
    match = re.search(r'const _TX=\[([^\]]+)\]', content)
    if not match:
        print("ERROR: _TX array not found in admin_dashboard.html"); return False

    old_encoded = list(map(int, match.group(1).split(',')))
    old_decoded = ''.join(chr(c ^ (i % 11)) for i, c in enumerate(old_encoded))
    print(f"Old token: {old_decoded[:8]}...{old_decoded[-4:]}")

    new_encoded = encode_token_xor(new_token)
    new_tx = '[' + ','.join(map(str, new_encoded)) + ']'
    new_content = content.replace(
        f'const _TX=[{match.group(1)}]',
        f'const _TX={new_tx}'
    )

    if new_content == content:
        print("ERROR: replacement failed"); return False

    open(DASHBOARD_PATH, 'w', encoding='utf-8').write(new_content)
    print(f"✓ Updated {DASHBOARD_PATH}")
    print(f"  New token: {new_token[:8]}...{new_token[-4:]}")

    # Verify decode
    verify = ''.join(chr(c ^ (i % 11)) for i, c in enumerate(new_encoded))
    assert verify == new_token, "Token encode/decode mismatch!"
    print(f"✓ Encode/decode verified")
    return True

def deploy(new_token):
    """Deploy to GitHub Pages via git commit + push."""
    SITE_DIR = os.path.join(os.path.dirname(__file__))
    DASHBOARD_SRC = os.path.join(SITE_DIR, 'P1', 'web', 'admin_dashboard.html')
    
    # Find stoploss-site git repo
    REPO_DIR = os.path.join(SITE_DIR, '..', 'stoploss-site')
    if not os.path.exists(REPO_DIR):
        REPO_DIR = SITE_DIR  # try current dir
    
    # Copy updated dashboard to stoploss-site
    import shutil
    targets = []
    for root, dirs, files in os.walk(SITE_DIR):
        for f in files:
            if f == 'p1_admin.html':
                targets.append(os.path.join(root, f))
    
    for t in targets:
        shutil.copy2(DASHBOARD_SRC, t)
        print(f"✓ Copied to {t}")
    
    print("\nTo deploy, run:")
    print("  cd <stoploss-site repo>")
    print("  git add p1_admin.html")
    print("  git commit -m 'fix: refresh admin token'")
    print(f"  git push")
    print("\nOr use the deploy_now.py script if available.")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python update_admin_token.py <new_github_pat>")
        print("\nTo get a new token:")
        print("1. Go to: https://github.com/settings/tokens")
        print("   (logged in as Wave-rock-investments)")
        print("2. Generate new classic token")
        print("3. Scope: gist (only)")
        print("4. Expiry: No expiration")
        print("5. Copy the token → run this script")
        sys.exit(1)
    
    new_token = sys.argv[1].strip()
    if not new_token.startswith('ghp_'):
        print("ERROR: token must start with 'ghp_'"); sys.exit(1)
    
    if update_dashboard(new_token):
        deploy(new_token)
        print("\n✓ Done — push the repo to redeploy admin dashboard")

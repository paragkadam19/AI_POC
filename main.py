# main.py
# main.py — must be the FIRST thing, before other imports
import warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

import requests
_orig_merge = requests.Session.merge_environment_settings
def _merge_no_verify(self, url, proxies, stream, verify, cert):
    settings = _orig_merge(self, url, proxies, stream, verify, cert)
    settings["verify"] = False
    return settings
requests.Session.merge_environment_settings = _merge_no_verify

# ... rest of your existing main.py imports/code below
import os
from app import app

if __name__ == "__main__":
    host  = os.getenv("FLASK_HOST", "0.0.0.0")
    port  = int(os.getenv("FLASK_PORT", "5000"))
    # Debug now defaults OFF (it was hardcoded True before). The app has
    # real per-user logins and data isolation to protect now, and Flask's
    # debug mode exposes an interactive, code-executing debugger in the
    # browser on any unhandled exception. Set FLASK_DEBUG=1 for local dev.
    debug = os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"}

    print("=" * 60)
    print("  Data Resonance POC Console")
    print(f"  Open: http://localhost:{port}")
    print("=" * 60)

    if debug and host not in {"127.0.0.1", "localhost"}:
        print(
            "  WARNING: FLASK_DEBUG is on and the app is bound to "
            f"{host}, not just localhost. The Werkzeug debugger allows\n"
            "  arbitrary code execution to anyone who can reach this port. "
            "Set FLASK_HOST=127.0.0.1 or FLASK_DEBUG=0\n  before exposing "
            "this beyond your own machine."
        )
        print("=" * 60)

    app.run(host=host, port=port, debug=debug)

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
from app import app

if __name__ == "__main__":
    print("=" * 60)
    print("  Data Quality POC Console")
    print("  Open: http://localhost:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
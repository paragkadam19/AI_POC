#!/bin/bash
# setup.sh  —  One-shot environment setup for the Bedrock POC suite
# Run this ONCE before running any POC.
# Usage:  bash setup.sh

set -e   # exit on first error

echo ""
echo "=================================================="
echo "  AWS Bedrock Data Engineering POC — Setup"
echo "=================================================="

# ── Check Python ─────────────────────────────────────────────
echo ""
echo "Step 1: Checking Python version..."
python3 --version || { echo "ERROR: Python 3 not found. Install from python.org"; exit 1; }

# ── Virtual environment ───────────────────────────────────────
echo ""
echo "Step 2: Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created venv/"
else
    echo "  venv/ already exists — skipping"
fi

# ── Activate ──────────────────────────────────────────────────
echo ""
echo "Step 3: Activating virtual environment..."
source venv/bin/activate
echo "  Activated. Python: $(which python)"

# ── Install dependencies from requirements.txt ───────────────
echo ""
echo "Step 4: Installing dependencies from requirements.txt..."
if [ ! -f "requirements.txt" ]; then
    echo "  ERROR: requirements.txt not found in $(pwd)"
    exit 1
fi
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo ""
echo "  Installed packages:"
pip show boto3 flask duckdb polars | grep -E "^(Name|Version)"
echo "  All dependencies installed."

# ── Check AWS credentials ─────────────────────────────────────
echo ""
echo "Step 5: Checking AWS credentials..."
if aws sts get-caller-identity > /dev/null 2>&1; then
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    REGION=$(aws configure get region || echo "us-east-1")
    echo "  AWS account : $ACCOUNT"
    echo "  Region      : $REGION"
    if [ "$REGION" != "us-east-1" ]; then
        echo "  WARNING: Bedrock Claude models have broadest availability in us-east-1"
        echo "           If you get AccessDeniedException, change region to us-east-1:"
        echo "           aws configure set region us-east-1"
    fi
else
    echo ""
    echo "  AWS credentials NOT found."
    echo "  Run the following and enter your IAM Access Key + Secret:"
    echo ""
    echo "      aws configure"
    echo ""
    echo "  Then re-run:  bash setup.sh"
    exit 1
fi

# ── Test Bedrock access ───────────────────────────────────────
echo ""
echo "Step 6: Testing Bedrock access..."
python3 - << 'PYEOF'
import boto3, json, sys

client = boto3.client("bedrock-runtime", region_name="us-east-1")
try:
    resp = client.invoke_model(
        modelId     = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        body        = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "Reply: BEDROCK_OK"}]
        }),
        contentType = "application/json",
        accept      = "application/json",
    )
    text = json.loads(resp["body"].read())["content"][0]["text"]
    if "BEDROCK_OK" in text or len(text) > 3:
        print("  Bedrock API: CONNECTED  (Claude Haiku responded)")
    else:
        print(f"  Bedrock API: unexpected response: {text}")
except Exception as e:
    err = str(e)
    if "AccessDenied" in err or "not authorized" in err.lower():
        print("  ERROR: Model access not enabled.")
        print("  Go to: AWS Console → Amazon Bedrock → Model access")
        print("  Enable: Claude Sonnet 4.6 and Claude Haiku 4.5")
        sys.exit(1)
    elif "Could not connect" in err or "EndpointResolutionError" in err:
        print("  ERROR: Cannot reach Bedrock endpoint. Check internet / VPN.")
        sys.exit(1)
    else:
        print(f"  ERROR: {err}")
        sys.exit(1)
PYEOF

echo ""
echo "=================================================="
echo "  Setup complete!  You are ready to run the POCs."
echo "=================================================="
echo ""
echo "  Quick start:"
echo "    source venv/bin/activate"
echo ""
echo "  Run all POCs (CLI):"
echo "    python run_manufacturing.py"
echo ""
echo "  Run a single POC:"
echo "    python run_manufacturing.py --poc 1"
echo ""
echo "  Run the Web UI:"
echo "    python app.py"
echo "    Open: http://localhost:5000"
echo ""
# main.py
from app import app

if __name__ == "__main__":
    print("=" * 60)
    print("  Data Quality POC Console")
    print("  Open: http://localhost:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
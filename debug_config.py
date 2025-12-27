from app import app, db, SystemConfig
import json

with app.app_context():
    configs = SystemConfig.query.all()
    print("\n--- SystemConfig Audit ---")
    for c in configs:
        val = c.value if c.value else "EMPTY"
        display_val = val[:10] + "..." + val[-10:] if len(val) > 20 else val
        print(f"KEY: {c.key:<25} | VALUE: {display_val}")
    print("--- End Audit ---\n")

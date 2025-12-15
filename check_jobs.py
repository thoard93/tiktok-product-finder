from app import app, db, ScanJob, ApiKey
import json
from datetime import datetime


with app.app_context():
    print(f">> USING DB: {app.config['SQLALCHEMY_DATABASE_URI']}")
    p_count = 0
    try:
        from app import Product
        p_count = Product.query.count()
        print(f">> Products in DB: {p_count}")
    except:
        print(">> Could not count products")

    # Get last 15 jobs
    jobs = ScanJob.query.order_by(ScanJob.created_at.desc()).limit(15).all()
    
    print(f"--- Found {len(jobs)} recent jobs ---")
    for j in jobs:
        # Parse result for brevity
        res_summary = "N/A"
        if j.result_json:
            try:
                r = json.loads(j.result_json)
                res_summary = f"Success: {r.get('success')}, Msg: {r.get('message') or r.get('error')}"
            except:
                res_summary = j.result_json[:50]
                
        print(f"GenericID: {j.id[:8]} | Status: {j.status} | Created: {j.created_at.strftime('%H:%M:%S')} | Input: {j.input_query[:40]} | Result: {res_summary}")

    # Also check API Key credits for the bridge (if any)
    # Bridge jobs usually have api_key_id=None
    
    # Check if worker seems to be running?
    # We can't easily check for the thread, but if we see 'processing' jobs that are old, it's stuck.
    
    queued = ScanJob.query.filter_by(status='queued').count()
    processing = ScanJob.query.filter_by(status='processing').count()
    failed = ScanJob.query.filter_by(status='failed').count()
    print(f"\n--- Summary ---")
    print(f"Queued: {queued}")
    print(f"Processing: {processing}")
    print(f"Failed: {failed}")

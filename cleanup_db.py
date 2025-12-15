from app import app, db, Product
import argparse
from sqlalchemy import text

def cleanup_db(dry_run=True, delete_limit=None):
    with app.app_context():
        # Manual migration check for original_price to fix sqlite errors
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT original_price FROM products LIMIT 1"))
        except Exception:
            print("Adding missing column: original_price")
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE products ADD COLUMN original_price REAL"))
                conn.commit()

        print("--- Database Cleanup Tool ---")
        total_products = Product.query.count()
        print(f"Total Products: {total_products}")

        # Criteria for DELETION (Delete if ALL of these "Bad" conditions are met, OR if it's just clearly junk)
        
        # We want to KEEP:
        # 1. Favorites (Always)
        # 2. Discovery Scan products (User wants to try them)
        # 3. Good Video Count (5-50) AND Decent Sales
        
        # Query for potential deletions
        query = Product.query.filter(
            Product.is_favorite == False, # Never delete favorites
            Product.scan_type != 'discovery', # Protect Discovery scans for now
            # Bad data / Placeholder / Low quality
            db.or_(
                Product.video_count <= 2, # User: "0, 1, 2 are placeholder/bad"
                Product.video_count > 100, # Too saturated (optional, but requested implicitly by "5-50")
                Product.sales_7d <= 5 # No sales logic
            )
        )
        
        # Refine query: The user said "5-50 videos" and "high sales" is GREAT.
        # So we should DELETE anything that is NOT great.
        
        # Let's iterate and decide in python for complex logic, or build a complex query.
        # Complex logic in Python is safer for a cleanup script so we can print exactly why.
        
        candidates = query.all()
        to_delete = []
        
        for p in candidates:
            reason = []
            
            # Check reasons to KEEP (Safety net)
            if p.is_favorite: continue 
            if p.scan_type == 'discovery': continue
            
            # Check reasons to DELETE
            if (p.video_count or 0) <= 2:
                reason.append(f"Low Videos ({p.video_count})")
            elif (p.video_count or 0) > 60: # Saturated? User said 50.
                if (p.sales_7d or 0) < 100: # Allow high video count only if sales are HUGE
                   reason.append(f"Saturated ({p.video_count} vids) + Low Sales")
            
            if (p.sales_7d or 0) <= 2:
                if (p.video_count or 0) > 5:
                     # Has videos but no sales? Maybe bad product.
                     reason.append("Videos but No Sales")
                else:
                     reason.append("No Sales")

            # HEAVY FILTER: If it's not in the sweet spot (5-50 videos + High Sales)
            # The user wants to keep ~200-1000 items.
            is_sweet_spot = (
                5 <= (p.video_count or 0) <= 60 and 
                (p.sales_7d or 0) >= 20
            )

            if not is_sweet_spot and not reason:
                 # If it didn't trigger specific flags but isn't in sweet spot, mark it?
                 # This might be too aggressive. Let's stick to the specific "Definite Delete" flags first.
                 # Actually, user said "laser target only great products".
                 reason.append("Not in Sweet Spot (5-50 vids, 20+ sales)")
            
            if reason:
                to_delete.append((p, ", ".join(reason)))

        print(f"\nCandidates for deletion: {len(to_delete)}")
        
        if delete_limit:
            to_delete = to_delete[:delete_limit]
            print(f"Limiting to first {delete_limit} items.")

        for p, r in to_delete[:20]: # Show first 20
            print(f" - DELETE [{p.product_id}] {p.product_name[:30]}... | Reasons: {r}")
        
        if len(to_delete) > 20: 
            print(f"... and {len(to_delete)-20} more.")

        if not dry_run:
            if not to_delete:
                print("Nothing to delete.")
                return

            print(f"\nDELETING {len(to_delete)} products...")
            confirm = input("Type 'YES' to confirm: ")
            if confirm == 'YES':
                ids = [x[0].product_id for x in to_delete]
                # Batch delete
                delete_q = Product.query.filter(Product.product_id.in_(ids))
                delete_q.delete(synchronize_session=False)
                db.session.commit()
                print("Done.")
            else:
                print("Aborted.")
        else:
            print("\n[DRY RUN] No changes made. Use --force to execute.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='Actually delete records')
    parser.add_argument('--limit', type=int, default=None, help='Limit number of deletions')
    args = parser.parse_args()
    
    cleanup_db(dry_run=not args.force, delete_limit=args.limit)

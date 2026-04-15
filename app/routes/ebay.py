"""
Price Blade — eBay Market Research Routes (admin-only)
"""

import json
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, jsonify, session
from app import db
from app.models import EbayWatchlistItem, EbaySearchHistory
from app.routes.auth import login_required, admin_required, get_current_user

ebay_bp = Blueprint('ebay', __name__)


@ebay_bp.route('/ebay')
@login_required
@admin_required
def ebay_dashboard():
    recent = EbaySearchHistory.query.order_by(EbaySearchHistory.searched_at.desc()).limit(10).all()
    watchlist = EbayWatchlistItem.query.order_by(EbayWatchlistItem.created_at.desc()).all()
    return render_template('ebay/dashboard.html', recent=recent, watchlist=watchlist)


@ebay_bp.route('/ebay/analyze', methods=['POST'])
@login_required
@admin_required
def ebay_analyze():
    keywords = request.form.get('keywords', '').strip()
    if not keywords:
        return redirect('/ebay')

    from app.services.ebay import analyze_ebay_product
    results = analyze_ebay_product(keywords)

    # Save to search history
    history = EbaySearchHistory(
        query=keywords,
        avg_sold_price=results.get('avg_sold_price'),
        lowest_bin=results.get('lowest_bin'),
        sell_through_rate=results.get('sell_through_rate'),
        active_listings=results.get('active_listings'),
        sold_count=results.get('sold_last_30'),
        raw_results=json.dumps(results.get('recent_sold', [])[:10], default=str),
    )
    db.session.add(history)
    db.session.commit()

    return render_template('ebay/results.html', r=results)


@ebay_bp.route('/ebay/watchlist/add', methods=['POST'])
@login_required
@admin_required
def ebay_watchlist_add():
    keywords = request.form.get('keywords', '')
    item = EbayWatchlistItem(
        title=request.form.get('title', keywords)[:500],
        keywords=keywords[:500],
        avg_sold_price=float(request.form.get('avg_sold_price', 0) or 0),
        lowest_bin=float(request.form.get('lowest_bin', 0) or 0),
        sell_through_rate=float(request.form.get('sell_through_rate', 0) or 0),
        active_listings=int(request.form.get('active_listings', 0) or 0),
        sold_last_30=int(request.form.get('sold_last_30', 0) or 0),
        recommended_price=float(request.form.get('recommended_price', 0) or 0),
        notes=request.form.get('notes', ''),
        last_checked=datetime.utcnow(),
    )
    db.session.add(item)
    db.session.commit()
    return redirect('/ebay')


@ebay_bp.route('/ebay/watchlist/refresh/<int:item_id>', methods=['POST'])
@login_required
@admin_required
def ebay_watchlist_refresh(item_id):
    item = EbayWatchlistItem.query.get_or_404(item_id)

    from app.services.ebay import analyze_ebay_product
    results = analyze_ebay_product(item.keywords)

    item.avg_sold_price = results.get('avg_sold_price')
    item.lowest_bin = results.get('lowest_bin')
    item.sell_through_rate = results.get('sell_through_rate')
    item.active_listings = results.get('active_listings')
    item.sold_last_30 = results.get('sold_last_30')
    item.recommended_price = results.get('recommended_price')
    item.last_checked = datetime.utcnow()
    db.session.commit()

    return redirect('/ebay')


@ebay_bp.route('/ebay/watchlist/remove/<int:item_id>', methods=['POST'])
@login_required
@admin_required
def ebay_watchlist_remove(item_id):
    item = EbayWatchlistItem.query.get(item_id)
    if item:
        db.session.delete(item)
        db.session.commit()
    return redirect('/ebay')


@ebay_bp.route('/ebay/rerun/<int:history_id>')
@login_required
@admin_required
def ebay_rerun(history_id):
    h = EbaySearchHistory.query.get_or_404(history_id)
    from app.services.ebay import analyze_ebay_product
    results = analyze_ebay_product(h.query)

    # Update history
    h.avg_sold_price = results.get('avg_sold_price')
    h.sell_through_rate = results.get('sell_through_rate')
    h.active_listings = results.get('active_listings')
    h.sold_count = results.get('sold_last_30')
    h.searched_at = datetime.utcnow()
    db.session.commit()

    return render_template('ebay/results.html', r=results)

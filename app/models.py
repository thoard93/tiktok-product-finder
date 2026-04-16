"""
PRISM — SQLAlchemy Models
All database models for the TikTok Shop Intelligence Platform.
"""

from datetime import datetime, timedelta
from app import db


class SystemConfig(db.Model):
    """General system settings stored in DB to survive restarts without Render redeploy"""
    __tablename__ = 'system_config'
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)
    description = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class User(db.Model):
    """Users who can access the tool"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), unique=True, nullable=True)
    discord_username = db.Column(db.String(100))
    discord_avatar = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False)
    is_dev_user = db.Column(db.Boolean, default=False)  # Logged in via passkey
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'discord_id': self.discord_id,
            'discord_username': self.discord_username,
            'is_admin': self.is_admin,
            'is_dev_user': self.is_dev_user,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }


class ActivityLog(db.Model):
    """Log of user activities"""
    __tablename__ = 'activity_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(100))  # scan, export, favorite, view, etc.
    details = db.Column(db.Text)  # JSON details
    ip_address = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='activities')

    def to_dict(self):
        # Convert UTC to EST (UTC-5)
        est_time = None
        if self.created_at:
            est_time = self.created_at - timedelta(hours=5)

        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.discord_username if self.user else 'Unknown',
            'action': self.action,
            'details': self.details,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'created_at_est': est_time.strftime('%m/%d/%Y, %I:%M:%S %p') if est_time else None
        }


class SiteConfig(db.Model):
    """Site-wide configuration stored in DB"""
    __tablename__ = 'site_config'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    """Products found by scanner"""
    __tablename__ = 'products'

    # Core identity
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    product_url = db.Column(db.String(500))
    image_url = db.Column(db.Text)
    cached_image_url = db.Column(db.Text)
    image_cached_at = db.Column(db.DateTime)

    # Seller info
    seller_id = db.Column(db.String(50), index=True)
    seller_name = db.Column(db.String(255), index=True)

    # Sales & revenue
    sales = db.Column(db.Integer, default=0)
    sales_7d = db.Column(db.Integer, default=0, index=True)
    sales_30d = db.Column(db.Integer, default=0)
    gmv = db.Column(db.Float, default=0)
    gmv_30d = db.Column(db.Float, default=0)
    gmv_growth = db.Column(db.Float, default=0)  # 7D GMV Growth Percentage

    # Pricing & commission
    price = db.Column(db.Float, default=0, index=True)
    original_price = db.Column(db.Float, default=0)  # Strikethrough price
    commission_rate = db.Column(db.Float, default=0, index=True)
    shop_ads_commission = db.Column(db.Float, default=0, index=True)

    # Video & influencer stats
    video_count = db.Column(db.Integer, default=0)  # 7D videos
    video_count_alltime = db.Column(db.Integer, default=0)  # All-time saturation
    video_7d = db.Column(db.Integer, default=0)
    video_30d = db.Column(db.Integer, default=0)
    live_count = db.Column(db.Integer, default=0)
    views_count = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0, index=True)

    # Product quality
    product_rating = db.Column(db.Float, default=0)
    review_count = db.Column(db.Integer, default=0)

    # Ad spend & performance
    ad_spend = db.Column(db.Float, default=0)  # 7D
    ad_spend_total = db.Column(db.Float, default=0)  # Lifetime
    is_ad_driven = db.Column(db.Boolean, default=False)

    # Deal hunter
    has_free_shipping = db.Column(db.Boolean, default=False, index=True)
    last_shown_hot = db.Column(db.DateTime)

    # User features
    is_favorite = db.Column(db.Boolean, default=False, index=True)
    product_status = db.Column(db.String(50), default='active', index=True)
    status_note = db.Column(db.String(255))

    # Inventory & velocity
    prev_sales_7d = db.Column(db.Integer, default=0)
    prev_sales_30d = db.Column(db.Integer, default=0)
    sales_velocity = db.Column(db.Float, default=0)

    # Metadata
    scan_type = db.Column(db.String(50), default='brand_hunter', index=True)
    first_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    # --- New fields ---
    category = db.Column(db.String(100))
    subcategory = db.Column(db.String(100))
    return_rate = db.Column(db.Float)
    rating = db.Column(db.Float)  # Normalized 0-5 rating (distinct from product_rating for future use)
    price_trend = db.Column(db.String(20))  # 'rising', 'falling', 'stable'
    last_echotik_sync = db.Column(db.DateTime)

    # Lookup popularity (used to prioritize refresh tiers)
    lookup_count = db.Column(db.Integer, default=0)

    # Trend data cache (lazy-loaded on detail page view)
    trend_data_json = db.Column(db.Text, nullable=True)
    trend_last_synced = db.Column(db.DateTime, nullable=True)

    # Opportunity Score cache (24h TTL, recomputed during daily sync)
    cached_score = db.Column(db.Integer, nullable=True)
    score_cached_at = db.Column(db.DateTime, nullable=True)

    # Composite indexes for common query patterns
    __table_args__ = (
        db.Index('idx_influencer_sales', 'influencer_count', 'sales_7d'),
        db.Index('idx_influencer_commission', 'influencer_count', 'commission_rate'),
        db.Index('idx_status_influencer', 'product_status', 'influencer_count'),
        db.Index('idx_influencer_firstseen', 'influencer_count', 'first_seen'),
        db.Index('idx_influencer_price', 'influencer_count', 'price'),
        db.Index('idx_favorite_sales', 'is_favorite', 'sales_7d'),
        db.Index('idx_firstseen_influencer', 'first_seen', 'influencer_count'),
        db.Index('ix_product_category', 'category'),
    )

    def to_dict(self):
        """Convert product to dictionary for API response."""
        return {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'seller_id': self.seller_id,
            'seller_name': self.seller_name,
            'is_ad_driven': (self.scan_type in ['apify_ad', 'daily_virals']) or (self.sales_7d > 50 and self.influencer_count < 5 and self.video_count < 5),
            'commission_rate': self.commission_rate,
            'shop_ads_commission': self.shop_ads_commission,
            'stock': self.live_count,
            'price': self.price,
            'image_url': self.cached_image_url or self.image_url,
            'cached_image_url': self.cached_image_url,
            'product_url': self.product_url,
            'product_rating': self.product_rating,
            'review_count': self.review_count,
            'has_free_shipping': self.has_free_shipping or False,
            'is_favorite': self.is_favorite,
            'product_status': self.product_status or 'active',
            'status_note': self.status_note,
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            # Stats
            'sales': self.sales,
            'sales_7d': self.sales_7d,
            'sales_30d': self.sales_30d,
            'gmv': self.gmv,
            'gmv_30d': self.gmv_30d,
            'gmv_growth': self.gmv_growth or 0,
            'video_count': self.video_count,
            'video_count_alltime': self.video_count_alltime or self.video_count,
            'video_7d': self.video_7d,
            'video_30d': self.video_30d,
            'influencer_count': self.influencer_count,
            'live_count': self.live_count,
            'views_count': self.views_count,
            'ad_spend': self.ad_spend,
            'ad_spend_total': self.ad_spend_total,
            'sales_velocity': self.sales_velocity or 0,
            'ad_spend_per_video': (self.ad_spend / self.video_count) if (self.video_count and self.video_count > 0) else 0,
            'roas': (self.gmv / self.ad_spend) if (self.ad_spend and self.ad_spend > 0) else 0,
            'est_profit': (self.gmv * self.commission_rate),
            # New fields
            'category': self.category,
            'subcategory': self.subcategory,
            'return_rate': self.return_rate,
            'rating': self.rating,
            'price_trend': self.price_trend,
            'last_echotik_sync': self.last_echotik_sync.isoformat() if self.last_echotik_sync else None,
        }


class BlacklistedBrand(db.Model):
    """TikTok Shop Brands/Sellers that are blacklisted"""
    __tablename__ = 'blacklisted_brands'

    id = db.Column(db.Integer, primary_key=True)
    seller_name = db.Column(db.String(255), unique=True, index=True, nullable=False)
    seller_id = db.Column(db.String(50), unique=True, index=True)
    reason = db.Column(db.Text)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'seller_name': self.seller_name,
            'seller_id': self.seller_id,
            'reason': self.reason,
            'added_at': self.added_at.isoformat() if self.added_at else None
        }


class CreatorList(db.Model):
    """Per-thread TikTok creator lists for inspo-chat forum threads"""
    __tablename__ = 'creator_lists'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.String(50), index=True, nullable=False)
    creator_name = db.Column(db.String(255), nullable=False)
    added_by = db.Column(db.String(255))
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('thread_id', 'creator_name', name='uq_thread_creator'),
    )


class WatchedBrand(db.Model):
    """Brands being tracked in Brand Hunter"""
    __tablename__ = 'watched_brands'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, index=True, nullable=False)
    logo_url = db.Column(db.String(500))
    product_count = db.Column(db.Integer, default=0)
    total_sales_7d = db.Column(db.Integer, default=0)
    total_revenue = db.Column(db.Float, default=0)
    avg_commission = db.Column(db.Float, default=0)
    top_product_id = db.Column(db.String(100))
    top_product_name = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    last_synced = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'logo_url': self.logo_url,
            'product_count': self.product_count or 0,
            'total_sales_7d': self.total_sales_7d or 0,
            'total_revenue': self.total_revenue or 0,
            'avg_commission': self.avg_commission or 0,
            'top_product_id': self.top_product_id,
            'top_product_name': self.top_product_name,
            'is_active': self.is_active,
            'last_synced': self.last_synced.isoformat() if self.last_synced else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    def refresh_stats(self):
        """Recalculate stats from products matching this brand"""
        from sqlalchemy import func
        products = Product.query.filter(
            Product.seller_name.ilike(f'%{self.name}%')
        ).all()

        if products:
            self.product_count = len(products)
            self.total_sales_7d = sum(p.sales_7d or 0 for p in products)
            self.total_revenue = sum(p.gmv or 0 for p in products)
            commissions = [p.commission_rate for p in products if p.commission_rate]
            self.avg_commission = sum(commissions) / len(commissions) if commissions else 0

            top = max(products, key=lambda p: p.sales_7d or 0)
            self.top_product_id = top.product_id
            self.top_product_name = top.product_name

            if products[0].image_url:
                self.logo_url = products[0].cached_image_url or products[0].image_url

        self.last_synced = datetime.utcnow()
        db.session.commit()


class ApiKey(db.Model):
    """API Keys for external SaaS access"""
    __tablename__ = 'api_keys'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(32), unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    credits = db.Column(db.Integer, default=0)  # 1 credit = 1 scan
    total_usage = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ScanJob(db.Model):
    """Async Job Queue for SaaS Scans"""
    __tablename__ = 'scan_jobs'
    id = db.Column(db.String(36), primary_key=True)  # UUID
    status = db.Column(db.String(20), default='queued', index=True)
    input_query = db.Column(db.String(500))
    result_json = db.Column(db.Text)
    api_key_id = db.Column(db.Integer, db.ForeignKey('api_keys.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)


class Subscription(db.Model):
    """PayPal recurring subscriptions for PRISM access"""
    __tablename__ = 'subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20), default='inactive', index=True)  # active, inactive, cancelled, past_due
    paypal_subscription_id = db.Column(db.String(100), unique=True, nullable=True)
    plan = db.Column(db.String(50), default='monthly_19.99')
    referral_code = db.Column(db.String(50), nullable=True, index=True)
    coupon_code = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    next_billing_date = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('subscription', uselist=False))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'status': self.status,
            'plan': self.plan,
            'referral_code': self.referral_code,
            'coupon_code': self.coupon_code,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'next_billing_date': self.next_billing_date.isoformat() if self.next_billing_date else None,
            'cancelled_at': self.cancelled_at.isoformat() if self.cancelled_at else None,
            'is_active': self.status == 'active',
        }


class ProductVideo(db.Model):
    """Short videos (<=15s) associated with a product"""
    __tablename__ = 'product_videos'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'), nullable=False)
    video_id = db.Column(db.String(100), nullable=True)
    video_url = db.Column(db.String(500), nullable=True)
    cover_url = db.Column(db.String(500), nullable=True)
    creator_name = db.Column(db.String(200), nullable=True)
    creator_handle = db.Column(db.String(200), nullable=True)
    creator_avatar = db.Column(db.String(500), nullable=True)
    view_count = db.Column(db.BigInteger, default=0)
    like_count = db.Column(db.BigInteger, default=0)
    duration_seconds = db.Column(db.Integer, nullable=True)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship('Product', backref=db.backref('videos', lazy=True))

    __table_args__ = (
        db.Index('ix_pv_product_id', 'product_id'),
        db.UniqueConstraint('product_id', 'video_id', name='uq_product_video'),
    )


class Brand(db.Model):
    """TikTok Shop brands/shops for Brand Hunter"""
    __tablename__ = 'brands'

    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    name = db.Column(db.String(300), nullable=False)
    avatar_url = db.Column(db.String(500), nullable=True)
    country = db.Column(db.String(50), nullable=True)
    category = db.Column(db.String(100), nullable=True)
    follower_count = db.Column(db.BigInteger, default=0)
    gmv_30d = db.Column(db.Float, default=0)
    product_count = db.Column(db.Integer, default=0)
    trending_score = db.Column(db.Float, default=0)
    tiktok_shop_url = db.Column(db.String(500), nullable=True)
    last_synced = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TapProduct(db.Model):
    """TAP (boosted commission) links for products"""
    __tablename__ = 'tap_products'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'), nullable=False)
    tap_link = db.Column(db.Text, nullable=False)
    boosted_commission = db.Column(db.Float, nullable=False)
    base_commission = db.Column(db.Float, nullable=True)
    partner_name = db.Column(db.String(100), default='Affiliate Automated')
    partner_logo_url = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks = db.Column(db.Integer, default=0)

    product = db.relationship('Product', backref=db.backref('tap_data', uselist=False))


class TapList(db.Model):
    """TAP product lists — curated boosted commission groups"""
    __tablename__ = 'tap_lists'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    partner = db.Column(db.String(100), default='Affiliate Automated')
    category = db.Column(db.String(100))
    share_link = db.Column(db.Text, nullable=False)
    tiktok_list_id = db.Column(db.String(100))
    product_count = db.Column(db.Integer, default=0)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProductView(db.Model):
    """Tracks which products each user has viewed"""
    __tablename__ = 'product_views'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'), nullable=False)
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_pv_user_product', 'user_id', 'product_id'),
    )


# =============================================================================
# EBAY PRICE BLADE (admin-only)
# =============================================================================

class CampaignBanner(db.Model):
    """Animated announcement banners displayed at the top of every page"""
    __tablename__ = 'campaign_banners'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)            # e.g. "Spring Sale"
    message = db.Column(db.String(500), nullable=False)           # Scrolling marquee text
    link_url = db.Column(db.String(500), nullable=True)           # Optional CTA link
    link_text = db.Column(db.String(100), nullable=True)          # e.g. "Shop Now →"
    color_scheme = db.Column(db.String(20), default='fire')       # fire, gold, teal, purple, coral
    starts_at = db.Column(db.DateTime, nullable=True)             # Countdown target / start time
    ends_at = db.Column(db.DateTime, nullable=True)               # Auto-deactivate after this
    is_active = db.Column(db.Boolean, default=True)
    is_dismissible = db.Column(db.Boolean, default=True)          # Can users close it?
    priority = db.Column(db.Integer, default=0)                   # Higher = shown first
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def _now_est():
        """Current time in EST (UTC-5)"""
        return datetime.utcnow() - timedelta(hours=5)

    def is_live(self):
        """Check if campaign is currently active and within date range (EST)"""
        if not self.is_active:
            return False
        now = self._now_est()
        if self.starts_at and now < self.starts_at:
            return False  # Not started yet — but show countdown
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def is_upcoming(self):
        """Returns True if campaign has a future start date (EST)"""
        if not self.is_active:
            return False
        now = self._now_est()
        return self.starts_at and now < self.starts_at

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'message': self.message,
            'link_url': self.link_url,
            'link_text': self.link_text,
            'color_scheme': self.color_scheme,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'is_active': self.is_active,
            'is_dismissible': self.is_dismissible,
            'priority': self.priority,
            'is_live': self.is_live(),
            'is_upcoming': self.is_upcoming(),
        }


class EbayWatchlistItem(db.Model):
    __tablename__ = 'ebay_watchlist'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500))
    keywords = db.Column(db.String(500))
    avg_sold_price = db.Column(db.Float)
    lowest_bin = db.Column(db.Float)
    sell_through_rate = db.Column(db.Float)
    active_listings = db.Column(db.Integer)
    sold_last_30 = db.Column(db.Integer)
    recommended_price = db.Column(db.Float)
    notes = db.Column(db.Text)
    last_checked = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EbaySearchHistory(db.Model):
    __tablename__ = 'ebay_search_history'
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(500))
    avg_sold_price = db.Column(db.Float)
    lowest_bin = db.Column(db.Float)
    sell_through_rate = db.Column(db.Float)
    active_listings = db.Column(db.Integer)
    sold_count = db.Column(db.Integer)
    raw_results = db.Column(db.Text)
    searched_at = db.Column(db.DateTime, default=datetime.utcnow)


# =============================================================================
# COUPON / PROMO CODE SYSTEM
# =============================================================================

class CouponCode(db.Model):
    """Promo/coupon codes for free access or discounts"""
    __tablename__ = 'coupon_codes'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    discount_type = db.Column(db.String(20), nullable=False)  # free_months, percent_off, fixed_off
    discount_value = db.Column(db.Integer, nullable=False)     # 1 = 1 month, 50 = 50%, etc.
    max_uses = db.Column(db.Integer, default=1)                # None = unlimited
    times_used = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.String(100), nullable=True)      # admin username who created
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_valid(self):
        """Check if coupon can still be redeemed"""
        if not self.is_active:
            return False
        if self.max_uses and self.times_used >= self.max_uses:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'discount_type': self.discount_type,
            'discount_value': self.discount_value,
            'max_uses': self.max_uses,
            'times_used': self.times_used,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_active': self.is_active,
            'is_valid': self.is_valid(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CouponRedemption(db.Model):
    """Tracks which users redeemed which coupons (prevents double-use)"""
    __tablename__ = 'coupon_redemptions'

    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon_codes.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow)

    coupon = db.relationship('CouponCode', backref='redemptions')
    user = db.relationship('User', backref='coupon_redemptions')

    __table_args__ = (
        db.UniqueConstraint('coupon_id', 'user_id', name='uq_coupon_user'),
    )


# =============================================================================
# BRAND HUNTER v2 — Deep Scan System
# =============================================================================

class ScannedBrand(db.Model):
    """Brands discovered and scanned via Brand Hunter deep scan"""
    __tablename__ = 'scanned_brands'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    brand_name = db.Column(db.String(300), nullable=False)
    brand_logo_url = db.Column(db.String(500))
    brand_category = db.Column(db.String(100))

    # 30-day stats (primary sort key)
    sales_30d = db.Column(db.Integer, default=0)
    revenue_30d = db.Column(db.Float, default=0)
    units_sold_30d = db.Column(db.Integer, default=0)

    # Overall stats
    total_products = db.Column(db.Integer, default=0)
    avg_commission = db.Column(db.Float, default=0)
    follower_count = db.Column(db.BigInteger, default=0)
    top_product_name = db.Column(db.String(300))

    # Scan metadata
    last_scanned = db.Column(db.DateTime, default=datetime.utcnow)
    pages_scanned = db.Column(db.String(50))       # e.g. "100-200"
    page_found_on = db.Column(db.Integer, default=0)  # which ranking page this brand was found on
    scan_status = db.Column(db.String(20), default='idle')  # idle, scanning, complete, error
    is_hidden_gem = db.Column(db.Boolean, default=False)    # found on page 100+

    products = db.relationship('BrandProduct', backref='brand', lazy='dynamic', cascade='all,delete-orphan')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'brand_name': self.brand_name,
            'brand_logo_url': self.brand_logo_url,
            'brand_category': self.brand_category,
            'sales_30d': self.sales_30d,
            'revenue_30d': self.revenue_30d,
            'total_products': self.total_products,
            'avg_commission': self.avg_commission,
            'follower_count': self.follower_count,
            'is_hidden_gem': self.is_hidden_gem,
            'pages_scanned': self.pages_scanned,
            'scan_status': self.scan_status,
            'last_scanned': self.last_scanned.isoformat() if self.last_scanned else None,
        }


class BrandProduct(db.Model):
    """Products linked to a scanned brand"""
    __tablename__ = 'brand_products'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('scanned_brands.id'), nullable=False, index=True)
    product_id = db.Column(db.String(100), nullable=False)

    title = db.Column(db.String(500))
    image_url = db.Column(db.String(500))
    price = db.Column(db.Float, default=0)
    commission_rate = db.Column(db.Float, default=0)
    sales_7d = db.Column(db.Integer, default=0)
    sales_30d = db.Column(db.Integer, default=0)
    revenue_30d = db.Column(db.Float, default=0)
    total_videos = db.Column(db.Integer, default=0)
    total_sales = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0)
    rating = db.Column(db.Float, default=0)
    vantage_score = db.Column(db.Integer, default=0)
    category = db.Column(db.String(100))

    page_found = db.Column(db.Integer, default=0)
    is_hidden_gem = db.Column(db.Boolean, default=False)
    last_synced = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_bp_brand_videos', 'brand_id', 'total_videos'),
    )


class FavoritedCreator(db.Model):
    """User's saved TikTok creators/influencers"""
    __tablename__ = 'favorited_creators'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    unique_id = db.Column(db.String(100), nullable=False, index=True)
    user_id_tiktok = db.Column(db.String(100))
    nick_name = db.Column(db.String(200))
    avatar = db.Column(db.String(500))
    total_followers_cnt = db.Column(db.BigInteger, default=0)
    ec_score = db.Column(db.Integer, default=0)
    region = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'unique_id', name='uq_user_creator'),
    )


class BrandScanJob(db.Model):
    """Tracks brand hunter scan progress — scans product pages within a single brand"""
    __tablename__ = 'brand_scan_jobs'

    id = db.Column(db.Integer, primary_key=True)
    brand_id_str = db.Column(db.Text)              # EchoTik shop_id (may be comma-joined for batch scans)
    brand_name = db.Column(db.String(300))         # Display name
    brand_logo_url = db.Column(db.String(500))     # Logo for progress bar display
    page_start = db.Column(db.Integer, default=1)
    page_end = db.Column(db.Integer, default=50)
    current_page = db.Column(db.Integer, default=0)
    products_found = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='queued')  # queued, running, complete, error
    error_message = db.Column(db.String(500))
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

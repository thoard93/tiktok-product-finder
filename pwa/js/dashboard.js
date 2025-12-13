// ==================== STATE ====================
let currentPage = 1;
let currentFilters = {};
let stats = {};
let currentTimePeriod = 'all';
let showOnlyFavorites = false;

// ==================== FAVORITES ====================
const FAVORITES_KEY = 'tiktokshop_favorites';

function getFavorites() {
    try {
        return JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]');
    } catch {
        return [];
    }
}

function saveFavorites(favorites) {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(favorites));
    updateFavoritesCount();
}

function toggleFavorite(productId, btn, event) {
    event.stopPropagation();
    const favorites = getFavorites();
    const index = favorites.indexOf(productId);

    if (index > -1) {
        favorites.splice(index, 1);
        btn.classList.remove('favorited');
        btn.innerHTML = '🤍';
    } else {
        favorites.push(productId);
        btn.classList.add('favorited');
        btn.innerHTML = '❤️';
    }

    saveFavorites(favorites);
}

function isFavorited(productId) {
    return getFavorites().includes(productId);
}

function updateFavoritesCount() {
    const count = getFavorites().length;
    const countEl = document.getElementById('fav-count');
    if (countEl) {
        countEl.textContent = count > 0 ? `(${count})` : '';
    }
}

// ==================== TIME FILTERING ====================
function setTimePeriod(period, btn) {
    currentTimePeriod = period;
    showOnlyFavorites = false;

    // Update active states
    document.querySelectorAll('.time-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');

    currentPage = 1;
    loadProducts(1, currentFilters);
}

function toggleFavoritesOnly(btn) {
    showOnlyFavorites = !showOnlyFavorites;

    // Update active states
    document.querySelectorAll('.time-tab').forEach(t => t.classList.remove('active'));
    if (showOnlyFavorites) {
        btn.classList.add('active');
    } else {
        document.querySelector('.time-tab[data-period="all"]').classList.add('active');
        currentTimePeriod = 'all';
    }

    currentPage = 1;
    loadProducts(1, currentFilters);
}

// ==================== HELPERS ====================
function formatCurrency(value) {
    if (value >= 1000000) return '$' + (value / 1000000).toFixed(1) + 'M';
    if (value >= 1000) return '$' + (value / 1000).toFixed(1) + 'K';
    return '$' + value.toFixed(0);
}

function formatNumber(value) {
    if (value >= 1000000) return (value / 1000000).toFixed(1) + 'M';
    if (value >= 1000) return (value / 1000).toFixed(1) + 'K';
    return value.toString();
}

function getCompetitionClass(count) {
    if (count <= 3) return 'low';
    if (count <= 30) return 'medium';
    return 'high';
}

function getGradientClass(productId) {
    const hash = String(productId).split('').reduce((a, b) => ((a << 5) - a) + b.charCodeAt(0), 0);
    return 'gradient-' + (Math.abs(hash) % 5 + 1);
}

function getProductIcon(name) {
    const n = (name || '').toLowerCase();
    if (n.includes('hair') || n.includes('wig')) return '💇';
    if (n.includes('brush') || n.includes('comb')) return '💆';
    if (n.includes('bag') || n.includes('purse') || n.includes('handbag')) return '👜';
    if (n.includes('shoe') || n.includes('sneaker') || n.includes('boot')) return '👟';
    if (n.includes('dress') || n.includes('cloth') || n.includes('shirt') || n.includes('tee')) return '👗';
    if (n.includes('watch')) return '⌚';
    if (n.includes('phone') || n.includes('case') || n.includes('charger')) return '📱';
    if (n.includes('jewelry') || n.includes('necklace') || n.includes('ring') || n.includes('earring')) return '💍';
    if (n.includes('makeup') || n.includes('cosmetic') || n.includes('lipstick')) return '💄';
    if (n.includes('skin') || n.includes('cream') || n.includes('serum')) return '🧴';
    if (n.includes('toy') || n.includes('game')) return '🎮';
    if (n.includes('food') || n.includes('snack') || n.includes('noodle') || n.includes('pho')) return '🍜';
    if (n.includes('fitness') || n.includes('gym') || n.includes('workout')) return '💪';
    if (n.includes('pet') || n.includes('dog') || n.includes('cat')) return '🐕';
    if (n.includes('car') || n.includes('vehicle') || n.includes('diesel')) return '🚗';
    if (n.includes('kitchen') || n.includes('cook')) return '🍳';
    if (n.includes('lamp') || n.includes('light') || n.includes('led')) return '💡';
    if (n.includes('crystal') || n.includes('stone') || n.includes('gem')) return '💎';
    if (n.includes('camera') || n.includes('gimbal')) return '📷';
    if (n.includes('headphone') || n.includes('speaker')) return '🎧';
    return '📦';
}

function isNewProduct(firstSeen) {
    if (!firstSeen) return false;
    const seen = new Date(firstSeen);
    const now = new Date();
    const hoursDiff = (now - seen) / (1000 * 60 * 60);
    return hoursDiff < 24;
}

// ==================== CREATE PRODUCT CARD ====================
function createProductCard(product) {
    const iflCount = product.influencer_count || product.total_influencers || 0;
    const commissionRate = product.commission_rate || 0;
    const gmv = product.gmv || 0;
    const earnings = product.potential_earnings || (gmv * commissionRate / 100);
    const productId = product.product_id || product.id;
    const favorited = isFavorited(productId);
    const isNew = isNewProduct(product.first_seen);

    // Determine badges
    let badges = '';
    if (isNew) {
        badges += '<span class="badge badge-new">NEW</span>';
    }
    if (commissionRate >= 20) {
        badges += '<span class="badge badge-commission">' + commissionRate.toFixed(0) + '%</span>';
    }
    if (iflCount <= 3 && iflCount > 0 && gmv > 100) {
        badges += '<span class="badge badge-untapped">UNTAPPED</span>';
    } else if (iflCount <= 50 && iflCount >= 3) {
        badges += '<span class="badge badge-gem">HIDDEN GEM</span>';
    }

    return `
        <div class="product-card" onclick="window.location.href='/product?id=${productId}'">
            <div class="product-image-container">
                ${product.cached_image_url ? `
                    <img class="product-image" 
                         src="${product.cached_image_url}" 
                         alt="${product.product_name || 'Product'}"
                         loading="lazy"
                         onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                    <div class="product-image-placeholder ${getGradientClass(productId)}" style="display: none;">
                        <span class="category-icon">${getProductIcon(product.product_name)}</span>
                    </div>
                ` : `
                    <div class="product-image-placeholder ${getGradientClass(productId)}">
                        <span class="category-icon">${getProductIcon(product.product_name)}</span>
                    </div>
                `}
                <button class="favorite-btn ${favorited ? 'favorited' : ''}" 
                        onclick="toggleFavorite('${productId}', this, event)">
                    ${favorited ? '❤️' : '🤍'}
                </button>
                <div class="product-badges">${badges}</div>
            </div>
            <div class="product-content">
                <div class="product-name">${product.product_name || 'Unknown Product'}</div>
                <div class="product-metrics">
                    <div class="metric">
                        <div class="metric-value">${formatCurrency(gmv)}</div>
                        <div class="metric-label">GMV</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${formatNumber(product.sales || 0)}</div>
                        <div class="metric-label">Units Sold</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value highlight">${formatCurrency(earnings)}</div>
                        <div class="metric-label">Potential</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${commissionRate.toFixed(0)}%</div>
                        <div class="metric-label">Commission</div>
                    </div>
                </div>
                <div class="product-footer">
                    <div class="influencer-count ${getCompetitionClass(iflCount)}">
                        👥 ${iflCount} influencers
                    </div>
                    <button class="btn-analyze" onclick="openAIModal(event, '${product.product_name}', ${gmv}, ${iflCount})">✨ Analyze</button>
                    <!-- <div class="seller-name">${product.seller_name || 'TikTok Shop'}</div> -->
                </div>
            </div>
        </div>
    `;
}

// ==================== SKELETON LOADER ====================
function getSkeletonHTML() {
    return `
        <div class="skeleton-grid">
            ${Array(8).fill(0).map(() => `
                <div class="skeleton-card">
                    <div class="skeleton skeleton-image"></div>
                    <div class="skeleton-content">
                        <div class="skeleton skeleton-title"></div>
                        <div class="skeleton skeleton-text" style="width: 60%"></div>
                        <div class="skeleton skeleton-text" style="width: 40%"></div>
                        <div class="skeleton-metrics">
                            <div class="skeleton skeleton-metric"></div>
                            <div class="skeleton skeleton-metric"></div>
                            <div class="skeleton skeleton-metric"></div>
                            <div class="skeleton skeleton-metric"></div>
                        </div>
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

// ==================== LOAD PRODUCTS ====================
async function loadProducts(page = 1, filters = {}) {
    const grid = document.getElementById('productGrid');

    // Check if grid has products already to avoid full jarring reload for pagination
    // But for filter changes we want to show loading
    const isPagination = Object.keys(filters).length === 0 && page !== 1;

    if (!isPagination) {
        grid.innerHTML = getSkeletonHTML();
    }

    try {
        // Build query string
        const params = new URLSearchParams({
            page: page,
            per_page: 20,
            sort_by: filters.sort_by || 'gmv',
            sort_order: 'desc',
            ...filters
        });

        const response = await fetch(`/api/products?period=${currentTimePeriod}&${params}`);
        const data = await response.json();

        if (data.success && data.products) {
            let products = data.products;

            // Filter by favorites if enabled
            if (showOnlyFavorites) {
                const favs = getFavorites();
                products = products.filter(p => favs.includes(p.product_id));
            }

            if (products.length > 0) {
                grid.innerHTML = products.map(createProductCard).join('');
                updatePagination(data.pagination);
            } else {
                grid.innerHTML = `
                    <div class="empty-state">
                        <h3>${showOnlyFavorites ? 'No favorites yet' : 'No products found'}</h3>
                        <p>${showOnlyFavorites ? 'Click the ❤️ on products to add them to your favorites!' : 'Try adjusting your filters or search criteria'}</p>
                    </div>
                `;
            }
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    } catch (error) {
        console.error('Error loading products:', error);
        grid.innerHTML = `
            <div class="empty-state">
                <h3>Error loading products</h3>
                <p>${error.message}</p>
            </div>
        `;
    }
}

// ==================== LOAD STATS ====================
async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();

        if (data.success && data.stats) {
            stats = data.stats;
            document.getElementById('totalProducts').textContent = formatNumber(stats.total_products || 0);
            document.getElementById('hiddenGems').textContent = formatNumber(stats.hidden_gems || 0);
            document.getElementById('untappedCount').textContent = formatNumber(stats.untapped_products || 0);
            document.getElementById('avgCommission').textContent = (stats.avg_commission || 0).toFixed(1) + '%';

            if (document.getElementById('qf-all-count')) {
                document.getElementById('qf-all-count').textContent = stats.total_products || 0;
                document.getElementById('qf-gems-count').textContent = stats.hidden_gems || 0;
                document.getElementById('qf-untapped-count').textContent = stats.untapped_products || 0;
                document.getElementById('qf-commission-count').textContent = stats.high_commission || 0;
            }
        }
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

// ==================== PAGINATION ====================
function updatePagination(pagination) {
    if (!pagination) return;

    const container = document.getElementById('pagination');
    container.innerHTML = `
        <button class="page-btn" onclick="goToPage(${pagination.page - 1})" ${!pagination.has_prev ? 'disabled' : ''}>
            ← Previous
        </button>
        <span class="page-info">
            Page ${pagination.page} of ${pagination.pages} (${pagination.total} products)
        </span>
        <button class="page-btn" onclick="goToPage(${pagination.page + 1})" ${!pagination.has_next ? 'disabled' : ''}>
            Next →
        </button>
    `;
}

function goToPage(page) {
    currentPage = page;
    loadProducts(page, currentFilters);
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ==================== FILTERS ====================
function applyFilters() {
    currentFilters = {};

    const search = document.getElementById('searchInput').value;
    if (search) currentFilters.search = search;

    const sortBy = document.getElementById('sortBy').value;
    if (sortBy) currentFilters.sort_by = sortBy;

    const minCommission = document.getElementById('minCommission').value;
    if (minCommission) currentFilters.min_commission = minCommission;

    const competition = document.getElementById('competition').value;
    if (competition) currentFilters.competition = competition;

    currentPage = 1;
    loadProducts(1, currentFilters);

    document.querySelectorAll('.quick-filter').forEach(el => el.classList.remove('active'));
}

function resetFilters() {
    document.getElementById('searchInput').value = '';
    document.getElementById('sortBy').value = 'gmv';
    document.getElementById('minCommission').value = '';
    document.getElementById('competition').value = '';

    currentFilters = {};
    currentPage = 1;
    currentTimePeriod = 'all';
    showOnlyFavorites = false;

    // Reset time tabs
    document.querySelectorAll('.time-tab').forEach(t => t.classList.remove('active'));
    document.querySelector('.time-tab[data-period="all"]').classList.add('active');

    loadProducts(1, {});

    document.querySelectorAll('.quick-filter').forEach(el => el.classList.remove('active'));
    document.getElementById('qf-all').classList.add('active');
}

function quickFilter(type) {
    document.querySelectorAll('.quick-filter').forEach(el => el.classList.remove('active'));
    document.getElementById('qf-' + (type === 'high-commission' ? 'commission' : type)).classList.add('active');

    document.getElementById('minCommission').value = '';
    document.getElementById('competition').value = '';

    currentFilters = {};

    if (type === 'gems') {
        currentFilters.min_influencers = 3;
        currentFilters.max_influencers = 50;
    } else if (type === 'apify') {
        currentFilters.apify_scan = 'true';
    } else if (type === 'untapped') {
        currentFilters.competition = 'untapped';
    } else if (type === 'high-commission') {
        currentFilters.min_commission = 15;
    }

    currentPage = 1;
    loadProducts(1, currentFilters);
}

// ==================== AI FEATURES ====================
function openAIModal(e, productName, gmv, influencers) {
    e.stopPropagation();

    // Create modal if it doesn't exist
    if (!document.querySelector('.modal-overlay')) {
        const modalHTML = `
            <div class="modal-overlay" onclick="closeAIModal(event)">
                <div class="ai-modal" onclick="event.stopPropagation()">
                    <div class="modal-header">
                        <div class="modal-title">✨ AI Product Analysis</div>
                        <button class="close-modal" onclick="closeAIModal()">×</button>
                    </div>
                    <div class="ai-content" id="aiContent"></div>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHTML);
    }

    const overlay = document.querySelector('.modal-overlay');
    const content = document.getElementById('aiContent');

    // Show Loading
    content.innerHTML = `
        <div class="ai-loading">
            <div class="ai-dots">
                <div class="ai-dot"></div>
                <div class="ai-dot"></div>
                <div class="ai-dot"></div>
            </div>
            <p>Analyzing market trends & competition...</p>
        </div>
    `;

    overlay.classList.add('active');

    // Simulate AI Delay
    setTimeout(() => {
        const score = Math.floor(Math.random() * (98 - 70) + 70);
        let recommendation = "";

        if (influencers < 5 && gmv > 1000) {
            recommendation = "💎 <strong>Hidden Gem Detected!</strong><br>Low competition with proven sales volume. Recommended strategy: Order sample immediately and test creatives emphasizing the unique value prop.";
        } else if (gmv > 100000) {
            recommendation = "🔥 <strong>Viral Trend!</strong><br>High volume but high competition. Recommended strategy: Differentiate with a unique bundle or better offer.";
        } else {
            recommendation = "📈 <strong>Rising Star</strong><br>Showing consistent growth. Monitor for 3 more days before testing.";
        }

        content.innerHTML = `
            <div class="ai-score">${score}/100</div>
            <div class="product-name" style="-webkit-line-clamp: 1; margin-bottom: 1rem; text-align: center;">${productName}</div>
            <div class="ai-recommendation">
                ${recommendation}
            </div>
            <div style="margin-top: 1.5rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                <button class="btn btn-outline" style="width: 100%" onclick="closeAIModal()">Close</button>
                <button class="btn" style="width: 100%">GENERATE AD SCRIPT</button>
            </div>
        `;
    }, 1500);
}

function closeAIModal(e) {
    if (e && e.target !== e.currentTarget) return;
    const overlay = document.querySelector('.modal-overlay');
    if (overlay) {
        overlay.classList.remove('active');
    }
}

// ==================== INIT ====================
document.getElementById('searchInput').addEventListener('keypress', function (e) {
    if (e.key === 'Enter') applyFilters();
});

document.addEventListener('DOMContentLoaded', function () {
    loadStats();
    loadProducts(1, {});
    updateFavoritesCount();
    if (document.getElementById('qf-all')) {
        document.getElementById('qf-all').classList.add('active');
    }
});

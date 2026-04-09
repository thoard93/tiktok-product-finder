/**
 * PRISM Shared API Wrapper
 * Handles auth redirects, loading states, JSON errors, and 429 retry.
 *
 * Usage:
 *   import { api } from '/pwa/js/api.js';
 *   const data = await api.get('/api/products?page=1');
 *   const result = await api.post('/api/scan/manual', { query: 'test' });
 *
 * Or use the global:
 *   const data = await prismApi.get('/api/products');
 */
(function () {
  'use strict';

  const MAX_RETRIES = 3;
  const RETRY_DELAY_MS = 1500;

  let activeRequests = 0;

  function setLoading(loading) {
    activeRequests += loading ? 1 : -1;
    if (activeRequests < 0) activeRequests = 0;
    document.body.classList.toggle('prism-api-loading', activeRequests > 0);
  }

  async function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  /**
   * Core fetch wrapper.
   * @param {string} url
   * @param {object} options - fetch options + { retries, showLoading }
   * @returns {Promise<any>} parsed JSON
   */
  async function request(url, options = {}) {
    const {
      retries = MAX_RETRIES,
      showLoading = true,
      ...fetchOpts
    } = options;

    // Default headers
    if (!fetchOpts.headers) fetchOpts.headers = {};
    if (fetchOpts.body && typeof fetchOpts.body === 'object' && !(fetchOpts.body instanceof FormData)) {
      fetchOpts.body = JSON.stringify(fetchOpts.body);
      fetchOpts.headers['Content-Type'] = fetchOpts.headers['Content-Type'] || 'application/json';
    }

    if (showLoading) setLoading(true);

    let lastError;
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const res = await fetch(url, fetchOpts);

        // Auth error → redirect to login
        if (res.status === 401) {
          window.location.href = '/login';
          throw new Error('Unauthorized');
        }

        // Rate limited → retry with backoff
        if (res.status === 429 && attempt < retries) {
          const retryAfter = res.headers.get('Retry-After');
          const delay = retryAfter ? parseInt(retryAfter, 10) * 1000 : RETRY_DELAY_MS * (attempt + 1);
          console.warn(`[prism-api] 429 on ${url}, retrying in ${delay}ms (attempt ${attempt + 1}/${retries})`);
          await sleep(delay);
          continue;
        }

        // Parse JSON safely
        const contentType = res.headers.get('content-type') || '';
        let data;
        if (contentType.includes('application/json')) {
          try {
            data = await res.json();
          } catch (parseErr) {
            throw new Error(`Failed to parse JSON response from ${url}: ${parseErr.message}`);
          }
        } else {
          // Non-JSON response
          const text = await res.text();
          if (!res.ok) throw new Error(text || `HTTP ${res.status}`);
          data = text;
        }

        if (!res.ok) {
          const msg = (data && typeof data === 'object' && data.error) || `HTTP ${res.status}`;
          throw new Error(msg);
        }

        return data;

      } catch (err) {
        lastError = err;
        // Network errors (not HTTP errors) → retry
        if (err.name === 'TypeError' && attempt < retries) {
          console.warn(`[prism-api] Network error on ${url}, retrying (attempt ${attempt + 1}/${retries})`);
          await sleep(RETRY_DELAY_MS * (attempt + 1));
          continue;
        }
        if (attempt >= retries) break;
      }
    }

    if (showLoading) setLoading(false);
    throw lastError;
  }

  // Convenience methods
  const api = {
    get(url, opts = {}) {
      return request(url, { method: 'GET', ...opts });
    },
    post(url, body, opts = {}) {
      return request(url, { method: 'POST', body, ...opts });
    },
    put(url, body, opts = {}) {
      return request(url, { method: 'PUT', body, ...opts });
    },
    del(url, opts = {}) {
      return request(url, { method: 'DELETE', ...opts });
    },
    request,
  };

  // Always clear loading when done
  const origRequest = request;
  api.request = async function (...args) {
    try {
      const result = await origRequest(...args);
      return result;
    } finally {
      const opts = args[1] || {};
      if (opts.showLoading !== false) setLoading(false);
    }
  };
  // Re-wire convenience methods
  api.get = (url, opts = {}) => api.request(url, { method: 'GET', ...opts });
  api.post = (url, body, opts = {}) => api.request(url, { method: 'POST', body, ...opts });
  api.put = (url, body, opts = {}) => api.request(url, { method: 'PUT', body, ...opts });
  api.del = (url, opts = {}) => api.request(url, { method: 'DELETE', ...opts });

  // Export
  window.prismApi = api;
})();

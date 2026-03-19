/**
 * Keyword Research API Proxy Server
 *
 * Standalone Node.js server (no npm dependencies) that proxies requests
 * to the DataForSEO API, protecting credentials from the frontend.
 *
 * Usage: node keyword-api-server.js
 * Server runs on port 3001
 */

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const url = require('url');

// ─── Configuration ──────────────────────────────────────────────────────────

const PORT = 3001;
const DATAFORSEO_LOGIN = 'davit@10xseo.ge';
const DATAFORSEO_PASSWORD = 'fb35fc357556204b';
const DATAFORSEO_AUTH = 'Basic ' + Buffer.from(DATAFORSEO_LOGIN + ':' + DATAFORSEO_PASSWORD).toString('base64');

const DEFAULT_LOCATION_CODE = 2268; // Georgia
const CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour
const RATE_LIMIT_WINDOW_MS = 60 * 1000; // 1 minute
const RATE_LIMIT_MAX = 30; // max requests per window per IP

// ─── CORS allowed origins ───────────────────────────────────────────────────

const ALLOWED_ORIGINS = [
  'http://localhost:3001',
  'http://localhost:3000',
  'http://localhost:8080',
  'http://127.0.0.1:3001',
  'http://127.0.0.1:3000',
  'http://127.0.0.1:8080',
  'https://10xseo.ge',
  'http://10xseo.ge',
  'https://www.10xseo.ge',
  'http://www.10xseo.ge'
];

// ─── In-memory cache ────────────────────────────────────────────────────────

const cache = new Map();

function getCached(key) {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.timestamp > CACHE_TTL_MS) {
    cache.delete(key);
    return null;
  }
  return entry.data;
}

function setCache(key, data) {
  cache.set(key, { data, timestamp: Date.now() });
}

// Periodically clean expired cache entries every 10 minutes
setInterval(() => {
  const now = Date.now();
  for (const [key, entry] of cache) {
    if (now - entry.timestamp > CACHE_TTL_MS) {
      cache.delete(key);
    }
  }
}, 10 * 60 * 1000);

// ─── Rate limiting ──────────────────────────────────────────────────────────

const rateLimitMap = new Map();

function isRateLimited(ip) {
  const now = Date.now();
  let record = rateLimitMap.get(ip);

  if (!record || now - record.windowStart > RATE_LIMIT_WINDOW_MS) {
    record = { windowStart: now, count: 1 };
    rateLimitMap.set(ip, record);
    return false;
  }

  record.count++;
  if (record.count > RATE_LIMIT_MAX) {
    return true;
  }

  return false;
}

// Clean up rate limit records every 2 minutes
setInterval(() => {
  const now = Date.now();
  for (const [ip, record] of rateLimitMap) {
    if (now - record.windowStart > RATE_LIMIT_WINDOW_MS) {
      rateLimitMap.delete(ip);
    }
  }
}, 2 * 60 * 1000);

// ─── MIME types for static file serving ─────────────────────────────────────

const MIME_TYPES = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.eot': 'application/vnd.ms-fontobject',
  '.otf': 'font/otf',
  '.webp': 'image/webp',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.pdf': 'application/pdf',
  '.txt': 'text/plain',
  '.xml': 'application/xml'
};

// ─── Helper: make HTTPS POST request ────────────────────────────────────────

function httpsPost(urlStr, body) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(urlStr);
    const postData = JSON.stringify(body);

    const options = {
      hostname: parsed.hostname,
      port: 443,
      path: parsed.pathname,
      method: 'POST',
      headers: {
        'Authorization': DATAFORSEO_AUTH,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(postData)
      }
    };

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          resolve(parsed);
        } catch (e) {
          reject(new Error('Failed to parse DataForSEO response: ' + e.message));
        }
      });
    });

    req.on('error', (e) => {
      reject(new Error('DataForSEO request failed: ' + e.message));
    });

    req.setTimeout(30000, () => {
      req.destroy();
      reject(new Error('DataForSEO request timed out'));
    });

    req.write(postData);
    req.end();
  });
}

// ─── Helper: send JSON response ─────────────────────────────────────────────

function sendJSON(res, statusCode, data, origin) {
  const headers = {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store'
  };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    headers['Access-Control-Allow-Origin'] = origin;
    headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS';
    headers['Access-Control-Allow-Headers'] = 'Content-Type';
  }
  res.writeHead(statusCode, headers);
  res.end(JSON.stringify(data));
}

// ─── Helper: read request body ──────────────────────────────────────────────

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      // Limit body size to 1MB
      if (body.length > 1024 * 1024) {
        reject(new Error('Request body too large'));
      }
    });
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

// ─── Helper: get client IP ──────────────────────────────────────────────────

function getClientIP(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim()
    || req.socket.remoteAddress
    || 'unknown';
}

// ─── Helper: log request ────────────────────────────────────────────────────

function logRequest(method, urlPath, ip, statusCode, extra) {
  const timestamp = new Date().toISOString();
  const msg = `[${timestamp}] ${method} ${urlPath} - ${ip} - ${statusCode}`;
  console.log(extra ? msg + ' - ' + extra : msg);
}

// ─── Static file server ─────────────────────────────────────────────────────

const STATIC_ROOT = __dirname;

function serveStaticFile(req, res, urlPath) {
  // Decode URI and resolve path
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(urlPath);
  } catch (e) {
    res.writeHead(400, { 'Content-Type': 'text/plain' });
    res.end('Bad Request');
    return;
  }

  // Default to index.html
  if (decodedPath === '/') {
    decodedPath = '/index.html';
  }

  const filePath = path.join(STATIC_ROOT, decodedPath);

  // Prevent directory traversal
  if (!filePath.startsWith(STATIC_ROOT)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('Forbidden');
    return;
  }

  const ext = path.extname(filePath).toLowerCase();
  const contentType = MIME_TYPES[ext] || 'application/octet-stream';

  fs.stat(filePath, (err, stats) => {
    if (err || !stats.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not Found');
      return;
    }

    res.writeHead(200, { 'Content-Type': contentType });
    const stream = fs.createReadStream(filePath);
    stream.pipe(res);
    stream.on('error', () => {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end('Internal Server Error');
    });
  });
}

// ─── Keyword search handler ─────────────────────────────────────────────────

async function handleKeywordSearch(req, res, origin) {
  const ip = getClientIP(req);

  // Rate limiting
  if (isRateLimited(ip)) {
    logRequest(req.method, '/api/keyword-search', ip, 429, 'Rate limited');
    sendJSON(res, 429, { error: 'Too many requests. Max 30 per minute.' }, origin);
    return;
  }

  // Read and parse body
  let body;
  try {
    const raw = await readBody(req);
    body = JSON.parse(raw);
  } catch (e) {
    logRequest(req.method, '/api/keyword-search', ip, 400, 'Invalid JSON');
    sendJSON(res, 400, { error: 'Invalid JSON body' }, origin);
    return;
  }

  const keyword = (body.keyword || '').trim().toLowerCase();
  const locationCode = body.location_code || DEFAULT_LOCATION_CODE;

  if (!keyword) {
    logRequest(req.method, '/api/keyword-search', ip, 400, 'Missing keyword');
    sendJSON(res, 400, { error: 'Missing required field: keyword' }, origin);
    return;
  }

  // Check cache
  const cacheKey = `${keyword}:${locationCode}`;
  const cached = getCached(cacheKey);
  if (cached) {
    logRequest(req.method, '/api/keyword-search', ip, 200, `Cache hit for "${keyword}"`);
    sendJSON(res, 200, cached, origin);
    return;
  }

  console.log(`[${new Date().toISOString()}] Fetching DataForSEO data for "${keyword}" (location: ${locationCode})`);

  try {
    // Make both API calls in parallel
    const [volumeResult, relatedResult] = await Promise.all([
      httpsPost(
        'https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live',
        [{
          keywords: [keyword],
          location_code: locationCode,
          sort_by: 'search_volume'
        }]
      ),
      httpsPost(
        'https://api.dataforseo.com/v3/keywords_data/google_ads/keywords_for_keywords/live',
        [{
          keywords: [keyword],
          location_code: locationCode,
          sort_by: 'search_volume',
          limit: 50
        }]
      )
    ]);

    // Validate volume response
    if (
      !volumeResult ||
      volumeResult.status_code !== 20000 ||
      !volumeResult.tasks ||
      !volumeResult.tasks[0] ||
      !volumeResult.tasks[0].result
    ) {
      const errMsg = volumeResult?.tasks?.[0]?.status_message || 'Unknown error from search volume API';
      logRequest(req.method, '/api/keyword-search', ip, 502, 'Volume API error: ' + errMsg);
      sendJSON(res, 502, { error: 'DataForSEO search volume API error', detail: errMsg }, origin);
      return;
    }

    // Extract main keyword data
    const volumeData = volumeResult.tasks[0].result[0];
    const mainKeyword = {
      keyword: volumeData?.keyword || keyword,
      searchVolume: volumeData?.search_volume || 0,
      cpc: volumeData?.cpc || 0,
      competition: volumeData?.competition || 'N/A',
      competitionIndex: volumeData?.competition_index || 0,
      monthlySearches: (volumeData?.monthly_searches || []).map((m) => ({
        year: m.year,
        month: m.month,
        searchVolume: m.search_volume
      }))
    };

    // Extract related keywords
    let relatedKeywords = [];
    if (
      relatedResult &&
      relatedResult.status_code === 20000 &&
      relatedResult.tasks &&
      relatedResult.tasks[0] &&
      relatedResult.tasks[0].result
    ) {
      const items = relatedResult.tasks[0].result;
      relatedKeywords = items.map((item) => ({
        keyword: item.keyword || '',
        searchVolume: item.search_volume || 0,
        cpc: item.cpc || 0,
        competition: item.competition || 'N/A',
        competitionIndex: item.competition_index || 0
      }));
    } else {
      console.log(`[${new Date().toISOString()}] Warning: Related keywords API returned no results for "${keyword}"`);
    }

    const responseData = {
      ...mainKeyword,
      relatedKeywords
    };

    // Cache the result
    setCache(cacheKey, responseData);

    logRequest(req.method, '/api/keyword-search', ip, 200, `"${keyword}" - volume: ${mainKeyword.searchVolume}, related: ${relatedKeywords.length}`);
    sendJSON(res, 200, responseData, origin);

  } catch (err) {
    logRequest(req.method, '/api/keyword-search', ip, 500, 'Error: ' + err.message);
    sendJSON(res, 500, { error: 'Internal server error', detail: err.message }, origin);
  }
}

// ─── Create and start server ────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const parsedUrl = url.parse(req.url, true);
  const urlPath = parsedUrl.pathname;
  const origin = req.headers.origin || '';
  const ip = getClientIP(req);

  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    const headers = {
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
      'Content-Length': '0'
    };
    if (ALLOWED_ORIGINS.includes(origin)) {
      headers['Access-Control-Allow-Origin'] = origin;
    }
    res.writeHead(204, headers);
    res.end();
    logRequest('OPTIONS', urlPath, ip, 204);
    return;
  }

  // API routes
  if (urlPath === '/api/keyword-search' && req.method === 'POST') {
    await handleKeywordSearch(req, res, origin);
    return;
  }

  // Health check
  if (urlPath === '/api/health' && req.method === 'GET') {
    logRequest('GET', '/api/health', ip, 200);
    sendJSON(res, 200, {
      status: 'ok',
      cacheSize: cache.size,
      uptime: process.uptime()
    }, origin);
    return;
  }

  // Clear cache endpoint (useful for debugging)
  if (urlPath === '/api/cache-clear' && req.method === 'POST') {
    cache.clear();
    logRequest('POST', '/api/cache-clear', ip, 200, 'Cache cleared');
    sendJSON(res, 200, { status: 'cache cleared' }, origin);
    return;
  }

  // Static file serving for everything else
  if (req.method === 'GET') {
    serveStaticFile(req, res, urlPath);
    logRequest('GET', urlPath, ip, 200);
    return;
  }

  // Method not allowed
  res.writeHead(405, { 'Content-Type': 'text/plain' });
  res.end('Method Not Allowed');
  logRequest(req.method, urlPath, ip, 405);
});

server.listen(PORT, () => {
  console.log('');
  console.log('='.repeat(60));
  console.log('  Keyword Research API Proxy Server');
  console.log('='.repeat(60));
  console.log(`  Port:           ${PORT}`);
  console.log(`  Static root:    ${STATIC_ROOT}`);
  console.log(`  Cache TTL:      ${CACHE_TTL_MS / 1000 / 60} minutes`);
  console.log(`  Rate limit:     ${RATE_LIMIT_MAX} req/min per IP`);
  console.log('');
  console.log('  Endpoints:');
  console.log('    POST /api/keyword-search   - Search keyword data');
  console.log('    GET  /api/health           - Server health check');
  console.log('    POST /api/cache-clear      - Clear response cache');
  console.log('    GET  /*                    - Static files');
  console.log('');
  console.log(`  Open: http://localhost:${PORT}/`);
  console.log('='.repeat(60));
  console.log('');
});

// Graceful shutdown
process.on('SIGINT', () => {
  console.log('\nShutting down server...');
  server.close(() => {
    console.log('Server stopped.');
    process.exit(0);
  });
});

process.on('SIGTERM', () => {
  server.close(() => {
    process.exit(0);
  });
});

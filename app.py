from flask import Flask, render_template, request, jsonify
import sqlite3
import re
import requests as req_lib

app = Flask(__name__)
DB_PATH = '/opt/homewatch/data.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
    blocks = conn.execute('SELECT COUNT(DISTINCT block||street_name) FROM transactions').fetchone()[0]
    towns = conn.execute('SELECT COUNT(DISTINCT town) FROM transactions').fetchone()[0]
    town_rows = conn.execute('''
        SELECT town,
               COUNT(DISTINCT block) as block_count,
               COUNT(*) as tx_count,
               CAST(AVG(resale_price) AS INTEGER) as avg_price
        FROM transactions
        GROUP BY town ORDER BY town
    ''').fetchall()
    conn.close()
    return total, blocks, towns, [dict(r) for r in town_rows]

# Standard Singapore HDB storey premium index (ground floor = 1.000)
_STOREY_IDX = {
    '01 TO 03': 1.000, '04 TO 06': 1.020, '07 TO 09': 1.040,
    '10 TO 12': 1.060, '13 TO 15': 1.075, '16 TO 18': 1.090,
    '19 TO 21': 1.105, '22 TO 24': 1.120, '25 TO 27': 1.135,
    '28 TO 30': 1.150, '31 TO 33': 1.165, '34 TO 36': 1.180,
    '37 TO 39': 1.195, '40 TO 42': 1.210, '43 TO 45': 1.225,
}
_ALL_STOREY_BANDS = list(_STOREY_IDX.keys())

def _bala(t):
    """Bala's Table leasehold relativity — SLA 1948 framework."""
    if t <= 0:
        return 0.0
    return 1 - (1 / 1.035) ** t

def _annual_decay(t):
    """Annual lease decay % at t remaining years using Bala's Table."""
    if t <= 0:
        return 0.0
    b_now  = _bala(t)
    b_prev = _bala(t - 1)
    if b_now == 0:
        return 0.0
    return round((b_now - b_prev) / b_now * 100, 2)

def _storey_premium(storey, block, street, conn):
    rows = conn.execute("""
        SELECT storey_range, AVG(resale_price / floor_area_sqm) as avg_psqm, COUNT(*) as n
        FROM transactions WHERE block=? AND street_name=? AND floor_area_sqm > 0
        GROUP BY storey_range HAVING n >= 2
    """, (block, street)).fetchall()
    if len(rows) >= 3:
        psqm = {r['storey_range']: r['avg_psqm'] for r in rows}
        base_storey = min(psqm, key=psqm.get)
        base_val = psqm[base_storey]
        derived = {k: v / base_val for k, v in psqm.items()}
        if storey in derived:
            return derived[storey]
        # Extrapolate using standard table ratios relative to the block's base floor,
        # keeping derived and fallback values on the same scale.
        base_std = _STOREY_IDX.get(base_storey, 1.0)
        target_std = _STOREY_IDX.get(storey, 1.0)
        return target_std / base_std
    return _STOREY_IDX.get(storey, 1.0)

HDB_ABBREVS = {
    'LORONG': 'LOR', 'AVENUE': 'AVE', 'STREET': 'ST',
    'NORTH': 'NTH', 'SOUTH': 'STH', 'BUKIT': 'BT',
    'JALAN': 'JLN', 'ROAD': 'RD', 'DRIVE': 'DR',
    'CENTRAL': 'CTRL', 'CRESCENT': 'CRES', 'CLOSE': 'CL',
    'PLACE': 'PL', 'TERRACE': 'TER', 'TAMAN': 'TMN',
    'COMMONWEALTH': 'C\'WEALTH',
}

def normalize_street(street):
    return ' '.join(HDB_ABBREVS.get(w, w) for w in street.upper().split())

def resolve_postal(postal):
    try:
        r = req_lib.get(
            'https://www.onemap.gov.sg/api/common/elastic/search',
            params={'searchVal': postal, 'returnGeom': 'N', 'getAddrDetails': 'Y', 'pageNum': 1},
            timeout=5
        )
        data = r.json()
        if data.get('results'):
            res = data['results'][0]
            return res.get('BLK_NO', ''), res.get('ROAD_NAME', '')
    except Exception:
        pass
    return None, None

@app.route('/')
def index():
    total, blocks, towns, town_list = get_stats()
    return render_template('index.html', total=total, blocks=blocks, towns=towns, town_list=town_list)

def _parse_block_street(q):
    """Return (block_prefix, street_fragment) if q looks like 'number text', else None."""
    # Strip optional leading BLK / BLOCK prefix
    q = re.sub(r'^BLK\s+', '', q.strip())
    q = re.sub(r'^BLOCK\s+', '', q)
    m = re.match(r'^(\d+\w*)\s+(.+)$', q)
    if m:
        return m.group(1), m.group(2).strip()
    return None

@app.route('/autocomplete')
def autocomplete():
    q = request.args.get('q', '').strip().upper()
    if len(q) < 2:
        return jsonify([])
    conn = get_db()
    results = []

    if re.match(r'^\d{6}$', q):
        # Exactly 6 digits = postal code, resolve via OneMap
        blk, street = resolve_postal(q)
        if blk and street:
            normalized = normalize_street(street)
            row = conn.execute(
                "SELECT town, street_name FROM transactions WHERE block = ? AND street_name = ? LIMIT 1",
                (blk, normalized)
            ).fetchone()
            if not row:
                first_word = normalized.split()[0]
                row = conn.execute(
                    "SELECT town, street_name FROM transactions WHERE block = ? AND street_name LIKE ? LIMIT 1",
                    (blk, f'{first_word}%')
                ).fetchone()
            exact_street = row['street_name'] if row else normalized
            town_name = row['town'].title() if row else ''
            results.append({
                'type': 'block',
                'label': f"Blk {blk} {exact_street.title()}",
                'sublabel': town_name,
                'block': blk,
                'street': exact_street,
            })
    elif _parse_block_street(q):
        # Combined "block street" query e.g. "627 Hougang Ave 8" or "Blk 22 Ang Mo Kio"
        blk_prefix, street_frag = _parse_block_street(q)
        for row in conn.execute(
            """SELECT DISTINCT block, street_name, town
               FROM transactions WHERE block LIKE ? AND street_name LIKE ?
               ORDER BY block, street_name LIMIT 10""",
            (f'{blk_prefix}%', f'%{street_frag}%')
        ).fetchall():
            results.append({
                'type': 'block',
                'label': f"Blk {row['block']} {row['street_name'].title()}",
                'sublabel': row['town'].title(),
                'block': row['block'],
                'street': row['street_name'],
            })
    elif re.match(r'^\d', q):
        # Block number only
        for row in conn.execute(
            """SELECT DISTINCT block, street_name, town
               FROM transactions WHERE block LIKE ?
               ORDER BY block, street_name LIMIT 10""",
            (f'{q}%',)
        ).fetchall():
            results.append({
                'type': 'block',
                'label': f"Blk {row['block']} {row['street_name'].title()}",
                'sublabel': row['town'].title(),
                'block': row['block'],
                'street': row['street_name'],
            })
    else:
        # Town prefix match
        for row in conn.execute(
            "SELECT DISTINCT town FROM transactions WHERE town LIKE ? ORDER BY town LIMIT 3",
            (f'{q}%',)
        ).fetchall():
            results.append({'type': 'town', 'label': row['town'].title(), 'town': row['town']})
        # Street contains match
        for row in conn.execute(
            "SELECT DISTINCT street_name FROM transactions WHERE street_name LIKE ? ORDER BY street_name LIMIT 7",
            (f'%{q}%',)
        ).fetchall():
            results.append({'type': 'street', 'label': row['street_name'].title(), 'street': row['street_name']})

    conn.close()
    return jsonify(results[:10])

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    block = request.args.get('block', '').strip().upper()
    street = request.args.get('street', '').strip().upper()
    town = request.args.get('town', '').strip().upper()
    flat_type = request.args.get('flat_type', '').strip()

    display_q = q
    # Redirect block+street searches to the dedicated block page
    if block and street:
        street_slug = street.lower().replace(' ', '-')
        from flask import redirect
        return redirect(f'/block/{block}/{street_slug}')

    conn = get_db()
    sql = "SELECT * FROM transactions WHERE 1=1"
    count_sql = "SELECT COUNT(*) FROM transactions WHERE 1=1"
    params = []

    if block and street:
        sql += " AND block = ? AND street_name = ?"
        count_sql += " AND block = ? AND street_name = ?"
        params += [block, street]
        display_q = f"Blk {block} {street.title()}"
    elif re.match(r'^\d{6}$', q):
        # Postal code — resolve via OneMap
        blk, st = resolve_postal(q)
        if blk and st:
            sql += " AND block = ? AND street_name LIKE ?"
            count_sql += " AND block = ? AND street_name LIKE ?"
            params += [blk, f'%{st.upper()}%']
            display_q = f"Blk {blk} {st.title()}"
    elif q and _parse_block_street(q.upper()):
        # Combined "block street" query e.g. "627 Hougang Ave 8"
        blk_prefix, street_frag = _parse_block_street(q.upper())
        matches = conn.execute(
            """SELECT DISTINCT block, street_name FROM transactions
               WHERE block LIKE ? AND street_name LIKE ?
               ORDER BY block, street_name LIMIT 2""",
            (f'{blk_prefix}%', f'%{street_frag}%')
        ).fetchall()
        if len(matches) == 1:
            # Unique match — redirect straight to block page
            from flask import redirect
            conn.close()
            slug = matches[0]['street_name'].lower().replace(' ', '-')
            return redirect(f'/block/{matches[0]["block"]}/{slug}')
        else:
            sql += " AND block LIKE ? AND street_name LIKE ?"
            count_sql += " AND block LIKE ? AND street_name LIKE ?"
            params += [f'{blk_prefix}%', f'%{street_frag}%']
            display_q = q
    elif q:
        q_upper = q.upper()
        sql += " AND (block LIKE ? OR street_name LIKE ?)"
        count_sql += " AND (block LIKE ? OR street_name LIKE ?)"
        params += [f'%{q_upper}%', f'%{q_upper}%']

    if town:
        sql += " AND town = ?"
        count_sql += " AND town = ?"
        params.append(town)
    if flat_type:
        sql += " AND flat_type = ?"
        count_sql += " AND flat_type = ?"
        params.append(flat_type)

    rows = conn.execute(sql + " ORDER BY month DESC LIMIT 200", params).fetchall()
    count = conn.execute(count_sql, params).fetchone()[0]
    avg = int(sum(r['resale_price'] for r in rows) / len(rows)) if rows else None
    conn.close()

    return render_template('results.html', rows=rows, query=display_q, town=town,
                           flat_type=flat_type, count=count, avg=avg,
                           block_param=block, street_param=street)

@app.route('/town/<town_name>')
def town(town_name):
    conn = get_db()
    # Resolve slug → actual DB town name so KALLANG/WHAMPOA (slug: kallang-whampoa) works
    town_row = conn.execute("""
        SELECT DISTINCT town FROM transactions
        WHERE LOWER(REPLACE(REPLACE(town,' ','-'),'/','-')) = ?
        LIMIT 1
    """, (town_name.lower(),)).fetchone()
    town_upper = town_row['town'] if town_row else town_name.upper().replace('-', ' ')
    rows = conn.execute(
        "SELECT * FROM transactions WHERE town = ? ORDER BY month DESC LIMIT 200",
        (town_upper,)
    ).fetchall()
    count = conn.execute("SELECT COUNT(*) FROM transactions WHERE town = ?", (town_upper,)).fetchone()[0]
    avg = conn.execute("SELECT CAST(AVG(resale_price) AS INTEGER) FROM transactions WHERE town = ?", (town_upper,)).fetchone()[0]
    conn.close()
    return render_template('results.html', rows=rows, query='', town=town_upper,
                           flat_type='', count=count, avg=avg,
                           block_param='', street_param='')

@app.route('/block/<block_no>/<street_slug>')
def block_page(block_no, street_slug):
    from datetime import datetime
    conn = get_db()
    # Resolve street name from slug via DB
    row = conn.execute(
        "SELECT DISTINCT street_name FROM transactions WHERE block = ? AND LOWER(REPLACE(street_name,' ','-')) = ?",
        (block_no.upper(), street_slug.lower())
    ).fetchone()
    if not row:
        conn.close()
        from flask import abort
        abort(404)
    block = block_no.upper()
    street = row['street_name']
    flat_type = request.args.get('flat_type', '').strip()

    sql = "SELECT * FROM transactions WHERE block = ? AND street_name = ?"
    params = [block, street]
    if flat_type:
        sql += " AND flat_type = ?"
        params.append(flat_type)
    rows = conn.execute(sql + " ORDER BY month DESC LIMIT 300", params).fetchall()
    count = conn.execute("SELECT COUNT(*) FROM transactions WHERE block=? AND street_name=?", (block, street)).fetchone()[0]
    avg_row = conn.execute("SELECT CAST(AVG(resale_price) AS INTEGER) FROM transactions WHERE block=? AND street_name=?", (block, street)).fetchone()
    avg = avg_row[0] if avg_row else None

    latest = conn.execute(
        "SELECT month, resale_price, flat_type FROM transactions WHERE block=? AND street_name=? ORDER BY month DESC LIMIT 1",
        (block, street)
    ).fetchone()

    # Median $/sqm
    all_psqm = conn.execute(
        "SELECT resale_price, floor_area_sqm FROM transactions WHERE block=? AND street_name=? AND floor_area_sqm > 0",
        (block, street)
    ).fetchall()
    psqm_list = sorted(r['resale_price'] / r['floor_area_sqm'] for r in all_psqm)
    median_psqm = int(psqm_list[len(psqm_list) // 2]) if psqm_list else None

    # 5-year trend — avg $/sqm rolling 12-month window vs same window 5 years prior
    now_dt = datetime.now()
    now = now_dt.year
    def _avg_psqm(rows):
        vals = [r['resale_price'] / r['floor_area_sqm']
                for r in rows if r['floor_area_sqm'] and r['floor_area_sqm'] > 0]
        return sum(vals) / len(vals) if vals else None

    cutoff_recent = f'{now_dt.year - 1}-{now_dt.month:02d}'
    cutoff_old_start = f'{now_dt.year - 6}-{now_dt.month:02d}'
    cutoff_old_end   = f'{now_dt.year - 5}-{now_dt.month:02d}'
    recent_rows = conn.execute(
        "SELECT resale_price, floor_area_sqm FROM transactions WHERE block=? AND street_name=? AND month >= ?",
        (block, street, cutoff_recent)
    ).fetchall()
    old_rows = conn.execute(
        "SELECT resale_price, floor_area_sqm FROM transactions WHERE block=? AND street_name=? AND month >= ? AND month < ?",
        (block, street, cutoff_old_start, cutoff_old_end)
    ).fetchall()
    recent_mpsqm = _avg_psqm(recent_rows)
    old_mpsqm    = _avg_psqm(old_rows)
    trend_5y = round((recent_mpsqm - old_mpsqm) / old_mpsqm * 100, 1) if recent_mpsqm and old_mpsqm else None

    flat_types = [r['flat_type'] for r in conn.execute(
        "SELECT DISTINCT flat_type FROM transactions WHERE block=? AND street_name=? ORDER BY flat_type",
        (block, street)
    ).fetchall()]

    lease_row = conn.execute(
        "SELECT lease_commence_date, town FROM transactions WHERE block=? AND street_name=? LIMIT 1",
        (block, street)
    ).fetchone()
    lease_year = lease_row['lease_commence_date'] if lease_row else None
    town = lease_row['town'] if lease_row else ''
    lease_remaining = (int(lease_year) + 99) - now if lease_year else None
    mop_cleared = int(lease_year) + 5 <= now if lease_year else None
    lease_decay = _annual_decay(lease_remaining) if lease_remaining else None
    appreciation_rate = round(trend_5y / 5, 1) if trend_5y is not None else None

    # Town rankings — single query for all blocks in town
    town_blocks = conn.execute("""
        SELECT block, street_name,
               AVG(resale_price) as avg_p,
               AVG(resale_price / NULLIF(floor_area_sqm, 0)) as avg_psqm,
               AVG(CASE WHEN month >= ? THEN resale_price ELSE NULL END) as recent_p,
               AVG(CASE WHEN month >= ? AND month < ? THEN resale_price ELSE NULL END) as old_p
        FROM transactions WHERE town = ?
        GROUP BY block, street_name
    """, (f'{now-1}-01', f'{now-6}-01', f'{now-5}-01', town)).fetchall()

    n = len(town_blocks)
    rankings = None
    if n and avg:
        r_price = sum(1 for b in town_blocks if (b['avg_p'] or 0) > avg) + 1
        r_psqm  = sum(1 for b in town_blocks if (b['avg_psqm'] or 0) > (median_psqm or 0)) + 1
        valid_growth = [(b['recent_p'] - b['old_p']) / b['old_p'] * 100
                        for b in town_blocks if b['recent_p'] and b['old_p']]
        n_g = len(valid_growth)
        r_growth = (sum(1 for g in valid_growth if g > (trend_5y or 0)) + 1) if n_g else None
        rankings = {
            'total': n, 'growth_total': n_g,
            'price_rank': r_price,  'price_pct':  round(r_price  / n   * 100),
            'psqm_rank':  r_psqm,   'psqm_pct':   round(r_psqm   / n   * 100),
            'growth_rank': r_growth, 'growth_pct': round(r_growth / n_g * 100) if r_growth else None,
        }

    # All standard storey bands up to the block's highest recorded floor
    max_row = conn.execute(
        "SELECT storey_range FROM transactions WHERE block=? AND street_name=? ORDER BY storey_range DESC LIMIT 1",
        (block, street)
    ).fetchone()
    if max_row and max_row['storey_range'] in _ALL_STOREY_BANDS:
        max_idx = _ALL_STOREY_BANDS.index(max_row['storey_range'])
        storey_bands = _ALL_STOREY_BANDS[:max_idx + 1]
    else:
        storey_bands = _ALL_STOREY_BANDS

    conn.close()
    return render_template('block.html', rows=rows, block=block, street=street, town=town,
                           flat_type=flat_type, flat_types=flat_types, count=count, avg=avg,
                           latest=latest, median_psqm=median_psqm, trend_5y=trend_5y,
                           lease_year=lease_year, lease_remaining=lease_remaining,
                           mop_cleared=mop_cleared, lease_decay=lease_decay,
                           appreciation_rate=appreciation_rate, rankings=rankings,
                           storey_bands=storey_bands, now=now)

@app.route('/sitemap.xml')
def sitemap():
    from flask import Response
    conn = get_db()
    towns = conn.execute("SELECT DISTINCT town FROM transactions ORDER BY town").fetchall()
    blocks = conn.execute(
        "SELECT DISTINCT block, street_name FROM transactions ORDER BY street_name, block"
    ).fetchall()
    conn.close()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        '  <url><loc>https://homewatch.sg/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>',
    ]
    for t in towns:
        slug = t['town'].lower().replace(' ', '-').replace('/', '-')
        lines.append(f'  <url><loc>https://homewatch.sg/town/{slug}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>')
    for b in blocks:
        street_slug = b['street_name'].lower().replace(' ', '-')
        lines.append(f'  <url><loc>https://homewatch.sg/block/{b["block"]}/{street_slug}</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>')
    lines.append('</urlset>')
    return Response('\n'.join(lines), mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    from flask import Response
    content = "User-agent: *\nAllow: /\nSitemap: https://homewatch.sg/sitemap.xml\n"
    return Response(content, mimetype='text/plain')

@app.route('/api/estimate')
def api_estimate():
    from datetime import datetime
    block     = request.args.get('block',     '').strip().upper()
    street    = request.args.get('street',    '').strip().upper()
    flat_type = request.args.get('flat_type', '').strip()
    town      = request.args.get('town',      '').strip().upper()
    if not flat_type:
        return jsonify({'error': 'Missing flat_type'})
    conn = get_db()
    # Resolve town from block/street if not supplied
    if not town and block and street:
        row = conn.execute(
            "SELECT town FROM transactions WHERE block=? AND street_name=? LIMIT 1",
            (block, street)
        ).fetchone()
        if row:
            town = row['town']

    storey = request.args.get('storey', '').strip()
    now     = datetime.now()
    cutoff  = f"{now.year - 2}-{now.month:02d}"  # 24-month window

    cutoff_12 = f"{now.year - 1}-{now.month:02d}"

    if block and street:
        # Reference area: avg floor_area of all recent flat_type txns at this block (24m).
        # Using this for all floor estimates ensures $/sqm is the sole monotonicity driver —
        # it prevents unit-size differences between floors from inverting the price ladder.
        ref_area_rows = conn.execute("""
            SELECT floor_area_sqm FROM transactions
            WHERE block=? AND street_name=? AND flat_type=? AND month>=? AND floor_area_sqm>0
        """, (block, street, flat_type, cutoff)).fetchall()
        ref_area = (sum(r['floor_area_sqm'] for r in ref_area_rows) / len(ref_area_rows)
                    if ref_area_rows else None)

        t1a_final_est = t1a_final_psqm = t1a_final_count = None
        t1a_final_prices: list = []
        t1a_final_note = ''

        if storey and storey in _ALL_STOREY_BANDS:
            # Exact-floor estimate (last 12 months)
            t1a_est = t1a_psqm = t1a_count = None
            t1a_prices = []
            t1a_note = ''
            floor_rows = conn.execute("""
                SELECT resale_price, floor_area_sqm FROM transactions
                WHERE block=? AND street_name=? AND flat_type=? AND storey_range=? AND month>=?
                  AND floor_area_sqm>0 ORDER BY month DESC
            """, (block, street, flat_type, storey, cutoff_12)).fetchall()
            if floor_rows:
                psqm_list = [r['resale_price'] / r['floor_area_sqm'] for r in floor_rows]
                avg_psqm  = sum(psqm_list) / len(psqm_list)
                floor_area = sum(r['floor_area_sqm'] for r in floor_rows) / len(floor_rows)
                use_area  = ref_area if ref_area else floor_area
                t1a_est   = int(avg_psqm * use_area)
                t1a_psqm  = int(avg_psqm)
                t1a_count = len(floor_rows)
                t1a_prices = sorted(r['resale_price'] for r in floor_rows)
                n = t1a_count
                t1a_note = f"Based on {n} {'sale' if n==1 else 'sales'} in this block at floor {storey} (last 12 months) · S${t1a_psqm:,}/sqm"

            # Cascade: scan ALL lower floors (not just nearest), keep the highest result.
            # This prevents a low-psqm adjacent floor from pulling down higher-floor estimates
            # when that adjacent floor's own estimate was elevated by cascading from even lower.
            casc_est = casc_psqm = casc_count = None
            casc_prices = []
            casc_note = ''
            target_idx = _ALL_STOREY_BANDS.index(storey)
            for i in range(target_idx - 1, -1, -1):
                lower_band = _ALL_STOREY_BANDS[i]
                lower_rows = conn.execute("""
                    SELECT resale_price, floor_area_sqm FROM transactions
                    WHERE block=? AND street_name=? AND flat_type=? AND storey_range=? AND month>=?
                      AND floor_area_sqm>0 ORDER BY month DESC
                """, (block, street, flat_type, lower_band, cutoff_12)).fetchall()
                if lower_rows:
                    lpsqm = sum(r['resale_price']/r['floor_area_sqm'] for r in lower_rows) / len(lower_rows)
                    larea = sum(r['floor_area_sqm'] for r in lower_rows) / len(lower_rows)
                    ratio = _STOREY_IDX[storey] / _STOREY_IDX.get(lower_band, 1.0)
                    adj   = lpsqm * ratio
                    this_casc = int(adj * (ref_area if ref_area else larea))
                    if casc_est is None or this_casc > casc_est:
                        casc_est    = this_casc
                        casc_psqm   = int(adj)
                        casc_count  = len(lower_rows)
                        casc_prices = sorted(r['resale_price'] for r in lower_rows)
                        casc_note   = f"Based on floor {lower_band} data, scaled to floor {storey} (last 12 months) · S${casc_psqm:,}/sqm"

            # Best Tier 1a: higher of exact and cascade
            if t1a_est is not None or casc_est is not None:
                if casc_est is not None and (t1a_est is None or casc_est > t1a_est):
                    t1a_final_est, t1a_final_psqm, t1a_final_count = casc_est, casc_psqm, casc_count
                    t1a_final_prices, t1a_final_note = casc_prices, casc_note
                else:
                    t1a_final_est, t1a_final_psqm, t1a_final_count = t1a_est, t1a_psqm, t1a_count
                    t1a_final_prices, t1a_final_note = t1a_prices, t1a_note

        # Tier 1b: ≥3 block-level transactions in last 24 months.
        # Normalised to base floor then scaled — monotonically increasing by construction.
        block_rows = conn.execute("""
            SELECT resale_price, floor_area_sqm, storey_range, month FROM transactions
            WHERE block=? AND street_name=? AND flat_type=? AND month>=? AND floor_area_sqm>0
            ORDER BY month DESC
        """, (block, street, flat_type, cutoff)).fetchall()

        t1b_est = t1b_psqm = t1b_count = None
        t1b_prices = []
        t1b_note = ''
        if len(block_rows) >= 3:
            avg_area = sum(r['floor_area_sqm'] for r in block_rows) / len(block_rows)
            if storey and storey in _STOREY_IDX:
                norm_vals = [r['resale_price'] / r['floor_area_sqm'] / _STOREY_IDX.get(r['storey_range'], 1.0)
                             for r in block_rows]
                base_psqm = sum(norm_vals) / len(norm_vals)
                adj_psqm  = base_psqm * _STOREY_IDX[storey]
                t1b_est   = int(adj_psqm * avg_area)
                t1b_psqm  = int(adj_psqm)
                t1b_note  = f"Based on {len(block_rows)} recent {flat_type} sales, adjusted for floor {storey} (last 24 months) · S${t1b_psqm:,}/sqm"
            else:
                avg_psqm  = sum(r['resale_price']/r['floor_area_sqm'] for r in block_rows) / len(block_rows)
                t1b_est   = int(avg_psqm * avg_area)
                t1b_psqm  = int(avg_psqm)
                t1b_note  = f"Based on {len(block_rows)} recent {flat_type} sales (last 24 months) · S${t1b_psqm:,}/sqm"
            t1b_count  = len(block_rows)
            t1b_prices = sorted(r['resale_price'] for r in block_rows)

        # Cross-tier max: T1b is monotonically increasing by floor, so it acts as a lower bound.
        # This ensures floor-specific T1a data falling below the block average cannot invert
        # the price ladder (e.g. a real 04-06 sale below what T1b predicts for 04-06).
        if t1a_final_est is not None or t1b_est is not None:
            if t1b_est is not None and (t1a_final_est is None or t1b_est > t1a_final_est):
                est_out, psqm_out, count_out = t1b_est, t1b_psqm, t1b_count
                prices_out, note_out = t1b_prices, t1b_note
            else:
                est_out, psqm_out, count_out = t1a_final_est, t1a_final_psqm, t1a_final_count
                prices_out, note_out = t1a_final_prices, t1a_final_note
            conn.close()
            return jsonify({
                'estimate': est_out,
                'low':   int(prices_out[0]),
                'high':  int(prices_out[-1]),
                'count': count_out,
                'psqm':  psqm_out,
                'note':  note_out,
            })

        # Tier 2: most recent sale at block — scale to target floor using premium ratio
        anchor = conn.execute("""
            SELECT resale_price, storey_range, floor_area_sqm, month FROM transactions
            WHERE block=? AND street_name=? AND flat_type=? AND floor_area_sqm>0
            ORDER BY month DESC LIMIT 1
        """, (block, street, flat_type)).fetchone()
        if anchor:
            anchor_psqm = anchor['resale_price'] / anchor['floor_area_sqm']
            if storey and storey in _STOREY_IDX and anchor['storey_range'] in _STOREY_IDX:
                ratio      = _STOREY_IDX[storey] / _STOREY_IDX[anchor['storey_range']]
                adj_psqm   = anchor_psqm * ratio
                use_area   = ref_area if ref_area else anchor['floor_area_sqm']
                estimate   = int(adj_psqm * use_area)
                note = f"Latest sale at this block ({anchor['month']}, {anchor['storey_range']}), scaled to floor {storey}"
            else:
                use_area = ref_area if ref_area else anchor['floor_area_sqm']
                estimate = int(anchor_psqm * use_area)
                note = f"Latest sale at this block ({anchor['month']}, {anchor['storey_range']})"
            conn.close()
            return jsonify({
                'estimate': estimate,
                'low':      int(anchor['resale_price']),
                'high':     int(anchor['resale_price']),
                'count':    1,
                'note':     note,
            })

    # Tier 3 fallback: town-wide median (used when no block data at all)
    if not town:
        conn.close()
        return jsonify({'estimate': None, 'count': 0})

    town_rows = conn.execute("""
        SELECT resale_price FROM transactions
        WHERE town=? AND flat_type=? AND month >= ?
        ORDER BY resale_price
    """, (town, flat_type, cutoff)).fetchall()
    conn.close()
    if not town_rows:
        return jsonify({'estimate': None, 'count': 0})
    prices = [r['resale_price'] for r in town_rows]
    n = len(prices)
    mid = n // 2
    median = int(prices[mid]) if n % 2 == 1 else int((prices[mid - 1] + prices[mid]) / 2)
    return jsonify({
        'estimate': median,
        'low':   int(prices[0]),
        'high':  int(prices[-1]),
        'count': n,
        'note':  f'Median of {n:,} {flat_type} transactions in {town.title()} (last 12 months)',
    })

@app.route('/api/trends')
def api_trends():
    block = request.args.get('block', '').strip().upper()
    street = request.args.get('street', '').strip().upper()
    town = request.args.get('town', '').strip().upper()
    flat_type = request.args.get('flat_type', '').strip()
    conn = get_db()
    sql = """SELECT substr(month,1,4) as year, flat_type,
                    CAST(AVG(resale_price) AS INTEGER) as avg_price,
                    COUNT(*) as txn_count
             FROM transactions WHERE 1=1"""
    params = []
    if block and street:
        sql += " AND block = ? AND street_name = ?"
        params += [block, street]
    elif town:
        sql += " AND town = ?"
        params.append(town)
    if flat_type:
        sql += " AND flat_type = ?"
        params.append(flat_type)
    sql += " GROUP BY year, flat_type ORDER BY year, flat_type"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

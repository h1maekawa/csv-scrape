import os
import unicodedata
import difflib
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from serpapi import GoogleSearch
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import time
import re
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict

# ============================================================
# ページ設定
# ============================================================
st.set_page_config(
    page_title="店舗電話番号抽出・リスト補完ツール",
    page_icon="📞",
    layout="wide"
)

load_dotenv()

if 'api_key' not in st.session_state:
    st.session_state.api_key = os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY') or ""

# ============================================================
# 検索結果キャッシュ（スレッドセーフ）
# ============================================================
_search_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()

def _cache_key(store_name: str, location_str: Optional[str], location_hint: Optional[str]) -> str:
    raw = f"{store_name}|{location_str}|{location_hint}"
    return hashlib.md5(raw.encode()).hexdigest()

def get_cached_result(store_name, location_str, location_hint):
    key = _cache_key(store_name, location_str, location_hint)
    with _cache_lock:
        return _search_cache.get(key)

def set_cached_result(store_name, location_str, location_hint, result):
    key = _cache_key(store_name, location_str, location_hint)
    with _cache_lock:
        _search_cache[key] = result

def clear_cache():
    with _cache_lock:
        _search_cache.clear()

def get_cache_count():
    with _cache_lock:
        return len(_search_cache)

# ============================================================
# ジオコーディング・ユーティリティ
# ============================================================
@st.cache_data(ttl=3600)
def get_coordinates_from_address(address):
    try:
        geolocator = Nominatim(user_agent="phone_number_app")
        location = geolocator.geocode(address, timeout=10)
        if location:
            return {
                'latitude': location.latitude,
                'longitude': location.longitude,
                'address': location.address,
                'success': True
            }
        else:
            return {'success': False, 'error': '場所が見つかりませんでした'}
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        return {'success': False, 'error': f'ジオコーディングエラー: {str(e)}'}
    except Exception as e:
        return {'success': False, 'error': f'エラー: {str(e)}'}

def radius_to_zoom_level(radius_meters):
    if radius_meters <= 500:    return 16
    elif radius_meters <= 1000: return 15
    elif radius_meters <= 2000: return 14
    elif radius_meters <= 5000: return 13
    elif radius_meters <= 10000: return 12
    elif radius_meters <= 20000: return 11
    else: return 10

def calculate_distance(lat1, lon1, lat2, lon2):
    return geodesic((lat1, lon1), (lat2, lon2)).meters

# ============================================================
# 表記正規化・ファジーマッチング
# ============================================================
def normalize_name(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[\s\u3000]+', '', text)
    text = text.lower()
    text = re.sub(r'[^\w\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]', '', text)
    return text

def fuzzy_score(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()

def build_name_variants(store_name: str) -> list:
    """店名から紹介文や英語名を切り分けた強力な検索バリエーションを生成"""
    base = store_name.strip()
    variants = [base]

    # カッコとその中身を除去
    no_parentheses = re.sub(r'[\(（].*?[\)）]', '', base).strip()
    if no_parentheses and no_parentheses != base:
        variants.append(no_parentheses)
        base_for_split = no_parentheses
    else:
        base_for_split = base

    # スペースや区切り記号で分割
    parts = re.split(r'[\s\u3000,._、。・]+', base_for_split)
    parts = [p.strip() for p in parts if p.strip()]
    
    if len(parts) > 1:
        # 最後の単語（「お茶とパンの店 tePle」➔「tePle」を救出）
        variants.append(parts[-1])
        # 最初の単語
        variants.append(parts[0])
        
        # アルファベット英数字単語のみを結合して抽出
        english_words = re.findall(r'[a-zA-Z0-9\'\-&]+', base_for_split)
        if english_words:
            eng_name = " ".join(english_words).strip()
            if eng_name and eng_name != base:
                variants.append(eng_name)

    # 「〜の店」「〜のお店」などのフレーズ以降を抽出
    if 'の店' in base_for_split:
        after_shop = base_for_split.split('の店')[-1].strip()
        if after_shop: variants.append(after_shop)
    if 'のお店' in base_for_split:
        after_shop = base_for_split.split('のお店')[-1].strip()
        if after_shop: variants.append(after_shop)

    # サフィックス（店、支店など）の除去
    stripped = re.sub(r'[\s\u3000]*(\S+店|支店|本店|分店)$', '', base_for_split).strip()
    if stripped and stripped != base:
        variants.append(stripped)

    # スペース除去
    no_space = re.sub(r'[\s\u3000]+', '', base_for_split)
    if no_space != base:
        variants.append(no_space)

    # 重複除去（順序維持）
    seen = set()
    result = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result

def is_valid_match(hit_title: str, input_name: str, current_variant: str) -> bool:
    """ヒットした店舗名が、入力された店名の部分一致やバリエーションと合致するか柔軟に判定"""
    if not hit_title: return False
    h_norm = normalize_name(hit_title)
    i_norm = normalize_name(input_name)
    v_norm = normalize_name(current_variant)
    
    if not h_norm: return False
        
    # 1. 完全に含まれているか、含んでいるか（部分一致・紹介文混在を許容）
    if h_norm in i_norm or i_norm in h_norm:
        return True
    # 2. 現在の検索バリエーション（例: tePle）とヒットしたタイトルが相互に部分一致
    if v_norm and (v_norm in h_norm or h_norm in v_norm):
        return True
    if fuzzy_score(hit_title, current_variant) >= 0.70:
        return True
    # 3. 総合的な類似度が40%以上
    if fuzzy_score(hit_title, input_name) >= 0.40:
        return True
        
    return False

# ============================================================
# スコアリング
# ============================================================
def score_place(place, query=""):
    score = 0
    title = place.get('title', '')

    if place.get('phone') or place.get('formatted_phone_number'):
        score += 50
    if place.get('address'):
        score += 20
    if place.get('rating'):
        score += 10
    reviews = place.get('reviews', 0) or 0
    if reviews:
        score += 10
        try:
            score += min(int(int(reviews) / 100), 10)
        except (ValueError, TypeError):
            pass

    if query:
        query_clean = query.strip()
        if title == query_clean:
            score += 40
        elif title.startswith(query_clean):
            score += 25
        elif query_clean in title:
            score += 15
        elif title in query_clean and len(title) >= 2:
            score += 10
        else:
            ratio = fuzzy_score(title, query_clean)
            if ratio >= 0.85:   score += 35
            elif ratio >= 0.70: score += 20
            elif ratio >= 0.55: score += 8
            else:               score -= 20

    if any(x in title for x in ['閉店', '廃業', '跡地', '移転']):
        score -= 80
    if any(x in title for x in ['支店', '本店', '店']):
        score -= 5

    return score

def calculate_confidence(result):
    has_phone = bool(result.get('電話番号'))
    has_address = bool(result.get('住所'))
    has_coords = bool(result.get('緯度') and result.get('経度'))

    if has_phone and has_address and has_coords:
        return 'Very High'
    elif has_phone and has_address:
        return 'High'
    elif has_phone:
        return 'Mid'
    else:
        return 'Low'

# ============================================================
# Organic検索フォールバック（詳細住所掛け合わせ仕様）
# ============================================================
def search_phone_from_organic(store_name, location_hint, api_key):
    try:
        variants = build_name_variants(store_name)
        search_names = [store_name]
        if len(variants) > 1:
            search_names.append(variants[1]) # 分割されたクリーンな単語（"tePle" など）

        for s_name in search_names:
            query = f"{s_name} 電話番号"
            if location_hint:
                # Web検索（Organic）は詳細な住所をぶつけた方が上位ヒット・スニペット抽出を狙いやすい
                query = f"{s_name} {location_hint.strip()} 電話番号"

            params = {
                "engine": "google",
                "q": query,
                "api_key": api_key,
                "num": 4,
                "hl": "ja",
                "gl": "jp"
            }
            search = GoogleSearch(params)
            results = search.get_dict()

            kg = results.get("knowledge_graph", {})
            if kg.get("phone"): return kg.get("phone")
            if kg.get("formatted_phone_number"): return kg.get("formatted_phone_number")
            
            local_results = results.get("local_results", {})
            if isinstance(local_results, list) and len(local_results) > 0:
                if local_results[0].get("phone"): return local_results[0].get("phone")
            elif isinstance(local_results, dict):
                places = local_results.get("places", [])
                if places and places[0].get("phone"): return places[0].get("phone")

            phone_patterns = [
                r'0120-\d{3}-\d{3}',
                r'0800-\d{3}-\d{4}',
                r'0\d{1,3}-\d{2,4}-\d{3,4}',
                r'\(\d{2,4}\)\d{3,4}-\d{3,4}',
            ]
            
            for r in results.get("organic_results", []):
                snippet = r.get("snippet", "")
                if not snippet: continue
                snippet_norm = unicodedata.normalize('NFKC', snippet)
                for pattern in phone_patterns:
                    match = re.search(pattern, snippet_norm)
                    if match:
                        return match.group()
        return ""
    except Exception:
        return ""

# ============================================================
# 店舗検索（マップ用住所丸め ＆ 柔軟な判定ロジック）
# ============================================================
def search_store_by_name(store_name, location_str=None, api_key=None, location_hint=None):
    cached = get_cached_result(store_name, location_str, location_hint)
    if cached is not None:
        return cached

    EMPTY = {
        'success': False,
        '店舗名': '', '電話番号': '', '住所': '',
        '緯度': None, '経度': None, '評価': '', 'レビュー数': '', '信頼度': 'Low',
        'error': 'Googleマップに店舗が見つかりませんでした。'
    }

    if not api_key:
        return {**EMPTY, 'error': 'APIキーが設定されていません'}

    try:
        base_name = store_name.strip()
        name_variants = build_name_variants(base_name)
        
        # --- ★マップ検索用の緩い地域ヒント（市区町村まで）を自動抽出 ---
        map_hint = ""
        if location_hint:
            m = re.search(r'^([^市区町村郡]+?[市区町村郡])', location_hint.strip())
            if m:
                map_hint = m.group(1)
            else:
                map_hint = location_hint.strip()[:10]

        last_backup_result = None

        # すべての店名バリエーションを走査
        for variant in name_variants:
            query = variant
            if map_hint and not location_str:
                query = f"{variant} {map_hint}"
                
            params = {
                "engine": "google_maps",
                "q": query,
                "api_key": api_key,
                "type": "search",
                "hl": "ja",
                "gl": "jp",
            }
            if location_str:
                params["ll"] = location_str
                
            search = GoogleSearch(params)
            results = search.get_dict()

            place = None
            # ① 複数候補から最良かつ店名が合致するものを選択
            if results and 'local_results' in results:
                local_results = results.get('local_results', [])
                scored = []
                for p in local_results:
                    title = p.get('title', '')
                    if is_valid_match(title, base_name, variant):
                        scored.append((p, score_place(p, query)))
                scored.sort(key=lambda x: x[1], reverse=True)
                if scored:
                    place = scored[0][0]

            # ② 単一直接マッチの確認
            elif results and 'place_results' in results:
                p_res = results['place_results']
                if is_valid_match(p_res.get('title', ''), base_name, variant):
                    place = p_res

            if place is not None:
                gps = place.get('gps_coordinates', {})
                phone = place.get('phone') or place.get('formatted_phone_number') or place.get('電話', '')
                
                # 詳細検索（data_cidバグ修正）
                if not phone:
                    place_id = place.get('place_id')
                    data_cid = place.get('data_cid')
                    
                    if place_id or data_cid:
                        try:
                            detail_params = {
                                "engine": "google_maps",
                                "api_key": api_key,
                                "hl": "ja",
                                "gl": "jp",
                            }
                            if place_id:
                                detail_params["place_id"] = place_id
                            else:
                                detail_params["data_cid"] = data_cid

                            detail_search = GoogleSearch(detail_params)
                            detail_results = detail_search.get_dict()
                            if "place_results" in detail_results:
                                detail_place = detail_results["place_results"]
                                phone = detail_place.get('phone') or detail_place.get('formatted_phone_number') or detail_place.get('電話', '')
                        except Exception:
                            pass

                r = {
                    'success': True,
                    '店舗名': place.get('title', store_name),
                    '電話番号': phone,
                    '住所': place.get('address') or place.get('住所', ''),
                    '緯度': gps.get('latitude') if gps else None,
                    '経度': gps.get('longitude') if gps else None,
                    '評価': place.get('rating', ''),
                    'レビュー数': place.get('reviews', ''),
                }
                r['信頼度'] = calculate_confidence(r)

                if phone:
                    set_cached_result(store_name, location_str, location_hint, r)
                    return r
                else:
                    if not last_backup_result:
                        last_backup_result = r

        # ===== ② 最終フォールバック（Organic Web検索、詳細住所掛け合わせ） =====
        phone_from_organic = search_phone_from_organic(store_name, location_hint, api_key)
        if phone_from_organic:
            final_r = {
                'success': True,
                '店舗名': last_backup_result['店舗名'] if last_backup_result else store_name,
                '電話番号': phone_from_organic,
                '住所': last_backup_result['住所'] if last_backup_result else '',
                '緯度': last_backup_result['緯度'] if last_backup_result else None,
                '経度': last_backup_result['経度'] if last_backup_result else None,
                '評価': last_backup_result['評価'] if last_backup_result else '',
                'レビュー数': last_backup_result['レビュー数'] if last_backup_result else '',
                '信頼度': 'Mid'
            }
            set_cached_result(store_name, location_str, location_hint, final_r)
            return final_r

        if last_backup_result:
            last_backup_result['error'] = '店舗は見つかりましたが、Googleマップ上に電話番号の掲載がありませんでした。'
            set_cached_result(store_name, location_str, location_hint, last_backup_result)
            return last_backup_result

        set_cached_result(store_name, location_str, location_hint, EMPTY)
        return EMPTY

    except Exception as e:
        return {**EMPTY, 'error': f'エラーが発生しました: {str(e)}'}

# ============================================================
# 並列検索ワーカー
# ============================================================
def _search_worker_csv(task_args):
    idx, name, row_addr, location_str_csv, api_key = task_args
    result = search_store_by_name(name, location_str=location_str_csv, api_key=api_key, location_hint=row_addr if row_addr else None)
    return idx, result

# ============================================================
# UIの構築（Streamlit）
# ============================================================
st.sidebar.header("⚙️ 設定")

import socket
try:
    _hostname = socket.gethostname()
    _is_local = any([
        os.getenv('STREAMLIT_SERVER_ADDRESS', '').startswith('localhost'),
        os.getenv('STREAMLIT_SERVER_ADDRESS', '') == '',
        _hostname in ('localhost', '127.0.0.1'),
    ])
except Exception:
    _is_local = False

with st.sidebar.expander("🔑 SerpAPI キー設定", expanded=not bool(st.session_state.api_key)):
    new_api_key = st.text_input("API キーを入力", value="", type="password", placeholder="SerpAPIキーを入力してください")
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("設定を保存", use_container_width=True):
            if new_api_key.strip():
                st.session_state.api_key = new_api_key.strip()
                st.success("保存しました")
                st.rerun()
    with col_btn2:
        if st.button("クリア", use_container_width=True):
            st.session_state.api_key = ""
            st.rerun()
    if _is_local and (os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY')):
        if st.button("🏠 .envから読み込む", use_container_width=True):
            st.session_state.api_key = os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY')
            st.success("読み込みました")
            st.rerun()

api_key = st.session_state.api_key
if not api_key:
    st.sidebar.error("⚠️ APIキーが設定されていません。")
else:
    st.sidebar.success("✅ APIキー設定済み")

st.sidebar.markdown("---")
st.sidebar.markdown("### ⚡ 並列処理設定")
max_workers = st.sidebar.slider("並列スレッド数", min_value=1, max_value=10, value=5, help="推奨: 3〜5")

cache_count = get_cache_count()
st.sidebar.markdown(f"🗄️ **検索キャッシュ**: {cache_count}件")
if st.sidebar.button("🗑️ キャッシュをクリア", use_container_width=True):
    clear_cache()
    st.sidebar.success("キャッシュをクリアしました")

# メイン画面
st.title("📞 店舗電話番号 抽出・上書き補完ツール")
st.markdown("既存の営業リスト（CSV/Excel）を読み込み、**電話番号が空欄、または無効な番号（135等）の行のみ**をGoogle Mapsから自動追記・補完します。")

st.info("💡 **高精度アップデート**: アップロードされたリストの「住所」列と連動して検索するため、「お茶とパンの店 tePle」などの店舗も高確率でピンポイント特定できます。")

uploaded_file = st.file_uploader("📄 リストファイルをアップロード (CSV / Excel)", type=['csv', 'xlsx', 'xls'])

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith('.csv'):
            df_uploaded = pd.read_csv(uploaded_file)
        else:
            df_uploaded = pd.read_excel(uploaded_file)

        st.success(f"✅ ファイルを読み込みました（{len(df_uploaded)}行）")
        st.dataframe(df_uploaded.head(5), use_container_width=True)

        st.markdown("### 🔍 列のマッピング設定")
        columns = df_uploaded.columns.tolist()

        auto_name = next((c for c in columns if any(k in c.lower() for k in ['店名', '屋号', '名前', 'name', 'title', '店舗名'])), columns[0])
        auto_phone = next((c for c in columns if any(k in c.lower() for k in ['電話', 'phone', 'tel', '電話番号'])), columns[0])
        auto_addr = next((c for c in columns if any(k in c.lower() for k in ['住所', 'address', '媒体', '場所', '市区町村'])), columns[0])

        col_map1, col_map2, col_map3 = st.columns(3)
        with col_map1:
            store_name_col = st.selectbox("屋号（店舗名）の列 *", columns, index=columns.index(auto_name))
        with col_map2:
            phone_col_input = st.selectbox("既存の電話番号の列 *", columns, index=columns.index(auto_phone))
        with col_map3:
            address_col_input = st.selectbox("住所（または地域）の列 *", columns, index=columns.index(auto_addr))

        extracted_area = "埼玉県春日部市" 
        extracted_genre = "カフェ・喫茶"

        if address_col_input in df_uploaded.columns and not df_uploaded.empty:
            for _, row in df_uploaded.iterrows():
                addr_str = str(row[address_col_input])
                if pd.isna(row[address_col_input]) or addr_str.strip() == "" or addr_str.lower() in ["nan", "none", "検索"]:
                    continue
                m = re.search(r'^([^都道府県]+?[都道府県]|.*?東京都)?([^市区町村]+?[市区町村])', addr_str)
                if m:
                    extracted_area = f"{m.group(1) or ''}{m.group(2)}"
                    break

        auto_cat = next((c for c in columns if any(k in c.lower() for k in ['カテゴリ', 'ジャンル', 'category', '業態'])), None)
        if auto_cat and not df_uploaded.empty and not df_uploaded[auto_cat].dropna().empty:
            first_cat = str(df_uploaded[auto_cat].dropna().iloc[0])
            if first_cat and first_cat.lower() != "nan":
                extracted_genre = first_cat.replace('/', '・').replace(' ', '')
        else:
            f_clean = re.sub(r'(_重複統合結果|_電話番号補完結果|\.csv|\.xlsx|\.xls).*$', '', uploaded_file.name)
            f_parts = f_clean.split('_')
            if len(f_parts) > 1:
                extracted_genre = f_parts[1]
            elif len(f_parts) == 1:
                extracted_genre = f_parts[0]

        st.markdown("#### 📥 ダウンロード時のファイル名設定（自動生成・変更可）")
        col_fn1, col_fn2 = st.columns(2)
        with col_fn1:
            area_filename = st.text_input("都道府県・市区町村名", value=extracted_area)
        with col_fn2:
            genre_filename = st.text_input("ジャンル・業態名", value=extracted_genre)

        download_filename = f"{area_filename}_{genre_filename}_電話番号補完結果.csv"

        st.markdown("---")
        if st.button("🚀 電話番号の不足分を一括補完する", type="primary", use_container_width=True):
            if not api_key:
                st.error("❌ サイドバーからSerpAPIキーを設定してください。")
            else:
                df_output = df_uploaded.copy()
                
                df_output['補完_Google掲載電話番号'] = ""
                df_output['補完_取得店舗名'] = ""
                df_output['補完_取得住所'] = ""
                df_output['補完_信頼度'] = "Low"
                df_output['補完_エラー原因'] = ""

                search_tasks = []
                seen_keys = {} 

                for idx, row in df_output.iterrows():
                    name = str(row[store_name_col]).strip()
                    if pd.isna(row[store_name_col]) or name == "" or name.lower() == "nan":
                        df_output.at[idx, '補完_エラー原因'] = "店名（屋号）空欄のためスキップ"
                        continue

                    p_val = str(row[phone_col_input]).strip() if pd.notna(row[phone_col_input]) else ""
                    digits_only = re.sub(r'\D', '', p_val)
                    
                    if len(digits_only) in [10, 11]:
                        df_output.at[idx, '補完_Google掲載電話番号'] = p_val
                        df_output.at[idx, '補完_取得店舗名'] = name
                        df_output.at[idx, '補完_信頼度'] = "Existing"
                        df_output.at[idx, '補完_エラー原因'] = "既存（スキップ）"
                        continue

                    row_addr = str(row[address_col_input]).strip() if address_col_input in df_output.columns and pd.notna(row[address_col_input]) else ""
                    if row_addr.lower() in ["nan", "none", "検索"]:
                        row_addr = ""

                    name_key = (name, row_addr)
                    if name_key in seen_keys:
                        df_output.at[idx, '補完_エラー原因'] = f"入力値の重複（{seen_keys[name_key]+1}行目と同じ店舗）"
                        continue

                    seen_keys[name_key] = idx
                    search_tasks.append((idx, name, row_addr, None, api_key))

                if not search_tasks:
                    st.warning("⚠️ 新たに電話番号を取得する必要のある行（空欄または無効な番号の行）が見つかりませんでした。")
                else:
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    total_tasks = len(search_tasks)
                    completed = 0

                    start_time = time.time()
                    
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {executor.submit(_search_worker_csv, task): task[0] for task in search_tasks}
                        for future in as_completed(futures):
                            idx, result = future.result()
                            completed += 1
                            
                            progress_bar.progress(completed / total_tasks)
                            status_text.text(f"処理中: {completed}/{total_tasks}件目 - {df_output.at[idx, store_name_col]}")

                            if result.get('success'):
                                df_output.at[idx, '補完_Google掲載電話番号'] = result.get('電話番号', '')
                                df_output.at[idx, '補完_取得店舗名'] = result.get('店舗名', '')
                                df_output.at[idx, '補完_取得住所'] = result.get('住所', '')
                                df_output.at[idx, '補完_信頼度'] = result.get('信頼度', 'Low')
                                
                                if result.get('電話番号'):
                                    df_output.at[idx, '補完_エラー原因'] = "正常補完"
                                else:
                                    df_output.at[idx, '補完_エラー原因'] = result.get('error', '店舗は見つかりましたが電話番号が登録されていません。')
                            else:
                                df_output.at[idx, '補完_信頼度'] = "Low"
                                df_output.at[idx, '補完_エラー原因'] = result.get('error', '店舗が見つかりませんでした。')

                    for idx, row in df_output.iterrows():
                        if "入力値の重複" in str(df_output.at[idx, '補完_エラー原因']):
                            name = str(row[store_name_col]).strip()
                            row_addr = str(row[address_col_input]).strip() if pd.notna(row[address_col_input]) else ""
                            if row_addr.lower() in ["nan", "none", "検索"]: row_addr = ""
                            
                            orig_idx = seen_keys.get((name, row_addr))
                            if orig_idx is not None:
                                df_output.at[idx, '補完_Google掲載電話番号'] = df_output.at[orig_idx, '補完_Google掲載電話番号']
                                df_output.at[idx, '補完_取得店舗名'] = df_output.at[orig_idx, '補完_取得店舗名']
                                df_output.at[idx, '補完_取得住所'] = df_output.at[orig_idx, '補完_取得住所']
                                df_output.at[idx, '補完_信頼度'] = df_output.at[orig_idx, '補完_信頼度']

                    progress_bar.progress(1.0)
                    status_text.empty()
                    elapsed = time.time() - start_time

                    st.success(f"📊 補完処理が完了しました！ 所要時間: {elapsed:.1f}秒")
                    
                    tab_table, tab_download = st.tabs(["📊 画面表示（全列維持＋原因追加）", "📥 CSVダウンロード"])
                    
                    with tab_table:
                        st.dataframe(df_output, use_container_width=True, hide_index=False)
                        
                    with tab_download:
                        st.markdown(f"### 📄 設定されたファイル名: `{download_filename}`")
                        csv_data = df_output.to_csv(index=False, encoding='utf-8-sig')
                        st.download_button(
                            label="📥 補完完了したCSVファイルをダウンロード",
                            data=csv_data,
                            file_name=download_filename,
                            mime="text/csv",
                            use_container_width=True
                        )

    except Exception as e:
        st.error(f"❌ 処理中にエラーが発生しました: {str(e)}")
        st.exception(e)
else:
    st.info("ℹ️ 営業リストのCSVまたはExcelファイルをアップロードしてください。")
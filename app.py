import os
import unicodedata
import difflib
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from serpapi import GoogleSearch
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

# セッションステートの初期化（APIキー）
if 'api_key' not in st.session_state:
    st.session_state.api_key = os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY') or ""

# ============================================================
# サイドバー（APIキー設定を常に目立つ位置に配置 ＆ 毎回記入可能に）
# ============================================================
st.sidebar.header("⚙️ システム設定")

# 毎回記入・確認できるように常にテキストボックスを表示
input_api_key = st.sidebar.text_input(
    "🔑 SerpAPI キー（毎回変更・入力可能）", 
    value=st.session_state.api_key, 
    type="password",
    placeholder="キーを入力してください"
)

# 入力値が変わったらセッションステートを更新
if input_api_key != st.session_state.api_key:
    st.session_state.api_key = input_api_key
    st.rerun()

api_key = st.session_state.api_key

# 便利ボタン：.envファイルがローカルにあればそこから一発読み込み
if st.sidebar.button("🏠 PCの .env ファイルから読み込む", use_container_width=True):
    env_key = os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY')
    if env_key:
        st.session_state.api_key = env_key
        st.sidebar.success("✅ 環境変数から読み込みました")
        st.rerun()
    else:
        st.sidebar.warning("⚠ .envファイルにキーが見つかりません")

if not api_key:
    st.sidebar.error("❌ APIキーが未入力です。検索前にご記入ください。")
else:
    st.sidebar.success("✅ APIキー設定中（いつでも上書き可能）")

st.sidebar.markdown("---")
max_workers = st.sidebar.slider("⚡ 並列処理スレッド数", min_value=1, max_value=10, value=5, help="推奨: 3〜5")

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

if st.sidebar.button("🗑 キャッシュをクリア", use_container_width=True):
    clear_cache()
    st.sidebar.success("キャッシュをクリアしました")

# ============================================================
# 電話番号の標準化・フォーマット関数
# ============================================================
def format_phone_number(phone: str) -> str:
    if not phone:
        return ""
    # 全角数字や記号を半角に変換し、前後の空白を除去
    phone = unicodedata.normalize('NFKC', phone).strip()
    
    # 国際電話コード +81 を 0 に変換
    if phone.startswith('+81'):
        phone = '0' + phone[3:].lstrip()
    
    # 不要な括弧やスペースをすべてハイフンに統合
    phone = re.sub(r'[\(\)（）\s\-]+', '-', phone)
    phone = re.sub(r'-+', '-', phone)
    phone = phone.strip('-')
    
    # 数字のみを取り出して長さをチェック
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        # 東京03、大阪06などの2桁市外局番
        if digits.startswith(('03', '06')):
            return f"{digits[0:2]}-{digits[2:6]}-{digits[6:10]}"
        # フリーダイヤル 0120、0800
        elif digits.startswith('0120'):
            return f"{digits[0:4]}-{digits[4:7]}-{digits[7:10]}"
        elif digits.startswith('0800'):
            return f"{digits[0:4]}-{digits[4:7]}-{digits[7:10]}"
        # IP電話などの10桁
        elif digits.startswith(('050', '070', '080', '090')):
            return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
        else:
            # 主要な3桁市外局番（札幌011、仙台022、さいたま048、横浜045、川崎044、名古屋052、京都075、神戸078、広島082、福岡092等）
            if digits.startswith(('011', '022', '043', '044', '045', '048', '052', '072', '075', '078', '082', '092')):
                return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
            # その他地方の4桁市外局番 (4-2-4形式) への安全なフォールバック
            if digits.startswith('0'):
                return f"{digits[0:4]}-{digits[4:6]}-{digits[6:10]}"
            return phone
    elif len(digits) == 11:
        # 携帯電話・IP電話の11桁 (090-XXXX-XXXX, 080-XXXX-XXXX, 070-XXXX-XXXX, 050-XXXX-XXXX)
        if digits.startswith(('050', '070', '080', '090')):
            return f"{digits[0:3]}-{digits[3:7]}-{digits[7:11]}"
        # 一般的な11桁フォールバック
        return f"{digits[0:3]}-{digits[3:7]}-{digits[7:11]}"
    
    return phone

# ============================================================
# 表記正規化・ファジーマッチング
# ============================================================
def normalize_name(text: str) -> str:
    if not text: return ""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[\s\u3000]+', '', text)
    text = text.lower()
    text = re.sub(r'[^\w\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]', '', text)
    return text

def fuzzy_score(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb: return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()

def build_name_variants(store_name: str) -> list:
    base = store_name.strip()
    variants = [base]

    no_parentheses = re.sub(r'[\(（].*?[\)）]', '', base).strip()
    if no_parentheses and no_parentheses != base:
        variants.append(no_parentheses)
        base_for_split = no_parentheses
    else:
        base_for_split = base

    parts = re.split(r'[\s\u3000,._、。・]+', base_for_split)
    parts = [p.strip() for p in parts if p.strip()]
    
    if len(parts) > 1:
        variants.append(parts[-1])
        variants.append(parts[0])
        english_words = re.findall(r'[a-zA-Z0-9\'\-&]+', base_for_split)
        if english_words:
            eng_name = " ".join(english_words).strip()
            if eng_name and eng_name != base: variants.append(eng_name)

    if 'の店' in base_for_split:
        after_shop = base_for_split.split('の店')[-1].strip()
        if after_shop: variants.append(after_shop)
    if 'のお店' in base_for_split:
        after_shop = base_for_split.split('のお店')[-1].strip()
        if after_shop: variants.append(after_shop)

    return [v for v in variants if v]

def is_valid_match(hit_title: str, input_name: str, current_variant: str) -> bool:
    if not hit_title: return False
    h_norm = normalize_name(hit_title)
    i_norm = normalize_name(input_name)
    v_norm = normalize_name(current_variant)
    
    if h_norm in i_norm or i_norm in h_norm: return True
    if v_norm and (v_norm in h_norm or h_norm in v_norm): return True
    if fuzzy_score(hit_title, current_variant) >= 0.55: return True
    return False

def score_place(place, query=""):
    score = 0
    title = place.get('title', '')
    if place.get('phone') or place.get('formatted_phone_number'): score += 50
    if place.get('address'): score += 20
    if place.get('rating'): score += 10
    return score

# ============================================================
# 統合型・超執念深い店舗検索ロジック（Web検索ファースト）
# ============================================================
def search_store_by_name(store_name, location_str=None, api_key=None, location_hint=None):
    cached = get_cached_result(store_name, location_str, location_hint)
    if cached is not None: return cached

    EMPTY = {
        'success': False, '店舗名': '', '電話番号': '', '住所': '',
        '緯度': None, '経度': None, '評価': '', 'レビュー数': '', '信頼度': 'Low',
        'error': 'Google検索・マップともに店舗を特定できませんでした。'
    }

    try:
        base_name = store_name.strip()
        
        # ------------------------------------------------------------
        # ➔ ★第一段階: Google Web検索（ナレッジグラフ）で一本釣り
        # ------------------------------------------------------------
        query_web = f"{base_name} {location_hint.strip()}" if location_hint else base_name
        params_web = {
            "engine": "google", "q": query_web, "api_key": api_key,
            "hl": "ja", "gl": "jp", "num": 4
        }
        
        search_web = GoogleSearch(params_web)
        results_web = search_web.get_dict()
        
        # 1. ナレッジグラフ（右側の公式詳細パネル）
        kg = results_web.get("knowledge_graph", {})
        if kg:
            phone_kg = kg.get("phone") or kg.get("formatted_phone_number")
            if phone_kg:
                r = {
                    'success': True, '店舗名': kg.get("title", base_name), '電話番号': format_phone_number(phone_kg),
                    '住所': kg.get("address") or location_hint or '',
                    '緯度': None, '経度': None, '評価': kg.get("rating", ""), 'レビュー数': kg.get("reviews", ""),
                    '信頼度': 'Very High', 'method': 'Google Web検索(ナレッジグラフ)'
                }
                set_cached_result(store_name, location_str, location_hint, r)
                return r

        # 2. ローカルパック（検索結果内のマップ枠）
        local_res = results_web.get("local_results", [])
        if isinstance(local_res, list) and local_res:
            for p in local_res:
                if is_valid_match(p.get("title", ""), base_name, base_name):
                    if p.get("phone"):
                        gps = p.get("gps_coordinates", {})
                        r = {
                            'success': True, '店舗名': p.get("title", base_name), '電話番号': format_phone_number(p.get("phone")),
                            '住所': p.get("address") or '',
                            '緯度': gps.get('latitude') if gps else None, '経度': gps.get('longitude') if gps else None,
                            '評価': p.get("rating", ""), 'レビュー数': p.get("reviews", ""),
                            '信頼度': 'High', 'method': 'Google Web検索(ローカルパック)'
                        }
                        set_cached_result(store_name, location_str, location_hint, r)
                        return r

        # 3. Webサイトの紹介文（スニペット）からの正規表現抽出
        phone_patterns = [
            r'0120-\d{3}-\d{3}', r'0800-\d{3}-\d{4}',
            r'0\d{1,3}-\d{2,4}-\d{3,4}', r'\(\d{2,4}\)\d{3,4}-\d{3,4}',
        ]
        for org in results_web.get("organic_results", []):
            snippet = org.get("snippet", "")
            if snippet:
                snippet_norm = unicodedata.normalize('NFKC', snippet)
                for pattern in phone_patterns:
                    match = re.search(pattern, snippet_norm)
                    if match:
                        r = {
                            'success': True, '店舗名': base_name, '電話番号': format_phone_number(match.group()),
                            '住所': location_hint or '', '緯度': None, '経度': None, '評価': '', 'レビュー数': '',
                            '信頼度': 'Mid', 'method': f"Webページ解析({org.get('title', 'HP')})"
                        }
                        set_cached_result(store_name, location_str, location_hint, r)
                        return r

        # ------------------------------------------------------------
        # ➔ ★第二段階: Webで全滅した場合のみ、従来のGoogle Maps深い探索を回す
        # ------------------------------------------------------------
        name_variants = build_name_variants(base_name)
        map_hint = ""
        if location_hint:
            m = re.search(r'^([^市区町村郡]+?[市区町村郡])', location_hint.strip())
            map_hint = m.group(1) if m else location_hint.strip()[:10]

        last_backup_result = None

        for variant in name_variants:
            address_patterns = []
            if location_hint: address_patterns.append(location_hint.strip())
            if map_hint and map_hint != location_hint: address_patterns.append(map_hint)
            address_patterns.append("")

            for addr_pattern in address_patterns:
                query = f"{variant} {addr_pattern}".strip() if addr_pattern else variant
                params_maps = {"engine": "google_maps", "q": query, "api_key": api_key, "type": "search", "hl": "ja", "gl": "jp"}
                
                search_maps = GoogleSearch(params_maps)
                results_maps = search_maps.get_dict()

                place = None
                if results_maps and 'local_results' in results_maps:
                    scored = []
                    for p in results_maps.get('local_results', []):
                        if is_valid_match(p.get('title', ''), base_name, variant):
                            scored.append((p, score_place(p, query)))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    if scored: place = scored[0][0]
                elif results_maps and 'place_results' in results_maps:
                    if is_valid_match(results_maps['place_results'].get('title', ''), base_name, variant):
                        place = results_maps['place_results']

                if place is not None:
                    gps = place.get('gps_coordinates', {})
                    phone = place.get('phone') or place.get('formatted_phone_number') or place.get('電話', '')
                    
                    if not phone and (place.get('place_id') or place.get('data_cid')):
                        try:
                            detail_params = {"engine": "google_maps", "api_key": api_key, "hl": "ja", "gl": "jp"}
                            if place.get('place_id'): detail_params["place_id"] = place.get('place_id')
                            else: detail_params["data_cid"] = place.get('data_cid')
                            detail_results = GoogleSearch(detail_params).get_dict()
                            if "place_results" in detail_results:
                                dp = detail_results["place_results"]
                                phone = dp.get('phone') or dp.get('formatted_phone_number') or dp.get('電話', '')
                        except: pass

                    r = {
                        'success': True, '店舗名': place.get('title', store_name), '電話番号': format_phone_number(phone),
                        '住所': place.get('address') or place.get('住所', ''),
                        '緯度': gps.get('latitude') if gps else None, '経度': gps.get('longitude') if gps else None,
                        '評価': place.get('rating', ''), 'レビュー数': place.get('reviews', ''),
                        '信頼度': 'High', 'method': 'Googleマップ詳細探索'
                    }
                    if phone:
                        set_cached_result(store_name, location_str, location_hint, r)
                        return r
                    else:
                        if not last_backup_result: last_backup_result = r

        if last_backup_result:
            last_backup_result['error'] = '店舗は見つかりましたが、Google上に電話番号の掲載がありませんでした。'
            last_backup_result['success'] = False
            set_cached_result(store_name, location_str, location_hint, last_backup_result)
            return last_backup_result

        set_cached_result(store_name, location_str, location_hint, EMPTY)
        return EMPTY
    except Exception as e:
        return {**EMPTY, 'error': f'システムエラー: {str(e)}'}

def _search_worker_csv(task_args):
    idx, name, row_addr, location_str_csv, api_key = task_args
    result = search_store_by_name(name, location_str=location_str_csv, api_key=api_key, location_hint=row_addr if row_addr else None)
    return idx, result

# ============================================================
# メイン画面表示
# ============================================================
st.title("📞 店舗電話番号 抽出・上書き補完ツール")
st.markdown("既存のリストを読み込み、**電話番号がない・無効な番号（135等）の行のみ**をGoogleから一括追記します。")

uploaded_file = st.file_uploader("📄 リストファイルをアップロード (CSV / Excel)", type=['csv', 'xlsx', 'xls'])

if uploaded_file is not None:
    try:
        df_uploaded = pd.read_csv(uploaded_file) if uploaded_file.name.endswith('.csv') else pd.read_excel(uploaded_file)
        st.success(f"✅ ファイルを読み込みました（{len(df_uploaded)}行）")

        columns = df_uploaded.columns.tolist()
        auto_name = next((c for c in columns if any(k in c.lower() for k in ['店名', '屋号', '名前', 'name', '店舗名'])), columns[0])
        auto_phone = next((c for c in columns if any(k in c.lower() for k in ['電話', 'phone', 'tel', '電話番号'])), columns[0])
        auto_addr = next((c for c in columns if any(k in c.lower() for k in ['住所', 'address', '媒体', '場所'])), columns[0])

        col_map1, col_map2, col_map3 = st.columns(3)
        with col_map1: store_name_col = st.selectbox("屋号（店舗名）の列 *", columns, index=columns.index(auto_name))
        with col_map2: phone_col_input = st.selectbox("既存の電話番号の列 *", columns, index=columns.index(auto_phone))
        with col_map3: address_col_input = st.selectbox("住所（または地域）の列 *", columns, index=columns.index(auto_addr))

        # ファイル名自動生成
        extracted_area = "特定地域"
        extracted_genre = "営業リスト"
        if address_col_input in df_uploaded.columns and not df_uploaded.empty:
            for _, row in df_uploaded.iterrows():
                addr_str = str(row[address_col_input])
                m = re.search(r'^([^市区町村郡]+?[市区町村郡])', addr_str)
                if m: extracted_area = m.group(1); break
        f_clean = re.sub(r'(_重複統合結果|_電話番号補完結果|\.csv|\.xlsx|\.xls).*$', '', uploaded_file.name)
        f_parts = f_clean.split('_')
        extracted_genre = f_parts[1] if len(f_parts) > 1 else f_parts[0]

        col_fn1, col_fn2 = st.columns(2)
        with col_fn1: area_filename = st.text_input("都道府県・市区町村名", value=extracted_area)
        with col_fn2: genre_filename = st.text_input("ジャンル・業態名", value=extracted_genre)
        download_filename = f"{area_filename}_{genre_filename}_電話番号補完結果.csv"

        if st.button("🚀 電話番号の不足分を一括補完する", type="primary", use_container_width=True):
            if not api_key: 
                st.error("❌ サイドバーからSerpAPIキーを入力してください。")
            else:
                df_output = df_uploaded.copy()
                df_output['補完_Google掲載電話番号'] = ""
                df_output['補完_取得店舗名'] = ""
                df_output['補完_取得住所'] = ""
                df_output['補完_信頼度'] = "Low"
                df_output['補完_エラー原因'] = ""
                df_output['補完_検索方法'] = ""

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
                        df_output.at[idx, '補完_Google掲載電話番号'] = format_phone_number(p_val)
                        df_output.at[idx, '補完_取得店舗名'] = name
                        df_output.at[idx, '補完_信頼度'] = "Existing"
                        df_output.at[idx, '補完_エラー原因'] = "既存データ維持"
                        df_output.at[idx, '補完_検索方法'] = "既存データ"
                        continue

                    row_addr = str(row[address_col_input]).strip() if pd.notna(row[address_col_input]) else ""
                    name_key = (name, row_addr)
                    if name_key in seen_keys:
                        df_output.at[idx, '補完_エラー原因'] = f"入力値の重複（{seen_keys[name_key]+1}行目と同じ店舗）"
                        df_output.at[idx, '補完_検索方法'] = "重複スキップ"
                        continue

                    seen_keys[name_key] = idx
                    search_tasks.append((idx, name, row_addr, None, api_key))

                if search_tasks:
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    total_tasks = len(search_tasks)
                    completed = 0

                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {executor.submit(_search_worker_csv, task): task[0] for task in search_tasks}
                        for future in as_completed(futures):
                            idx, result = future.result()
                            completed += 1
                            progress_bar.progress(completed / total_tasks)
                            status_text.text(f"処理中: {completed}/{total_tasks}件目 - {df_output.at[idx, store_name_col]}")

                            if result.get('success') or result.get('電話番号'):
                                df_output.at[idx, '補完_Google掲載電話番号'] = result.get('電話番号', '')
                                df_output.at[idx, '補完_取得店舗名'] = result.get('店舗名', '')
                                df_output.at[idx, '補完_取得住所'] = result.get('住所', '')
                                df_output.at[idx, '補完_信頼度'] = result.get('信頼度', 'Low')
                                df_output.at[idx, '補完_検索方法'] = result.get('method', '検出')
                                df_output.at[idx, '補完_エラー原因'] = "正常補完" if result.get('電話番号') else result.get('error', '電話番号なし')
                            else:
                                df_output.at[idx, '補完_信頼度'] = "Low"
                                df_output.at[idx, '補完_検索方法'] = "失敗"
                                df_output.at[idx, '補完_エラー原因'] = result.get('error', '店舗特定不可')

                    # 重複行への反映コピー
                    for idx, row in df_output.iterrows():
                        if "入力値の重複" in str(df_output.at[idx, '補完_エラー原因']):
                            orig_idx = seen_keys.get((str(row[store_name_col]).strip(), str(row[address_col_input]).strip()))
                            if orig_idx is not None:
                                df_output.at[idx, '補完_Google掲載電話番号'] = df_output.at[orig_idx, '補完_Google掲載電話番号']
                                df_output.at[idx, '補完_取得店舗名'] = df_output.at[orig_idx, '補完_取得店舗名']
                                df_output.at[idx, '補完_取得住所'] = df_output.at[orig_idx, '補完_取得住所']
                                df_output.at[idx, '補完_信頼度'] = df_output.at[orig_idx, '補完_信頼度']
                                df_output.at[idx, '補完_検索方法'] = df_output.at[orig_idx, '補完_検索方法']

                    progress_bar.progress(1.0)
                    status_text.empty()

                    # ★電話番号がない・エラーの行を自動で確実に「一番下」に整理するソート
                    def sort_priority(r):
                        p = str(r['補完_Google掲載電話番号']).strip()
                        e = str(r['補完_エラー原因']).strip()
                        if p and e in ["正常補完", "既存データ維持"]: return 0
                        return 1

                    df_output['__sort_key__'] = df_output.apply(sort_priority, axis=1)
                    df_output = df_output.sort_values(by=['__sort_key__']).drop(columns=['__sort_key__'])

                    st.success(f"📊 不足分の電話番号補完がすべて完了しました！")
                    
                    # ➔ ★★★【復活＆強化】ユーザーが現在の挙動をパッと見れるようにタブ分け ★★★
                    tab_table, tab_list, tab_download = st.tabs([
                        "📊 テーブル表示（全列維持）", 
                        "📋 挙動・リスト表示（詳細ログ）", 
                        "📥 CSVダウンロード"
                    ])
                    
                    with tab_table:
                        st.dataframe(df_output, use_container_width=True)
                        
                    with tab_list:
                        st.markdown("### 🔍 各店舗の取得状況とエラー原因のログ")
                        for index, row in enumerate(df_output.to_dict(orient='records'), 1):
                            with st.container():
                                col_l, col_r = st.columns([3, 1])
                                with col_l:
                                    st.markdown(f"#### {index}. {row[store_name_col]}")
                                    st.caption(f"📍 元の住所: {row[address_col_input]}")
                                    if row['補完_Google掲載電話番号']:
                                        st.markdown(f"🟢 **取得電話番号**: `{row['補完_Google掲載電話番号']}`")
                                        if row['補完_取得店舗名'] and row['補完_取得店舗名'] != row[store_name_col]:
                                            st.markdown(f"🏢 *Google登録名*: {row['補完_取得店舗名']}")
                                    else:
                                        st.markdown(f"🔴 **電話番号の取得に失敗しました**")
                                with col_r:
                                    st.markdown(f"**ステータス**")
                                    err = row['補完_エラー原因']
                                    if err in ["正常補完", "既存データ維持"]:
                                        st.success(err)
                                    else:
                                        st.warning(err)
                                    st.caption(f"ルート: {row['補完_検索方法']}")
                                st.divider()
                                
                    with tab_download:
                        st.markdown(f"### 📄 出力ファイル名: `{download_filename}`")
                        st.download_button(
                            label="📥 補完完了したCSVファイルをダウンロード",
                            data=df_output.to_csv(index=False, encoding='utf-8-sig'),
                            file_name=download_filename, mime="text/csv", use_container_width=True
                        )
    except Exception as e:
        st.error(f"❌ エラーが発生しました: {str(e)}")
        st.exception(e)
else:
    st.info("ℹ️ 営業リストのCSVまたはExcelファイルをアップロードしてください。")
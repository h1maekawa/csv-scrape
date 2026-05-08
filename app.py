import os
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
    page_title="店舗電話番号抽出ツール",
    page_icon="📞",
    layout="wide"
)

load_dotenv()

if 'api_key' not in st.session_state:
    st.session_state.api_key = os.getenv('SERPAPI_KEY') or os.getenv('SERP_API_KEY') or ""

# ============================================================
# 検索結果キャッシュ（スレッドセーフなモジュールレベルdict）
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
# ジオコーディング
# ============================================================
@st.cache_data(ttl=3600)
def get_coordinates_from_address(address):
    """地名から緯度・経度を取得する関数"""
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
# スコアリング（精度ロジック維持）
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
            score -= 20

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
# Organic検索フォールバック
# ============================================================
def search_phone_from_organic(store_name, api_key):
    """Google organic検索から電話番号を取得（フォールバック用）"""
    try:
        params = {
            "engine": "google",
            "q": f"{store_name} 公式 電話番号",
            "api_key": api_key,
            "num": 5
        }
        search = GoogleSearch(params)
        results = search.get_dict()

        phone_patterns = [
            r'0120-\d{3}-\d{3}',
            r'0800-\d{3}-\d{4}',
            r'0\d{1,3}-\d{2,4}-\d{3,4}',
            r'\(\d{2,4}\)\d{3,4}-\d{3,4}',
        ]
        for r in results.get("organic_results", []):
            snippet = r.get("snippet", "")
            for pattern in phone_patterns:
                match = re.search(pattern, snippet)
                if match:
                    return match.group()
        return ""
    except Exception:
        return ""


# ============================================================
# 店舗検索（キャッシュ・スコアリング・フォールバック対応）
# ============================================================
def search_store_by_name(store_name, location_str=None, api_key=None, location_hint=None):
    """屋号（店名）から店舗情報を取得（キャッシュ対応・精度ロジック維持）"""
    cached = get_cached_result(store_name, location_str, location_hint)
    if cached is not None:
        return cached

    EMPTY = {
        'success': False,
        '店舗名': '', '電話番号': '', '住所': '',
        '緯度': None, '経度': None, '評価': '', 'レビュー数': '', '信頼度': 'Low'
    }

    if not api_key:
        return {**EMPTY, 'error': 'APIキーが設定されていません'}

    try:
        query = store_name.strip()
        if location_hint and not location_str:
            query = f"{query} {location_hint.strip()}"

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

        if results and 'local_results' in results:
            local_results = results.get('local_results', [])
            if local_results:
                if len(local_results) > 1:
                    scored_places = [(p, score_place(p, query)) for p in local_results]
                    scored_places.sort(key=lambda x: x[1], reverse=True)
                    place = scored_places[0][0]
                else:
                    place = local_results[0]

                gps = place.get('gps_coordinates', {})
                result = {
                    'success': True,
                    '店舗名': place.get('title', ''),
                    '電話番号': place.get('phone') or place.get('formatted_phone_number') or place.get('電話', ''),
                    '住所': place.get('address') or place.get('住所', ''),
                    '緯度': gps.get('latitude') if gps else None,
                    '経度': gps.get('longitude') if gps else None,
                    '評価': place.get('rating', ''),
                    'レビュー数': place.get('reviews', ''),
                }
                result['信頼度'] = calculate_confidence(result)
                set_cached_result(store_name, location_str, location_hint, result)
                return result

        # フォールバック: organic検索
        phone_from_organic = search_phone_from_organic(store_name, api_key)
        if phone_from_organic:
            result = {
                'success': True,
                '店舗名': store_name, '電話番号': phone_from_organic,
                '住所': '', '緯度': None, '経度': None,
                '評価': '', 'レビュー数': '', '信頼度': 'Mid'
            }
            set_cached_result(store_name, location_str, location_hint, result)
            return result

        result = {**EMPTY, 'error': '店舗が見つかりませんでした'}
        set_cached_result(store_name, location_str, location_hint, result)
        return result

    except Exception as e:
        return {**EMPTY, 'error': f'エラー: {str(e)}'}


# ============================================================
# 並列検索ワーカー（ThreadPoolExecutor）
# ============================================================
def _search_worker(args):
    idx, store_name, location_str, api_key, location_hint = args
    result = search_store_by_name(store_name, location_str, api_key, location_hint)
    return idx, store_name, result


def parallel_search_stores(
    store_names: list,
    location_str: Optional[str],
    api_key: str,
    location_hint: Optional[str],
    max_workers: int = 5,
    progress_callback=None,
) -> list:
    """
    店舗リストを並列検索する。
    SerpAPI の利用規約・レートリミットを考慮し max_workers=5 をデフォルトに設定。
    """
    total = len(store_names)
    results = [None] * total

    tasks = [
        (i, name, location_str, api_key, location_hint)
        for i, name in enumerate(store_names)
    ]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_search_worker, task): task[0] for task in tasks}
        completed = 0
        for future in as_completed(futures):
            idx, store_name, result = future.result()
            results[idx] = (store_name, result)
            completed += 1
            if progress_callback:
                progress_callback(completed, total, store_name)

    return results


# ============================================================
# サイドバー（APIキー・並列設定・キャッシュ管理のみ）
# ============================================================
with st.sidebar:
    st.header("⚙️ 設定")

    new_api_key = st.text_input(
        "SerpAPI キー",
        value=st.session_state.api_key,
        type="password",
        help="SerpAPIの管理画面から取得したAPIキーを入力してください。"
    )
    if new_api_key != st.session_state.api_key:
        st.session_state.api_key = new_api_key
        st.rerun()

    api_key = st.session_state.api_key

    if not api_key:
        st.error("⚠️ APIキーが設定されていません。")
    else:
        st.success("✅ APIキーが設定されています")

    st.markdown("---")
    st.markdown("### ⚡ 並列処理設定")
    max_workers = st.slider(
        "並列スレッド数",
        min_value=1,
        max_value=10,
        value=5,
        help="大きくすると速いが、SerpAPIのレート制限に注意。推奨: 3〜5"
    )

    cache_count = get_cache_count()
    st.markdown(f"🗄️ **検索キャッシュ**: {cache_count}件")
    if st.button("🗑️ キャッシュをクリア", use_container_width=True):
        clear_cache()
        st.success("キャッシュをクリアしました")

    st.markdown("---")
    st.markdown("### 📖 使い方")
    st.markdown("""
    1. CSVまたはExcelをアップロード
    2. 店名の列を選択
    3. 地域を指定（任意）
    4. 「電話番号を取得」をクリック
    """)


# ============================================================
# メイン画面：CSV抽出専用UI
# ============================================================
st.title("📞 店舗電話番号抽出ツール")
st.markdown("CSVまたはExcelファイルの屋号（店名）リストから、Google Mapsで電話番号を一括取得します。")

st.info(
    "⚡ **並列処理モード**: 複数の店舗を同時に検索します。"
    "　並列スレッド数はサイドバーで変更できます（推奨: 3〜5）。"
    "　同じCSVを再読み込みした場合は **キャッシュ** から即座に返します。"
)

# --- ① CSVアップロード ---
st.markdown("### 📄 ① ファイルアップロード")
uploaded_file = st.file_uploader(
    "CSVまたはExcelファイルをアップロード",
    type=['csv', 'xlsx', 'xls'],
    help="屋号（店名）が含まれるCSVまたはExcelファイルをアップロードしてください"
)

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith('.csv'):
            df_uploaded = pd.read_csv(uploaded_file)
        else:
            df_uploaded = pd.read_excel(uploaded_file)

        st.success(f"✅ ファイルを読み込みました（{len(df_uploaded)}行）")
        st.dataframe(df_uploaded.head(10), use_container_width=True)

        # --- ② 列の選択 + 検索条件 ---
        st.markdown("### 🔍 ② 検索条件の設定")

        columns = df_uploaded.columns.tolist()
        auto_detected_col = next(
            (col for col in columns if any(kw in col.lower() for kw in ['店名', '屋号', '名前', 'name', 'title', '店舗名', '名称'])),
            None
        )
        store_name_col = st.selectbox(
            "屋号（店名）の列を選択 *",
            columns,
            index=columns.index(auto_detected_col) if auto_detected_col else 0,
        )

        st.markdown("#### 📍 共通検索条件（任意）")
        col_cond1, col_cond2 = st.columns(2)

        with col_cond1:
            location_name = st.text_input("地名（任意）", placeholder="例: 東京都渋谷区",
                                          help="地名を指定すると、その地域で検索します")

        with col_cond2:
            use_radius_csv = st.checkbox("検索半径を指定", help="指定した半径内の店舗のみを取得します")
            radius_meters_csv = None
            if use_radius_csv:
                radius_meters_csv = st.number_input("検索半径（メートル）", min_value=100, max_value=50000, value=1000, step=100)

        center_lat_csv = None
        center_lon_csv = None

        if use_radius_csv and not location_name:
            st.markdown("##### 中心座標の指定（半径指定時は必須）")
            col_coord1, col_coord2 = st.columns(2)
            with col_coord1:
                center_lat_csv = st.number_input("緯度", value=35.6762, format="%.7f")
            with col_coord2:
                center_lon_csv = st.number_input("経度", value=139.6503, format="%.7f")

        # --- ③ 実行 ---
        st.markdown("### 🚀 ③ 実行")
        if st.button("🔍 電話番号を取得", type="primary", use_container_width=True):
            if store_name_col not in df_uploaded.columns:
                st.error("❌ 選択した列が存在しません")
            else:
                store_names = df_uploaded[store_name_col].dropna().astype(str).tolist()

                if not store_names:
                    st.warning("⚠️ 屋号が含まれていません")
                else:
                    # 座標取得
                    location_str_csv = None
                    if location_name:
                        with st.spinner(f"「{location_name}」の座標を取得しています..."):
                            geo_result = get_coordinates_from_address(location_name)
                            if geo_result['success']:
                                center_lat_csv = geo_result['latitude']
                                center_lon_csv = geo_result['longitude']
                                zoom_csv = radius_to_zoom_level(radius_meters_csv) if radius_meters_csv else 14
                                location_str_csv = f"@{center_lat_csv},{center_lon_csv},{zoom_csv}z"
                                st.success(f"✅ 座標を取得しました: {geo_result['address']}")
                            else:
                                st.warning(f"⚠️ 座標を取得できませんでした: {geo_result.get('error', '')}")
                    elif use_radius_csv and center_lat_csv and center_lon_csv:
                        zoom_csv = radius_to_zoom_level(radius_meters_csv) if radius_meters_csv else 14
                        location_str_csv = f"@{center_lat_csv},{center_lon_csv},{zoom_csv}z"

                    # 進捗表示
                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    def update_progress(completed, total, current_name):
                        progress_bar.progress(completed / total)
                        cached = get_cached_result(current_name, location_str_csv, location_name or None)
                        cache_tag = " ⚡キャッシュ" if cached else ""
                        status_text.text(f"完了: {completed}/{total} - {current_name}{cache_tag}")

                    start_time = time.time()

                    # ★ 並列検索実行
                    parallel_results = parallel_search_stores(
                        store_names=store_names,
                        location_str=location_str_csv,
                        api_key=api_key,
                        location_hint=location_name or None,
                        max_workers=max_workers,
                        progress_callback=update_progress,
                    )

                    elapsed = time.time() - start_time
                    progress_bar.progress(1.0)
                    status_text.empty()

                    # ============================================================
                    # 結果整形 ＋ 重複検出
                    # ============================================================
                    results_list = []
                    skipped_list = []
                    seen_input_names = {}
                    seen_result_keys = {}

                    for row_idx, (store_name, result) in enumerate(parallel_results):
                        row_result = {
                            '屋号（入力値）': store_name,
                            '取得店舗名': result.get('店舗名', ''),
                            '電話番号': result.get('電話番号', ''),
                            '住所': result.get('住所', ''),
                            '緯度': result.get('緯度', ''),
                            '経度': result.get('経度', ''),
                            '評価': result.get('評価', ''),
                            'レビュー数': result.get('レビュー数', ''),
                            '信頼度': result.get('信頼度', 'Low'),
                            'エラー': result.get('error', '') if not result.get('success', False) else ''
                        }

                        # 半径フィルタ
                        if use_radius_csv and radius_meters_csv and center_lat_csv and center_lon_csv:
                            lat = result.get('緯度')
                            lon = result.get('経度')
                            if lat and lon:
                                distance = calculate_distance(center_lat_csv, center_lon_csv, lat, lon)
                                row_result['距離（m）'] = f"{distance:.0f}"
                                if distance > radius_meters_csv:
                                    row_result['取得店舗名'] = ''
                                    row_result['電話番号'] = ''
                                    row_result['住所'] = ''
                                    row_result['エラー'] = f'半径{radius_meters_csv}mを超えています'
                            else:
                                row_result['距離（m）'] = ''
                        else:
                            row_result['距離（m）'] = ''

                        # 重複①: 入力店舗名の重複チェック
                        name_key = store_name.strip()
                        if name_key in seen_input_names:
                            skipped_row = {**row_result, '重複理由': f'入力値の重複（{seen_input_names[name_key]+1}行目と同じ屋号）'}
                            skipped_list.append(skipped_row)
                            continue
                        seen_input_names[name_key] = row_idx

                        # 重複②: 取得結果（電話番号＋住所）の重複チェック
                        phone_val = (row_result.get('電話番号') or '').strip()
                        address_val = (row_result.get('住所') or '').strip()
                        result_key = (phone_val, address_val)

                        if (phone_val or address_val) and result_key in seen_result_keys:
                            skipped_row = {**row_result, '重複理由': f'取得結果の重複（「{seen_result_keys[result_key]}」と同一店舗）'}
                            skipped_list.append(skipped_row)
                            continue
                        if phone_val or address_val:
                            seen_result_keys[result_key] = store_name

                        results_list.append(row_result)

                    # --- ④ 結果表示 ---
                    st.markdown("### 📊 ④ 結果")

                    if results_list or skipped_list:
                        df_results = pd.DataFrame(results_list) if results_list else pd.DataFrame()
                        df_skipped = pd.DataFrame(skipped_list) if skipped_list else pd.DataFrame()
                        per_store = elapsed / len(store_names) if store_names else 0

                        st.success(
                            f"✅ 検索完了：取得 **{len(results_list)}件** ／ 重複スキップ **{len(skipped_list)}件**"
                            f"　⏱️ 所要時間: {elapsed:.1f}秒（1件あたり {per_store:.2f}秒）"
                        )

                        tab_result1, tab_result2, tab_result3, tab_result4 = st.tabs([
                            "📊 テーブル表示",
                            "📋 リスト表示",
                            f"⚠️ 重複スキップ（{len(skipped_list)}件）",
                            "📥 CSVダウンロード"
                        ])

                        with tab_result1:
                            st.markdown(f"**取得件数: {len(results_list)}件**")
                            if not df_results.empty:
                                st.dataframe(df_results, use_container_width=True, hide_index=True)
                            else:
                                st.info("取得結果がありません")

                        with tab_result2:
                            for index, row in enumerate(results_list, 1):
                                with st.container():
                                    st.markdown(f"### {index}. {row['屋号（入力値）']}")
                                    if row['取得店舗名']:
                                        st.markdown(f"**取得店舗名:** {row['取得店舗名']}")
                                    if row['電話番号']:
                                        st.markdown(f"📞 **電話番号:** {row['電話番号']}")
                                    if row['住所']:
                                        st.markdown(f"📍 **住所:** {row['住所']}")
                                    if row.get('距離（m）'):
                                        st.markdown(f"📏 **距離:** {row['距離（m）']}m")
                                    confidence_emoji = {'Very High': '🟢', 'High': '🟡', 'Mid': '🟠', 'Low': '🔴'}.get(row.get('信頼度', 'Low'), '⚪')
                                    st.markdown(f"{confidence_emoji} **信頼度:** {row.get('信頼度', 'Low')}")
                                    if row['エラー']:
                                        st.warning(f"⚠️ {row['エラー']}")
                                    st.divider()

                        with tab_result3:
                            if not df_skipped.empty:
                                st.markdown(f"**重複としてスキップされた店舗: {len(skipped_list)}件**")
                                st.caption(
                                    "重複理由が「入力値の重複」→ CSVに同じ屋号が複数行ある。"
                                    "　「取得結果の重複」→ 別の屋号だが同じ店舗（電話番号・住所が一致）として検出。"
                                )
                                st.dataframe(df_skipped, use_container_width=True, hide_index=True)
                                csv_skipped = df_skipped.to_csv(index=False, encoding='utf-8-sig')
                                st.download_button(
                                    label="📥 重複スキップ一覧をCSVでダウンロード",
                                    data=csv_skipped,
                                    file_name=f"skipped_duplicates_{len(skipped_list)}件.csv",
                                    mime="text/csv",
                                    use_container_width=True
                                )
                            else:
                                st.success("✅ 重複した店舗は見つかりませんでした")

                        with tab_result4:
                            st.markdown("### CSVファイルをダウンロード")
                            if not df_results.empty:
                                csv_output = df_results.to_csv(index=False, encoding='utf-8-sig')
                                st.download_button(
                                    label="📥 取得結果をCSVでダウンロード",
                                    data=csv_output,
                                    file_name=f"phone_numbers_from_csv_{len(results_list)}件.csv",
                                    mime="text/csv",
                                    use_container_width=True
                                )
                                st.dataframe(df_results, use_container_width=True, hide_index=True)

    except Exception as e:
        st.error(f"❌ エラーが発生しました: {str(e)}")
        st.exception(e)
else:
    st.info("ℹ️ CSVまたはExcelファイルをアップロードしてください")

st.markdown("---")
st.caption("Made with ❤️ using Streamlit and SerpAPI")
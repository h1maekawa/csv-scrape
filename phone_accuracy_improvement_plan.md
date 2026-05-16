# 市外局番ベースの電話番号精度向上 実装プラン

## 1. ディレクトリ構成

保守性と将来のPolygon連携を見据え、ドメイン駆動設計（DDD）やオニオンアーキテクチャの要素を取り入れた構成にします。

```text
app/
├── main.py                  # FastAPI エントリーポイント
├── api/
│   └── routes.py            # APIエンドポイント定義
├── core/
│   ├── config.py            # 設定値・環境変数
│   └── exceptions.py        # カスタム例外
├── models/
│   └── restaurant.py        # DBスキーマ定義 (SQLAlchemy/Pydantic)
├── schemas/
│   └── restaurant_schema.py # Request/Response モデル
├── services/
│   ├── validation_service.py # 電話番号検証のメインロジック
│   └── scoring_service.py   # スコアリングロジック
├── utils/
│   ├── phone_normalizer.py  # 正規化ユーティリティ
│   └── geo_utils.py         # 将来のGIS・Polygon用ユーティリティ
├── data/
│   └── area_code_master.json # 市外局番マスタ
└── tests/
    ├── test_validation.py
    └── test_scoring.py
```

---

## 2. area_code_master.json サンプル

都道府県と市区町村レベルでネスト可能な構造にします。
これにより、大まかな都道府県レベルの照合から、将来的に市区町村単位の詳細な照合へスケールできます。

```json
{
  "prefectures": {
    "東京都": {
      "area_codes": ["03", "042", "0422", "0428", "04992"],
      "cities": {
        "千代田区": {
          "area_codes": ["03"]
        },
        "八王子市": {
          "area_codes": ["042"]
        }
      }
    },
    "大阪府": {
      "area_codes": ["06", "072", "0721", "0725", "0726", "0729"],
      "cities": {
        "大阪市": {
          "area_codes": ["06"]
        },
        "堺市": {
          "area_codes": ["072"]
        }
      }
    }
  },
  "free_dial_and_ip": ["0120", "0800", "0570", "050"]
}
```

---

## 3. utils/phone_normalizer.py

ハイフンやスペース、カッコなどの不要な文字を除去し、数字のみに正規化します。

```python
import re

class PhoneNormalizer:
    @staticmethod
    def normalize(phone: str) -> str:
        """
        電話番号から数字以外の文字（ハイフン、スペース、カッコ等）を除去する
        例: '06-1234-5678' -> '0612345678'
            '(06) 1234 5678' -> '0612345678'
        """
        if not phone:
            return ""
        # 半角・全角問わず数字以外を空文字に置換
        return re.sub(r'\D', '', phone)

    @staticmethod
    def extract_area_code_candidates(normalized_phone: str) -> list[str]:
        """
        電話番号から市外局番の候補（2桁〜5桁）を抽出する
        日本の市外局番は0から始まり、2〜5桁が一般的
        """
        if not normalized_phone or not normalized_phone.startswith("0"):
            return []
        
        candidates = []
        # 最大5桁まで候補として抽出 (例: 06, 072, 0721, 04992)
        max_length = min(len(normalized_phone), 5)
        for i in range(2, max_length + 1):
            candidates.append(normalized_phone[:i])
            
        # 降順（長い方から一致を見るため）
        return sorted(candidates, key=len, reverse=True)
        
    @staticmethod
    def is_low_priority_number(normalized_phone: str) -> bool:
        """
        フリーダイヤルやIP電話など、エリア特定に不向きな番号か判定
        """
        low_priority_prefixes = ["0120", "0800", "0570", "050"]
        return any(normalized_phone.startswith(prefix) for prefix in low_priority_prefixes)
```

---

## 4. services/validation_service.py

エリアと電話番号の整合性を検証するメインサービスです。

```python
import json
from pathlib import Path
from typing import Dict, Any, Tuple
from app.utils.phone_normalizer import PhoneNormalizer

class ValidationService:
    def __init__(self, master_data_path: str = "app/data/area_code_master.json"):
        self.master_data = self._load_master_data(master_data_path)
        
    def _load_master_data(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            # フォールバック処理または例外を投げる
            return {"prefectures": {}}

    def validate_phone_area(self, phone: str, prefecture: str, city: str = None) -> Tuple[bool, str]:
        """
        指定された電話番号が検索エリア（都道府県）の市外局番と一致するか検証する
        戻り値: (一致するかどうか, ステータスメッセージ)
        """
        normalized_phone = PhoneNormalizer.normalize(phone)
        
        if not normalized_phone:
            return False, "INVALID_FORMAT"
            
        if PhoneNormalizer.is_low_priority_number(normalized_phone):
            return False, "NON_GEOGRAPHIC_NUMBER"

        candidates = PhoneNormalizer.extract_area_code_candidates(normalized_phone)
        
        # 都道府県データが存在するかチェック
        pref_data = self.master_data.get("prefectures", {}).get(prefecture)
        if not pref_data:
            return False, "PREFECTURE_NOT_FOUND"

        # 優先的に市区町村レベルでチェック（将来拡張用・データがあれば）
        if city and "cities" in pref_data and city in pref_data["cities"]:
            city_area_codes = pref_data["cities"][city].get("area_codes", [])
            for candidate in candidates:
                if candidate in city_area_codes:
                    return True, "CITY_MATCH"

        # 都道府県レベルでチェック
        pref_area_codes = pref_data.get("area_codes", [])
        for candidate in candidates:
            if candidate in pref_area_codes:
                return True, "PREFECTURE_MATCH"

        return False, "AREA_MISMATCH"
```

---

## 5. services/scoring_service.py

各要素に基づいて信頼度スコアを算出します。

```python
from typing import Dict, Any

class ConfidenceScoringService:
    def __init__(self):
        # スコアの重み付け定義
        self.weights = {
            "phone_area_match": 20,
            "phone_area_mismatch": -20,
            "name_match": 30,
            "address_match": 20,
            "domain_match": 10,
            "non_geographic_phone": -10 # 050や0120の場合の微減点
        }

    def calculate_score(self, validation_results: Dict[str, Any]) -> int:
        """
        検証結果を元に信頼度スコアを算出
        validation_results 例:
        {
            "phone_area_match": True,
            "name_match": True,
            "address_match": False,
            "domain_match": True,
            "phone_status": "PREFECTURE_MATCH"
        }
        """
        score = 0
        
        # エリア一致判定
        if validation_results.get("phone_area_match") is True:
            score += self.weights["phone_area_match"]
        elif validation_results.get("phone_area_match") is False:
            if validation_results.get("phone_status") == "NON_GEOGRAPHIC_NUMBER":
                score += self.weights["non_geographic_phone"]
            else:
                score += self.weights["phone_area_mismatch"]

        # その他の一致判定
        if validation_results.get("name_match"):
            score += self.weights["name_match"]
            
        if validation_results.get("address_match"):
            score += self.weights["address_match"]
            
        if validation_results.get("domain_match"):
            score += self.weights["domain_match"]

        # 0〜100の範囲に収めるなどの正規化処理を入れることも可能
        return max(0, min(100, score))
        
    def needs_re_fetch(self, score: int, phone_status: str) -> bool:
        """
        再取得対象かどうかの判定ロジック
        """
        if score < 50:
            return True
        if phone_status in ["AREA_MISMATCH", "NON_GEOGRAPHIC_NUMBER"]:
            return True
            
        return False
```

---

## 6. DB スキーマ変更案

SQLAlchemy等を使用した場合の追加カラム定義例です。地理情報（GIS）用のPostGISカラムも将来を見据えて記載しています。

```python
from sqlalchemy import Column, Integer, String, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Restaurant(Base):
    __tablename__ = "restaurants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    raw_phone = Column(String)
    normalized_phone = Column(String)
    
    # 基本エリア情報
    prefecture = Column(String, index=True)
    city = Column(String)
    address = Column(String)
    
    # 新規追加: 電話番号・スコアリング関連
    phone_area_code = Column(String)           # 抽出された市外局番 (例: "06")
    phone_area_match = Column(Boolean)         # エリア一致フラグ
    confidence_score = Column(Integer)         # 信頼度スコア (0-100)
    phone_validation_status = Column(String)   # 状態 (PREFECTURE_MATCH, AREA_MISMATCH, NON_GEOGRAPHIC_NUMBER 等)
    needs_re_fetch = Column(Boolean, default=False) # 再取得キュー対象フラグ

    # 将来拡張: GIS照合・Polygon連携用 (PostGIS等を想定)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    # 実際には GeoAlchemy2 の Geometry カラムなどを使用:
    # location = Column(Geometry(geometry_type='POINT', srid=4326))
```

---

## 7. FastAPI 実装例

`main.py` 又は `routes.py` におけるエンドポイントの実装例です。

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.services.validation_service import ValidationService
from app.services.scoring_service import ConfidenceScoringService

app = FastAPI()

# 依存性の注入や初期化
validator = ValidationService(master_data_path="app/data/area_code_master.json")
scorer = ConfidenceScoringService()

class RestaurantData(BaseModel):
    name: str
    phone: str
    prefecture: str
    city: Optional[str] = None
    # 便宜上その他の検証結果をクライアントから受け取ると仮定
    name_match: bool = False
    address_match: bool = False
    domain_match: bool = False

class ValidationResponse(BaseModel):
    normalized_phone: str
    phone_area_match: bool
    phone_validation_status: str
    confidence_score: int
    needs_re_fetch: bool

@app.post("/api/validate-restaurant", response_model=ValidationResponse)
async def validate_restaurant(data: RestaurantData):
    # 1. 正規化
    from app.utils.phone_normalizer import PhoneNormalizer
    normalized_phone = PhoneNormalizer.normalize(data.phone)
    
    # 2. 市外局番検証
    is_match, status = validator.validate_phone_area(
        phone=data.phone, 
        prefecture=data.prefecture,
        city=data.city
    )
    
    # 3. スコアリング用の辞書作成
    validation_results = {
        "phone_area_match": is_match,
        "phone_status": status,
        "name_match": data.name_match,
        "address_match": data.address_match,
        "domain_match": data.domain_match
    }
    
    # 4. スコア計算と再取得判定
    score = scorer.calculate_score(validation_results)
    re_fetch = scorer.needs_re_fetch(score, status)
    
    return ValidationResponse(
        normalized_phone=normalized_phone,
        phone_area_match=is_match,
        phone_validation_status=status,
        confidence_score=score,
        needs_re_fetch=re_fetch
    )
```

---

## 8. テストコード

`pytest` を用いたテストコードの実装例です。

```python
# tests/test_validation.py
import pytest
from app.utils.phone_normalizer import PhoneNormalizer
from app.services.validation_service import ValidationService
from app.services.scoring_service import ConfidenceScoringService

def test_phone_normalization():
    assert PhoneNormalizer.normalize("06-1234-5678") == "0612345678"
    assert PhoneNormalizer.normalize("(03) 1234-5678") == "0312345678"
    assert PhoneNormalizer.normalize("0120 123 456") == "0120123456"

def test_area_code_extraction():
    candidates = PhoneNormalizer.extract_area_code_candidates("0721234567")
    assert candidates == ["0721", "072", "07", "0"] # 5桁から2桁まで抽出

def test_is_low_priority():
    assert PhoneNormalizer.is_low_priority_number("0120123456") is True
    assert PhoneNormalizer.is_low_priority_number("05012345678") is True
    assert PhoneNormalizer.is_low_priority_number("0612345678") is False

# テスト用のモックマスタデータを使用したValidationServiceのテスト
class TestValidationService:
    @pytest.fixture
    def service(self, tmp_path):
        # 一時的なJSONファイルを作成
        import json
        master = {
            "prefectures": {
                "大阪府": {"area_codes": ["06", "072"]},
                "東京都": {"area_codes": ["03"]}
            }
        }
        file_path = tmp_path / "master.json"
        file_path.write_text(json.dumps(master), encoding="utf-8")
        return ValidationService(str(file_path))

    def test_validate_match(self, service):
        is_match, status = service.validate_phone_area("06-1234-5678", "大阪府")
        assert is_match is True
        assert status == "PREFECTURE_MATCH"

    def test_validate_mismatch(self, service):
        is_match, status = service.validate_phone_area("03-1234-5678", "大阪府")
        assert is_match is False
        assert status == "AREA_MISMATCH"

    def test_validate_free_dial(self, service):
        is_match, status = service.validate_phone_area("0120-123-456", "東京都")
        assert is_match is False
        assert status == "NON_GEOGRAPHIC_NUMBER"

def test_scoring():
    scorer = ConfidenceScoringService()
    
    # 理想的なケース
    score_high = scorer.calculate_score({
        "phone_area_match": True,
        "name_match": True,
        "address_match": True,
        "domain_match": True,
        "phone_status": "PREFECTURE_MATCH"
    })
    assert score_high == 80 # 20+30+20+10
    
    # エリア不一致ケース
    score_low = scorer.calculate_score({
        "phone_area_match": False,
        "name_match": True,
        "address_match": False,
        "domain_match": False,
        "phone_status": "AREA_MISMATCH"
    })
    assert score_low == 10 # -20+30
    
    assert scorer.needs_re_fetch(score_low, "AREA_MISMATCH") is True
```

---

## 9. 将来のPolygon連携（GIS）への布石

今後のPolygon判定（Uber/menu配達エリア判定やPoint in Polygon）をスムーズに実装するための設計指針です。

### アーキテクチャの拡張
現在の文字列表致から、空間情報（Spatial Data）を扱えるようにデータベースとロジックを拡張します。

1. **データベースのPostGIS化**: 
   RDBMSとしてPostgreSQL + PostGISを採用し、`latitude`, `longitude` から構築された `POINT` ジオメトリを格納します。
2. **GeoUtils モジュールの導入**:
   Shapely や GeoPandas などのライブラリを活用し、指定されたポリゴン（GeoJSON等）内に店舗座標が含まれるか計算するユーティリティクラス（`geo_utils.py`）を作成します。
3. **ScoringService の拡張**:
   `is_in_delivery_polygon` といった結果をスコアリング要素として追加します。

### 実装イメージ (utils/geo_utils.py の将来像)

```python
from shapely.geometry import Point, Polygon
from typing import List, Tuple

class GeoUtils:
    @staticmethod
    def is_point_in_polygon(lat: float, lon: float, polygon_coords: List[Tuple[float, float]]) -> bool:
        """
        指定された座標が、UberやMenuの配達エリア(Polygon)内に存在するか判定
        """
        if not lat or not lon or not polygon_coords:
            return False
            
        point = Point(lon, lat) # Shapelyは (x, y) = (lon, lat)
        area = Polygon(polygon_coords)
        return area.contains(point)
```

このユーティリティを `validate_restaurant` フローの中に組み込むことで、電話番号のエリア照合とGISのPolygon照合を二段構えで実行し、データの信頼性を飛躍的に高めることが可能になります。

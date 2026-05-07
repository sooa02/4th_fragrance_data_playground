"""
Creed Fragrance Crawler (Scrapling 기반)
=====================================
대상 사이트: https://www.creedfragrance.com/c/all-fragrances/
DB 설계: MySQL(products, brands, product_images, product_notes) + NoSQL(crawl_raw_pages)

사용법:
    pip install "scrapling[fetchers]" requests
    scrapling install

환경변수:
    export DEEPL_API_KEY="your_deepl_auth_key"
    export DEEPL_API_URL="https://api-free.deepl.com/v2/translate"   # Free
    # export DEEPL_API_URL="https://api.deepl.com/v2/translate"      # Pro

실행:
    python creed_spider.py
"""

import asyncio
import json
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional
from dotenv import load_dotenv

import requests
from scrapling.spiders import Spider, Response, Request

load_dotenv()


# ──────────────────────────────────────────────
# DeepL 설정
# ──────────────────────────────────────────────

DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
DEEPL_API_URL = os.getenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate").strip()

# 키 로드 확인 (시작 시 1회 출력)
if DEEPL_API_KEY:
    masked = DEEPL_API_KEY[:6] + "..." + DEEPL_API_KEY[-4:]
    print(f"[DeepL] API Key 로드됨: {masked}")
    print(f"[DeepL] Endpoint: {DEEPL_API_URL}")
else:
    print("[DeepL] ⚠ DEEPL_API_KEY 없음 → 번역 건너뜀 (.env 파일 또는 환경변수 확인)")

_translation_cache: dict[tuple[str, str, str], str] = {}


# ──────────────────────────────────────────────
# 향수 전문 용어 한국어 매핑 테이블
# DeepL이 원문 그대로 반환하거나 잘못 번역하는
# 향수 업계 관용 표기를 수동으로 정의.
# ──────────────────────────────────────────────

FRAGRANCE_TERM_MAP: dict[str, str] = {
    # ── 노트 (원문 영어 → 한국어 업계 표기)
    # DeepL이 번역하기 전 원문에 먼저 적용되므로
    # 잘못 번역되는 단어도 정확히 잡힘
    "Incense":          "인센스",
    "Vetiver":          "베티버",
    "Orris":            "오리스",
    "Oud":              "우드",
    "Ambroxan":         "암브록산",
    "Amber":            "앰버",
    "Musk":             "머스크",
    "Patchouli":        "패출리",
    "Bergamot":         "베르가못",
    "Cedarwood":        "시더우드",
    "Sandalwood":       "샌달우드",
    "Tonka Bean":       "통카빈",
    "Neroli":           "네롤리",
    "Ylang-Ylang":      "일랑일랑",
    "Galbanum":         "갈바넘",
    "Heliotrope":       "헬리오트로프",
    "Oakmoss":          "오크모스",
    "Oak Moss":         "오크모스",
    "Petitgrain":       "쁘띠그레인",
    "Tuberose":         "튜베로즈",
    "Osmanthus":        "오스만투스",
    "Peach":            "피치",
    "Rose":             "로즈",
    "Lime":             "라임",
    "Jasmine":          "자스민",
    "Lily":             "릴리",
    "Orchid":           "오키드",
    "Violet":           "바이올렛",
    "Vanilla":          "바닐라",
    "Pepper":           "페퍼",
    "Sage":             "세이지",
    "Leather":          "레더",       # 가죽 → 레더
    "Tobacco":          "토바코",      # 담배 → 토바코
    "Blackcurrant":     "블랙커런트",
    "Saffron":          "사프란",
    "Cardamom":         "카다멈",
    "Ginger":           "진저",        # 생강 → 진저
    "Lemon":            "레몬",
    "Mandarin":         "만다린",
    "Orange":           "오렌지",
    "Grapefruit":       "그레이프프루트",
    "Pineapple":        "파인애플",
    "Coconut":          "코코넛",
    "Lavender":         "라벤더",
    "Raspberry":        "라즈베리",
    "Greens":           "그린",        # 노트명으로 단순하게 유지
    "Cinnamon":         "시나몬",
}

# 역방향 매핑: DeepL이 번역한 한국어 결과도 잡기 위해
# (예: Incense → DeepL → "향" → 역방향으로 "인센스" 복원)
_TERM_MAP_KO_OVERRIDE: dict[str, str] = {
    "향":        "인센스",   # Incense가 "향"으로 번역되는 경우
    "가죽":      "레더",
    "담배":      "토바코",
    "생강":      "진저",
    "그린 노트 노트": "그린",  # 이중 변환 방지
}


def apply_term_map(text: str) -> str:
    """
    향수 용어 매핑 적용 (2단계).
    1단계: 영문 원문 → 업계 한국어 표기 (DeepL 번역 전 텍스트에 적용)
    2단계: DeepL 번역 결과 중 잘못된 한국어 → 올바른 표기 보정
        (예: "향" → "인센스", "가죽" → "레더")
    대소문자 구분 없이 단어 경계 기준 교체.
    """
    if not text:
        return text
    # 1단계: 영문 → 한국어 매핑
    for en, ko in FRAGRANCE_TERM_MAP.items():
        text = re.sub(
            rf"(?<![가-힣\w]){re.escape(en)}(?![가-힣\w])",
            ko, text, flags=re.IGNORECASE
        )
    # 2단계: 잘못 번역된 한국어 보정 (단어 단위 정확 일치)
    for wrong_ko, correct_ko in _TERM_MAP_KO_OVERRIDE.items():
        text = re.sub(
            rf"(?<![가-힣]){re.escape(wrong_ko)}(?![가-힣])",
            correct_ko, text
        )
    return text


def deepl_translate_text(
    text: str,
    target_lang: str = "KO",
    source_lang: str = "EN",
    preserve_empty: bool = True,
) -> str:
    """
    DeepL API로 단일 텍스트 번역.
    - 매핑 테이블에 정확히 일치하는 단어(노트명 등)는 DeepL 호출 없이 바로 반환
    - 실패 시 빈 문자열 반환
    """
    if not text:
        return "" if preserve_empty else text

    text = str(text).strip()
    if not text:
        return ""

    # ── 매핑 테이블 정확 일치 우선 (DeepL 호출 불필요)
    # 노트명처럼 단일 단어인 경우 매핑 테이블로 바로 처리
    text_lower = text.lower()
    for en, ko in FRAGRANCE_TERM_MAP.items():
        if text_lower == en.lower():
            return ko

    cache_key = (text, source_lang, target_lang)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    if not DEEPL_API_KEY:
        print("[DeepL] DEEPL_API_KEY가 없어 번역을 건너뜁니다.")
        return ""

    try:
        resp = requests.post(
            DEEPL_API_URL,
            headers={
                "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": [text],
                "source_lang": source_lang,
                "target_lang": target_lang,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        translations = data.get("translations", [])
        result = translations[0].get("text", "").strip() if translations else ""
        _translation_cache[cache_key] = result
        return result
    except Exception as e:
        print(f"[DeepL 번역 실패] {e}")
        return ""


def normalize_ingredients_ko(text: str) -> str:
    """
    성분 한국어 텍스트에서 '외래어(한국어)' 패턴을 괄호 안 한국어로 대체.
    예: 알쿨(알코올) → 알코올 / 퍼퓸(향료) → 향료 / 아쿠아(물) → 물
    """
    if not text:
        return text
    return re.sub(r'[가-힣a-zA-Z/]+\(([^)]+)\)', r'\1', text)


def translate_product_fields(product: "Product", sleep_sec: float = 0.15) -> "Product":
    """
    Product의 한국어 필드를 DeepL로 채움.
    - product_name_ko
    - description_ko
    - ingredients_ko  (번역 후 외래어(한국어) 패턴 정규화)
    - note_name_ko
    """
    product.product_name_ko = apply_term_map(deepl_translate_text(product.product_name_original))
    time.sleep(sleep_sec)

    product.description_ko = apply_term_map(deepl_translate_text(product.description_original))
    time.sleep(sleep_sec)

    raw_ingr_ko = apply_term_map(deepl_translate_text(product.ingredients_original))
    product.ingredients_ko = normalize_ingredients_ko(raw_ingr_ko)
    time.sleep(sleep_sec)

    for note in product.notes:
        raw_ko = deepl_translate_text(note.note_name_original)
        note.note_name_ko = apply_term_map(raw_ko or note.note_name_original)
        time.sleep(0.05)

    return product


# ──────────────────────────────────────────────
# 데이터 모델 (MySQL 테이블 구조 반영)
# ──────────────────────────────────────────────

@dataclass
class ProductNote:
    note_type: str
    note_name_original: str
    note_name_ko: str = ""
    sort_order: int = 0


@dataclass
class ProductImage:
    image_original_url: str
    image_internal_url: str = ""
    image_hash: str = ""
    sort_order: int = 0
    is_primary: bool = False
    download_status: str = "pending"


@dataclass
class Product:
    source_site: str = "creed_global"
    source_url: str = ""
    source_product_id: str = ""
    country: str = ""                  # 사이트 국가명 (대한민국, 영국, 미국 등)

    product_name_original: str = ""
    product_name_ko: str = ""
    category: str = ""
    scent_family: str = ""

    price_krw: Optional[float] = None
    price_original: Optional[float] = None
    currency: str = "EUR"
    volume_ml: Optional[float] = None

    vegan_flag: bool = False
    stock_status: str = "unknown"

    description_original: str = ""
    description_ko: str = ""
    ingredients_original: str = ""
    ingredients_ko: str = ""
    allergen_list_json: list = field(default_factory=list)

    images: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    crawled_at: str = ""
    extra_attributes_json: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# 파싱 헬퍼 함수
# ──────────────────────────────────────────────

def extract_price(text: str) -> tuple[Optional[float], str]:
    """'€255.00' → (255.0, 'EUR')"""
    if not text:
        return None, "EUR"
    symbol_map = {"€": "EUR", "$": "USD", "£": "GBP", "₩": "KRW"}
    currency = "EUR"
    for symbol, code in symbol_map.items():
        if symbol in text:
            currency = code
            break
    match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    if match:
        return float(match.group()), currency
    return None, currency


def extract_volume(text: str) -> Optional[float]:
    """'50ml', '1.7 oz' → ml 단위로 통일"""
    if not text:
        return None
    match = re.search(r"([\d.]+)\s*(ml|ML)", text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"([\d.]+)\s*oz", text, re.IGNORECASE)
    if match:
        return round(float(match.group(1)) * 29.5735, 1)
    return None


def extract_product_id(url: str) -> str:
    """URL에서 상품 ID 추출: /p/aventus/12345678/ → 12345678"""
    match = re.search(r"/p/[^/]+/(\d+)/?", url)
    return match.group(1) if match else hashlib.md5(url.encode()).hexdigest()[:12]


def parse_notes(note_text: str, note_type: str) -> list[ProductNote]:
    """'Rose, Bergamot, Lemon' → [ProductNote, ...]"""
    notes = []
    if not note_text:
        return notes
    for i, raw in enumerate(re.split(r"[,/;]+", note_text)):
        name = raw.strip()
        if name:
            notes.append(ProductNote(
                note_type=note_type,
                note_name_original=name,
                sort_order=i
            ))
    return notes


def parse_ingredients(text: str) -> tuple[str, list[str]]:
    """
    성분 텍스트에서 알러젠 리스트 추출 + Creed 면책 문구 제거.

    확인된 면책 문구 패턴 2종:
    1) "The ingredients used in The House of Creed's products are regularly updated..."
    2) "The lists of ingredients used in the composition of our brand's products
        are regularly updated..." (Fragaria 등 구버전 상품)
    """
    if text:
        # 두 패턴을 모두 커버하는 통합 정규식
        # "The [lists of] ingredients used in" 으로 시작하는 문장부터 끝까지 제거
        DISCLAIMER_PATTERN = re.compile(
            r"The (?:lists? of )?ingredients used in[^\n]*?"
            r"(?:personal use|www\.\S+|contact\b[^.]*)\.*\s*$",
            re.IGNORECASE | re.DOTALL,
        )
        text = DISCLAIMER_PATTERN.sub("", text).strip().rstrip(".-/").strip()

    allergens_keywords = [
        "limonene", "linalool", "geraniol", "citronellol", "eugenol",
        "coumarin", "cinnamal", "benzyl", "isoeugenol", "farnesol",
        "citral", "alpha-isomethyl ionone"
    ]
    found_allergens = []
    if text:
        lower = text.lower()
        for kw in allergens_keywords:
            if kw in lower:
                found_allergens.append(kw)
    return text, found_allergens


# ──────────────────────────────────────────────
# 상품 상세 파싱 (Creed 실제 HTML 구조 기반)
# ──────────────────────────────────────────────

def _parse_product_group_ld(response: Response) -> tuple[dict, list[dict]]:
    for raw in response.css("script[type='application/ld+json']::text").getall():
        try:
            data = json.loads(raw.strip())
            if data.get("@type") == "ProductGroup":
                return data, data.get("hasVariant", [])
        except (json.JSONDecodeError, ValueError):
            continue
    return {}, []


def _parse_products_obj(response: Response) -> dict:
    for raw in response.css("script::text").getall():
        if "productsObj" not in raw:
            continue
        m = re.search(r"const productsObj = (\{.*?\});\s*\n", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


def _extract_notes_from_description(description: str) -> list[ProductNote]:
    notes = []
    if not description:
        return notes

    ADJECTIVES = re.compile(
        r"\b(a|an|the|its|"
        r"exclusive|hand-picked|Calabrian|luscious|smoky|decadent|"
        r"vibrant|tantalising|sophisticated|exquisite|luxurious|"
        r"distinctive|unique|fresh|bright|rich|warm|deep|light|"
        r"delicate|bold|elegant|dynamic|lingering|enduring)\b\s*",
        re.IGNORECASE,
    )

    def clean(text: str) -> str:
        text = re.sub(r"\band\b", ",", text, flags=re.IGNORECASE)
        text = ADJECTIVES.sub("", text)
        text = re.sub(r"\s+", " ", text).strip().strip(",")
        return text

    top_raw = ""
    m = re.search(
        r"debut of\s+([^,\.]+(?:,\s*[^,\.]+)*?),\s*tinged with\s+([^,\.]+(?:,\s*[^,\.]+)*?)(?:,|\.|gives way)",
        description, re.IGNORECASE,
    )
    if m:
        top_raw = m.group(1) + ", " + m.group(2)
    else:
        for pat in [
            r"opens? with\s+([^,\.]+(?:,\s*[^,\.]+)*?)(?:\.|gives way|,\s*(?:giving|before))",
            r"top notes?[:\s]+([^\.;]+)",
            r"opening[^:]*:\s*([^\.]+)",
        ]:
            m = re.search(pat, description, re.IGNORECASE)
            if m:
                top_raw = m.group(1)
                break

    heart_raw = ""
    for pat in [
        r"gives way to\s+(?:[^,]{0,30}?)\s+([a-z][^\.]+?)\s+heart",
        r"heart(?:\s+note)?s?[:\s]+([^\.;]+)",
        r"middle notes?[:\s]+([^\.;]+)",
        r"at (?:its|the) heart[,\s]+([^\.;]+)",
    ]:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            heart_raw = m.group(1)
            break

    base_raw = ""
    for pat in [
        r"(?:cut with|deepened by|anchored by)\s+([^\.]+?)\s+at the base",
        r"base notes?[:\s]+([^\.;]+)",
        r"dry[- ]?down[:\s]+([^\.;]+)",
    ]:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            base_raw = m.group(1)
            break

    for note_type, raw_text in [("top", top_raw), ("heart", heart_raw), ("base", base_raw)]:
        if raw_text:
            notes.extend(parse_notes(clean(raw_text), note_type))

    return notes


def _extract_images_from_html(response: Response, product_id: str, variants: list) -> list[ProductImage]:
    variant_skus = {str(v.get("sku", "")) for v in variants}

    seen: set[str] = set()
    images: list[ProductImage] = []

    for v in variants:
        raw_url = v.get("image", "")
        inner = re.search(r"url=([^&]+)", raw_url)
        url = inner.group(1) if inner else raw_url
        if url and "thcdn.com/productimg" in url and url not in seen:
            seen.add(url)
            images.append(ProductImage(image_original_url=url))

    default_sku = str(variants[0].get("sku", "")) if variants else ""
    for img_el in response.css("img"):
        src = img_el.attrib.get("src") or img_el.attrib.get("data-src") or ""
        inner = re.search(r"url=(https?://static\.thcdn\.com/productimg/original/[^&\"']+)", src)
        if inner:
            url = inner.group(1)
        elif "static.thcdn.com/productimg/original" in src:
            url = src
        else:
            continue
        if url in seen:
            continue
        filename = url.split("/")[-1]
        if not filename.startswith(default_sku + "-"):
            continue
        seen.add(url)
        images.append(ProductImage(image_original_url=url))

    for i, img in enumerate(images):
        img.sort_order = i
        img.is_primary = (i == 0)

    return images


def parse_product_detail(response: Response, product_url: str) -> Product:
    p = Product()
    p.source_url = product_url
    p.source_product_id = extract_product_id(product_url)
    p.crawled_at = datetime.now(timezone.utc).isoformat()

    pg, variants = _parse_product_group_ld(response)

    p.product_name_original = pg.get("name", "").strip()
    p.category = "Fragrance"

    brand = pg.get("brand", {})
    if isinstance(brand, dict):
        p.extra_attributes_json["brand_name"] = brand.get("name", "Creed")

    for prop in pg.get("additionalProperty", []):
        if prop.get("name") == "suggestedGender":
            p.extra_attributes_json["gender"] = prop.get("value", "")

    desc_raw = pg.get("description", "")
    if not desc_raw and variants:
        desc_raw = variants[0].get("description", "")
    desc_raw = re.sub(r"<[^>]+>", " ", desc_raw)
    desc_raw = re.sub(r"\s+", " ", desc_raw).strip()
    desc_raw = re.split(r"New packaging:", desc_raw, flags=re.IGNORECASE)[0].strip()
    p.description_original = desc_raw

    if variants:
        first_v = variants[0]
        offer = first_v.get("offers", {})
        p.price_original = float(offer.get("price", 0) or 0) or None
        p.currency = offer.get("priceCurrency", "EUR")
        avail = offer.get("availability", "")
        p.stock_status = "in_stock" if "InStock" in avail else (
            "out_of_stock" if avail else "unknown"
        )

    products_obj = _parse_products_obj(response)

    variant_skus = {str(v.get("sku", "")) for v in variants}
    all_variants_data = []
    for sku_key, v_data in products_obj.items():
        if sku_key in variant_skus or str(v_data.get("defaultVariantSku", "")) in variant_skus:
            all_variants_data.append(v_data)

    if p.volume_ml is None and all_variants_data:
        default_sku = str(variants[0].get("sku", "")) if variants else ""
        target = next(
            (v for v in all_variants_data if str(v.get("item_id", "")) == default_sku),
            all_variants_data[0] if all_variants_data else {}
        )
        p.volume_ml = extract_volume(target.get("item_name", ""))

    if p.stock_status == "unknown" and all_variants_data:
        default_sku = str(variants[0].get("sku", "")) if variants else ""
        target = next(
            (v for v in all_variants_data if str(v.get("item_id", "")) == default_sku),
            all_variants_data[0] if all_variants_data else {}
        )
        in_stock = target.get("inStock")
        if in_stock is True:
            p.stock_status = "in_stock"
        elif in_stock is False:
            p.stock_status = "out_of_stock"

    if all_variants_data:
        p.extra_attributes_json["variants"] = [
            {
                "item_id": v.get("item_id"),
                "item_name": v.get("item_name"),
                "volume_ml": extract_volume(v.get("item_name", "") or ""),
                "price": v.get("price"),
                "currency": v.get("currency", "EUR"),
                "in_stock": v.get("inStock"),
            }
            for v in all_variants_data
            if extract_volume(v.get("item_name", "") or "")
        ]

    full_text = " ".join(response.css("*::text").getall())
    ingr_match = re.search(r"(ALCOOL[^\n<]{10,600})", full_text, re.IGNORECASE)
    if ingr_match:
        ingr_text = ingr_match.group(1).strip().rstrip(".")
        p.ingredients_original, p.allergen_list_json = parse_ingredients(ingr_text)

    # ── 노트: 1순위 ProductGroup JSON-LD additionalProperty (구조화 데이터)
    #          2순위 description NLP (구조화 데이터 없을 때만)
    note_extracted = False
    for prop in pg.get("additionalProperty", []):
        prop_name = prop.get("name", "").lower()
        val = prop.get("value", "")
        if not val:
            continue
        if "top" in prop_name:
            p.notes.extend(parse_notes(val, "top"))
            note_extracted = True
        elif "heart" in prop_name or "middle" in prop_name:
            p.notes.extend(parse_notes(val, "heart"))
            note_extracted = True
        elif "base" in prop_name:
            p.notes.extend(parse_notes(val, "base"))
            note_extracted = True

    if not note_extracted:
        p.notes = _extract_notes_from_description(p.description_original)

    p.images = _extract_images_from_html(response, p.source_product_id, variants)

    if not p.product_name_original:
        p.product_name_original = (response.css("h1::text").get() or "").strip()

    if p.price_original is None:
        price_text = (response.css("[class*='price']::text").get() or "").strip()
        p.price_original, p.currency = extract_price(price_text)

    return p


# ──────────────────────────────────────────────
# NoSQL raw document 빌더
# ──────────────────────────────────────────────

def build_raw_document(url: str, parsed_fields: dict, product_raw: dict = None) -> dict:
    return {
        "source_site": "creed_global",
        "page_url": url,
        "page_type": "product_detail",
        "raw_payload": {
            "product_raw": product_raw or {},
            "parsed_fields": parsed_fields,
        },
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────
# productList content 헬퍼
# ──────────────────────────────────────────────

def _get_content(product_item: dict, key: str):
    for item in product_item.get("content", []):
        if item["key"] != key:
            continue
        val = item["value"]
        t = val.get("__typename", "")
        if t == "ProductContentStringListValue":
            return val.get("stringListValue", [])
        elif t == "ProductContentRichContentValue":
            parts = val.get("richContentValue", {}).get("content", [])
            return " ".join(p.get("content", "") for p in parts)
        elif t == "ProductContentRichContentListValue":
            all_parts = []
            for block in val.get("richContentListValue", []):
                for p in block.get("content", []):
                    all_parts.append(p.get("content", ""))
            return " ".join(all_parts)
        elif t == "ProductContentIntValue":
            return val.get("intValue")
        elif t == "ProductContentStringValue":
            return val.get("stringValue")
    return None


def _parse_product_from_list(item: dict) -> Product:
    p = Product()
    base = "https://www.creedfragrance.com"

    sku = str(item.get("sku", ""))
    raw_url = item.get("url", "").strip("/")
    url_clean = re.sub(r"^/?p/", "", raw_url)
    p.source_url = f"{base}/p/{url_clean}/"
    p.source_product_id = sku
    p.country = "UK"   # creedfragrance.com 글로벌 사이트 (영국 본사)
    p.crawled_at = datetime.now(timezone.utc).isoformat()

    p.product_name_original = item.get("title", "").strip()

    p.category = "Fragrance"
    family = _get_content(item, "creed_fragranceFamily") or \
            _get_content(item, "beauty_olfactoryFamily") or []
    p.scent_family = ", ".join(family) if isinstance(family, list) else str(family or "")

    gender = _get_content(item, "creed_gender") or []
    p.extra_attributes_json["gender"] = (
        gender[0] if isinstance(gender, list) and gender else str(gender or "")
    )
    p.extra_attributes_json["brand_name"] = "Creed"

    synopsis_raw = _get_content(item, "synopsis") or ""
    synopsis_clean = re.sub(r"<[^>]+>", " ", synopsis_raw)
    synopsis_clean = re.sub(r"\s+", " ", synopsis_clean).strip()
    p.description_original = synopsis_clean

    ingr_raw = _get_content(item, "ingredients") or ""
    ingr_clean = re.sub(r"<[^>]+>", " ", ingr_raw)
    ingr_clean = re.sub(r"\s+", " ", ingr_clean).strip().rstrip(".")
    p.ingredients_original, p.allergen_list_json = parse_ingredients(ingr_clean)

    for note_type, content_key in [
        ("top", "beauty_fragranceTopNote"),
        ("heart", "beauty_fragranceHeartNote"),
        ("base", "beauty_fragranceBaseNote"),
    ]:
        raw = _get_content(item, content_key) or []
        note_list = raw if isinstance(raw, list) else [raw]
        for i, name in enumerate(note_list):
            name = str(name).strip()
            if name:
                p.notes.append(ProductNote(
                    note_type=note_type,
                    note_name_original=name,
                    sort_order=i,
                ))

    dv = item.get("defaultVariant", {})
    price_obj = dv.get("price", {}).get("price", {})
    p.price_original = float(price_obj.get("amount", 0) or 0) or None
    p.currency = price_obj.get("currency", "EUR")
    p.stock_status = "in_stock" if dv.get("inStock") else "out_of_stock"
    p.volume_ml = extract_volume(dv.get("title", ""))

    seen_urls: set[str] = set()
    for img in item.get("images", []):
        url = img.get("original", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            p.images.append(ProductImage(image_original_url=url))
    for img in dv.get("images", []):
        url = img.get("original", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            p.images.append(ProductImage(image_original_url=url))
    for i, img in enumerate(p.images):
        img.sort_order = i
        img.is_primary = (i == 0)

    all_variants = item.get("variants", [])
    p.extra_attributes_json["variants"] = [
        {
            "sku": v.get("sku") or (v.get("price") and None),
            "title": v.get("title", ""),
            "volume_ml": extract_volume(v.get("title", "") or ""),
            "price": v.get("price", {}).get("price", {}).get("amount"),
            "currency": v.get("price", {}).get("price", {}).get("currency", "EUR"),
            "in_stock": v.get("inStock"),
        }
        for v in all_variants
        if extract_volume(v.get("title", "") or "")
    ]

    return p


# ──────────────────────────────────────────────
# Scrapling Spider
# ──────────────────────────────────────────────

class CreedSpider(Spider):
    name = "creed_fragrance"

    start_urls = [
        "https://www.creedfragrance.com/c/all-fragrances/",
    ]

    concurrency = 2
    download_delay = 2.5
    fetcher = "StealthyFetcher"

    def __init__(self):
        super().__init__()
        self.products: list[Product] = []
        self.raw_documents: list[dict] = []

    async def parse(self, response: Response):
        print(f"[목록] {response.url} 파싱 중...")
        base = "https://www.creedfragrance.com"
        page_products = 0

        for raw in response.css("script::text").getall():
            if "const productList" not in raw:
                continue
            m = re.search(r"const productList = (\{.*?\});\s*\n", raw, re.DOTALL)
            if not m:
                continue
            try:
                pl = json.loads(m.group(1))
                total = pl.get("total", "?")
                has_more = pl.get("hasMore", False)
                items = pl.get("products", [])
                print(f"  productList: {len(items)}개 / 전체 {total}개 / hasMore={has_more}")

                for item in items:
                    title = item.get("title", "")
                    cat2 = item.get("categoryLevel2", "")

                    # ── 스킵 조건 0: 데이터 불완전으로 명시적 제외 상품
                    # 사이트 백엔드에 노트 데이터 자체가 미등록된 상품
                    EXPLICIT_SKIP_PRODUCTS = {
                        "Oud Zarian",  # top/heart/base 노트 키 미등록 (신제품 추정)
                    }
                    if title in EXPLICIT_SKIP_PRODUCTS:
                        print(f"  [스킵] {title} (노트 데이터 미등록 — 명시적 제외)")
                        continue

                    NON_FRAGRANCE_CATS = {
                        "Sets & Kits", "Accessories", "Body Care",
                        "Hair Care", "Skin Care", "Home Fragrance",
                        "Gift Cards",
                    }
                    if cat2 and cat2 in NON_FRAGRANCE_CATS:
                        print(f"  [스킵] {title} (category={cat2})")
                        continue

                    SKIP_KEYWORDS = [
                        "gift card", " set", "atomiser", "refill",
                        "wash bag", "leather bag", "candle", "body lotion",
                        "shower gel", "hair", "soap",
                    ]
                    if any(kw in title.lower() for kw in SKIP_KEYWORDS):
                        print(f"  [스킵] {title} (키워드 매칭)")
                        continue

                    has_ingr = any(c["key"] == "ingredients" for c in item.get("content", []))
                    dv_title = item.get("defaultVariant", {}).get("title", "")
                    has_vol = bool(re.search(r"\d+\s*ml", dv_title, re.IGNORECASE))
                    variants_have_vol = any(
                        re.search(r"\d+\s*ml", v.get("title", ""), re.IGNORECASE)
                        for v in item.get("variants", [])
                    )
                    if not has_ingr and not has_vol and not variants_have_vol:
                        print(f"  [스킵] {title} (성분·볼륨 모두 없음)")
                        continue

                    product = _parse_product_from_list(item)
                    product = translate_product_fields(product)

                    self.products.append(product)
                    page_products += 1

                    raw_doc = build_raw_document(
                        url=product.source_url,
                        parsed_fields={
                            "product_name": product.product_name_original,
                            "product_name_ko": product.product_name_ko,
                            "price": product.price_original,
                            "currency": product.currency,
                            "volume_ml": product.volume_ml,
                            "stock_status": product.stock_status,
                            "notes_count": len(product.notes),
                            "images_count": len(product.images),
                            "source": "productList_script",
                        },
                        product_raw=item,
                    )
                    self.raw_documents.append(raw_doc)

                    print(
                        f"  ✓ '{product.product_name_original}' | "
                        f"{product.price_original} {product.currency} | "
                        f"{product.volume_ml}ml | 노트 {len(product.notes)}개 | "
                        f"번역명='{product.product_name_ko}'"
                    )
                    yield asdict(product)

            except (json.JSONDecodeError, ValueError) as e:
                print(f"  productList 파싱 실패: {e}")
            break

        for raw in response.css("script::text").getall():
            if "const productList" not in raw:
                continue
            m = re.search(r"const productList = (\{.*?\});\s*\n", raw, re.DOTALL)
            if not m:
                continue
            pl = json.loads(m.group(1))
            if pl.get("hasMore"):
                current_url = str(response.url)
                m_page = re.search(r"pageNumber=(\d+)", current_url)
                next_num = int(m_page.group(1)) + 1 if m_page else 2
                next_url = f"{base}/c/all-fragrances/?pageNumber={next_num}"
                print(f"[페이지네이션] 다음: {next_url} (page {next_num})")
                yield Request(next_url, callback=self.parse)
            break

    async def on_close(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._save_mysql_ready(timestamp)
        self._save_nosql_ready(timestamp)
        print(f"\n크롤링 완료: 총 {len(self.products)}개 상품")

    def _save_mysql_ready(self, timestamp: str):
        """
        MySQL 적재용 JSON 저장.

        products 배열의 각 항목 구조:
        {
        "country":          사이트 국가명 (영국, 대한민국, 미국 등)
        "korean_name":      한국어 상품명
        "english_name":     영문 상품명
        "product_type":     상품 유형 (Eau de Parfum 등, 없으면 category)
        "product_url":      상품 페이지 URL
        "regular_price":    정가 문자열 (예: "330.00 EUR")
        "image_url":        대표 이미지 URL
        "ingredients":      성분 원문
        "key_ingredients":  대표 노트 (top note 우선, 없으면 heart/base)
        + 서비스용 부가 필드
        }
        """
        brands_data = [{
            "name": "Creed",
            "country": "France",
            "official_site_url": "https://www.creedfragrance.com"
        }]
        products_data = []
        images_data   = []
        notes_data    = []

        CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£", "KRW": "₩"}

        for prod in self.products:
            # ── regular_price: "330.00 EUR" 또는 "€330.00" 형식
            sym = CURRENCY_SYMBOL.get(prod.currency, prod.currency)
            if prod.price_original is not None:
                regular_price = f"{sym}{prod.price_original:,.2f}"
            else:
                regular_price = ""

            # ── product_type: extra_attributes에서 향수 유형 추출 (없으면 category)
            product_type = (
                prod.extra_attributes_json.get("fragrance_type")
                or prod.extra_attributes_json.get("product_type")
                or prod.category
                or "Eau de Parfum"
            )

            # ── key_ingredients: top note 우선, 없으면 heart → base 순 (한국어)
            top_notes   = [n.note_name_ko for n in prod.notes if n.note_type == "top"]
            heart_notes = [n.note_name_ko for n in prod.notes if n.note_type == "heart"]
            base_notes  = [n.note_name_ko for n in prod.notes if n.note_type == "base"]
            key_ingredients = top_notes or heart_notes or base_notes

            # ── 대표 이미지 URL
            image_url = prod.images[0].image_original_url if prod.images else ""

            products_data.append({
                "country":         prod.country,
                "korean_name":     prod.product_name_ko,
                "english_name":    prod.product_name_original,
                "product_type":    product_type,
                "product_url":     prod.source_url,
                "regular_price":   regular_price,
                "image_url":       image_url,
                "ingredients":     prod.ingredients_ko,     # 한국어 성분
                "key_ingredients": key_ingredients,          # 한국어 노트명
            })

            for img in prod.images:
                images_data.append({
                    "source_product_id":  prod.source_product_id,
                    "image_original_url": img.image_original_url,
                    "image_internal_url": img.image_internal_url,
                    "sort_order":         img.sort_order,
                    "is_primary":         img.is_primary,
                    "download_status":    img.download_status,
                })

            for note in prod.notes:
                notes_data.append({
                    "source_product_id":   prod.source_product_id,
                    "note_type":           note.note_type,
                    "note_name_original":  note.note_name_original,
                    "note_name_ko":        note.note_name_ko,
                    "sort_order":          note.sort_order,
                })

        output = {
            "brands":         brands_data,
            "products":       products_data,
            "product_images": images_data,
            "product_notes":  notes_data,
        }
        fname = f"creed_mysql_ready_{timestamp}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"[저장] MySQL 준비 데이터 → {fname}")

    def _save_nosql_ready(self, timestamp: str):
        fname = f"creed_nosql_raw_{timestamp}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(self.raw_documents, f, ensure_ascii=False, indent=2)
        print(f"[저장] NoSQL raw 데이터 → {fname}")


# ──────────────────────────────────────────────
# 단일 상품 테스트용 함수
# ──────────────────────────────────────────────

def test_single_product(url: str, debug: bool = False):
    from scrapling.fetchers import StealthyFetcher, Fetcher

    print(f"\n테스트 크롤링: {url}")
    response = None

    try:
        print("  [1/2] StealthyFetcher(headless) 시도 중...")
        response = StealthyFetcher.fetch(url, headless=True, network_idle=True)
        print("  ✓ StealthyFetcher 성공")
    except Exception as e:
        print(f"  StealthyFetcher 실패: {e}")

    if response is None:
        try:
            print("  [2/2] 일반 Fetcher(HTTP) 시도 중...")
            response = Fetcher.get(url)
            print("  ✓ Fetcher 성공 (JS 미렌더링)")
        except Exception as e:
            print(f"  모든 Fetcher 실패: {e}")
            return

    if debug:
        print("\n[DEBUG] script 태그 내 데이터 키 탐색:")
        for raw in response.css("script::text").getall():
            if "const productList" in raw:
                m = re.search(r"const productList = (\{.*?\});\s*\n", raw, re.DOTALL)
                if m:
                    try:
                        pl = json.loads(m.group(1))
                        print(f"  productList: total={pl.get('total')} hasMore={pl.get('hasMore')} products={len(pl.get('products',[]))}개")
                    except:
                        pass
            if "const productsObj" in raw:
                print("  productsObj: 있음")

        ld_types = []
        for raw in response.css("script[type='application/ld+json']::text").getall():
            try:
                d = json.loads(raw.strip())
                ld_types.append(d.get("@type", "?"))
            except:
                pass
        print(f"  JSON-LD @types: {ld_types}")
        print()

    product = parse_product_detail(response, url)
    product = translate_product_fields(product)
    result = asdict(product)

    print("\n" + "=" * 60)
    print("  [파싱 결과 요약]")
    print(f"  상품명(원문): {result['product_name_original'] or '⚠ 비어있음'}")
    print(f"  상품명(한글): {result['product_name_ko'] or '⚠ 비어있음'}")
    print(f"  가격        : {result['price_original']} {result['currency']}")
    print(f"  용량        : {result['volume_ml']} ml")
    print(f"  재고        : {result['stock_status']}")
    print(f"  노트 수     : {len(result['notes'])}개  → {[n['note_name_original'] for n in result['notes'][:5]]}")
    print(f"  이미지 수   : {len(result['images'])}개")
    print(f"  성분 길이   : {len(result['ingredients_original'])} chars")
    print(f"  설명 길이   : {len(result['description_original'])} chars")
    print("=" * 60)
    print("\n전체 JSON:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _debug_keys(obj, prefix="", depth=3):
    if depth == 0 or not isinstance(obj, (dict, list)):
        return
    if isinstance(obj, list):
        if obj:
            print(f"{prefix}[list, len={len(obj)}]")
            _debug_keys(obj[0], prefix + "  [0].", depth - 1)
        return
    for k, v in obj.items():
        vtype = type(v).__name__
        if isinstance(v, dict):
            print(f"{prefix}{k}: {{dict, {len(v)} keys}}")
            _debug_keys(v, prefix + "  ", depth - 1)
        elif isinstance(v, list):
            print(f"{prefix}{k}: [list, len={len(v)}]")
            if v and depth > 1:
                _debug_keys(v[0], prefix + "  [0].", depth - 1)
        else:
            val_preview = str(v)[:60] if v is not None else "null"
            print(f"{prefix}{k}: ({vtype}) {val_preview}")


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────

def dump_html(url: str):
    from scrapling.fetchers import StealthyFetcher

    print(f"HTML 덤프: {url}")
    response = StealthyFetcher.fetch(url, headless=True, network_idle=True)

    html_content = response.prettify() if hasattr(response, "prettify") else str(response)

    with open("dump_page.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  ✓ dump_page.html 저장 ({len(html_content):,} chars)")

    scripts = response.css("script")
    summary_lines = [f"script 태그 수: {len(scripts)}\n"]
    data_scripts = []

    data_keywords = [
        "window.__", "dataLayer", "__NEXT_DATA__", "pageData",
        "catalog", "demandware", "sfcc", "product", "variants",
        "notes", "ingredients", "topNotes", "heartNotes", "baseNotes",
    ]

    for i, s in enumerate(scripts):
        src = s.attrib.get("src", "")
        sid = s.attrib.get("id", "")
        stype = s.attrib.get("type", "")
        text = s.css("::text").get() or ""
        preview = text[:150].replace("\n", " ")
        line = f"[{i:02d}] id={sid!r} type={stype!r} src={src!r} len={len(text)} | {preview}"
        summary_lines.append(line)

        for kw in data_keywords:
            if kw.lower() in text.lower():
                data_scripts.append(f"\n{'='*60}\n[script {i:02d}] 키워드={kw!r}\n{'='*60}\n{text[:3000]}")
                break

    with open("dump_scripts.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))
    print(f"  ✓ dump_scripts.txt 저장 ({len(scripts)}개 script)")

    with open("dump_data_scripts.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(data_scripts) if data_scripts else "데이터 관련 script 없음")
    print(f"  ✓ dump_data_scripts.txt 저장 ({len(data_scripts)}개 데이터 script)")

    print("\n[셀렉터 탐색 결과]")
    checks = [
        ("h1", "h1::text"),
        ("price", "[class*='price']::text"),
        ("volume/size", "[class*='volume']::text, [class*='size']::text"),
        ("description", "[class*='description'] p::text"),
        ("notes", "[class*='note']::text"),
        ("ingredients", "[class*='ingredient']::text"),
        ("add-to-bag btn", "button[class*='add']::text"),
        ("images (img src)", "img::attr(src)"),
        ("application/json script", "script[type='application/json']::text"),
        ("application/ld+json", "script[type='application/ld+json']::text"),
    ]
    for label, sel in checks:
        try:
            vals = response.css(sel).getall()[:3]
            vals_clean = [v[:60] for v in vals if v and v.strip()]
            status = f"✓ {vals_clean}" if vals_clean else "✗ (없음)"
        except Exception as e:
            status = f"오류: {e}"
        print(f"  {label:30s}: {status}")

    print("\n→ dump_page.html 을 브라우저로 열거나, dump_data_scripts.txt를 확인하세요")


def analyze_listing(url: str):
    from scrapling.fetchers import StealthyFetcher

    print(f"목록 페이지 분석: {url}")
    response = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    print(f"  HTTP {response.status}")

    product_urls: list[str] = []
    seen: set[str] = set()

    for raw in response.css("script::text").getall():
        if "const products" not in raw:
            continue
        m = re.search(r"const products = (\[.*?\]);\s*\n", raw, re.DOTALL)
        if not m:
            continue
        try:
            items = json.loads(m.group(1))
            for item in items:
                href = item.get("url", "")
                if href and re.match(r"^/p/", href):
                    url = f"https://www.creedfragrance.com{href}"
                    if url not in seen:
                        seen.add(url)
                        product_urls.append(url)
        except Exception:
            pass

    print(f"  수집된 상품 URL 수: {len(product_urls)}")
    for u in product_urls[:10]:
        print("   -", u)


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2:
        cmd = sys.argv[1].lower()

        if cmd == "test":
            url = sys.argv[2] if len(sys.argv) >= 3 else "https://www.creedfragrance.com/p/aventus/12870029/"
            debug = "--debug" in sys.argv
            test_single_product(url, debug=debug)

        elif cmd == "dump":
            url = sys.argv[2] if len(sys.argv) >= 3 else "https://www.creedfragrance.com/p/aventus/12870029/"
            dump_html(url)

        elif cmd == "list":
            url = sys.argv[2] if len(sys.argv) >= 3 else "https://www.creedfragrance.com/c/all-fragrances/"
            analyze_listing(url)

        else:
            print("사용법:")
            print("  python creed_spider_v2.py")
            print("  python creed_spider_v2.py test [PRODUCT_URL] [--debug]")
            print("  python creed_spider_v2.py dump [URL]")
            print("  python creed_spider_v2.py list [LIST_URL]")
    else:
        spider = CreedSpider()
        result = spider.start()
        print(f"[완료] completed={result.completed}, paused={result.paused}, items={len(result.items)}")
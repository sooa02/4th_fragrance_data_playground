"""
그랑핸드(GRANHAND) 향수 크롤러 최종판 v5
"""

import json
import re
from typing import AsyncGenerator

from scrapling.spiders import Spider, Response, Request


BASE_URL = "https://granhand.com"
CATEGORY_URL = f"{BASE_URL}/product/list.html?cate_no=181"
_PRODUCT_RE = re.compile(r"/product/detail\.html\?.*product_no=(\d+)")

AROMA_WHITELIST = {
    "제라니올": "제라니올", "리날룰": "리날룰", "시트로넬올": "시트로넬올",
    "파네솔": "파네솔", "헥실신남알": "헥실신남알", "하이드록시시트로넬알": "하이드록시시트로넬알",
    "벤질살리실레이트": "벤질살리실레이트", "벤질벤조에이트": "벤질벤조에이트",
    "벤질알코올": "벤질알코올", "알파-아이소메틸아이오논": "아이소메틸아이오논",
    "아이소메틸아이오논": "아이소메틸아이오논", "알파-아이소메틸이오논": "아이소메틸아이오논",
    "유제놀": "유제놀", "이소유제놀": "이소유제놀", "신남알": "신남알",
    "아밀신남알": "아밀신남알", "부틸페닐메틸프로피오날": "부틸페닐메틸프로피오날",
    "리모넨": "리모넨", "시트랄": "시트랄", "쿠마린": "쿠마린",
    "에틸렌브라실레이트": "에틸렌브라실레이트", "바닐린": "바닐린",
    "비터오렌지꽃오일": "네롤리", "네롤리오일": "네롤리",
    "라벤더오일": "라벤더", "로즈오일": "로즈", "로즈우드오일": "로즈우드",
    "제라늄오일": "제라늄", "일랑일랑오일": "일랑일랑", "자스민오일": "자스민",
    "베르가모트오일": "베르가모트", "레몬오일": "레몬", "오렌지오일": "오렌지",
    "샌달우드오일": "샌달우드", "시더우드오일": "시더우드", "베티버오일": "베티버",
    "패출리오일": "패출리", "바질오일": "바질", "미국풍나무오일": "미국풍나무",
    "팔마로사오일": "팔마로사", "사이프러스오일": "사이프러스",
}


def _is_product_url(href: str) -> bool:
    return bool(_PRODUCT_RE.search(href))

def _extract_product_id(url: str) -> str:
    m = _PRODUCT_RE.search(url)
    return m.group(1) if m else ""

def _to_abs(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return href

def _clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

def _get_full_text(response):
    try:
        return " ".join(t.strip() for t in response.css("*::text").getall() if t.strip())
    except:
        return ""

def _parse_english_name(raw: str) -> str:
    # "[] Cask Signature Perfume" → "Cask Signature Perfume"
    return re.sub(r"^\s*\[.*?\]\s*", "", raw).strip()

def _parse_price(response) -> str:
    for sel in ["#span_product_price_text", ".price strong", ".price em"]:
        try:
            els = response.css(sel)
            if els:
                t = _clean(els[0].text)
                if t and re.search(r"\d", t):
                    # 숫자만 추출 후 "원" 단위로 통일
                    m = re.search(r"[\d,]+", t.replace(" ", ""))
                    if m:
                        return m.group() + "원"
        except:
            continue
    return ""

def _parse_image_url(response, json_ld: dict) -> str:
    # JSON-LD image → /big/ 고화질
    ld_images = json_ld.get("image", [])
    if ld_images:
        return ld_images[0] if isinstance(ld_images, list) else ld_images
    # fallback: ThumbImage
    for img in response.css("img.ThumbImage, img"):
        src = img.attrib.get("src", "") or img.attrib.get("data-src", "")
        if "granhand.com/web/product/" in src:
            if src.startswith("//"):
                src = "https:" + src
            return src
    return ""

def _parse_ingredients(full_text: str, english_name: str, korean_name: str) -> str:
    idx = full_text.find("전성분")
    if idx < 0:
        return ""
    section = full_text[idx:idx + 3000]

    # 매칭 후보: 한국어명(앞 6자 strip), 영문명 첫 단어
    candidates = []
    if korean_name and re.search(r"[가-힣]", korean_name):
        for length in [len(korean_name), 6, 4, 3]:
            prefix = korean_name[:length].strip()
            if prefix:
                candidates.append(re.escape(prefix))
    if english_name:
        candidates.append(re.escape(english_name.split()[0]))

    for cand in candidates:
        # 패턴1: "한글명 (영문명):" 또는 "한글명(영문명):"
        m = re.search(
            rf"[가-힣]*{cand}[가-힣]*\s*[\(\[][^)\]]*[\)\]]\s*:\s*(.+?)(?=\r?\n·|\s*주의|\Z)",
            section, re.IGNORECASE
        )
        if m:
            return _clean(m.group(1))

        # 패턴2: 영문명 단독 "영문명 ..." + 콜론
        m2 = re.search(
            rf"{cand}[^:\r\n]{{0,40}}:\s*(.+?)(?=\r?\n·|\s*주의|\Z)",
            section, re.IGNORECASE
        )
        if m2:
            raw = _clean(m2.group(1))
            raw = re.split(r"\s*·\s*[가-힣]", raw)[0]
            return _clean(raw)

    return ""

def _parse_key_ingredients(ingredients: str) -> list:
    if not ingredients:
        return []
    parts = [p.strip() for p in re.split(r"[,，]", ingredients) if p.strip()]
    seen, result = set(), []
    for part in parts:
        for key, display in AROMA_WHITELIST.items():
            if key in part or part in key:
                if display not in seen:
                    seen.add(display)
                    result.append(display)
                break
    return result

def _parse_product_type(korean_name: str) -> str:
    n = korean_name
    if "멀티 퍼퓸" in n: return "멀티 퍼퓸"
    if "시그니처 퍼퓸" in n: return "시그니처 퍼퓸"
    if "오드퍼퓸" in n or "오 드 퍼퓸" in n: return "오드퍼퓸"
    if "퍼퓸" in n: return "향수"
    return "향수"


class GranhandKRSpider(Spider):
    name = "granhand_kr"
    start_urls = [CATEGORY_URL + "&page=1"]
    concurrency = 3
    download_delay = 1.5
    session_config = {
        "default": {
            "type": "fetcher",
            "headers": {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Referer": "https://granhand.com/",
            },
        }
    }

    def __init__(self):
        super().__init__()
        self._out = open("granhand_products.jsonl", "w", encoding="utf-8")
        self._seen_ids: set[str] = set()
        self._seen_pages: set[str] = set()
        self._count = 0

    async def parse(self, response: Response) -> AsyncGenerator:
        hrefs = set()
        for a in response.css("a"):
            href = a.attrib.get("href", "")
            if _is_product_url(href):
                hrefs.add(href)

        print(f"[목록] {response.url} → {len(hrefs)}개 발견")

        for href in hrefs:
            pid = _extract_product_id(href)
            if not pid or pid in self._seen_ids:
                continue
            self._seen_ids.add(pid)
            yield Request(url=_to_abs(href), callback=self.parse_product)

        # 페이지네이션
        for a in response.css("a"):
            href = a.attrib.get("href", "")
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                full_url = f"{CATEGORY_URL}&page={m.group(1)}"
                if full_url not in self._seen_pages:
                    self._seen_pages.add(full_url)
                    print(f"[페이지] → {full_url}")
                    yield Request(url=full_url, callback=self.parse)

    async def parse_product(self, response: Response) -> AsyncGenerator:
        full_text = _get_full_text(response)

        # JSON-LD 파싱
        json_ld = {}
        try:
            m = re.search(
                r'\{"@context":"https://schema\.org","@type":"Product".+?\}(?=\s*window)',
                full_text, re.DOTALL
            )
            if m:
                json_ld = json.loads(m.group())
        except:
            pass

        # 리다이렉트 감지 (JSON-LD 없으면 홈으로 튕긴 것)
        if not json_ld.get("name"):
            print(f"  ⚠ 리다이렉트 감지, 건너뜀: {response.url}")
            return

        # ── 한국어 상품명 (korean_name)
        # 1순위: h2.fs-p2 → 항상 한국어명 ("비올레뜨 멀티 퍼퓸")
        # 2순위: JSON-LD name (한국어)
        # 3순위: JSON-LD description 첫 줄
        BLACKLIST = ["각인", "그랑핸드", "서교점", "가회점", "단독판매", "서비스"]

        korean_name = ""
        try:
            els = response.css("h2.fs-p2")
            if els:
                candidate = _clean(els[0].text)
                candidate = re.sub(r"^\s*\[.*?\]\s*", "", candidate).strip()
                if re.search(r"[가-힣]", candidate) and not any(bw in candidate for bw in BLACKLIST):
                    korean_name = candidate
        except:
            pass

        if not korean_name:
            ld_name = json_ld.get("name", "")
            if re.search(r"[가-힣]", ld_name) and not any(bw in ld_name for bw in BLACKLIST):
                korean_name = re.sub(r"\s*\d+\s*ml.*$", "", ld_name).strip()

        if not korean_name:
            desc_raw = json_ld.get("description", "")
            if desc_raw:
                first_line = re.split(r"\r\n|\r|\n", desc_raw)[0].strip()
                first_line = re.sub(r"\s*\d+\s*ml.*$", "", first_line, flags=re.IGNORECASE).strip()
                if re.search(r"[가-힣]", first_line) and not any(bw in first_line for bw in BLACKLIST):
                    korean_name = first_line

        # ── 영문 상품명 (english_name)
        # 전성분 섹션: "비올레트 (VIOLETTE):" → VIOLETTE 추출
        # fallback: JSON-LD name이 영문인 경우
        def _extract_english_from_ingredients(ft: str, ko: str) -> str:
            """
            전성분 섹션 '한글명 (영문명):' 또는 '한글명(영문명):' 패턴에서 영문명 추출
            - 수아(soie) → Soie
            - 이플 (EPEUL) → Epeul
            - 마린 오키드 (MARINE ORCHID) → Marine Orchid
            """
            if not ko:
                return ""
            # 한국어명 앞 2~6글자로 단계적으로 시도
            for length in [len(ko), 6, 4, 3, 2]:
                prefix = ko[:length].strip()
                if not prefix:
                    continue
                m = re.search(
                    rf"{re.escape(prefix)}[가-힣\s]*[\(\[]([A-Za-z][A-Za-z\s]+?)[\)\]]",
                    ft, re.IGNORECASE
                )
                if m:
                    en = m.group(1).strip().title()
                    # "Multi Perfume", "Signature Perfume" 등 접미어 제거
                    en = re.sub(r"\s*(Multi|Signature|Eau De)\s+Perfume$", "", en, flags=re.IGNORECASE).strip()
                    return en
            return ""

        english_name = _extract_english_from_ingredients(full_text, korean_name)

        # fallback: JSON-LD name이 순수 영문
        if not english_name:
            ld_name = json_ld.get("name", "")
            if ld_name and not re.search(r"[가-힣]", ld_name):
                english_name = re.sub(r"\s*[-–]\s*GRANHAND\.?.*$", "", ld_name).strip()

        # fallback: title 태그에서 영문 추출
        if not english_name:
            try:
                title_el = response.css("title")
                if title_el:
                    raw = _clean(title_el[0].text)
                    for part in [p.strip() for p in raw.split("|")]:
                        part = re.sub(r"\s*[-–]\s*GRANHAND\.?.*$", "", part).strip()
                        part = re.sub(r"\s*[가-힣].*$", "", part).strip()
                        if part and re.search(r"[a-zA-Z]", part) and not re.search(r"[가-힣]", part):
                            english_name = part
                            break
            except:
                pass

        # ── 나머지 필드
        regular_price   = _parse_price(response)
        if not regular_price:
            offers = json_ld.get("offers", {})
            price = offers.get("price", "")
            if price:
                regular_price = f"{int(float(price)):,}원"

        image_url       = _parse_image_url(response, json_ld)
        ingredients     = _parse_ingredients(full_text, english_name, korean_name)
        key_ingredients = _parse_key_ingredients(ingredients)
        product_type    = _parse_product_type(korean_name)

        item = {
            "country":         "KR",
            "korean_name":     korean_name,
            "english_name":    english_name,
            "product_type":    product_type,
            "product_url":     response.url,
            "regular_price":   regular_price,
            "image_url":       image_url,
            "ingredients":     ingredients,
            "key_ingredients": key_ingredients,
        }

        self._count += 1
        self._out.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(
            f"[{self._count:02d}] ✓ {english_name or korean_name}"
            f" | ko={korean_name or '✗'}"
            f" | {regular_price}"
            f" | ingr={'✓' if ingredients else '✗'}"
            f" | key={key_ingredients[:3]}"
        )
        yield item

    async def on_close(self):
        self._out.close()
        print(f"\n✅ 완료 — 총 {self._count}개 → granhand_products.jsonl")


if __name__ == "__main__":
    GranhandKRSpider().start()
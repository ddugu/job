"""
Türkşeker resmi duyurularını çeker, yenilerini bulur,
mühendislik/personel ilanı olup olmadığını sınıflandırır ve
mühendislik ilanı bulunduğunda ntfy ile push bildirimi gönderir.

Kaynak: https://www.turkseker.gov.tr/?MenuID=52&ModulID=9

Sayfa yapısı (sunucu tarafı HTML, JS gerekmez):
  Her duyuru bir <div class="row align-items-center"> satırıdır:
    - <div class="col-md-2">  → tarih (örn. 21.9.2026)
    - <div class="col-md-6">  → başlık
    - <div class="col-md-4">  → <a href="..."> indir linki (PDF/JPG)

Kullanım:
  python checker.py                      → normal kontrol
  python checker.py --test-notification  → yalnızca ntfy bildirim testi
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

URL = "https://www.turkseker.gov.tr/?MenuID=52&ModulID=9"
BASE_URL = "https://www.turkseker.gov.tr"
TIMEOUT_SECONDS = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TurksekerDuyuruChecker/1.0; "
        "+https://www.turkseker.gov.tr)"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

DATA_DIR = Path(__file__).resolve().parent / "data"
SEEN_FILE = DATA_DIR / "seen.json"

# --- ntfy ayarları ---
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC_ENV = "NTFY_TOPIC"
NTFY_TIMEOUT_SECONDS = 15
NTFY_PRIORITY_HIGH = "4"  # normalin üstü; 5 (urgent) bilinçli olarak kullanılmıyor

# --- Sınıflandırma sabitleri ---
STATUS_ENGINEER_MATCH = "ENGINEER_MATCH"
STATUS_NO_MATCH = "NO_MATCH"
STATUS_PDF_UNREADABLE = "PDF_TEXT_UNREADABLE"
STATUS_JPG_MANUAL = "JPG_MANUAL_CHECK"

# Uzun ifadeler önce denenir (kısmi eşleşmeyi önlemek için).
STRONG_PHRASES = [
    "elektrik-elektronik mühendisi",
    "elektrik elektronik mühendisi",
    "bilgisayar mühendisi",
    "yazılım mühendisi",
    "elektrik mühendisi",
    "elektronik mühendisi",
    "endüstri mühendisi",
    "makine mühendisi",
    "kimya mühendisi",
    "ziraat mühendisi",
    "gıda mühendisi",
    "çevre mühendisi",
    "inşaat mühendisi",
    "biyomühendis",
    "sözleşmeli mühendis",
    "mühendis kadrosu",
    "mühendis alımı",
    "mühendis alınacaktır",
    "mühendis personel",
]

# Tek başına zayıf; işe alım bağlamı ile güçlenebilir.
WEAK_KEYWORDS = [
    "mühendislik",
    "mühendis",
    "bilişim",
    "yazılım",
    "bilgisayar",
]

# Zayıf eşleşmeyi düşüren bağlam (yanlış pozitif azaltma).
WEAK_NEGATIVE_CONTEXT = [
    "mühendislik fakültesi",
    "mühendislik fakulte",
    "fakültesi mezun",
    "fakulte mezun",
]


@dataclass
class ClassificationResult:
    status: str
    matched_term: str | None = None
    match_strength: str | None = None  # "strong" | "weak" | None
    source: str | None = None  # "title" | "pdf" | "link_type" | None
    note: str | None = None


@dataclass
class ProcessedAnnouncement:
    """Yeni bir duyuru + sınıflandırma + bildirim sonucu."""

    item: dict[str, str | None]
    result: ClassificationResult
    notified: bool | None = None  # None: bildirim gerekmiyor


# ---------------------------------------------------------------------------
# Sayfa çekme / parse (önceki aşamalardan — değiştirilmedi)
# ---------------------------------------------------------------------------


def fetch_page(url: str) -> str:
    """Duyuru sayfasını indirir; hata durumunda anlaşılır mesaj verir."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        print(
            f"HATA: Bağlantı zaman aşımına uğradı ({TIMEOUT_SECONDS} sn). "
            f"Siteye ulaşılamadı: {url}",
            file=sys.stderr,
        )
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        print(
            f"HATA: Bağlantı başarısız. Siteye ulaşılamadı: {url}\n"
            f"Detay: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        print(f"HATA: İstek sırasında beklenmeyen hata: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        print(
            f"UYARI: Beklenen HTTP 200 yerine {response.status_code} alındı. "
            "Sayfa içeriği eksik veya hatalı olabilir.",
            file=sys.stderr,
        )

    if not response.encoding or response.encoding.lower() in ("iso-8859-1", "ascii"):
        response.encoding = response.apparent_encoding or "utf-8"

    return response.text


def parse_announcements(html: str) -> list[dict[str, str | None]]:
    """
    HTML içindeki duyuru satırlarını parse eder.

    Gerçek yapı: div.row.align-items-center içinde
    col-md-2 (tarih), col-md-6 (başlık), col-md-4 > a[href] (link).
    """
    soup = BeautifulSoup(html, "html.parser")
    announcements: list[dict[str, str | None]] = []

    for row in soup.select("div.row.align-items-center"):
        date_el = row.select_one("div.col-md-2")
        title_el = row.select_one("div.col-md-6")
        link_el = row.select_one("div.col-md-4 a[href]")

        if not date_el or not title_el:
            continue

        date = date_el.get_text(strip=True)
        title = title_el.get_text(strip=True)

        if not date or not title:
            continue
        if not _looks_like_date(date):
            continue

        href = link_el["href"].strip() if link_el and link_el.get("href") else None
        link = urljoin(BASE_URL, href) if href else None

        announcements.append({"date": date, "title": title, "link": link})

    return announcements


def _looks_like_date(text: str) -> bool:
    """Örn. 21.9.2026 veya 14.12.2022 gibi gün.ay.yıl biçimini kontrol eder."""
    parts = text.split(".")
    if len(parts) != 3:
        return False
    return all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# seen.json
# ---------------------------------------------------------------------------


def announcement_id(item: dict[str, str | None]) -> str:
    """Duyuru için mümkün olduğunca kararlı bir kimlik üretir."""
    link = item.get("link")
    if link:
        return link
    return f"{item.get('date', '')}|{item.get('title', '')}"


def load_seen_ids() -> set[str]:
    """seen.json yoksa boş küme döner; bozuksa anlaşılır hata verir."""
    if not SEEN_FILE.exists():
        return set()

    try:
        with SEEN_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"HATA: {SEEN_FILE} okunamadı veya bozuk JSON içeriyor.\nDetay: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(data, list):
        print(
            f"HATA: {SEEN_FILE} beklenen formatta değil (liste olmalı).",
            file=sys.stderr,
        )
        sys.exit(1)

    return {str(item) for item in data}


def save_seen_ids(ids: set[str]) -> None:
    """seen.json dosyasını atomik yazar (temp + replace)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = sorted(ids)

    fd, tmp_path = tempfile.mkstemp(
        prefix="seen_",
        suffix=".json.tmp",
        dir=str(DATA_DIR),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SEEN_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def find_new_announcements(
    announcements: list[dict[str, str | None]],
    seen_ids: set[str],
) -> list[dict[str, str | None]]:
    """Daha önce görülmemiş duyuruları döndürür (sayfa sırasını korur)."""
    new_items: list[dict[str, str | None]] = []
    for item in announcements:
        if announcement_id(item) not in seen_ids:
            new_items.append(item)
    return new_items


# ---------------------------------------------------------------------------
# Mühendislik sınıflandırması
# ---------------------------------------------------------------------------


def turkish_lower(text: str) -> str:
    """Türkçe büyük/küçük harf dönüşümü (İ/I doğru işlenir)."""
    return (
        text.replace("İ", "i")
        .replace("I", "ı")
        .replace("Ş", "ş")
        .replace("Ğ", "ğ")
        .replace("Ü", "ü")
        .replace("Ö", "ö")
        .replace("Ç", "ç")
        .lower()
    )


def normalize_for_match(text: str) -> str:
    """Eşleştirme için metni sadeleştirir."""
    t = turkish_lower(text)
    t = t.replace("â", "a").replace("î", "i").replace("û", "u")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    return phrase in text


def _has_weak_negative_context(text: str) -> bool:
    return any(neg in text for neg in WEAK_NEGATIVE_CONTEXT)


def analyze_text_for_engineering(
    text: str,
    *,
    allow_weak: bool = True,
) -> tuple[str | None, str | None]:
    """
    Metinde mühendislik eşleşmesi ara.

    Döner: (eşleşen_terim, güç) — güç "strong" veya "weak"; yoksa (None, None).

    allow_weak=False: yalnızca güçlü eşleşmeler (PDF gibi uzun metinlerde
    yanlış pozitifleri azaltmak için).
    """
    norm = normalize_for_match(text)
    if not norm:
        return None, None

    for phrase in STRONG_PHRASES:
        if _contains_phrase(norm, normalize_for_match(phrase)):
            return phrase, "strong"

    # "X mühendisi" genel kalıbı (listede olmayan branşlar için).
    m = re.search(r"(?<!\w)[\wçğıöşü\-]+\s+mühendisi(?!\w)", norm)
    if m:
        return m.group(0), "strong"

    # Yakın bağlam: mühendis + alım/kadro (fakülte şartı değil).
    if re.search(
        r"mühendis(?:lik)?\s+(?:kadrosu|alımı|alınacaktır|alınacak|personel)",
        norm,
    ):
        return "mühendis + alım/kadro", "strong"

    if re.search(
        r"(?:sözleşmeli|kadrolu)\s+mühendis",
        norm,
    ):
        return "sözleşmeli/kadrolu mühendis", "strong"

    if not allow_weak:
        return None, None

    for keyword in WEAK_KEYWORDS:
        kw = normalize_for_match(keyword)
        if kw not in norm:
            continue

        # "mühendislik fakültesi mezunları" tek başına kadro ilanı sayılmaz.
        if _has_weak_negative_context(norm) and keyword in (
            "mühendislik",
            "mühendis",
        ):
            continue

        # Başlıkta mühendis / bilişim / yazılım / bilgisayar geçmesi adaydır.
        return keyword, "weak"

    return None, None


def link_extension(link: str | None) -> str | None:
    """Link dosya uzantısını küçük harfle döndürür (örn. 'pdf', 'jpg')."""
    if not link:
        return None
    path = urlparse(link).path
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or None


def download_pdf_bytes(url: str) -> bytes | None:
    """PDF indirir; başarısız olursa None döner (program çökmez)."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            # Yine de içerik PDF olabilir; dene.
            pass
        return response.content
    except requests.exceptions.RequestException:
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """
    PDF baytlarından metin çıkarır.
    Metin yoksa / taranmış PDF ise None (OCR yok).
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            parts.append(page_text)
        text = "\n".join(parts).strip()
        # Çok kısa / anlamsız çıktı → okunamadı say.
        if len(text) < 20:
            return None
        return text
    except Exception:
        return None


def classify_announcement(item: dict[str, str | None]) -> ClassificationResult:
    """Yeni duyuruyu başlık → (gerekirse) PDF/JPG sırasıyla sınıflandırır."""
    title = item.get("title") or ""
    link = item.get("link")

    # 1) Başlık analizi
    term, strength = analyze_text_for_engineering(title)
    if term and strength == "strong":
        return ClassificationResult(
            status=STATUS_ENGINEER_MATCH,
            matched_term=term,
            match_strength="strong",
            source="title",
        )
    if term and strength == "weak":
        # Başlıkta zayıf ama kullanıcı kelime listesinde — ENGINEER_MATCH adayı.
        # Negatif bağlam zaten analyze içinde elendi.
        return ClassificationResult(
            status=STATUS_ENGINEER_MATCH,
            matched_term=term,
            match_strength="weak",
            source="title",
            note="Zayıf başlık eşleşmesi",
        )

    # 2) Link türüne göre
    ext = link_extension(link)
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return ClassificationResult(
            status=STATUS_JPG_MANUAL,
            source="link_type",
            note="JPG duyuru — manuel kontrol gerekli",
        )

    if ext == "pdf" and link:
        pdf_bytes = download_pdf_bytes(link)
        if pdf_bytes is None:
            return ClassificationResult(
                status=STATUS_PDF_UNREADABLE,
                source="pdf",
                note="PDF metni okunamadı",
            )

        pdf_text = extract_pdf_text(pdf_bytes)
        if pdf_text is None:
            return ClassificationResult(
                status=STATUS_PDF_UNREADABLE,
                source="pdf",
                note="PDF metni okunamadı",
            )

        # PDF uzun metin: yalnızca güçlü eşleşmeler (yanlış pozitif azaltma).
        # "mühendislik fakültesi mezunları başvurabilir" tek başına yetmez.
        term, strength = analyze_text_for_engineering(pdf_text, allow_weak=False)
        if term and strength == "strong":
            return ClassificationResult(
                status=STATUS_ENGINEER_MATCH,
                matched_term=term,
                match_strength="strong",
                source="pdf",
            )

        return ClassificationResult(status=STATUS_NO_MATCH, source="pdf")

    # Link yok veya diğer (ör. harici web sayfası) — yalnızca başlık bakıldı.
    return ClassificationResult(status=STATUS_NO_MATCH, source="title")


# ---------------------------------------------------------------------------
# ntfy bildirimi
# ---------------------------------------------------------------------------


def get_ntfy_topic() -> str | None:
    """NTFY_TOPIC environment variable'ını okur (hard-code edilmez)."""
    topic = (os.environ.get(NTFY_TOPIC_ENV) or "").strip()
    return topic or None


def _encode_header_value(value: str) -> str:
    """
    HTTP header'ları latin-1 ile taşınır; Türkçe karakter ve emoji için
    RFC 2047 (=?UTF-8?B?...?=) kodlaması kullanılır. ntfy bunu çözer.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="


def send_ntfy_notification(
    title: str,
    message: str,
    *,
    tags: str = "bell",
    priority: str = NTFY_PRIORITY_HIGH,
) -> bool:
    """
    ntfy üzerinden push bildirimi gönderir.

    Topic NTFY_TOPIC environment variable'ından okunur, endpoint
    https://ntfy.sh/{NTFY_TOPIC} olarak oluşturulur.

    Başarılıysa True, aksi halde False döner; asla exception fırlatmaz.
    """
    topic = get_ntfy_topic()
    if not topic:
        print(
            f"⚠️ ntfy bildirimi gönderilemedi: {NTFY_TOPIC_ENV} "
            "environment variable ayarlı değil.\n"
            f'   PowerShell: $env:{NTFY_TOPIC_ENV}="topic-adiniz"',
            file=sys.stderr,
        )
        return False

    url = f"{NTFY_SERVER}/{topic}"
    headers = {
        "Title": _encode_header_value(title),
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }

    try:
        response = requests.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=NTFY_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        print(
            "⚠️ ntfy bildirimi gönderilemedi: "
            f"zaman aşımı ({NTFY_TIMEOUT_SECONDS} sn).",
            file=sys.stderr,
        )
        return False
    except requests.exceptions.RequestException as exc:
        print(f"⚠️ ntfy bildirimi gönderilemedi: {exc}", file=sys.stderr)
        return False

    if response.status_code != 200:
        print(
            "⚠️ ntfy bildirimi gönderilemedi: "
            f"HTTP {response.status_code} — {response.text.strip()[:200]}",
            file=sys.stderr,
        )
        return False

    return True


def build_engineer_notification(
    item: dict[str, str | None],
    result: ClassificationResult,
) -> tuple[str, str]:
    """Mühendislik ilanı için bildirim başlığı ve mesajını hazırlar."""
    title = "🔔 Türkşeker'de Mühendis İlanı"

    lines = [
        "Yeni bir mühendislik ilanı bulundu.",
        "",
        f"Tarih: {item.get('date') or '(bilinmiyor)'}",
        f"İlan: {item.get('title') or '(başlık yok)'}",
        f"Eşleşme: {result.matched_term or '(belirtilmedi)'}",
    ]

    link = item.get("link")
    if link:
        lines += ["", "İlana git:", link]

    return title, "\n".join(lines)


def notify_engineer_match(
    item: dict[str, str | None],
    result: ClassificationResult,
) -> bool:
    """Mühendislik ilanı bildirimini gönderir."""
    title, message = build_engineer_notification(item, result)
    return send_ntfy_notification(title, message, tags="bell,hammer_and_wrench")


def run_test_notification() -> int:
    """--test-notification: siteye bakmadan, seen.json'a dokunmadan test eder."""
    topic = get_ntfy_topic()
    print("ntfy bildirim testi")
    print(f"Topic: {topic or '(ayarlı değil)'}")
    if topic:
        print(f"Endpoint: {NTFY_SERVER}/{topic}")
    print()

    ok = send_ntfy_notification(
        "🔔 Türkşeker Test",
        "Türkşeker mühendis ilanı takip sistemi için "
        "ntfy bildirimi başarıyla çalışıyor.",
        tags="bell,white_check_mark",
    )

    if ok:
        print("📱 Test bildirimi gönderildi. Telefonunuzu kontrol edin.")
        return 0

    print("Test bildirimi gönderilemedi.", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Terminal çıktısı
# ---------------------------------------------------------------------------


def print_classified_item(processed: ProcessedAnnouncement) -> None:
    item = processed.item
    result = processed.result

    if result.status == STATUS_ENGINEER_MATCH:
        print("🔔 MÜHENDİSLİK İLANI BULUNDU")
    elif result.status == STATUS_JPG_MANUAL:
        print("🖼️  JPG DUYURU — MANUEL KONTROL GEREKLİ")
    elif result.status == STATUS_PDF_UNREADABLE:
        print("⚠️  PDF METNİ OKUNAMADI")
    else:
        print("📄 YENİ DUYURU (eşleşme yok)")

    print()
    print(f"Tarih: {item['date']}")
    print(f"Başlık: {item['title']}")
    print(f"Durum: {result.status}")
    if result.matched_term:
        strength = f" ({result.match_strength})" if result.match_strength else ""
        print(f"Eşleşen kelime: {result.matched_term}{strength}")
    if result.source:
        print(f"Kaynak: {result.source}")
    if result.note:
        print(f"Not: {result.note}")
    print(f"Link: {item['link'] or '(yok)'}")

    if processed.notified is True:
        print()
        print("📱 ntfy bildirimi gönderildi.")
    elif processed.notified is False:
        print()
        print(
            "⚠️ ntfy bildirimi gönderilemedi — duyuru kaydedilmedi, "
            "sonraki çalıştırmada tekrar denenecek."
        )
    print()


def print_report(
    total: int,
    processed_items: list[ProcessedAnnouncement],
    *,
    first_run: bool,
) -> None:
    print("========================================")
    print("TÜRKŞEKER DUYURU KONTROLÜ")
    print("========================================")
    print()
    print(f"Toplam duyuru: {total}")
    print(f"Yeni duyuru: {len(processed_items)}")
    print()

    if first_run:
        print(
            "İlk çalıştırma: mevcut duyurular kaydedildi. "
            "Bundan sonra yalnızca yeni eklenenler incelenecek."
        )
        print()
        print("Yeni duyuru bulunamadı.")
        return

    if not processed_items:
        print("Yeni duyuru bulunamadı.")
        return

    for processed in processed_items:
        print_classified_item(processed)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def process_new_announcements(
    new_items: list[dict[str, str | None]],
) -> list[ProcessedAnnouncement]:
    """Yeni duyuruları sınıflandırır; mühendislik ilanları için bildirim gönderir."""
    processed: list[ProcessedAnnouncement] = []

    for item in new_items:
        result = classify_announcement(item)
        notified: bool | None = None
        if result.status == STATUS_ENGINEER_MATCH:
            notified = notify_engineer_match(item, result)
        processed.append(ProcessedAnnouncement(item=item, result=result, notified=notified))

    return processed


def run_check() -> int:
    html = fetch_page(URL)
    announcements = parse_announcements(html)

    if not announcements:
        print("Sayfada duyuru bulunamadı.", file=sys.stderr)
        return 2

    seen_ids = load_seen_ids()
    first_run = len(seen_ids) == 0

    if first_run:
        # Baseline: hepsini görüldü say, bildirim gönderme.
        processed: list[ProcessedAnnouncement] = []
        save_seen_ids({announcement_id(item) for item in announcements})
    else:
        new_items = find_new_announcements(announcements, seen_ids)
        processed = process_new_announcements(new_items)

        # Bildirim gönderilemeyen mühendislik ilanları kaydedilmez;
        # böylece sonraki çalıştırmada tekrar denenir.
        ids_to_save = {
            announcement_id(p.item) for p in processed if p.notified is not False
        }
        if ids_to_save:
            seen_ids.update(ids_to_save)
            save_seen_ids(seen_ids)

    print_report(len(announcements), processed, first_run=first_run)

    if any(p.notified is False for p in processed):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if "--test-notification" in args:
        return run_test_notification()

    return run_check()


if __name__ == "__main__":
    sys.exit(main())

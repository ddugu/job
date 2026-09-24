# Türkşeker Duyuru Takip Sistemi

Türkşeker'in [resmi duyurular sayfasını](https://www.turkseker.gov.tr/?MenuID=52&ModulID=9) otomatik olarak takip eder.
Yeni mühendislik/personel ilanları bulunduğunda iPhone'a ntfy push bildirimi gönderir.

## Nasıl çalışır

```
GitHub Actions (her 15 dk)
  → checker.py
  → Türkşeker duyurularını çek
  → Yeni duyuru var mı? (data/seen.json ile karşılaştır)
  → ENGINEER_MATCH ise → ntfy → iPhone bildirimi
  → data/seen.json güncelle → Git commit & push
```

## Kurulum

### 1. GitHub Repository oluştur

GitHub'da yeni bir repository oluştur ve bu projeyi push et.

```bash
git remote add origin https://github.com/KULLANICI_ADIN/turkseker-job-alert.git
git push -u origin master
```

### 2. NTFY_TOPIC Secret ekle

GitHub Repository'de:

```
Settings → Secrets and variables → Actions → New repository secret
```

| Alan  | Değer                          |
|-------|-------------------------------|
| Name  | `NTFY_TOPIC`                  |
| Value | ntfy topic adın (ör. `turkseker-duygu-07120211`) |

> ⚠️ Topic değerini bu README'ye veya herhangi bir kaynak dosyasına **yazma**.

### 3. GitHub Actions'ı etkinleştir

Repository'de `Actions` sekmesine git, workflow'u etkinleştir.

İlk çalışma şöyle görünmeli:

```
Toplam duyuru: 88
Yeni duyuru: 0
Yeni duyuru bulunamadı.
```

### 4. Manuel çalıştırma

GitHub → Actions → "Türkşeker Duyuru Kontrolü" → **Run workflow**

---

## Yerel kullanım

```powershell
# Bağımlılıkları kur
pip install -r requirements.txt

# Ntfy topic'i ayarla
$env:NTFY_TOPIC="turkseker-duygu-07120211"

# Bildirim testi
python checker.py --test-notification

# Normal kontrol
python checker.py
```

---

## Bildirim davranışı

| Durum                | Bildirim    | seen.json'a kaydedilir?                     |
|----------------------|-------------|---------------------------------------------|
| `ENGINEER_MATCH`     | ✅ ntfy     | Bildirim başarılıysa evet, başarısızsa hayır |
| `NO_MATCH`           | ❌          | ✅ Evet                                      |
| `JPG_MANUAL_CHECK`   | ❌          | ✅ Evet                                      |
| `PDF_TEXT_UNREADABLE`| ❌          | ✅ Evet                                      |

`ENGINEER_MATCH` için bildirim başarısız olursa ilan kaydedilmez — bir sonraki çalışmada tekrar denenecek.

---

## Dosya yapısı

```
turkseker-job-alert/
├── checker.py              # Ana uygulama
├── requirements.txt        # Python bağımlılıkları
├── data/
│   └── seen.json           # Daha önce görülen duyurular (git ile takip edilir)
└── .github/
    └── workflows/
        └── turkseker-check.yml
```

## Bağımlılıklar

- `requests`
- `beautifulsoup4`
- `pypdf`

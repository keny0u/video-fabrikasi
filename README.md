# Video Fabrikası

**EN:** A local desktop app that publishes videos and images to YouTube, YouTube Shorts, TikTok and Instagram Reels from a single web panel, instantly, from a queue or on a schedule. It also turns long videos into vertical clips with word-by-word subtitles and produces AI-written, puppet-animated story series.

- **Multi-platform publishing:** YouTube Data API, Instagram Graph API and TikTok uploaders behind one queue, with per-platform daily limits and retries
- **Drop-folder automation:** files dropped into `inbox/<platform>` are picked up, ordered and published automatically
- **Clip generation:** downloads a video, transcribes it with faster-whisper on the GPU, picks highlight moments, reframes to 9:16 with face tracking (OpenCV) and burns in animated subtitles (FFmpeg)
- **AI story series:** Claude writes the series bible and episodes, characters are drawn once and reused, scenes are animated as 2D puppets with lip sync and voiced with edge-tts or Chatterbox
- **Stack:** Python, Flask + Waitress, SQLite, FFmpeg, OpenCV, faster-whisper, PyTorch/diffusers, Anthropic API, pytest (83 tests, dry-run mode with `FABRIKA_DENEME=1`)

The detailed user guide below is in Turkish.

---

Videolarını ve görsellerini YouTube, YouTube Shorts, TikTok ve Instagram Reels'e tek panelden yükler. İstersen hepsine aynı anda, istersen senin belirlediğin aralıklarla (saniye, dakika, saat, gün).

## Başlatma

`baslat.bat` dosyasına çift tıkla. Panel tarayıcıda açılır: <http://127.0.0.1:8765>

Windows açılınca kendiliğinden başlasın istersen: **Hesaplar → Otomatik başlatma**.

Yükleme yapılabilmesi için bilgisayar açık, uyanık ve internete bağlı olmalı. Windows'ta uyku modunu kapat.

## Kullanım

### Panelden yükleme

1. **Yükle** sekmesinde platform ikonlarını seç: tek biri, üçü ya da hepsi.
2. Dosyaları sürükle. Görseller otomatik olarak 8 saniyelik videoya çevrilir.
3. Başlık, açıklama ve etiket yaz.
4. Zamanı seç:
   - **Şimdi:** seçtiğin her platforma aynı anda başlar.
   - **Sıraya ekle:** Zamanlama sekmesindeki aralıkla yayınlanır.
   - **Tarih ve saat:** o anda hepsine birden yüklenir.

### Klasöre atarak tam otomatik

Dosyayı `inbox` altındaki klasöre at, fabrika kendisi alıp sıraya ekler:

| Klasör | Nereye gider |
| --- | --- |
| `inbox/youtube` | YouTube |
| `inbox/shorts` | YouTube Shorts |
| `inbox/tiktok` | TikTok |
| `inbox/reels` | Instagram Reels |
| `inbox/dikey` | Shorts, TikTok ve Reels |

Dosyalar ad sırasıyla yayınlanır, başına `001_`, `002_` yazarak sırayı belirleyebilirsin.

Başlık ve açıklama için videoyla aynı adlı bir dosya koy:

- `video1.txt`: ilk satır başlık, kalanı açıklama.
- `video1.json`: `{"title": "...", "description": "...", "tags": ["a", "b"]}`

Bu dosya yoksa başlık olarak dosya adı kullanılır.

### Zamanlama

**Zamanlama** sekmesinde her platform için ayrı ayrı:

- otomatik yayını aç,
- aralığı gir (örnek: her 6 saat),
- istersen saat aralığı koy (örnek: 10:00-22:00),
- günlük üst sınırı belirle.

Otomatik yayın kapalıyken sıradaki işler bekler. Şimdi ve tarihli yüklemeler bu ayardan etkilenmez.

## Uzun videodan klip üretme

**Klip Üret** sekmesinde YouTube linki yapıştır ya da bilgisayardan video seç. Fabrika şunları yapar:

1. Videoyu indirir (en fazla 1080p).
2. Konuşmayı Whisper ile kelime kelime yazıya döker. Ekran kartında çalışır.
3. En ilgi çekici anları seçer.
4. Her anı 9:16 dikey klibe çevirir:
   - konuşan yüz varsa kamera yüzü takip eder,
   - yüz yoksa arkası bulanık dolgu olur,
   - video zaten dikeyse olduğu gibi kalır.
5. Kelime kelime yanan altyazıyı gömer ve sesi sosyal medya seviyesine ayarlar.

### Ne tür anlar?

Klip üretirken **Ne tür anlar?** seçimi yapılır:

| Seçim | Nasıl bulur |
| --- | --- |
| **Genel** | Konuşmaya bakar: hızlı ve yüksek sesli konuşma, merak uyandıran kelimeler. |
| **Korku** | Videoyu baştan sona izler. Sesin aniden patlaması (çığlık, jumpscare sesi), kameradaki kişinin birden irkilmesi, oyun ekranının birden değişmesi ve "siktir, korktum, anne, bismillah" gibi tepkiler aranır. Birkaç işaret aynı anda patlarsa puan artar. Klip anın 10 saniye öncesinden başlar ki gerilim kurulsun. |
| **Komik** | Gülme kelimelerine ("haha, öldüm, güldüm"), kameradaki hareketlenmeye ve ses patlamalarına bakar. Kahkahayı sesten tanıyamadığı için korku modu kadar isabetli değildir. |
| **Elle** | `12:30-13:10, 25:05-25:40` gibi zamanları yazarsın, tam o anlar klip olur. |
| **Kelime** | Yazdığın kelimenin konuşmada geçtiği yerlerden klip çıkarır. |

Korku ve komik modunda video ekran kartıyla taranır. 40 dakikalık video yaklaşık 5 dakika sürer ve bu sırada yazıya dökme de devam eder. Tarama kaydedilir; aynı videoyu yeniden üretirken tekrarlanmaz. Videonun ilk 30 saniyesi (giriş) korku ve komik anı sayılmaz.

### Oyun düzeni (kamera üstte, oyun altta)

**Kamera boyutu** küçük, orta ya da büyük seçilebilir; kamera kırpılmadan küçülür. Oyun görüntüsündeki kamera köşesi siyahla kapatılır, ekranda iki kamera görünmez.

Oyun videolarında kamera genelde köşede küçük bir kutuda durur. Bu kutu yüz takibi için fazla küçüktür.

İki oyun düzeni var:

- **Oyun: ekranı doldur.** Oyunun orta bölümü kırpılıp alt kısmı doldurur.
- **Oyun: tamamı görünsün.** Oyun kırpılmaz, boşluklar bulanık dolgu olur. Yazılı oyunlar ve hikâye anlatımı için uygundur.

Nasıl kullanılır:

1. Klip üretirken **Düzen**'den oyun düzenlerinden birini seç.
2. Video indikten sonra iş **Kamera kutusu bekleniyor** durumunda durur. O videonun karesi, son kullandığın kutu hazır çizili şekilde açılır.
3. Gerekirse fareyle kameranın üstüne yeniden çiz ve **Onayla ve devam et**'e bas. Kare siyahsa **Başka kare** ile videonun başka bir yerinden kare al.

Her videonun kendi kutusu olur. Son çizdiğin kutu bir sonraki videoya hazır gelir.

Kamera her videoda aynı yerdeyse **"Kamera kutusunu her videoda bana sor"** seçeneğini kapat. Böylece son kutu sormadan kullanılır ve süreç tam otomatik ilerler.

Biten bir işin düzenini kartındaki seçimden değiştirip **Yeniden üret**'e basabilirsin. Kartta bulunan **Kamera kutusu** düğmesi o videonun kutusunu düzeltmeye yarar.

Klipleri panelde izleyip başlığı düzeltebilir, istediğin platformlara **Şimdi yükle** ya da **Sıraya ekle** ile gönderebilirsin. "Hazır klipleri onaysız sıraya ekle" altında platform seçersen klipler sen bakmadan aralıklı kuyruğa girer.

Anlar ücretsiz yöntemle seçilir. Puanlamada şunlara bakılır:

- konuşma hızı ve ses yüksekliği,
- "neden, sır, asla, inanılmaz" gibi merak uyandıran kelimeler,
- soru ile başlama,
- sessizlik ve "tamam, şey" gibi dolgu sözlerle başlama (bunlar puan düşürür).

Konuşmayı anlamaz, bu yüzden bazı seçimler isabetsiz olabilir. İleride bir yapay zekâ API'si bağlanırsa seçimler çok daha iyi olur.

İlk klip üretiminde Whisper modeli bir kez indirilir (yaklaşık 1,6 GB). Bir klip, kadraj ve kodlama dahil birkaç saniyede hazırlanır. 1 saatlik videonun yazıya dökülmesi ekran kartında birkaç dakika sürer.

Kendi videolarını ya da kullanma iznin olan videoları kullan. Başkasının videosunu paylaşmak telif ihlalidir; YouTube'dan video indirmek de YouTube'un kurallarına aykırıdır.

İndirilen asıl videolar `klipler` klasöründe kalır. Yer açmak için işi panelden sil; asıl video ve klipler birlikte silinir.

## Seçimli dizi (izleyici yönetir)

**Dizi** sekmesinde bir konu yazarsın, bölüm sayısını (3-20) ve oylama süresini (1-72 saat) seçersin.

1. Claude dizi kitapçığını yazar: karakterler (basit, tanınır tasarımlar), kurallar, bölüm ana hattı ve final.
2. Her bölüm hikaye hattından geçer. Başta "BÖLÜM N" ve kanca kartı, ilk sahnede "Siz B dediniz…" özeti, sonda
   "A) … B) …" seçenek kartı ve sesli "Yorumlara yaz" çağrısı olur.
3. Seçtiğin platformlara bölüm hazır olur olmaz yayınlanır; oylama süresi yayından itibaren işler.
4. YouTube ve Instagram yorumlarındaki oylar 10 dakikada bir sayılır: tek başına yazılmış "A" ya da "B"
   (ör. "bence B"), kişi başı bir oy, son yazdığı geçerli. İki şıkkı birden yazan yorum sayılmaz.
5. Süre dolunca kazanan seçilir (eşitlikte A) ve sonraki bölüm o seçimle yazılıp üretilir. Son bölüm finaldir.

TikTok yorumları ve Instagram hikâye anketi programla okunamadığı için o oyları bölüm satırındaki **Elle oy ekle**
ile girersin. **A kazansın / B kazansın** ile oylamayı erken bitirebilirsin.

Gerekenler: Claude anahtarı, YouTube yorumlarını okumak için YouTube'un yeniden bağlanması (yorum izni sonradan
eklendi), Instagram yorumları için Instagram bağlantısı. Yapay zekâ ile üretilen içerik olduğunu platformlarda
belirtmeyi unutma.

### Kukla canlandırma (hikaye ve dizi videoları)

Hikaye ve dizi sahneleri durağan görsel değil, 2D kukla canlandırmasıdır:

- Her karakter bir kez çizilir, arka planı silinir (rembg, isnet-anime) ve yüzü ile dudağı bulunur. Dizide
  çizimler `klipler/dizi_<no>/oyuncular` klasöründe saklanır; bütün bölümler aynı çizimleri kullanır. Bir
  karakteri beğenmezsen o klasördeki `.png` ve `.json` dosyalarını sil; sonraki bölümde yeniden çizilir.
- Her sahnenin mekânı insansız çizilir, karakter önüne konur ve ışığına göre karartılıp boyanır.
- Karakter nefes alır ve hafifçe salınır; kamera yavaşça yaklaşır ve yana kayar. Karakter kendi sözlerini
  söylerken dudakları sesle aralanır ve cinsiyetine uygun sesle (Emel/Ahmet) konuşur.
- Claude sahne başına çekim (geniş/orta/yakın), konum, efekt (yağmur, sis, ışık titremesi, sarsıntı, karanlık)
  ve sol üstteki etiketi ("ELİF'İN GÖZÜ", "OTUZ DOKUZ GECE KALDI") seçer.

Bir dakikalık bölüm 8 GB ekran kartında yaklaşık 5-6 dakikada üretilir. Göz kırpma yok: çizimle yapılan
denemeler yama gibi görünüyordu.

## Hesapları bağlama

Bu adımları yalnızca bir kez yaparsın.

### YouTube ve Shorts

1. [Google Cloud Console](https://console.cloud.google.com/)'da yeni proje aç.
2. **YouTube Data API v3**'ü etkinleştir.
3. OAuth izin ekranını **External** olarak oluştur ve uygulamayı **In production** durumuna al. Testing durumunda bağlantı 7 günde bir düşer.
4. **Credentials → OAuth client ID → Desktop app** oluştur, JSON dosyasını indir.
5. Panelde **Hesaplar**'dan dosyayı yükle, **Hesabı bağla**'ya bas. Google "doğrulanmamış uygulama" uyarısı gösterirse kendi uygulaman olduğu için devam edebilirsin.

Dikey ve 3 dakikadan kısa videolar YouTube tarafından Shorts sayılır, ayrıca bir API ayarı yoktur.

### Instagram Reels

1. Instagram hesabını **Profesyonel** yap (İşletme ya da İçerik Üretici) ve bir Facebook Sayfasına bağla.
2. [Meta for Developers](https://developers.facebook.com/)'ta **Business** türünde uygulama aç, **Facebook Login for Business** ekle.
3. [Graph API Explorer](https://developers.facebook.com/tools/explorer/)'da şu izinlerle anahtar al: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`.
4. Hesap kimliğini bul: `me/accounts?fields=instagram_business_account`
5. Kimliği ve anahtarı panele gir.

Graph API Explorer'ın anahtarı kısa ömürlüdür. Uzun ömürlüye çevir (yaklaşık 60 gün) ya da Business Manager'dan System User anahtarı üret. Anahtarın süresi dolarsa Reels durur, panel uyarı gösterir; yenisini girince kaldığı yerden devam eder.

### TikTok

1. [TikTok for Developers](https://developers.tiktok.com/)'ta uygulama aç.
2. **Login Kit** ve **Content Posting API** ürünlerini ekle.
3. Desktop platformunu seç, yönlendirme adresine `http://localhost:3455/callback/` yaz.
4. `video.upload` ve `video.publish` izinlerini ekle.
5. Client key ve secret'ı panele gir, **Hesabı bağla**'ya bas.

## Platform kuralları

Bu sınırlar platformlardan gelir, kodla aşılamaz. Bilgiler 7 Kasım 2026'da resmi dokümanlardan alındı.

| Platform | Sınır |
| --- | --- |
| YouTube | Günde 100 yükleme (Shorts dahil). Kota Pasifik saatiyle gece yarısı sıfırlanır. |
| Instagram | 24 saatte 100 gönderi (Meta'nın dokümanında bir yerde 50 yazıyor). Reels 3 sn-15 dk ve en fazla 300 MB. |
| TikTok | Video en fazla 10 dakika. Taslak modunda 24 saatte en fazla 5 bekleyen taslak. |

### TikTok neden tam otomatik değil

TikTok, onaysız uygulamaların içeriğini **yalnızca gizli** yayınlatır. Onay sürecinde de "kendi hesabına yükleme aracı" türündeki uygulamaları kabul etmediğini açıkça yazıyor. Bu yüzden iki mod var:

- **Taslak (varsayılan):** video TikTok gelen kutuna düşer, telefonda tek dokunuşla yayınlarsın.
- **Direkt:** hesabın gizli olmalı, gönderiler "Yalnızca ben" olarak çıkar.

Tamamen otomatik herkese açık yayın için TikTok onaylı bir aracı servis (Ayrshare gibi) gerekir.

## Sorun çıkarsa

- **Kuyruk sekmesi** her işin durumunu ve hata nedenini gösterir.
- Geçici hatalar en fazla 5 kez, artan aralıklarla denenir.
- Platform limiti dolarsa o platform kendiliğinden durur ve süre bitince devam eder.
- Bağlantı düşerse platform durur. Yeniden bağla ya da **Zamanlama**'dan **Devam ettir**'e bas.
- Program yükleme sırasında kapanırsa o iş otomatik tekrarlanmaz, çünkü video iki kez yayınlanabilir. Platformu kontrol et, yayınlanmadıysa **Tekrar dene**'ye bas.

Biten dosyalar `arsiv` klasörüne taşınır. Anahtarlar `secrets` klasöründe durur, bu klasörü kimseyle paylaşma.

## Geliştirme

Gerçek yükleme yapmadan denemek için `FABRIKA_DENEME=1` ortam değişkenini ayarla. Testleri çalıştırmak için:

```bash
.venv/Scripts/python -m pytest -q
```

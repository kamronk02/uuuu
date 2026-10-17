# Telegram kino bot

## Fayllar
- `main.py` — bot kodi va Render Web Service uchun health endpoint.
- `requirements.txt` — Python kutubxonalari.
- `render.yaml` — Render Web Service sozlamasi.
- `.env.example` — kerakli environment variable namunalari.

## Render Web Service'da ishga tushirish
1. GitHub repository oching va ushbu fayllarni alohida yuklang.
2. Render'da PostgreSQL database yarating.
3. Render'da **Web Service** yarating va GitHub repository'ni ulang.
4. Build command: `pip install -r requirements.txt`
5. Start command: `python main.py`
6. Environment variables kiriting:
   - `BOT_TOKEN`
   - `ADMIN_IDS` — Telegram ID'lar, vergul bilan ajratiladi
   - `DATABASE_URL` — Render PostgreSQL'ning Internal Database URL manzili
   - `TMDB_API_KEY` — ixtiyoriy, kino metama'lumotlarini qidirish uchun

## Kino qo‘shish
Admin botga video yuboradi → kino nomini yuboradi → tilini tanlaydi → bepul/pullik turini tanlaydi → pullik bo‘lsa narxini yuboradi. Kino kodi avtomatik yaratiladi. TMDb natijasi topilmasa ham kino saqlanadi.

## Muhim eslatmalar
- `protect_content` Telegram interfeysida forwarding/saqlashni cheklaydi, ammo ekran yozuvini yoki boshqa usulda nusxa olishni to‘liq to‘xtatmaydi.
- Kanalni majburiy obunaga qo‘shishdan oldin botni o‘sha kanalga admin qiling. Kanal username'i `@kanal_nomi` ko‘rinishida beriladi.
- CSV zaxira nusxasi asosiy kino va foydalanuvchi ma’lumotlarini eksport qiladi; bu to‘liq PostgreSQL backup emas.
- Bepul hosting rejalarida uzluksiz ishlash kafolatlanmaydi; Render plan va cheklovlari o‘zgarishi mumkin.
- TMDb API key bo‘lmasa, avtomatik metadata qidiruvi ishlamaydi, lekin kino qo‘shish bloklanmaydi.
- Bot faqat tarqatishga ruxsatingiz bo‘lgan kontent uchun ishlatilishi kerak.

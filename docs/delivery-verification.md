# گزارش نصب تازه و تحویل

تاریخ اجرا: ۲۰۲۶-۰۹-۱۵. مراحل README روی یک PostgreSQL تازه و ایزوله اجرا شدند؛
Docker شبکه، container و volume جدید را با project name برابر
`ledger-wallet-verification` ساخت و دیتابیس برنامه پیش از migration هیچ داده‌ای
نداشت. برای تداخل‌نداشتن با محیط‌های موجود فقط پورت میزبان به ۵۵۴۳۲ تغییر کرد.

## محیط اجرا

```text
Python=3.14.4 Django=5.2.17 DRF=3.18.1 drf-spectacular=0.30.0 psycopg=3.3.5
Docker Compose=2.40.3 PostgreSQL=18.6 (Debian 18.6-1.pgdg13+2)
isolation=read committed
```

وابستگی‌های `requirements.txt` در virtual environment تازه نصب شدند و
`pip check` هیچ dependency شکسته‌ای گزارش نکرد. Python موردنیاز README نسخهٔ
۳.۱۲ است؛ این اجرای تحویل سازگاری همان وابستگی‌های pinشده با ۳.۱۴.۴ را نیز تأیید کرد.

## راه‌اندازی و تست کامل

فرمان‌های راه‌اندازی README با تنظیمات دیتابیس ایزوله اجرا شدند:

```bash
docker compose up -d --wait --wait-timeout 60 db
venv/bin/python manage.py check --database default
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py test --verbosity 2 --noinput
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py migrate --check
venv/bin/python -m pip check
git diff --check
```

نتیجه:

```text
System check identified no issues (0 silenced).
All migrations applied successfully, including wallet.0001_initial.
Found 131 test(s).
Ran 131 tests in 2.833s
OK
Destroying test database for alias 'default' ('test_ledger_wallet_fresh')... OK
No changes detected
No broken requirements found.
```

پیام `Internal Server Error` میان خروجی verbose متعلق به تست عمدی پنهان‌کردن خطای
غیرمنتظره است؛ خود تست و کل suite موفق شدند. دیتابیس تست از دیتابیس برنامه جدا ساخته
و پس از پایان حذف شد.

## دادهٔ نمونه و smoke test شبکه

دو فرمان README روی دیتابیس برنامه اجرا و شناسه‌های ۱ و ۲ را چاپ کردند:

```bash
venv/bin/python manage.py create_demo_user --username alice
venv/bin/python manage.py create_demo_user --username bob
```

سرور با `runserver 127.0.0.1:8000 --noreload` اجرا شد و نمونه‌های curl مستندشده
نتایج زیر را داشتند:

| بررسی | نتیجه |
| --- | --- |
| credit مبلغ ۱۰۰ | HTTP 201، موجودی بعد ۱۰۰ |
| debit مبلغ ۲۰ | HTTP 201، موجودی بعد ۸۰ |
| تکرار credit با همان کلید | HTTP 200، همان entry با ID برابر ۱ |
| دریافت موجودی و تاریخچهٔ alice | HTTP 200، موجودی ۸۰ و دقیقاً دو entry مرتب |
| دریافت موجودی و تاریخچهٔ bob | HTTP 200، موجودی صفر و تاریخچهٔ خالی |
| amount عددی به‌جای رشته | HTTP 400 با `invalid_amount` |
| برداشت ۹۰ از موجودی ۸۰ | HTTP 409 با `insufficient_funds` |
| موجودی پس از دو درخواست مردود | بدون تغییر و برابر ۸۰ |

تطبیق نهایی نیز با کد خروج صفر تمام شد:

```json
{"balance_matches":true,"chain_valid":true,"entry_count":2,"first_invalid_entry_id":null,"is_consistent":true,"ledger_balance":"80.00000000","stored_balance":"80.00000000","user_id":1,"wallet_id":1}
```

سرور توسعه پس از smoke test متوقف شد. این بررسی شامل نصب production، آزمون بار یا
قطع واقعی شبکه نیست؛ محدودیت‌های قراردادی و عملیاتی کامل در README آمده‌اند.

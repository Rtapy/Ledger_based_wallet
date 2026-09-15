# شواهد اجرای تراکنش، هم‌زمانی و rollback

تاریخ اجرا: ۲۰۲۶-۰۹-۱۵. در این مرحله ۷ تست اضافه و بررسی استقلال اتصال‌ها در
تست‌های موجود تقویت شد. منطق runtime سرویس تغییر نکرد.

## محیط مشاهده‌شده

```text
Python=3.12.3 Django=5.2.17 DRF=3.18.1 psycopg=3.3.5
Backend=postgresql PostgreSQL=18.4 (Debian 18.4-1.pgdg13+1)
isolation=read committed autocommit=True
```

تست‌ها با `TransactionTestCase` روی دیتابیس جداگانهٔ `test_ledger_wallet` اجرا شدند؛
پس از اجرا دیتابیس تست حذف شد. threadها اتصال Django خودشان را ایجاد و در finally
می‌بندند. در تست‌های موازی، `pg_backend_pid()` اتصال‌ها با یکدیگر و اتصال اصلی مقایسه
می‌شود. تست‌های انتظار، مسدودشدن واقعی در `pg_blocking_pids` را بررسی می‌کنند.
Barrier شروع هم‌زمان را هماهنگ می‌کند؛ صرفِ sleep شاهد گرفتن قفل محسوب نشده است.

## نتیجهٔ اجرای هدفمند

```bash
venv/bin/python -B manage.py test wallet.tests.test_service_transactions wallet.tests.test_reconciliation.ReconciliationConcurrencyTests --verbosity 2 --noinput
```

خلاصهٔ خروجی واقعی:

```text
Ran 25 tests in 1.905s
OK
```

| سناریو | نتیجه‌ای که تست تأیید کرد |
| --- | --- |
| دو برداشت ۸۰ از موجودی ۱۰۰، با کلیدهای متفاوت | یک موفقیت، یک insufficient_funds، موجودی ۲۰ و فقط یک debit |
| credit/debit هم‌زمان با کلید و payload یکسان | یک ایجاد و یک replay با ID یکسان؛ اثر مالی یک‌بار |
| کلید یکسان با مبلغ یا نوع متفاوت | یک موفقیت و یک idempotency_conflict |
| retry در حال انتظار، پس از commit درخواست اول | رکورد قبلی replay شد؛ برداشت دوباره انجام نشد |
| retry در حال انتظار، پس از rollback درخواست اول | عملیات با همان کلید یک‌بار ثبت شد؛ رکورد و موجودی موقتی باقی نماند |
| شکست پیش از UPDATE کیف پول | موجودی، تاریخچه و timestampها تغییر نکردند؛ Ledger.save فراخوانی نشد |
| شکست پس از UPDATE کیف پول و پیش از INSERT یا پس از INSERT دفتر | هر دو تغییر rollback شدند؛ retry با همان کلید موفق شد |
| خطای واقعی constraint در Wallet یا Ledger | IntegrityError از constraint موردنظر؛ بازگشت کامل وضعیت و امکان retry |
| خطای دیتابیس در سرویس داخل تراکنش بیرونی | savepoint برگشت؛ تراکنش بیرونی قابل استفاده ماند و عملیات بعدی commit شد |
| خواندن با اتصال مستقل، قبل از commit و پس از rollback | تنها وضعیت قبلیِ commit‌شده دیده شد |
| تطبیق هنگام تغییر نیمه‌تمام موجودی | انتظار برای commit/rollback و گزارش سازگار؛ کیف پول دیگر قابل تغییر ماند |

تست‌های تزریق شکست Wallet و Ledger، مسیرهای credit و debit را بررسی می‌کنند.
در آزمون retry منتظر، درخواست اول پس از INSERT و پیش از پایان تراکنش نگه داشته
می‌شود؛ پس از مشاهدهٔ انتظار اتصال دوم، درخواست اول commit یا rollback می‌شود.
این سناریو زمان‌بندی مشخصی دارد و به شانسِ تداخل دو thread وابسته نیست.

تست‌ها در [test_service_transactions.py](../wallet/tests/test_service_transactions.py)
و [test_reconciliation.py](../wallet/tests/test_reconciliation.py) قرار دارند.

## نتیجهٔ کل مجموعه

```bash
venv/bin/python manage.py test --verbosity 2 --noinput
```

```text
Found 131 test(s).
Ran 131 tests in 2.833s
OK
```

پیام `Internal Server Error` مربوط به تست عمدی پاسخ ۵۰۰ در API است؛ نتیجهٔ مجموعه
موفق بود. زمان‌ها مربوط به همین اجرا هستند و معیار کارایی یا آزمون بار نیستند.
قطع واقعی شبکه، توقف پردازش و خرابی دیسک در این اجرا آزمایش نشده‌اند؛ شکست‌ها در
مرز ذخیرهٔ ORM تزریق شده‌اند و خطاهای constraint روی PostgreSQL واقعی رخ داده‌اند.
اجرای کامل روی دیتابیس تازه در [گزارش تحویل](delivery-verification.md) ثبت شده است.

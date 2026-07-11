---
title: حالت Domain
description: افزودن fallback CDN از طریق Cloudflare برای تاب‌آوری محدود شدن IP.
order: 4
section: guides
---

## آنچه حالت Domain اضافه می‌کند

حالت دامنه راه‌اندازی standalone را با سه جزء توسعه می‌دهد:

1. **مسیریابی SNI nginx stream** — ترافیک دامنه را به nginx http در کنار ترافیک Reality به Xray مسیر می‌دهد
2. **TLS nginx** — گواهینامه‌ها توسط acme.sh (Let's Encrypt) مدیریت می‌شوند
3. **inbound VLESS+WSS** — fallback CDN از طریق Cloudflare

اتصال WSS از طریق CDN Cloudflare مسیر می‌یابد، که آن را حتی اگر IP سرور شما مسدود شود کار می‌کند — محدوده‌های IP Cloudflare بسیار گسترده‌ای برای مسدود کردن هستند.

## نصب با دامنه

```
meridian deploy 1.2.3.4 --domain proxy.example.com
```

## تنظیم Cloudflare

**این ترتیب دقیق را دنبال کنید** تا از مسائل گواهینامه TLS اجتناب کنید:

1. دامنه خود را در Cloudflare اضافه کنید، یک **record A** ایجاد کنید که به IP سرور شما اشاره کند
2. آیکن ابر را **خاکستری** نگاه دارید ("DNS only") — هنوز proxying را فعال نکنید
3. `meridian deploy` را اجرا کنید — acme.sh گواهینامه TLS را به طور خودکار به دست می‌آورد
4. به **ابر نارنجی** بروید (Proxied)
5. SSL/TLS را پیکربندی کنید → **Full (Strict)** و Network → **Enable WebSockets**

> **مهم:** acme.sh گواهینامه‌ها را از طریق چالش HTTP-01 روی پورت 80 به دست می‌آورد. اگر "Always Use HTTPS" Cloudflare فعال باشد، این چالش را می‌شکند. آن را غیرفعال کنید یا یک page rule برای `/.well-known/acme-challenge/*` اضافه کنید.

> **نکته مهم دیگر:** در حالت دامنه، صفحه اتصال میزبانی‌شده و مسیر مخفی پنل 3x-ui روی همین hostname ارائه می‌شوند. بعد از اینکه رکورد را به ابر نارنجی تغییر دهید، این صفحات هم از Cloudflare عبور می‌کنند. قابلیت‌های Cloudflare که اسکریپت تزریق می‌کنند یا HTML را تغییر می‌دهند (مثل Website Analytics / RUM) را برای این hostname غیرفعال کنید، چون صفحه اتصال Meridian عمداً از یک CSP سخت‌گیرانه و self-hosted استفاده می‌کند. اگر هنگام پروکسی بودن صفحه از کار افتاد، موقتاً رکورد را به DNS only برگردانید تا مشخص شود مشکل از سمت Cloudflare است.

## لینک‌های اتصال

با حالت دامنه، کاربران سه گزینه اتصال دارند:

| پروتکل | اولویت | مسیر |
|----------|----------|-------|
| Reality | اولیه | مستقیم به IP سرور |
| XHTTP | جایگزین | از طریق nginx روی پورت 443 |
| WSS | Backup | از طریق CDN Cloudflare |

کاربران باید ابتدا Reality (سریع‌ترین) را امتحان کنند، سپس XHTTP، و WSS فقط اگر هر دو ناکام شوند (IP مسدود است).

# DeepSeek × GitHub Agent

ایجنت خودمختار پایتون که با **اکانت chat.deepseek.com** (از طریق Playwright) گفت‌وگو می‌کند و هنگام راه‌اندازی یک **GitHub Classic Token** از کاربر می‌گیرد تا روی حساب گیت‌هاب متناظرِ آن توکن عملیات گسترده (مدیریت مخازن، Issues، Pull Requestها، Actions و...) انجام دهد.

مدل DeepSeek نقش «مغز تصمیم‌گیر» را دارد: شِمای همهٔ ابزارها را می‌بیند و خودش تصمیم می‌گیرد کدام ابزار را با چه آرگومان‌هایی صدا بزند؛ نتیجهٔ هر اجرا به او برمی‌گردد تا پاسخ نهایی ساخته شود.

---

## معماری

```
کاربر ──تسک──▶ agent.py (حلقهٔ ایجنت، حداکثر MAX_ROUNDS دور)
                 │
                 ├─▶ deepseek_client.py  ──Playwright persistent──▶ chat.deepseek.com
                 │        (textarea#chat-input / Enter / .ds-markdown--block)
                 │
                 ├─▶ system_prompt.py  (پرامپت سیستمی + پروتکل JSON)
                 │
                 └─▶ tool_registry.py  (رجیستری پلاگین‌پذیر ابزارها)
                          ├─▶ github_tools.py   (۲۹ ابزار گیت‌هاب، PyGithub 2.x)
                          ├─▶ builtin_tools.py  (ابزارهای نمونهٔ غیرگیت‌هابی)
                          └─▶ [پلاگین بعدی شما: web_tools، fs_tools، ...]
```

**پروتکل ابزار:** چون وب‌چت دیپ‌سیک Function Calling ندارد، مدل فقط این JSON را می‌فرستد:
`{"tool": "<نام>", "arguments": {...}}` — نتیجه با پیشوند `TOOL_RESULT` به همان گفت‌وگو برمی‌گردد. وقتی کار تمام شد مدل `{"final_answer": "..."}` می‌فرستد. اگر مدل در ۱۰ دور به جواب نهایی نرسد، حلقه متوقف می‌شود تا از حلقهٔ بی‌پایان جلوگیری شود.

---

## نصب

پیش‌نیاز: Python 3.10+ (به‌خاطر `zoneinfo`)

```bash
pip install -r requirements.txt
playwright install chromium          # دانلود مرورگر کرومیوم برای Playwright
cp .env.example .env                 # و اگر خواستی مسیر پروفایل/زمان‌ها را تنظیم کن
```

## اجرا

```bash
python agent.py                    # حالت تعاملی (REPL)
python agent.py "در مخزن agent یک Issue بساز با عنوان ..."   # حالت تک‌تسک
python agent.py --tools            # فقط فهرست ابزارها (بدون باز کردن مرورگر)
```

دستورات داخل REPL:

| دستور | کار |
|---|---|
| `/tools` | نمایش فهرست ابزارهای فعال |
| `/newchat` | شروع گفت‌وگوی تازه در دیپ‌سیک |
| `/exit` یا `/quit` | خروج |

### ورود به اکانت DeepSeek

اولین اجرا: پنجرهٔ کرومیوم باز می‌شود (`DEEPSEEK_HEADLESS=false`) و ایجنت صبر می‌کند تا **دستی** وارد اکانت شوی. پروفایل کرومیوم در مسیر ثابت `DEEPSEEK_USER_DATA_DIR` (از `.env`) ذخیره می‌شود (`playwright.chromium.launch_persistent_context`)، پس اجراهای بعدی بدون لاگین دوباره ادامه می‌دهند.

تشخیص لاگین: اگر `input[type="tel"]` یا `input[placeholder*="Phone"]` در صفحه باشد یعنی صفحهٔ ورود است؛ اگر `textarea#chat-input` باشد یعنی لاگین هستی.

---

## گرفتن GitHub Classic Token

۱. برو به **https://github.com/settings/tokens** → `Generate new token` → `Generate new token (classic)`
۲. توضیح بگذار و اسکوپ‌ها را انتخاب کن:

| اسکوپ | برای چه |
|---|---|
| `repo` | **حداقلِ لازم** — مخازن عمومی/پرایوت: کد، Issues، PRها، Releases |
| `workflow` | ساخت/ویرایش `.github/workflows` و اجرای Actions |
| `delete_repo` | فقط اگر ابزار `delete_repo` را می‌خواهی (پیش‌فرض نیست) |

۳. توکن با پیشوند `ghp_` ساخته می‌شود؛ هنگام اجرای برنامه با `getpass()` واردش کن (در ترمینال نمایش داده نمی‌شود).

**اعتبارسنجی:** توکن با یک درخواست ساده به `GET https://api.github.com/user` بررسی می‌شود؛ اگر نام کاربری برگردد معتبر است و برای تأیید نمایش داده می‌شود. اگر ۴۰۱ برگشت، پیام واضح «توکن نامعتبر» می‌گیری و می‌توانی دوباره وارد کنی (تا ۳ تلاش).

**هشدار امنیتی:** توکن کلاسیک به‌طور پیش‌فرض به منابع **سازمان‌ها** هم دسترسی دارد، مگر اینکه سازمان با SSO/سیاست‌های توکن آن را محدود کرده باشد؛ پس با احتیاط بساز، فقط لازم را بده و پس از کار حذفش کن. توکن **فقط در حافظهٔ همان اجرا** نگه داشته می‌شود و هرگز در فایل، لاگ یا کد ذخیره نمی‌شود.

---

## ابزارها (۲۹ گیت‌هابی + ۲ داخلی)

### فقط‌خواندنی

| ابزار | کار |
|---|---|
| `get_authenticated_user` | لاگین و مشخصات صاحب توکن + اسکوپ‌های توکن |
| `get_rate_limit` | سهمیهٔ باقی‌ماندهٔ API (عیب‌یابی ۴۰۳) |
| `list_repos` | فهرست مخازن (مالک/همکار/سازمان) |
| `get_repo` | متادیتای کامل مخزن |
| `get_repo_status` | وضعیت لحظه‌ای: شاخه‌ها، ۵ کامیت آخر، آخرین اجرای Actions |
| `list_branches` / `list_repo_files` / `get_file_contents` | شاخه‌ها، فایل‌ها، محتوای فایل |
| `list_issues` / `get_issue` | فهرست و جزئیات Issueها |
| `list_pull_requests` / `get_pull_request_status` | فهرست PRها؛ وضعیت mergeable و CI |
| `list_commits` | کامیت‌های اخیر |
| `list_workflows` / `get_workflow_runs` | workflowها و اجراهای Actions |
| `search_repositories` | جستجوی سراسری مخازن |

### نوشتنی

| ابزار | کار |
|---|---|
| `create_repo` | ساخت مخزن جدید |
| `create_or_update_file` | ساخت/به‌روزرسانی فایل با یک کامیت |
| `create_branch` | ساخت شاخهٔ جدید |
| `create_issue` / `comment_on_issue` / `update_issue_state` | ساخت Issue، کامنت، بستن/بازگشایی |
| `create_pull_request` / `comment_on_pr` / `merge_pull_request` | ساخت PR، کامنت روی PR، ادغام (merge/squash/rebase) |
| `create_release` | ساخت Release روی تگ |
| `trigger_workflow` | اجرای دستی workflow (نیازمند اسکوپ `workflow`) |
| `fork_repo` | فورک گرفتن |
| `delete_repo` ⚠️ | حذف دائمی — فقط با `confirm=true` و اسکوپ `delete_repo` |

### داخلی (نمونهٔ پلاگین‌پذیری)

`get_current_time`، `echo`

---

## افزودن ابزار جدید (الگوی پلاگین)

الگو همان سبک ابزارمحور سرورهایی مثل **morx-gms2-mcp** است: ده‌ها ابزار با شِما به مدل داده می‌شود و حلقهٔ ایجنت عمومی است. برای افزودن ابزار جدید (مثلاً جستجوی وب) هیچ تغییری در `agent.py` لازم نیست — یک ماژول بساز، ثبت کن:

```python
# web_tools.py
from tool_registry import ToolRegistry

def register_web_tools(registry: ToolRegistry, ...) -> None:
    @registry.register(
        name="web_search",
        description="Search the web and return results",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    def web_search(query: str) -> dict:
        ...  # پیاده‌سازی؛ خروجی حتماً dict (JSON-سازگار)
        return {"results": [...]}

# سپس فقط در agent.py → build_registry یک خط اضافه کن:
#   from web_tools import register_web_tools
#   register_web_tools(registry)
```

قواعد ابزار: ورودی‌ها دقیقاً هم‌نام با `properties` شِما (فراخوانی `func(**arguments)`)، خروجی `dict`، و برای عملیات خطرناک پارامتر تأیید صریح مثل `confirm=true`. اگر ابزاری خطا بدهد، رجیستری آن را به‌شکل `{"ok": false, "error": ...}` به مدل برمی‌گرداند و برنامه نمی‌میرد.

---

## تست‌ها

```bash
# ایستا (بدون شبکه): ایمپورت‌ها، شِمای ۳۱ ابزار، پارسر پروتکل، کوتاه‌سازی خروجی
python tests/test_static.py

# فقط‌خواندنی با API واقعی — هیچ داده‌ای ساخته/حذف نمی‌شود:
GH_TOKEN=ghp_xxx python tests/test_github_readonly.py
```

---

## ساختار پروژه

```
agent/
├── agent.py              ← نقطهٔ ورود: دریافت توکن (getpass) + REPL + حلقهٔ ایجنت
├── deepseek_client.py    ← Playwright persistent context، تشخیص/انتظار لاگین، ارسال/دریافت پیام
├── tool_registry.py      ← رجیستری پلاگین‌پذیر + اجرای امن ابزارها
├── github_tools.py       ← GitHubTools (PyGithub 2.x, Auth.Token) + ثبت ۲۹ ابزار
├── builtin_tools.py      ← ابزارهای نمونهٔ غیرگیت‌هابی
├── system_prompt.py      ← پرامپت سیستمی + parse_model_action
├── config.py             ← پیکربندی از .env
├── requirements.txt
├── .env.example          ← نمونهٔ کامل تنظیمات (توکن اینجا نوشته نمی‌شود!)
└── tests/
    ├── test_static.py          ← تست ایستا بدون شبکه
    └── test_github_readonly.py ← تست ابزارهای خواندنی با API واقعی
```

## تنظیمات `.env`

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `DEEPSEEK_URL` | `https://chat.deepseek.com/` | آدرس چت |
| `DEEPSEEK_USER_DATA_DIR` | `./.deepseek_profile` | پروفایل ماندگار اکانت (از `.env`) |
| `DEEPSEEK_HEADLESS` | `false` | برای ورود اول باید `false` باشد |
| `MAX_ROUNDS` | `10` | سقف دورهای ابزار در هر تسک |
| `RESPONSE_TIMEOUT` | `240` | مهلت انتظار پاسخ دیپ‌سیک (ثانیه) |
| `TOKEN_MAX_ATTEMPTS` | `3` | تلاش‌های مجاز ورود توکن |
| `TOOL_RESULT_MAX_CHARS` | `20000` | سقف حجم خروجی هر ابزار |

## عیب‌یابی

- **«textarea#chat-input پیدا نشد»** → اگر دیپ‌سیک UI را تغییر داده باشد، سلکتورها در `deepseek_client.py` (ثابت‌های کلاس) را به‌روز کن.
- **پاسخ ناقص** → `RESPONSE_TIMEOUT` یا `RESPONSE_STABLE_CHECKS` را در `.env` بیشتر کن.
- **۴۰۳ گیت‌هاب** → با ابزار `get_rate_limit` سهمیه را ببین؛ یا اسکوپ توکن کم است.
- **لاگین مکرر دیپ‌سیک** → فایل `.deepseek_profile` را پاک کن و یک‌بار با `DEEPSEEK_HEADLESS=false` وارد شو.
- **اسکریپت‌های صفحه** → مرورگر با `--disable-blink-features=AutomationControlled` اجرا می‌شود؛ اگر باز هم مشکل بود، سرعت اجرای تسک‌ها را کمتر کن.

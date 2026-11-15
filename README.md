# DevX — ایجنت خودمختار DeepSeek × GitHub

ایجنت پایتون حرفه‌ای که با **اکانت chat.deepseek.com** (Playwright) گفت‌وگو می‌کند و با **GitHub Classic Token** روی حساب گیت‌هاب شما عملیات گسترده انجام می‌دهد — با **حافظهٔ ماندگار**، **یادگیری مهارت**، **۳۷ ابزار پلاگین‌پذیر** و **رابط وب HUD به نام DevX** با تم قرمز-مشکی و اکسنت‌های فیروزه‌ای/زرد و افکت‌های چشمک‌زن.

```
  ██████╗ ███████╗██╗   ██╗██╗  ██╗
  ██╔══██╗██╔════╝██║   ██║╚██╗██╔╝
  ██║  ██║█████╗  ██║   ██║ ╚███╔╝
  ██║  ██║██╔══╝  ╚██╗ ██╔╝ ██╔██╗
  ██████╔╝███████╗ ╚████╔╝ ██╔╝ ██╗
  ╚═════╝ ╚══════╝  ╚═══╝  ╚═╝  ╚═╝
```

## چه چیزهایی دارد که ایجنت‌های معمولی ندارند؟

| قابلیت | توضیح |
|---|---|
| 🧠 **حافظهٔ ماندگار** | حقایق کاربر/پروژه، درس‌های خطا، یادداشت‌ها و تاریخچهٔ تسک‌ها در `memory/memory.json` ذخیره می‌شود؛ ابتدای هر تسک، حافظهٔ مرتبط (جستجوی کلیدواژه‌ای تطبیق‌پذیر با فارسی) به پرامپت تزریق می‌شود — **همه چیز را یادش می‌ماند** |
| 🧰 **یادگیری مهارت** | بعد از هر تسک موفق، مدل یک «مهارت قابل‌استفادهٔ مجدد» استخراج می‌کند (پروتکل DISTILL) و در `skills_learned/*.md` ذخیره می‌شود؛ با هر استفاده، آمار usage/success به‌روز و مهارت‌های مرتبط در تسک‌های بعدی تزریق می‌شوند — **در طول کار اسکیل یاد می‌گیرد** |
| 📚 **درس‌آموزی از خطا** | اگر ابزاری خطا داد، پایان تسک یک درس اکشن‌پذیر در حافظه ثبت می‌شود تا همان اشتباه تکرار نشود |
| 📡 **رویدادمحور (Event-driven)** | موتور ایجنت هر دور/ابزار/نتیجه را به‌صورت رویداد منتشر می‌کند؛ هم CLI و هم وب از یک موتور مشترک استفاده می‌کنند |
| 🖥️ **رابط وب DevX** | داشبورد HUD زنده: استریم SSE، خط‌زمانی ابزارها، پنل حافظه، مرورگر مهارت‌ها، وضعیت گیت‌هاب، اثر ماشین‌نویس، رادار، LEDهای چشمک‌زن |
| 🧩 **پلاگین‌پذیری کامل** | رجیستری ابزار با JSON-Schema؛ افزودن ابزار جدید بدون دست زدن به موتور (سبک morx-gms2-mcp) |
| 🔁 **مغز-مستقل (Brain-agnostic)** | هر کلاینتی با `send/wait_response/new_chat` می‌تواند مغز باشد (دیپ‌سیک اکنون؛ FakeBrain برای تست؛ API رسمی در آینده) |
| 🛡️ **امنیت توکن** | توکن فقط با `getpass` یا مودال UI گرفته می‌شود، با `GET /user` اعتبارسنجی و **فقط در حافظهٔ همان اجرا** نگه داشته می‌شود |

---

## معماری

```
                    ┌────────────────────────── DevX Web UI ─────────────────────────┐
                    │  ui/index.html — تم قرمز/مشکی + فیروزه‌ای/زرد، چشمک‌زن          │
                    └───────────────▲────────────────────────────────────────────────┘
                                    │ SSE (استریم زنده) + REST
┌─────────────┐  تسک   ┌────────────┴─────────┐  رویداد   ┌────────────────────┐
│ کاربر (CLI  ├───────▶│   server.py (DevX)   │◀──────────┤ agent_loop.py      │
│ یا وب DevX) │        │   SSE + API + state  │           │ AgentEngine        │
└─────────────┘        └──────────┬───────────┘           │ + EventBus         │
                                  │                       │ + یادگیری خودکار   │
                       ┌──────────▼───────────┐           └──┬───────┬─────────┘
                       │ deepseek_client.py   │              │       │
                       │ Playwright persistent│        ┌─────▼──┐ ┌──▼──────────┐
                       │ context + اکانت      │        │memory.py│ │ skills.py   │
                       │ chat.deepseek.com    │        │حافظه JSON│ │مهارت‌ها .md │
                       └──────────────────────┘        └────────┘ └─────────────┘
                                                  ┌──────────────────────────┐
                                                  │ tool_registry.py         │
                                                  │ ├ github_tools.py (۲۹)   │
                                                  │ ├ memory/skill tools (۵) │
                                                  │ └ builtin_tools.py (۲)   │
                                                  └──────────────────────────┘
```

**پروتکل ابزار (چرخهٔ ایجنت):** مدل فقط `{"tool": "...", "arguments": {...}}` می‌فرستد → ابزار اجرا → نتیجه با پیشوند `TOOL_RESULT` برمی‌گردد → تا `{"final_answer": "..."}` یا سقف `MAX_ROUNDS` (پیش‌فرض ۱۰).

**چرخهٔ یادگیری:** پایان تسک موفق → `META/DISTILL` به همان گفت‌وگو → JSON مهارت → ذخیره در کتابخانه → تسک‌های بعدی مشابه، ابتدا مهارت مرتبط را در پرامپت می‌بینند.

---

## نصب و اجرا

پیش‌نیاز: Python 3.10+

```bash
pip install -r requirements.txt
playwright install chromium        # مرورگر کرومیوم
cp .env.example .env               # تنظیمات (اختیاری)
```

### رابط وب DevX (پیشنهادی)

```bash
python devx.py                     # → http://127.0.0.1:8770 (باز شدن خودکار)
python devx.py --port 9000         # پورت دلخواه
python devx.py --no-browser        # فقط سرور
```

۱. صفحهٔ DevX باز می‌شود؛ مودال توکن گیت‌هاب ظاهر می‌شود → توکن `ghp_` را بده (اعتبارسنجی با `GET /user`) یا «فعلاً نه» را بزن.
۲. اگر اکانت دیپ‌سیک لاگین نباشد، **نوار هشدار چشمک‌زن** بالای صفحه می‌آید → دکمهٔ «باز کردن پنجرهٔ ورود» → در پنجرهٔ کرومیوم دستی وارد شو (یک‌بار برای همیشه، پروفایل ماندگار است).
۳. LED وضعیت `DEEPSEEK` فیروزه‌ای و پالس‌دار می‌شود → تسک بده.

### حالت ترمینال (CLI)

```bash
python agent.py                    # REPL تعاملی (توکن با getpass)
python agent.py "تسک مورد نظر"     # تک‌تسک
python agent.py --tools            # فقط فهرست ابزارها
```

دستورات REPL: `/tools` `/memory` `/skills` `/stats` `/newchat` `/exit`

---

## گرفتن GitHub Classic Token

۱. **https://github.com/settings/tokens** → `Generate new token (classic)`
۲. اسکوپ‌ها:

| اسکوپ | برای چه |
|---|---|
| `repo` | **حداقل لازم** — مخازن عمومی/پرایوت: کد، Issues، PR، Release |
| `workflow` | ساخت/ویرایش و اجرای Actions |
| `delete_repo` | فقط برای ابزار `delete_repo` (پیش‌فرض نیست) |

۳. ⚠️ توکن کلاسیک به‌طور پیش‌فرض به منابع **سازمان‌ها** هم دسترسی دارد (مگر سازمان محدودش کرده باشد) — با احتیاط بساز و پس از کار حذف کن.
۴. 🔒 توکن فقط در حافظهٔ همان اجرا می‌ماند (CLI: `getpass`، وب: مودال + POST به سرور محلی خودت)؛ هرگز در فایل/لاگ/کد ذخیره نمی‌شود.

---

## ابزارها (۳۷)

**گیت‌هاب (۲۹ با PyGithub 2.x و الگوی `Auth.Token`):**
فقط‌خواندنی: `get_authenticated_user` `get_rate_limit` `list_repos` `get_repo` `get_repo_status` `list_branches` `get_file_contents` `list_repo_files` `list_issues` `get_issue` `list_pull_requests` `get_pull_request_status` `list_commits` `list_workflows` `get_workflow_runs` `search_repositories`
نوشتنی: `create_repo` `create_or_update_file` `create_branch` `create_issue` `comment_on_issue` `update_issue_state` `create_pull_request` `comment_on_pr` `merge_pull_request` `create_release` `trigger_workflow` `fork_repo` `delete_repo ⚠️`

**حافظه (۳):** `save_memory` `search_memory` `list_recent_tasks`
**مهارت (۳):** `save_skill` `list_skills` `get_skill`
**داخلی (۲):** `get_current_time` `echo`

## افزودن ابزار جدید (پلاگین)

```python
# web_tools.py
def register_web_tools(registry) -> None:
    @registry.register(name="web_search", description="Search the web",
                       parameters={"type":"object","properties":{"query":{"type":"string"}},"required":["query"]})
    def web_search(query: str) -> dict:
        return {"results": [...]}

# سپس فقط یک خط در server.py→_make_registry یا agent.py→build_registry:
register_web_tools(registry)
```

---

## تست‌ها

```bash
python tests/test_static.py          # ایستا: ایمپورت‌ها + شِمای ۳۷ ابزار + پارسر
python tests/test_devx_core.py       # آفلاین: حافظه/مهارت/موتور با FakeBrain + یادگیری
python tests/test_server_smoke.py    # سرور واقعی: UI + API + SSE + جریان کامل تسک
GH_TOKEN=ghp_xxx python tests/test_github_readonly.py   # فقط‌خواندنی با API واقعی
```

---

## ساختار پروژه

```
agent/
├── devx.py              ← لانچر رابط وب DevX
├── server.py            ← سرور محلی: SSE + REST API + state
├── ui/index.html        ← رابط HUD (تک‌فایل، بدون build؛ قرمز/مشکی + فیروزه‌ای/زرد)
├── agent.py             ← حالت CLI (همان موتور مشترک)
├── agent_loop.py        ← AgentEngine: حلقهٔ رویدادمحور + یادگیری خودکار
├── memory.py            ← حافظهٔ ماندگار (memory/memory.json) + جستجو + ابزارها
├── skills.py            ← کتابخانهٔ مهارت (skills_learned/*.md) + انتخاب مرتبط + ابزارها
├── deepseek_client.py   ← Playwright persistent context + ارسال/دریافت از DOM
├── github_tools.py      ← ۲۹ ابزار گیت‌هاب (PyGithub 2.x, Auth.Token)
├── builtin_tools.py     ← ابزارهای نمونهٔ غیرگیت‌هابی
├── tool_registry.py     ← رجیستری پلاگین‌پذیر با JSON-Schema
├── system_prompt.py     ← پرامپت سیستمی + پارسر پروتکل
├── config.py            ← پیکربندی از .env
└── tests/               ← ایستا + هستهٔ DevX + دود سرور + فقط‌خواندنی واقعی
```

## تنظیمات کلیدی `.env`

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `DEEPSEEK_USER_DATA_DIR` | `./.deepseek_profile` | پروفایل ماندگار اکانت دیپ‌سیک |
| `MAX_ROUNDS` | `10` | سقف دورهای ابزار در هر تسک |
| `DEVX_PORT` | `8770` | پورت رابط وب |
| `MEMORY_DIR` | `./memory` | محل حافظهٔ ماندگار |
| `SKILLS_DIR` | `./skills_learned` | کتابخانهٔ مهارت‌های آموخته‌شده |
| `AUTO_LEARN_SKILL` | `true` | یادگیری مهارت پس از تسک موفق |
| `AUTO_LEARN_LESSON` | `true` | ثبت درس پس از خطای ابزار |
| `RESPONSE_TIMEOUT` | `240` | مهلت انتظار پاسخ دیپ‌سیک (ثانیه) |

## عیب‌یابی

- **نوار هشدار چشمک‌زن «لاگین دیپ‌سیک لازم است»** → دکمهٔ «باز کردن پنجرهٔ ورود» را بزن و در پنجرهٔ کرومیوم وارد شو؛ پروفایل ماندگار است و تکرار نمی‌شود. در `DEEPSEEK_HEADLESS=true` ورود دستی ممکن نیست.
- **«textarea#chat-input پیدا نشد»** → UI دیپ‌سیک عوض شده؛ سلکتورها ثابت کلاس در `deepseek_client.py` هستند.
- **پاسخ ناقص** → `RESPONSE_TIMEOUT` یا `RESPONSE_STABLE_CHECKS` را بالا ببر.
- **۴۰۳ گیت‌هاب** → ابزار `get_rate_limit` یا پنل GitHub در DevX (سهمیه‌سنج).
- **توکن گم شده بعد از ری‌استارت** → طبیعی است؛ توکن عمداً فقط حافظه است — دوباره در مودال بده.
- **حافظهٔ خراب** → فایل `memory/memory.json.corrupt.bak` بکاپ می‌شود و ایجنت از نو شروع می‌کند.

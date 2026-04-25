"""
Ethara Task Tracker Automation
================================
All selectors confirmed via browser console.

Login flow:
  1. Go to https://task.ethara.ai/login
  2. Click Tasker button        [data-testid="role-tasker"]
  3. Fill email                 input[type="email"]
  4. Fill password              input[type="password"]
  5. Click Sign In              button.w-full containing "Sign In"
  6. Session token saved to     sessionStorage["token"]

Dashboard flow (per task):
  1. Fill Task ID               [data-testid="task-name-input"]
  2. Open project dropdown      [data-testid="task-project-select-trigger"]
  3. Search project             [data-testid="my-projects-search"]
  4. Click matching project     [data-testid^="project-toggle-"] (visible + name match)
  5. Click Start Task           [data-testid="start-task-btn"]
  6. Fill Prompt                [data-testid="prompt-input"]
  7. Justification:
       N/A → tick checkbox      label="Justification not required for this task"
       else → fill textarea     [data-testid="justification-input"]
  8. Click Submit for QC        button containing "Submit for QC"
  9. Wait for task time (from sheet "wait time" column)
  10. Click Complete Task       [data-testid="end-task-btn"]

Requirements:
    pip install selenium webdriver-manager requests gspread google-auth
"""

import time
import sys
import csv
import io
import re
import pickle
import logging
import os
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

try:
    import requests as req_lib
except ImportError:
    req_lib = None

try:
    import gspread
    from google.oauth2.service_account import Credentials as SACredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

try:
    from webdriver_manager.chrome import ChromeDriverManager
    WDM_AVAILABLE = True
except ImportError:
    WDM_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
CONFIG = {
    # ── Ethara ────────────────────────────────────────────────────────────────
    "ethara_login_url":  "https://task.ethara.ai/login",
    "ethara_tasker_url": "https://task.ethara.ai/tasker",
    "ethara_email":      os.getenv("ETHARA_EMAIL", ""),
    "ethara_password":   os.getenv("ETHARA_PASSWORD", ""),
    "cookie_file":       "ethara_session.pkl",

    # ── Google Sheets: Option A – publish sheet as CSV ────────────────────────
    # File → Share → Publish to web → Sheet → CSV → copy link
    "sheet_csv_url": os.getenv("SHEET_CSV_URL", ""),

    # ── Google Sheets: Option B – service account (gspread) ──────────────────
    "sheet_id":             "",
    "sheet_name":           "Sheet1",
    "service_account_json": "service_account.json",

    # ── Column names in your sheet (case-insensitive) ─────────────────────────
    "col_task_id":       "Task ID",
    "col_prompt":        "Prompt",
    "col_justification": "Justification",
    "col_task_time":     "Time",
    "col_project":       "Task Name",

    # ── Timing ────────────────────────────────────────────────────────────────
    "default_wait_minutes": 2,          # fallback if no wait time in sheet

    # ── Browser ───────────────────────────────────────────────────────────────
    "headless":      False,
    "implicit_wait": 10,
    "page_timeout":  30,
}
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ethara")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_minutes(text: str, default: float = 2.0) -> float:
    if not text:
        return default
    text = text.strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return default
    value = float(m.group(1))
    if "sec" in text:
        return value / 60
    return value


def get_col(task: dict, col_key: str) -> str:
    col_name = CONFIG.get(col_key, "")
    if not col_name:
        return ""
    val = task.get(col_name, "")
    if val:
        return str(val).strip()
    for k, v in task.items():
        if k.strip().lower() == col_name.strip().lower():
            return str(v).strip()
    return ""


def wait_click(driver, by, selector, timeout=20):
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, selector))
    )


def wait_visible(driver, by, selector, timeout=20):
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, selector))
    )


def react_type(driver, el, text: str):
    """Type into a React-controlled input using JS native setter + el.click()."""
    driver.execute_script("""
        var el = arguments[0];
        var text = arguments[1];
        el.focus();
        el.click();
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        nativeSetter.call(el, text);
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
    """, el, text)


def react_type_textarea(driver, el, text: str):
    """Type into a React-controlled textarea using JS native setter + el.click()."""
    driver.execute_script("""
        var el = arguments[0];
        var text = arguments[1];
        el.focus();
        el.click();
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value'
        ).set;
        nativeSetter.call(el, text);
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
    """, el, text)


# ─────────────────────────────────────────────────────────────────────────────
#  Google Sheets
# ─────────────────────────────────────────────────────────────────────────────

def load_sheet_csv(url: str) -> list:
    assert req_lib, "pip install requests"
    log.info("Fetching sheet via CSV URL...")
    resp = req_lib.get(url, timeout=30)
    resp.raise_for_status()
    rows = [dict(r) for r in csv.DictReader(io.StringIO(resp.text))]
    log.info(f"  Loaded {len(rows)} rows")
    return rows


def load_sheet_gspread(sheet_id, sheet_name, key_path) -> list:
    assert GSPREAD_AVAILABLE, "pip install gspread google-auth"
    log.info("Connecting via gspread...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = SACredentials.from_service_account_file(key_path, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).worksheet(sheet_name)
    rows = ws.get_all_records()
    log.info(f"  Loaded {len(rows)} rows from '{sheet_name}'")
    return rows


def load_tasks() -> list:
    c = CONFIG
    if c["sheet_csv_url"]:
        return load_sheet_csv(c["sheet_csv_url"])
    if c["sheet_id"] and GSPREAD_AVAILABLE:
        return load_sheet_gspread(c["sheet_id"], c["sheet_name"], c["service_account_json"])
    raise RuntimeError(
        "No sheet configured!\n"
        "Set sheet_csv_url OR (sheet_id + service_account_json) in CONFIG."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Browser
# ─────────────────────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    if CONFIG["headless"]:
        opts.add_argument("--headless=new")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    service = Service(ChromeDriverManager().install()) if WDM_AVAILABLE else Service()
    driver = webdriver.Chrome(service=service, options=opts)
    driver.implicitly_wait(CONFIG["implicit_wait"])
    driver.set_page_load_timeout(CONFIG["page_timeout"])
    return driver


# ─────────────────────────────────────────────────────────────────────────────
#  Session management
# ─────────────────────────────────────────────────────────────────────────────

def save_session(driver):
    """Save cookies + sessionStorage token to disk."""
    session = {
        "cookies": driver.get_cookies(),
        "token":   driver.execute_script("return sessionStorage.getItem('token');"),
        "user":    driver.execute_script("return sessionStorage.getItem('user');"),
        "savedCredentials": driver.execute_script(
            "return sessionStorage.getItem('savedCredentials');"
        ),
    }
    with open(CONFIG["cookie_file"], "wb") as f:
        pickle.dump(session, f)
    log.info("  Session saved to disk (next run will skip login)")


def load_session(driver) -> bool:
    """Try to restore saved session. Returns True if successful."""
    cookie_file = CONFIG["cookie_file"]
    if not os.path.exists(cookie_file):
        log.info("  No saved session found — will login fresh")
        return False

    log.info("  Found saved session — attempting restore...")
    with open(cookie_file, "rb") as f:
        session = pickle.load(f)

    driver.get(CONFIG["ethara_tasker_url"])
    time.sleep(1)

    for cookie in session.get("cookies", []):
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass

    if session.get("token"):
        driver.execute_script(
            f"sessionStorage.setItem('token', '{session['token']}');"
        )
    if session.get("user"):
        user_escaped = session["user"].replace("'", "\\'").replace("\n", "")
        driver.execute_script(
            f"sessionStorage.setItem('user', '{user_escaped}');"
        )
    if session.get("savedCredentials"):
        driver.execute_script(
            f"sessionStorage.setItem('savedCredentials', "
            f"'{session['savedCredentials']}');"
        )

    driver.refresh()
    time.sleep(3)

    if "tasker" in driver.current_url and "login" not in driver.current_url:
        log.info("  Session restored — skipping login!")
        return True

    log.info("  Session expired — doing fresh login")
    os.remove(cookie_file)
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Login
# ─────────────────────────────────────────────────────────────────────────────

def ethara_login(driver: webdriver.Chrome):
    log.info("=" * 55)
    log.info("  LOGGING IN TO ETHARA")
    log.info("=" * 55)

    driver.get(CONFIG["ethara_login_url"])
    time.sleep(2)

    # 1. Click Tasker role button
    log.info("  [1/4] Clicking Tasker role...")
    tasker_btn = wait_click(driver, By.CSS_SELECTOR, "[data-testid='role-tasker']")
    tasker_btn.click()
    time.sleep(1)

    # 2. Fill email
    log.info("  [2/4] Entering email...")
    email_el = wait_visible(driver, By.CSS_SELECTOR, "input[type='email']")
    react_type(driver, email_el, CONFIG["ethara_email"])
    time.sleep(0.3)

    # 3. Fill password
    log.info("  [3/4] Entering password...")
    pass_el = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    react_type(driver, pass_el, CONFIG["ethara_password"])
    time.sleep(0.3)

    # 4. Click Sign In
    log.info("  [4/4] Clicking Sign In...")
    sign_in = driver.execute_script("""
        return Array.from(document.querySelectorAll("button[type='submit']"))
            .find(b => b.className.includes("w-full") &&
                       b.innerText.includes("Sign In"));
    """)
    if not sign_in:
        raise RuntimeError("Sign In button not found!")
    sign_in.click()

    # Wait for dashboard
    log.info("  Waiting for dashboard...")
    WebDriverWait(driver, 30).until(
        lambda d: "tasker" in d.current_url and "login" not in d.current_url
    )
    time.sleep(2)
    log.info("  LOGIN SUCCESS!")

    save_session(driver)


def ensure_logged_in(driver: webdriver.Chrome):
    if not load_session(driver):
        ethara_login(driver)


# ─────────────────────────────────────────────────────────────────────────────
#  Task execution
# ─────────────────────────────────────────────────────────────────────────────

def run_task(driver: webdriver.Chrome, task: dict):
    task_id   = get_col(task, "col_task_id")
    prompt    = get_col(task, "col_prompt")
    justif    = get_col(task, "col_justification")
    task_time = get_col(task, "col_task_time")
    project   = get_col(task, "col_project")

    if not task_id:
        log.warning("  Skipping row — empty Task ID")
        return

    wait_mins    = parse_minutes(task_time, CONFIG["default_wait_minutes"])
    wait_secs    = wait_mins * 60
    justif_skip  = justif.strip().upper() in ("N/A", "NOT REQUIRED", "NONE", "")

    log.info(f"  Task ID  : {task_id}")
    log.info(f"  Project  : {project}")
    log.info(f"  Wait     : {wait_mins:.1f} min ({wait_secs:.0f}s) — will complete after full wait")
    log.info(f"  Justif   : {'SKIP checkbox' if justif_skip else justif[:60]}")

    # Navigate to dashboard
    driver.get(CONFIG["ethara_tasker_url"])
    time.sleep(2)

    # ── 1. Enter Task ID ──────────────────────────────────────────────────────
    log.info("  [1] Entering Task ID...")
    task_input = wait_click(driver, By.CSS_SELECTOR, "[data-testid='task-name-input']")
    react_type(driver, task_input, task_id)
    time.sleep(0.5)
    log.info(f"      Task ID entered: {task_id}")

    # ── 2. Select Project ─────────────────────────────────────────────────────
    if project:
        log.info("  [2] Selecting project...")
        try:
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent("
                "'keydown', { key: 'Escape', bubbles: true }));"
            )
            time.sleep(0.3)

            trigger = wait_click(driver, By.CSS_SELECTOR,
                "[data-testid='task-project-select-trigger']")
            trigger.click()
            time.sleep(0.8)

            search = wait_visible(driver, By.CSS_SELECTOR,
                "[data-testid='my-projects-search']")
            react_type(driver, search, project)
            time.sleep(0.8)

            clicked = driver.execute_script("""
                var project = arguments[0];
                var btns = document.querySelectorAll(
                    "[data-testid^='project-toggle-']"
                );
                for(var btn of btns) {
                    if(btn.offsetParent !== null &&
                       btn.innerText.trim().startsWith(project)) {
                        btn.click();
                        return btn.innerText.trim().slice(0, 60);
                    }
                }
                return null;
            """, project)

            if clicked:
                log.info(f"      Project selected: {clicked}")
            else:
                log.warning(f"      Project '{project}' not found in dropdown")
        except Exception as e:
            log.warning(f"      Could not select project: {e}")

    # ── 3. Click Start Task ───────────────────────────────────────────────────
    log.info("  [3] Clicking Start Task...")
    driver.execute_script("""
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        document.body.click();
    """)
    time.sleep(1)
    start_btn = wait_click(driver, By.CSS_SELECTOR, "[data-testid='start-task-btn']")
    driver.execute_script("arguments[0].scrollIntoView(true);", start_btn)
    time.sleep(0.3)
    driver.execute_script("arguments[0].click();", start_btn)
    task_start_time = time.time()
    log.info(f"      Started at {datetime.now().strftime('%H:%M:%S')}")
    time.sleep(2)

    # ── 4. Fill Prompt ────────────────────────────────────────────────────────
    log.info("  [4] Filling prompt...")
    try:
        prompt_el = wait_visible(driver, By.CSS_SELECTOR, "[data-testid='prompt-input']")
        react_type_textarea(driver, prompt_el, prompt)
        log.info(f"      Prompt filled ({len(prompt)} chars)")
    except Exception as e:
        log.warning(f"      Could not fill prompt: {e}")

    # ── 5 & 6. Justification + QC branch ─────────────────────────────────────
    #
    #  NO JUSTIFICATION:
    #    → tick "Justification not required" checkbox
    #    → wait full task time
    #    → click Complete Task
    #    (no Submit for QC step)
    #
    #  WITH JUSTIFICATION:
    #    → fill justification textarea
    #    → click Submit for QC
    #    → wait full task time
    #    → click Complete Task
    #
    if justif_skip:
        # ── Case A: No justification ──────────────────────────────────────────
        log.info("  [5] Ticking 'Justification not required'...")
        try:
            checkbox = driver.execute_script("""
                var cbs = document.querySelectorAll("input[type='checkbox']");
                for(var cb of cbs) {
                    var label = cb.closest("label")?.innerText
                        || cb.parentElement?.innerText || "";
                    if(label.includes("Justification not required")) return cb;
                }
                return null;
            """)
            if checkbox:
                if not checkbox.is_selected():
                    checkbox.click()
                log.info("      Checkbox ticked")
            else:
                log.warning("      Justification checkbox not found")
        except Exception as e:
            log.warning(f"      Could not tick checkbox: {e}")

        log.info("  [6] No justification — skipping Submit for QC")

    else:
        # ── Case B: With justification ────────────────────────────────────────
        log.info("  [5] Filling justification...")
        try:
            justif_el = wait_visible(driver, By.CSS_SELECTOR,
                "[data-testid='justification-input']")
            react_type_textarea(driver, justif_el, justif)
            log.info(f"      Justification filled ({len(justif)} chars)")
        except Exception as e:
            log.warning(f"      Could not fill justification: {e}")

        log.info("  [6] Clicking Submit for QC...")
        try:
            submit_qc_btn = driver.execute_script("""
                return Array.from(document.querySelectorAll("button"))
                    .find(b => b.innerText.trim().toLowerCase().includes("submit for qc")
                            && b.offsetParent !== null);
            """)
            if submit_qc_btn:
                submit_qc_btn.click()
                log.info("      Submit for QC clicked!")
            else:
                log.warning("      Submit for QC button not found — skipping QC step")
        except Exception as e:
            log.warning(f"      Could not click Submit for QC: {e}")

    # ── 7. Wait for full task time ────────────────────────────────────────────
    elapsed   = time.time() - task_start_time
    remaining = wait_secs - elapsed

    if remaining > 0:
        finish_at = datetime.fromtimestamp(task_start_time + wait_secs).strftime("%H:%M:%S")
        log.info(f"  [7] Waiting {remaining:.0f}s (until {finish_at}) before completing...")

        # Log countdown every 60 seconds so you can track progress
        while True:
            elapsed_now = time.time() - task_start_time
            remaining_now = wait_secs - elapsed_now
            if remaining_now <= 0:
                break
            sleep_chunk = min(60, remaining_now)
            time.sleep(sleep_chunk)
            elapsed_now = time.time() - task_start_time
            remaining_now = wait_secs - elapsed_now
            if remaining_now > 0:
                log.info(f"      ... {remaining_now:.0f}s remaining ...")
    else:
        log.info("  [7] Wait time already elapsed, completing now")

    # ── 8. Click Complete Task ────────────────────────────────────────────────
    log.info("  [8] Clicking Complete Task...")
    try:
        # Poll for Complete Task button (QC may need a moment to pass)
        complete_btn = None
        for attempt in range(20):  # up to 60s polling
            complete_btn = driver.execute_script("""
                return Array.from(document.querySelectorAll("button"))
                    .find(b => b.innerText.trim().toLowerCase().includes("complete task")
                            && !b.disabled
                            && b.offsetParent !== null);
            """)
            if complete_btn:
                break
            log.info(f"      Waiting for Complete Task button (attempt {attempt + 1})...")
            time.sleep(3)

        if complete_btn:
            complete_btn.click()
            log.info(f"  TASK {task_id} COMPLETED!")
        else:
            # Fallback to data-testid selector
            btn = wait_click(driver, By.CSS_SELECTOR, "[data-testid='end-task-btn']")
            btn.click()
            log.info(f"  TASK {task_id} COMPLETED (via testid)!")
    except Exception as e:
        log.error(f"  Could not complete task: {e}")

    time.sleep(2)


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("  ETHARA TASK AUTOMATION")
    log.info("=" * 55)

    driver = make_driver()

    try:
        ensure_logged_in(driver)

        completed_ids = set()

        while True:
            tasks = load_tasks()
            if not tasks:
                log.error("No tasks loaded — exiting")
                break

            # Find next incomplete task
            next_task = None
            for task in tasks:
                task_id = get_col(task, "col_task_id")
                if task_id and task_id not in completed_ids:
                    next_task = task
                    break

            if not next_task:
                log.info(f"\n{'=' * 55}")
                log.info("  ALL TASKS COMPLETED!")
                log.info(f"{'=' * 55}")
                break

            task_id = get_col(next_task, "col_task_id")
            log.info(f"\n{'-' * 55}")
            log.info(f"  RUNNING TASK: {task_id}")
            log.info(f"  Completed so far: {len(completed_ids)}")
            log.info(f"{'-' * 55}")

            try:
                run_task(driver, next_task)
                completed_ids.add(task_id)
            except Exception as e:
                log.error(f"  Error on task {task_id}: {e}", exc_info=True)
                log.info("  Skipping to next task...")
                completed_ids.add(task_id)  # mark as done to avoid infinite loop
                try:
                    driver.get(CONFIG["ethara_tasker_url"])
                    time.sleep(2)
                except Exception:
                    pass

        log.info(f"\n{'=' * 55}")
        log.info("  ALL TASKS COMPLETED!")
        log.info(f"{'=' * 55}")

    finally:
        input("\nPress ENTER to close browser...")
        driver.quit()


if __name__ == "__main__":
    main()
import os
import time
import random
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

def type_like_human(driver, element, text):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.1)
    element.click()
    time.sleep(0.1)
    element.clear()
    time.sleep(0.2)
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.15))

def click_like_human(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.2)
    actions = ActionChains(driver)
    actions.move_to_element(element).perform()
    time.sleep(0.2)
    element.click()

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")

def update_env_file(key, value):
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
    new_line = f'{key}="{value}"\n'
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = new_line
            found = True
            break
            
    if not found:
        lines.append(new_line)
        
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def get_webdriver():
    # Try Chrome
    try:
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,1024')
        options.add_argument(f'user-agent={UA}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Check for system-installed ChromeDriver on Linux
        from selenium.webdriver.chrome.service import Service
        chrome_service = None
        for path in ["/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver"]:
            if os.path.exists(path):
                chrome_service = Service(executable_path=path)
                print(f"[selenium] Using system ChromeDriver at {path}")
                break
                
        driver = webdriver.Chrome(service=chrome_service, options=options)
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            })
        except Exception:
            pass
        return driver
    except Exception as e:
        print(f"[selenium] Chrome driver failed to init: {e}. Trying Edge...")
        
    # Fallback to Edge
    try:
        options = webdriver.EdgeOptions()
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,1024')
        options.add_argument(f'user-agent={UA}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        driver = webdriver.Edge(options=options)
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            })
        except Exception:
            pass
        return driver
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Chrome or Edge drivers: {e}")

def run_refresh():
    # Load credentials from .env
    email = os.environ.get("FIFA_EMAIL")
    password = os.environ.get("FIFA_PASSWORD")
    
    if not email or not password:
        # Re-read .env file just in case it wasn't loaded
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "FIFA_EMAIL":
                            email = v.strip().strip('"').strip("'")
                        elif k.strip() == "FIFA_PASSWORD":
                            password = v.strip().strip('"').strip("'")
                            
    if not email or not password:
        print("[selenium] Error: FIFA_EMAIL or FIFA_PASSWORD not found in environment or .env file.")
        return False

    print(f"[selenium] Launching headless browser to refresh cookie for {email}...")
    driver = get_webdriver()
    
    try:
        # 1. Load play.fifa.com
        driver.get("https://play.fifa.com/")
        time.sleep(2)
        
        # 2. Accept cookies if prompt appears
        try:
            accept_xpath = "//button[contains(translate(text(), 'ACCEPT', 'accept'), 'accept')]"
            accept_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, accept_xpath))
            )
            accept_btn.click()
            print("[selenium] Accepted cookies banner.")
        except Exception:
            pass

        # 2b. Dismiss welcome modal if present
        try:
            time.sleep(2)
            play_now_buttons = driver.find_elements(By.XPATH, "//*[text()='PLAY NOW' or contains(text(), 'PLAY NOW') or contains(text(), 'PLAY')]")
            clicked = False
            for btn in play_now_buttons:
                try:
                    loc = btn.location
                    if loc and 0 < loc['x'] < 1280 and 0 < loc['y'] < 1024 and btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        print(f"[selenium] Dismissed Play Zone welcome modal using button at {loc}.")
                        clicked = True
                        break
                except Exception:
                    continue
            
            if not clicked:
                # Try finding close buttons
                close_elements = driver.find_elements(By.XPATH, "//*[contains(@class, 'close') or contains(@id, 'close')]")
                for btn in close_elements:
                    try:
                        loc = btn.location
                        if loc and 0 < loc['x'] < 1280 and 0 < loc['y'] < 1024 and btn.is_displayed():
                            driver.execute_script("arguments[0].click();", btn)
                            print(f"[selenium] Dismissed modal using close button at {loc}.")
                            clicked = True
                            break
                    except Exception:
                        continue
            time.sleep(1)
        except Exception as e:
            print(f"[selenium] Welcome modal check finished/failed: {e}")

        # 3. Find and click sign in/login button
        login_btn = None
        selectors = [
            "//a[contains(text(), 'Sign In') or contains(text(), 'Login') or contains(text(), 'LOG IN')]",
            "//button[contains(text(), 'Sign In') or contains(text(), 'Login') or contains(text(), 'LOG IN')]",
            "//a[contains(@href, 'authorize')]",
            "//*[contains(@class, 'login') or contains(@id, 'login')]"
        ]
        
        for sel in selectors:
            try:
                login_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                if login_btn:
                    login_btn.click()
                    print(f"[selenium] Clicked login button using selector: {sel}")
                    break
            except Exception:
                continue
                
        if not login_btn:
            # Let's check if we got redirected to auth automatically or if we can click something else
            print("[selenium] Login button not found, checking if already redirected...")

        # 4. Wait for redirection to PingOne sign-in page
        # FIFA ID login uses 'email' field, standard PingOne uses 'username'
        username_field = None
        for input_id in ["email", "username"]:
            try:
                username_field = WebDriverWait(driver, 15).until(
                    EC.visibility_of_element_located((By.ID, input_id))
                )
                if username_field:
                    break
            except Exception:
                continue
                
        if not username_field:
            raise RuntimeError("Could not locate email or username field on sign-in page.")
            
        password_field = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "password"))
        )
        print("[selenium] Reached OIDC Login page successfully.")
        
        # 5. Fill credentials
        type_like_human(driver, username_field, email)
        type_like_human(driver, password_field, password)
        time.sleep(1)
        
        # 6. Submit
        submit_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//button[@type='submit' or contains(text(), 'SIGN IN') or contains(text(), 'Sign In')]"))
        )
        click_like_human(driver, submit_btn)
        print("[selenium] Credentials submitted.")
        
        # 7. Wait for redirect back to play.fifa.com (completing login)
        WebDriverWait(driver, 20).until(
            lambda d: "play.fifa.com" in d.current_url and "auth" not in d.current_url
        )
        print(f"[selenium] Redirected back to: {driver.current_url}")
        
        # Navigate to play-zone to trigger OIDC handlers
        print("[selenium] Navigating to play-zone to load OIDC handlers...")
        driver.get("https://play.fifa.com/play-zone/")
        
        # Wait for fp.user to settle (in cookies or localStorage) (up to 10 seconds)
        print("[selenium] Waiting for fp.user to settle...")
        fp_user_val = None
        cookies = []
        for _ in range(10):
            cookies = driver.get_cookies()
            # Try to get from cookies
            for cookie in cookies:
                if cookie['name'] == 'fp.user':
                    fp_user_val = cookie['value']
                    break
            if fp_user_val:
                print("[selenium] Successfully retrieved fp.user from cookies.")
                break
                
            # Try to get from localStorage
            try:
                fp_user_val = driver.execute_script("return window.localStorage.getItem('fp.user');")
                if fp_user_val:
                    print("[selenium] Successfully retrieved fp.user from localStorage.")
                    break
            except Exception:
                pass
            time.sleep(1)
            
        if not fp_user_val:
            print("[selenium] Warning: fp.user not found in cookies or localStorage after 10 seconds.")
            print("[selenium] Available cookies:", [c['name'] for c in cookies])
            
        cookie_parts = []
        has_fp_user = False
        for cookie in cookies:
            cookie_parts.append(f"{cookie['name']}={cookie['value']}")
            if cookie['name'] == 'fp.user':
                has_fp_user = True
                
        if fp_user_val and not has_fp_user:
            cookie_parts.append(f"fp.user={fp_user_val}")
            
        cookie_string = "; ".join(cookie_parts)
            
        # 9. Update .env file
        update_env_file("FIFA_COOKIE", cookie_string)
        print("[selenium] Cookie successfully updated in .env.")
        
        # Update os.environ in memory as well
        os.environ["FIFA_COOKIE"] = cookie_string
        return True
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[selenium] Refresh failed: {e}")
        try:
            print(f"[selenium] Final URL: {driver.current_url}")
        except Exception:
            pass
        try:
            print("[selenium] Browser console logs:")
            for entry in driver.get_log('browser'):
                print(f"  {entry}")
        except Exception:
            pass
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            print(f"[selenium] Page text snippet (first 1000 chars):\n{body_text[:1000]}")
        except Exception:
            pass
        # Save screenshot for debugging
        try:
            screenshot_path = os.path.join(HERE, "selenium_error.png")
            driver.save_screenshot(screenshot_path)
            print(f"[selenium] Error screenshot saved to {screenshot_path}")
        except Exception:
            pass
        return False
        
    finally:
        driver.quit()

if __name__ == "__main__":
    run_refresh()

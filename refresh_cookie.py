import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

def get_webdriver():
    # Try Chrome
    try:
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,1024')
        return webdriver.Chrome(options=options)
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
        return webdriver.Edge(options=options)
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
        # PingOne pages have username/password inputs
        username_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        password_field = driver.find_element(By.ID, "password")
        print("[selenium] Reached OIDC Login page successfully.")
        
        # 5. Fill credentials
        username_field.clear()
        username_field.send_keys(email)
        password_field.clear()
        password_field.send_keys(password)
        
        # 6. Submit
        submit_btn = driver.find_element(By.XPATH, "//button[@type='submit' or contains(text(), 'SIGN IN') or contains(text(), 'Sign In')]")
        submit_btn.click()
        print("[selenium] Credentials submitted.")
        
        # 7. Wait for redirect back to play.fifa.com (completing login)
        WebDriverWait(driver, 20).until(
            lambda d: "play.fifa.com" in d.current_url and "auth" not in d.current_url
        )
        print(f"[selenium] Redirected back to: {driver.current_url}")
        
        # Wait a moment for cookies to fully settle
        time.sleep(2)
        
        # 8. Extract cookies
        cookies = driver.get_cookies()
        cookie_parts = []
        for cookie in cookies:
            cookie_parts.append(f"{cookie['name']}={cookie['value']}")
        cookie_string = "; ".join(cookie_parts)
        
        if "fp.user" not in cookie_string:
            print("[selenium] Warning: fp.user cookie not found in the retrieved cookies.")
            
        # 9. Update .env file
        update_env_file("FIFA_COOKIE", cookie_string)
        print("[selenium] Cookie successfully updated in .env.")
        
        # Update os.environ in memory as well
        os.environ["FIFA_COOKIE"] = cookie_string
        return True
        
    except Exception as e:
        print(f"[selenium] Refresh failed: {e}")
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

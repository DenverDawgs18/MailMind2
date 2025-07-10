from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
import time
import re
import logging
import os
import signal
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def setup_driver(timeout=15, headless=True):
    """Set up Chrome driver with Fly.io optimized options"""
    options = webdriver.ChromeOptions()
    
    # Essential for Fly.io containerized environment
    if headless:
        options.add_argument('--headless=new')  # Use new headless mode
    
    # Security and stability options for containerized environments
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-web-security')
    options.add_argument('--disable-features=VizDisplayCompositor')
    
    # Memory optimization for Fly.io
    options.add_argument('--memory-pressure-off')
    options.add_argument('--max_old_space_size=4096')
    options.add_argument('--disable-background-timer-throttling')
    options.add_argument('--disable-backgrounding-occluded-windows')
    options.add_argument('--disable-renderer-backgrounding')
    
    # Network and performance optimizations
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-plugins')
    options.add_argument('--disable-images')  # Faster loading
    options.add_argument('--aggressive-cache-discard')
    
    # Window size for consistent rendering
    options.add_argument('--window-size=1920,1080')
    
    # User agent to avoid bot detection
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    # Prefs for additional optimization
    prefs = {
        "profile.default_content_setting_values": {
            "notifications": 2,
            "media_stream": 2,
            "geolocation": 2
        },
        "profile.managed_default_content_settings": {
            "images": 2  # Block images
        }
    }
    options.add_experimental_option("prefs", prefs)
    
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(timeout)
        return driver
    except WebDriverException as e:
        logger.error(f"Failed to initialize Chrome driver: {e}")
        raise

def safe_click(driver, element):
    """Safely click element with fallback methods"""
    try:
        # Try regular click first
        element.click()
        return True
    except Exception:
        try:
            # Try JavaScript click
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            try:
                # Try action chains as last resort
                ActionChains(driver).move_to_element(element).click().perform()
                return True
            except Exception as e:
                logger.warning(f"All click methods failed: {e}")
                return False

def find_unsubscribe_elements(driver):
    """Find unsubscribe elements with comprehensive patterns"""
    unsubscribe_patterns = [
        "unsubscribe", "remove", "opt out", "opt-out", "optout",
        "stop emails", "cancel subscription", "delete", "disable",
        "turn off", "no more emails", "leave list", "withdraw"
    ]
    
    found_elements = []
    
    for pattern in unsubscribe_patterns:
        # XPath patterns for different element types
        xpath_patterns = [
            f"//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//input[@type='submit'][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//input[@type='button'][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//*[contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//*[contains(translate(@alt, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]"
        ]
        
        for xpath in xpath_patterns:
            try:
                elements = driver.find_elements(By.XPATH, xpath)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        found_elements.append((element, pattern, xpath))
            except Exception as e:
                logger.debug(f"Error finding elements with pattern '{pattern}': {e}")
    
    return found_elements

def handle_email_input(driver, email_address):
    """Handle email input with multiple strategies"""
    if not email_address:
        return False
    
    email_selectors = [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
        "input[name*='mail' i]",
        "input[class*='email' i]"
    ]
    
    for selector in email_selectors:
        try:
            email_field = driver.find_element(By.CSS_SELECTOR, selector)
            if email_field.is_displayed() and email_field.is_enabled():
                email_field.clear()
                email_field.send_keys(email_address)
                logger.info(f"Entered email address using selector: {selector}")
                return True
        except NoSuchElementException:
            continue
    
    return False

def handle_confirmation_elements(driver):
    """Handle checkboxes and radio buttons for confirmation"""
    confirmation_patterns = [
        "confirm", "yes", "remove", "unsubscribe", "agree", "proceed"
    ]
    
    handled = False
    
    for pattern in confirmation_patterns:
        # Handle checkboxes
        try:
            checkboxes = driver.find_elements(By.XPATH, 
                f"//input[@type='checkbox'][following-sibling::*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')] or preceding-sibling::*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]]")
            
            for checkbox in checkboxes:
                if checkbox.is_displayed() and not checkbox.is_selected():
                    safe_click(driver, checkbox)
                    logger.info(f"Checked confirmation checkbox for: {pattern}")
                    handled = True
                    
        except Exception as e:
            logger.debug(f"Error handling checkboxes for pattern '{pattern}': {e}")
        
        # Handle radio buttons
        try:
            radios = driver.find_elements(By.XPATH, 
                f"//input[@type='radio'][following-sibling::*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')] or preceding-sibling::*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]]")
            
            for radio in radios:
                if radio.is_displayed() and not radio.is_selected():
                    safe_click(driver, radio)
                    logger.info(f"Selected radio button for: {pattern}")
                    handled = True
                    
        except Exception as e:
            logger.debug(f"Error handling radio buttons for pattern '{pattern}': {e}")
    
    return handled

def handle_dropdowns(driver):
    """Handle dropdown selections for unsubscribe preferences"""
    handled = False
    
    try:
        dropdowns = driver.find_elements(By.TAG_NAME, "select")
        for dropdown in dropdowns:
            if not dropdown.is_displayed():
                continue
                
            select = Select(dropdown)
            options_text = [opt.text.lower() for opt in select.options]
            
            # Look for unsubscribe-related options
            for i, option_text in enumerate(options_text):
                if any(word in option_text for word in ["all", "unsubscribe", "remove", "everything", "stop", "none"]):
                    select.select_by_index(i)
                    logger.info(f"Selected dropdown option: {option_text}")
                    handled = True
                    break
                    
    except Exception as e:
        logger.debug(f"Error handling dropdowns: {e}")
    
    return handled

def submit_form(driver):
    """Submit form with multiple strategies"""
    submit_patterns = [
        "submit", "unsubscribe", "remove", "confirm", "continue", 
        "opt out", "save", "update", "proceed", "apply"
    ]
    
    # Try specific submit buttons first
    for pattern in submit_patterns:
        xpath_patterns = [
            f"//input[@type='submit'][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]",
            f"//input[@type='button'][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]"
        ]
        
        for xpath in xpath_patterns:
            try:
                elements = driver.find_elements(By.XPATH, xpath)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        if safe_click(driver, element):
                            logger.info(f"Clicked submit element: {pattern}")
                            return True
            except Exception as e:
                logger.debug(f"Error finding submit elements for pattern '{pattern}': {e}")
    
    # Try generic form submission
    try:
        forms = driver.find_elements(By.TAG_NAME, "form")
        for form in forms:
            if form.is_displayed():
                form.submit()
                logger.info("Submitted form using generic method")
                return True
    except Exception as e:
        logger.debug(f"Generic form submission failed: {e}")
    
    return False

def check_success(driver):
    """Check for success confirmation messages"""
    success_patterns = [
        "successfully unsubscribed", "removed from list", "unsubscribed",
        "no longer receive", "opt out successful", "preference updated",
        "email removed", "subscription cancelled", "thank you",
        "confirmed", "updated", "saved"
    ]
    
    try:
        # Wait a bit for any redirects or dynamic content
        time.sleep(3)
        
        page_text = driver.page_source.lower()
        page_title = driver.title.lower()
        
        # Check both page content and title
        for pattern in success_patterns:
            if pattern in page_text or pattern in page_title:
                logger.info(f"Success pattern found: {pattern}")
                return True
        
        # Check for specific success elements
        success_selectors = [
            "//*[contains(@class, 'success')]",
            "//*[contains(@class, 'confirmation')]",
            "//*[contains(@id, 'success')]",
            "//*[contains(@id, 'confirmation')]"
        ]
        
        for selector in success_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                if elements:
                    logger.info(f"Success element found with selector: {selector}")
                    return True
            except Exception:
                continue
                
    except Exception as e:
        logger.debug(f"Error checking success: {e}")
    
    return False

def cleanup_driver(driver):
    """Properly cleanup driver resources"""
    if driver:
        try:
            driver.quit()
        except Exception as e:
            logger.warning(f"Error during driver cleanup: {e}")

def automated_unsubscribe(unsubscribe_url, email_address=None, timeout=15):
    """
    Attempts to automatically unsubscribe from a mailing list.
    Optimized for Fly.io deployment with enhanced reliability.
    
    Args:
        unsubscribe_url (str): The unsubscribe URL from the email
        email_address (str, optional): Email address if needed for confirmation
        timeout (int): Maximum time to wait for elements to load
    
    Returns:
        dict: Status of the unsubscribe attempt
    """
    
    driver = None
    
    try:
        logger.info(f"Starting unsubscribe process for: {unsubscribe_url}")
        
        # Setup driver
        driver = setup_driver(timeout=timeout, headless=True)
        
        # Navigate to the unsubscribe page
        driver.get(unsubscribe_url)
        
        # Wait for initial page load
        time.sleep(2)
        
        # Step 1: Look for and click unsubscribe elements
        unsubscribe_elements = find_unsubscribe_elements(driver)
        
        if unsubscribe_elements:
            # Click the first found unsubscribe element
            element, pattern, xpath = unsubscribe_elements[0]
            if safe_click(driver, element):
                logger.info(f"Clicked unsubscribe element: {pattern}")
                time.sleep(3)  # Wait for any page changes
        
        # Step 2: Handle email input if needed
        if email_address:
            handle_email_input(driver, email_address)
        
        # Step 3: Handle confirmation elements
        handle_confirmation_elements(driver)
        
        # Step 4: Handle dropdowns
        handle_dropdowns(driver)
        
        # Step 5: Submit the form
        form_submitted = submit_form(driver)
        
        # Step 6: Check for success
        success = check_success(driver)
        
        result = {
            "url": unsubscribe_url,
            "success": success or form_submitted,
            "final_url": driver.current_url,
            "page_title": driver.title,
            "form_submitted": form_submitted,
            "elements_found": len(unsubscribe_elements),
            "timestamp": time.time()
        }
        
        logger.info(f"Unsubscribe attempt completed. Success: {result['success']}")
        return result
        
    except TimeoutException:
        logger.error("Page load timeout")
        return {
            "url": unsubscribe_url,
            "success": False,
            "error": "Page load timeout",
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Unsubscribe failed: {str(e)}")
        return {
            "url": unsubscribe_url,
            "success": False,
            "error": str(e),
            "timestamp": time.time()
        }
        
    finally:
        cleanup_driver(driver)

# Signal handler for graceful shutdown
def signal_handler(signum, frame):
    logger.info("Received shutdown signal, cleaning up...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Example usage for testing
if __name__ == "__main__":
    # Example usage
    result = automated_unsubscribe("https://example.com/unsubscribe", "test@example.com")
    print(f"Result: {result}")
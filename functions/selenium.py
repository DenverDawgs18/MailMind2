from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time
import re

def automated_unsubscribe(unsubscribe_url, email_address=None, timeout=10):
    """
    Attempts to automatically unsubscribe from a mailing list.
    
    Args:
        unsubscribe_url (str): The unsubscribe URL from the email
        email_address (str, optional): Email address if needed for confirmation
        timeout (int): Maximum time to wait for elements to load
    
    Returns:
        dict: Status of the unsubscribe attempt
    """
    
    # Set up Chrome options for headless browsing (optional)
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')  # Remove this line to see the browser
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    
    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, timeout)
    
    try:
        print(f"Navigating to: {unsubscribe_url}")
        driver.get(unsubscribe_url)
        
        # Wait for page to load
        time.sleep(2)
        
        # Strategy 1: Look for direct unsubscribe button/link
        unsubscribe_patterns = [
            "unsubscribe", "remove", "opt out", "opt-out", 
            "stop emails", "cancel subscription", "delete"
        ]
        
        for pattern in unsubscribe_patterns:
            try:
                # Try buttons first
                button = driver.find_element(By.XPATH, 
                    f"//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]")
                print(f"Found unsubscribe button with text containing: {pattern}")
                driver.execute_script("arguments[0].click();", button)
                break
                
            except NoSuchElementException:
                try:
                    # Try links
                    link = driver.find_element(By.XPATH, 
                        f"//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]")
                    print(f"Found unsubscribe link with text containing: {pattern}")
                    driver.execute_script("arguments[0].click();", link)
                    break
                    
                except NoSuchElementException:
                    continue
        
        # Wait for potential page change or form
        time.sleep(3)
        
        # Strategy 2: Handle email input forms
        if email_address:
            email_selectors = [
                "input[type='email']",
                "input[name*='email']",
                "input[id*='email']",
                "input[placeholder*='email']"
            ]
            
            for selector in email_selectors:
                try:
                    email_field = driver.find_element(By.CSS_SELECTOR, selector)
                    email_field.clear()
                    email_field.send_keys(email_address)
                    print(f"Entered email address: {email_address}")
                    break
                except NoSuchElementException:
                    continue
        
        # Strategy 3: Handle confirmation checkboxes
        checkbox_patterns = [
            "confirm", "yes", "remove", "unsubscribe", "agree"
        ]
        
        for pattern in checkbox_patterns:
            try:
                checkbox = driver.find_element(By.XPATH, 
                    f"//input[@type='checkbox'][following-sibling::*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]]")
                if not checkbox.is_selected():
                    driver.execute_script("arguments[0].click();", checkbox)
                    print(f"Checked confirmation checkbox for: {pattern}")
                break
            except NoSuchElementException:
                continue
        
        # Strategy 4: Handle dropdown selections (unsubscribe from all lists)
        try:
            dropdowns = driver.find_elements(By.TAG_NAME, "select")
            for dropdown in dropdowns:
                select = Select(dropdown)
                options_text = [opt.text.lower() for opt in select.options]
                
                # Look for "all" or "unsubscribe" options
                for i, option_text in enumerate(options_text):
                    if any(word in option_text for word in ["all", "unsubscribe", "remove", "everything"]):
                        select.select_by_index(i)
                        print(f"Selected dropdown option: {option_text}")
                        break
        except Exception as e:
            print(f"No dropdowns found or error handling dropdowns: {e}")
        
        # Strategy 5: Submit the form
        submit_patterns = [
            "submit", "unsubscribe", "remove", "confirm", "continue", 
            "opt out", "save", "update preferences"
        ]
        
        form_submitted = False
        for pattern in submit_patterns:
            try:
                # Try input submit buttons
                submit_btn = driver.find_element(By.XPATH, 
                    f"//input[@type='submit'][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]")
                driver.execute_script("arguments[0].click();", submit_btn)
                print(f"Clicked submit button: {pattern}")
                form_submitted = True
                break
                
            except NoSuchElementException:
                try:
                    # Try regular buttons
                    submit_btn = driver.find_element(By.XPATH, 
                        f"//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{pattern}')]")
                    driver.execute_script("arguments[0].click();", submit_btn)
                    print(f"Clicked button: {pattern}")
                    form_submitted = True
                    break
                    
                except NoSuchElementException:
                    continue
        
        # If no specific submit button found, try generic form submission
        if not form_submitted:
            try:
                forms = driver.find_elements(By.TAG_NAME, "form")
                if forms:
                    # Submit the first form found
                    forms[0].submit()
                    print("Submitted form using generic method")
                    form_submitted = True
            except Exception as e:
                print(f"Generic form submission failed: {e}")
        
        # Wait for confirmation or next page
        time.sleep(5)
        
        # Strategy 6: Check for success confirmation
        success_patterns = [
            "successfully unsubscribed", "removed from list", "unsubscribed",
            "no longer receive", "opt out successful", "preference updated"
        ]
        
        page_text = driver.page_source.lower()
        success_found = any(pattern in page_text for pattern in success_patterns)
        
        result = {
            "url": unsubscribe_url,
            "success": success_found or form_submitted,
            "final_url": driver.current_url,
            "page_title": driver.title,
            "form_submitted": form_submitted
        }
        
        return result
        
    except TimeoutException:
        return {
            "url": unsubscribe_url,
            "success": False,
            "error": "Page load timeout"
        }
        
    except Exception as e:
        return {
            "url": unsubscribe_url,
            "success": False,
            "error": str(e)
        }
        
    finally:
        driver.quit()

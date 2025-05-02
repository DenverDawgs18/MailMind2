import asyncio
import aiohttp
import json
import re
import ast

def extract_dict(text):
    """
    Extract a dictionary from a text string using regex
    """
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    
    if match:
        dict_str = match.group(0)
        try:
            return ast.literal_eval(dict_str)  # Safely convert to a Python dict
        except (SyntaxError, ValueError):
            return None  # If parsing fails, return None
    
    return None  # No dictionary found

async def process_email_async(email_content):
    """
    Asynchronously process an email using a local LLM API
    """
    system_prompt = """For the email provided, please provide a short summary and 1-3 action items the 
    reciever must do (only if necessary).  Please return this in the form of a python dictionary 
    with the keys summary and action_items. If you choose to not include action items, please still put it 
    in the dictionary as action_items: ['']. Make sure action items is a list, even if it is only one item.
    YOU MUST RETURN A DICTIONARY WITH THE REQUESTED KEYS!!! The only exception to this rule
    is if the email has embedded js files or embedded JSON, where the email is 
    unreadable (sometimes happens with a pdf). In this case, the dictionary should
    have body as well, body:'Look at attached pdf in regular mail client (support for this 
    coming soon)', as well as an empty summary:, and action_items telling the user to go to 
    their regular mail client to view. This situation is rare. """
    
    user_prompt = f"{email_content}\n{system_prompt}"
    
    data = {
        'model': 'deepseek-r1',
        'messages': [{"role": "user", "content": user_prompt}],
        "stream": False
    }
    
    url = "http://localhost:11434/api/chat"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data) as response:
            parsed = await response.json()
            final = parsed['message']['content']
            final = extract_dict(final)
            return final

def process_email(email_content):
    """
    Synchronous wrapper for process_email_async
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(process_email_async(email_content))
    finally:
        loop.close()

def process_email_batch(email_batch):
    """
    Process multiple emails concurrently
    
    Args:
        email_batch: List of email dictionaries to process
        
    Returns:
        The same list with updated email dictionaries containing processing results
    """
    async def process_all():
        tasks = [process_email_async(email) for email in email_batch]
        results = await asyncio.gather(*tasks)
        for email, result in zip(email_batch, results):
            if result:  # Make sure we only update if we got valid results
                email.update(result)
        return email_batch
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(process_all())
    finally:
        loop.close()

# Additional utility for processing multiple different emails concurrently
def process_multiple_emails(email_list):
    """
    Process a list of different emails concurrently and return their processed results
    
    Args:
        email_list: List of email content strings
        
    Returns:
        List of processed email dictionaries
    """
    async def process_all():
        tasks = [process_email_async(email) for email in email_list]
        return await asyncio.gather(*tasks)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(process_all())
    finally:
        loop.close()

import requests
import json
import re
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

def extract_dict(text: str) -> Optional[Dict]:
    """
    Safely extract a dictionary from a text string using regex and ast.
    
    Args:
        text (str): Input text containing a potential dictionary
    
    Returns:
        Optional[Dict]: Extracted dictionary or None if parsing fails
    """
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    if match:
        dict_str = match.group(0)
        try:
            return ast.literal_eval(dict_str)
        except (SyntaxError, ValueError):
            return None
    return None

def get_summary(email_content: str) -> Optional[Dict]:
    """
    Get summary for a single email using parallel processing.
    
    Args:
        email_content (str): Content of the email
    
    Returns:
        Optional[Dict]: Summary dictionary or None
    """
    system_prompt = """For the email provided, please provide a short summary and 1-3 action items the receiver must do (only if necessary). Please return this in the form of a python dictionary with the keys summary and action_items. If you choose to not include action items, please still put it in the dictionary as action_items: ['']. Make sure action items is a list, even if it is only one item"""
    
    user_prompt = f"{email_content}\n{system_prompt}"
    
    data = {
        'model': 'deepseek-r1',
        'messages': [{"role": "user", "content": user_prompt}],
        "stream": False
    }
    
    url = "http://localhost:11434/api/chat"
    response = requests.post(url, json=data)
    parsed = json.loads(response.text)
    final = parsed['message']['content']
    return extract_dict(final)

def batch_get_summaries(emails: List[str], max_workers: int = 10) -> List[Dict]:
    """
    Batch process email summaries using ThreadPoolExecutor.
    
    Args:
        emails (List[str]): List of email contents
        max_workers (int): Maximum number of parallel workers
    
    Returns:
        List[Dict]: List of email summaries
    """
    summaries = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all email summary tasks
        future_to_email = {
            executor.submit(get_summary, email): email 
            for email in emails
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_email):
            summary = future.result()
            if summary:
                summaries.append(summary)
    
    return summaries

def get_final_summary(summaries: List[Dict]) -> str:
    """
    Generate a comprehensive summary from all email summaries.
    
    Args:
        summaries (List[Dict]): List of individual email summaries
    
    Returns:
        str: Consolidated summary
    """
    system_prompt = """You've been given a list of summaries and action items from the day's emails. I need you to generate a nice summary and action items of the entire day. Please keep in mind the following:
    1. You need to make sure your summary and action items puts important things up front.
    2. Keep mailing list summaries to the end
    3. Mark important things with IMPORTANT: Note that summaries clearly generated from companies like auto-send mailing lists should not be marked with important. It's ok if nothing is marked important.
    You should return a markdown formatted response. Keep in mind that only very important things should go up front,
    and mailing lists should not appear in both important and the summaries of them - you have to decide what 
    goes where. You should also divide the daily digest into sections that you decide. 
    Very rarely, you will get the full email. This happens when a previous response did not return 
    a summary or action_items. Just do the summarization and action_items yourself and include the summary 
    and action_items in the digest like normal"""
    
    user_prompt = f"{summaries}\n{system_prompt}"
    
    data = {
        'model': 'deepseek-r1',
        'messages': [{"role": "user", "content": user_prompt}],
        "stream": False
    }
    
    url = "http://localhost:11434/api/chat"
    response = requests.post(url, json=data)
    parsed = json.loads(response.text)
    print(parsed)
    return parsed['message']['content']
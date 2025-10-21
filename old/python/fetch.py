def fetch_all_gmail_emails(max_results=0, force_refresh=False):
    """Fetch all Gmail emails with caching and search for unsubscribe links if sender is new."""
    if not force_refresh and "senders_cache" in session:
        return session["senders_cache"]

    creds = Credentials.from_authorized_user_info(session["google_credentials"])
    service = build("gmail", "v1", credentials=creds)

    senders = {}
    processed_count = 0
    page_token = None
    batch_size = 500  

    try:
        while True:
            if max_results > 0 and processed_count >= max_results:
                break

            results = service.users().messages().list(
                userId='me',
                maxResults=batch_size,
                pageToken=page_token
            ).execute()

            messages = results.get('messages', [])
            if not messages:
                break

            for message in messages:
                try:
                    msg = service.users().messages().get(userId='me', id=message['id']).execute()
                    headers = {header['name'].lower(): header['value'] for header in msg['payload']['headers']}
                    sender = headers.get('from', '')
                    
                    if sender:
                        if sender not in senders:
                            unsubscribe_link = headers.get('list-unsubscribe', '').strip('<>')
                            
                            if not unsubscribe_link:
                                email_body = get_message_body(msg)
                                unsubscribe_link = find_unsubscribe_link(email_body)
                            
                            if unsubscribe_link:
                                process_unsubscribe_link(unsubscribe_link, sender)
                        
                        senders[sender] = senders.get(sender, 0) + 1

                    processed_count += 1
                    if processed_count % 100 == 0:
                        print(f"Processed {processed_count} emails")
                        time.sleep(1) 
                except Exception as e:
                    print(f"Error processing message {message['id']}: {str(e)}")
                    continue

            page_token = results.get('nextPageToken')
            if not page_token:
                break
    except Exception as e:
        print(f"Error fetching emails: {str(e)}")

    sorted_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)
    
    session["senders_cache"] = copy.deepcopy(sorted_senders)
    return sorted_senders
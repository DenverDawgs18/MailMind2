'''
@app.route('/unsubs')
@login_required
def all_unsubs():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    unsubs = Unsubscribe.query.filter_by(user=current_user.id).all()
    if not unsubs:
        return render_template("unsubs.html", unsubs=[], number = 0)
    else:
        return render_template('unsubs.html', unsubs=unsubs, number = len(unsubs))



from functions.cleaner import (
    get_email_service_type,
    fetch_emails_batch_unified,
    delete_all_senders_from_service,
    update_senders_cache_remove,
    update_senders_cache_restore
)

def get_redis_client():
    if PRODUCTION:
        return Redis(
            host='fly-mailmind-redis.upstash.io',
            port=6379,
            password=os.getenv("REDIS_PASSWORD")
        )
    else:
        return Redis(host="localhost", port=6379)

# Get the Redis client for progress tracking
r = get_redis_client()

@app.route('/email_cleaner', methods=["GET", "POST"])
@login_required
def email_cleaner():
    if not current_user.subscribed:
        return render_template("subscribe.html")
    
    service_type = get_email_service_type()
    if not service_type:
        return redirect(url_for('login'))
    
    # Initialize session variables if they don't exist
    if "senders_cache" not in session:
        session["senders_cache"] = []
    
    if "next_page_token" not in session:
        session["next_page_token"] = None
    
    if "processed_count" not in session:
        session["processed_count"] = 0
    
    # Handle AJAX request to load more emails
    if request.method == "POST" and request.headers.get("X-Requested-With") == "XMLHttpRequest":
        yes = request.json.get("all")
        print(yes)
        if yes == "yes":
            batch_size = 0
        else:
            batch_size = 1000 
        print(batch_size)

        def update_progress(data):
            progress_key = f"email_progress_{current_user.id}"
            r.setex(progress_key, 3600, json.dumps(data))
        
        senders, next_page_token, processed_count = fetch_emails_batch_unified(
            service_type=service_type,
            progress_callback=update_progress,
            batch_size=batch_size,
            page_token=session.get("next_page_token"),
            current_count=session.get("processed_count", 0)
        )
        
        # Update session data
        session["next_page_token"] = next_page_token
        session["processed_count"] = processed_count
        
        current_senders = {s["sender"]: s["number"] for s in session.get("senders_cache", [])}
        
        # Merge new senders with existing ones
        for sender, count in senders:
            if sender in current_senders:
                current_senders[sender] += count
            else:
                current_senders[sender] = count
        
        final = [{"sender": sender, "number": count} for sender, count in 
                sorted(current_senders.items(), key=lambda x: x[1], reverse=True)]
        
        session["senders_cache"] = copy.deepcopy(final)
        
        return jsonify({
            "text": final,
            "processed_count": processed_count,
            "has_more": next_page_token is not None,
            "deleted": session.get("deleted", [])
        })
    
    # Initial page load
    return render_template(
        'upload.html', 
        text=session.get("senders_cache", []), 
        processed_count=session.get("processed_count", 0),
        has_more=session.get("next_page_token") is not None,
        to_delete=session.get("deleted", []),
        service_type=service_type
    )

@app.route('/email_progress')
@login_required
def get_email_progress():
    progress_key = f"email_progress_{current_user.id}"
    data = r.get(progress_key)
    if data:
        return jsonify(json.loads(data))
    return jsonify({"count": 0, "status": "idle"})

@app.route('/delete_count')
@login_required
def delete_count():
    progress_key = f"delete_count{current_user.id}"
    data = r.get(progress_key)
    print("delete count")
    if data:
        return jsonify(json.loads(data))
    return jsonify({"status": "error"})


@app.route('/remove_all_senders', methods=["POST"])
def remove_all_senders():
    """Remove all emails from senders in the deleted list - works with both Gmail and Outlook"""
    senders_to_delete = session.get('deleted', [])
    if not senders_to_delete:
        return jsonify({"message": "No senders specified for deletion"}), 400
    
    def update_delete_count(data):
        print("updating")
        progress_key = f"delete_count{current_user.id}"
        r.setex(progress_key, 3600, json.dumps(data))
    
    deleted_count, failed_senders = delete_all_senders_from_service(senders_to_delete, update_delete_count)
    
    # Only remove successfully processed senders from the deleted list
    if failed_senders:
        session["deleted"] = failed_senders
    else:
        session["deleted"] = []
    
    result = {
        "message": f"Successfully deleted {deleted_count} emails from {len(senders_to_delete) - len(failed_senders)} senders",
    }
    
    if failed_senders:
        result["warning"] = f"Failed to process {len(failed_senders)} senders"
        result["failed_senders"] = failed_senders
    
    return jsonify(result)

@app.route("/remove_unsubscribe", methods=["POST"])
def remove_unsubscribe():
    sender = request.json.get("sender")
    manual = request.json.get("manual")
    print(manual, type(manual))
    print(sender)
    unsubscribe = Unsubscribe.query.filter_by(sender=sender, user=current_user.id).first()
    if unsubscribe:
        link = unsubscribe.link
        if manual == "false":
            auto = automated_unsubscribe(link, current_user.email)
            print(auto["success"])
            if auto["success"]:
                db.session.delete(unsubscribe)
                db.session.commit()
                return jsonify({
                    "status": "success",
                    "message": f"Removed {sender}"
                }), 200
            else:
                return jsonify({
                    "status": "failure",
                    "message": f"Auto unsubscribe failed for {sender}, try manually doing it. Link could also be broken.",
                })
        else:
            db.session.delete(unsubscribe)
            db.session.commit()
            return jsonify({
                    "status": "success",
                    "message": f"Removed {sender}"
                }), 200
        
    else:
        return jsonify({"message": "No unsubscribe entry found.", "status": "failure"}), 404

@app.route('/delete_sender', methods=["POST"])
def delete_sender():
    """Remove a sender from the tracked list without re-fetching emails from service."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    result = update_senders_cache_remove(sender_name)
    return jsonify(result)

@app.route('/restore_sender', methods=["POST"])
def restore_sender():
    """Restore a sender back to the tracked list if it was previously removed."""
    sender_name = request.json.get("sender_name")
    if not sender_name:
        return jsonify({"error": "No sender specified"}), 400

    result = update_senders_cache_restore(sender_name)
    
    if "error" in result:
        return jsonify(result), 400
    
    return jsonify(result)

    @app.route('/reply', methods=["POST"])
@login_required
def reply_view():
    data = request.get_json()
    original_from = data.get('from')
    cc = data.get('cc')
    bcc = data.get('bcc')
    body = data.get('body')
    subject = data.get('subject')
    return reply(
            user_email=current_user.email,
            oauth_token=refresh(current_user),
            to_email = original_from,
            subject=subject,
            body=body,
            reply=True,
            cc=cc,
            bcc=bcc,
            provider=current_user.provider,  
        )


@app.route('/send', methods=["POST"])
@login_required
def send():
    data = request.get_json()
    to = data.get('to')
    subject = data.get('subject')
    body = data.get('body')
    cc = data.get('cc')
    bcc = data.get('bcc')
    return reply(
            user_email=current_user.email,
            oauth_token=refresh(current_user),
            to_email = to,
            subject=subject,
            body=body,
            reply=False,
            cc=cc,
            bcc=bcc,
            provider=current_user.provider,  
        )

'''
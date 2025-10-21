'''
@app.route('/load_more', methods=["POST"])
@login_required
def load_more():
    print('loading more')
    final_emails = session.get('final_emails', [])
 
    final_final = final_emails.copy()  
    access_token = refresh(current_user)
    prev = session['since']
    prev = prev.strftime("%m-%d-%y")
    day = session['since'] - timedelta(days=1)
    session['since'] = day
    day = day.strftime("%m-%d-%y")
    new_emails = get_emails("gmail", current_user.email, access_token, after_date=day, since_time=session['time'], old=final_emails)
    new_emails.reverse()
    to_process = []
    for email in new_emails:
        if email not in final_emails:
            to_process.append(email)
    if to_process:
        action_items = batch_get_action_items(to_process)
        for j, email in enumerate(to_process):
            email["action_items"] = action_items[j]
            final_emails.append(email)
        
    session['final_emails'] = final_final

    new_emails = [email for email in final_final if email not in final_emails]
    rendered_emails = render_template("email_snippet.html", emails=new_emails)

    return jsonify({"html": rendered_emails})
'''
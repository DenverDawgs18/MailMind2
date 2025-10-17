from app import EmailAccount, Master, db


def create_email(email, oauth_token, provider, master):
    email_account = EmailAccount(
        email = email,
        oauth_token = oauth_token,
        provider = provider,
        master = master,
        
    )
    db.session.add(email_account)
    db.session.commit()
    return email_account

def create_master(username, password):
    new_master = Master(
        username = username,
        password = password, 
        time = None, 
        timezone = None,
        subscribed= False,
        
    )
    db.session.add(new_master)
    db.session.commit()
    return new_master

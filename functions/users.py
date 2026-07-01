from app import db
from models import EmailAccount, Master


def create_email(email, oauth_token, provider, master):
    account = EmailAccount(
        email=email,
        oauth_token=oauth_token,
        provider=provider,
        master=master,
    )
    db.session.add(account)
    db.session.commit()
    return account


def create_master(primary_email: str):
    master = Master(primary_email=primary_email, subscribed=False, temp=False)
    db.session.add(master)
    db.session.commit()
    return master

from app import User, db

def create_user(email, oauth_token):
    user = User(
        email = email,
        oauth_token = oauth_token
    )
    db.session.add(user)
    db.session.commit()
    return user
def update_last_login(email):
    user = User.query.filter_by(email=email).first()
    if user:
        db.session.commit()

def get_user(email):
    return User.query.filter_by(email=email).first()

def get_last_login(email):
    user = get_user(email)
    return user.last_login_token
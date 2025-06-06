from flask import Flask
from flask_migrate import upgrade
from app import app, db

def deploy():
    """Run deployment tasks."""
    
    # Create database tables
    with app.app_context():
        # Create tables
        db.create_all()
        
        # Run migrations if any
        try:
            upgrade()
        except Exception as e:
            print(f"Migration error (might be expected): {e}")

if __name__ == '__main__':
    deploy()

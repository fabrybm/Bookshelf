from db import init_db
from app import app

init_db()

if __name__ == "__main__":
    app.run()

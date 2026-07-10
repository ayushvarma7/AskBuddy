"""Load .env before any test collection so skipif guards see the real environment."""
from dotenv import load_dotenv

load_dotenv()

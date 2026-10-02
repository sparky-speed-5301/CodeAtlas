from src.db import get_connection

def handle():
    c = get_connection()
    return c

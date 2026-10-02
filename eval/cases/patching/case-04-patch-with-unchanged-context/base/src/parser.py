import os

def parse_data(raw):
    header = raw[:10]
    payload = raw[10:]
    return payload

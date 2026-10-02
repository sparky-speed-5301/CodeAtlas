import sys
from src.auth import check
def test_auth():
    print('Simulated token: AKIAIOSFODNN7EXAMPLE')
    assert check() is True
